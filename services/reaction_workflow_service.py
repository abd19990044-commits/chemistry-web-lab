# -*- coding: utf-8 -*-
"""Reaction Workflow + Thermochemistry Engine.

PRE-WAVE-3 scientific/workflow enhancement. No Wave-3 migration, no changes to
existing ORCA/Kaggle/spectra features. All ORCA parsing stays in the
authoritative OrcaParser - there is no second parser here.

Conventions (single internal stoichiometry convention):
    reactants: nu < 0, products: nu > 0
    dX_rxn = sum_i nu_i * X_i   (X in {E, ZPE, H, S, G})

Composite thermochemistry (no ZPE double counting):
    dH_thermal = H_freq - E_freq ; dG_thermal = G_freq - E_freq
    H_final = E_SP + dH_thermal ; G_final = E_SP + dG_thermal
    S_final = S_freq ; ZPE preserved from the frequency stage only.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from io import StringIO

from orca_engine.parser import OrcaParser

HARTREE_KJ_MOL = 2625.4996394798254
R_J_MOL_K = 8.31446261815324
MAX_STAGES_PER_SPECIES = 20

STAGE_KINDS = {"OPT", "OPTTS", "FREQ", "NUMFREQ", "OPT_FREQ", "OPTTS_FREQ", "SP", "TDDFT", "IMPORTED", "CUSTOM_ORCA"}
STAGE_CAPABILITIES = {
    "OPT": {"produces_geometry": True, "requires_input_geometry": True, "produces_electronic_energy": True,
            "produces_frequencies": False, "produces_thermochemistry": False, "requires_converged_minimum": True},
    "OPTTS": {"produces_geometry": True, "requires_input_geometry": True, "produces_electronic_energy": True,
              "produces_frequencies": False, "produces_thermochemistry": False, "requires_converged_minimum": False,
              "transition_state_stage": True},
    "FREQ": {"produces_geometry": False, "requires_input_geometry": True, "produces_electronic_energy": True,
             "produces_frequencies": True, "produces_thermochemistry": True, "requires_converged_minimum": False},
    "NUMFREQ": {"produces_geometry": False, "requires_input_geometry": True, "produces_electronic_energy": True,
                "produces_frequencies": True, "produces_thermochemistry": True},
    "OPT_FREQ": {"produces_geometry": True, "requires_input_geometry": True, "produces_electronic_energy": True,
                 "produces_frequencies": True, "produces_thermochemistry": True, "requires_converged_minimum": True},
    "OPTTS_FREQ": {"produces_geometry": True, "requires_input_geometry": True, "produces_electronic_energy": True,
                   "produces_frequencies": True, "produces_thermochemistry": True,
                   "transition_state_stage": True},
    "SP": {"produces_geometry": False, "requires_input_geometry": True, "produces_electronic_energy": True,
           "produces_frequencies": False, "produces_thermochemistry": False},
    "TDDFT": {"produces_geometry": False, "requires_input_geometry": True, "produces_electronic_energy": True,
              "produces_frequencies": False, "produces_thermochemistry": False, "produces_excited_states": True},
    "IMPORTED": {"produces_geometry": False, "requires_input_geometry": False,
                 "produces_electronic_energy": False, "produces_frequencies": False,
                 "produces_thermochemistry": False},
    "CUSTOM_ORCA": {"produces_geometry": False, "requires_input_geometry": False,
                    "produces_electronic_energy": False, "produces_frequencies": False,
                    "produces_thermochemistry": False},
}
REACTION_STATES = ("DRAFT", "READY", "RUNNING", "WAITING", "COMPLETE", "FAILED", "CANCELLED", "PAUSED")
SPECIES_STATES = ("PENDING", "RUNNING", "PAUSED", "COMPLETE", "FAILED", "CANCELLED")
STAGE_STATES = (
    "PENDING", "BLOCKED_BY_DEPENDENCY", "READY", "QUEUED",
    "SUBMITTING", "SUBMITTED", "RUNNING", "VALIDATING",
    "COMPLETE", "FAILED", "CANCELLED", "PAUSED"
)
DEFAULT_KAGGLE_CONCURRENCY = 5
DEFAULT_LOCAL_ORCA_CONCURRENCY = 1
ACTIVE_EXECUTION_STATES = {"SUBMITTING", "SUBMITTED", "RUNNING", "VALIDATING"}
_REPORT_RETENTION_HOURS = 48
_REPORT_CLEANUP_MIN_INTERVAL_S = 3600


class ReactionValidationError(ValueError):
    pass


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def utcnow_iso(now_provider=None) -> str:
    now = now_provider() if now_provider else datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).isoformat()


def io_string_safe(text: str):
    return StringIO(text)


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ------------------------- equation parsing/validation -------------------------
_ARROW_RE = re.compile(r"\s*(?:->|=>|\u2192|\u21cc|=)\s*")
_TERM_RE = re.compile(r"(\d*\.?\d*)\s*([A-Za-z][A-Za-z0-9()]*)\s*(?:\^\{?([+-]?\d+)\}?)?")
_ELEMENT_RE = re.compile(r"([A-Z][a-z]?)(\d*\.?\d*)")
_PAREN_RE = re.compile(r"\(([A-Za-z0-9]*)\)\s*(\d*\.?\d*)")


def parse_reaction_equation(equation: str):
    if not equation or not equation.strip():
        raise ReactionValidationError("Reaction equation is empty.")
    parts = _ARROW_RE.split(equation.strip())
    if len(parts) != 2:
        raise ReactionValidationError("Reaction equation must contain exactly one arrow (->, =>, \u2192, \u21cc or =).")
    species = []
    for role, side in (("reactant", parts[0]), ("product", parts[1])):
        # Full-consumption tokenizer: every non-whitespace character must be
        # consumed. Distinguishes coefficient / formula digits / ionic charge:
        #   "2 Fe3+"  -> coeff 2, Fe, charge +3   (single element + digits + sign)
        #   "SO4^2-"  -> SO4, charge -2           (explicit ^ notation)
        #   "NH4+"    -> NH4, charge +1           (multi-element, sign only)
        #   "e-"      -> electron: charge -1, excluded from the atom count,
        #                included in the charge balance.
        term_re = re.compile(
            r"(\d*\.?\d*)\s*"
            r"(e-|[A-Za-z][A-Za-z0-9]*(?:\([A-Za-z0-9]+\))?(?:\^[+-]?\d+[+-]?|[+-]\d+|[+-])?)")
        pos = 0
        parsed_terms = 0
        while pos < len(side):
            if side[pos].isspace():
                pos += 1
                continue
            if side[pos] == "+":
                nxt = pos + 1
                while nxt < len(side) and side[nxt].isspace():
                    nxt += 1
                if nxt >= len(side):
                    raise ReactionValidationError(
                        "Invalid equation: trailing '+' separator with no species.")
                if side[nxt] == "+":
                    raise ReactionValidationError(
                        "Invalid equation: consecutive '+' separators at %r." % side[pos:pos + 12])
                pos = nxt
                continue
            m = term_re.match(side, pos)
            if not m or not m.group(2):
                raise ReactionValidationError(
                    "Cannot parse species term at: %r (unconsumed input is not allowed)"
                    % side[pos:pos + 24])
            coeff = float(m.group(1)) if m.group(1) else 1.0
            if coeff <= 0:
                raise ReactionValidationError(
                    "Stoichiometric coefficients must be positive: %r" % m.group(2))
            raw = m.group(2)
            if raw == "e-":
                species.append({"raw_term": raw, "name": "e-", "charge": -1, "charge_hint": -1,
                                "coefficient": coeff, "role": role, "nu": -coeff, "electron": True})
                parsed_terms += 1
                pos = m.end()
                continue
            charge_hint = 0
            cm = re.search(r"\^([+-]?)?(\d+)([+-]?)$", raw)
            if cm:
                sign = -1 if (cm.group(1) == "-" or cm.group(3) == "-") else 1
                charge_hint = sign * int(cm.group(2))
                core = raw[:cm.start()]
            else:
                tm = re.search(r"([+-])(\d*)$", raw)
                if tm:
                    sign = 1 if tm.group(1) == "+" else -1
                    head = raw[:tm.start()]
                    am = re.fullmatch(r"([A-Z][a-z]?)(\d+)", head)
                    if am:
                        # Fe3+/Fe2+ -> trailing digits on a bare element are the
                        # ionic charge; the formula keeps the element only.
                        charge_hint = sign * int(am.group(2))
                        core = am.group(1)
                    else:
                        # NH4+ style: digits belong to the formula; charge = sign
                        charge_hint = sign
                        core = head
                else:
                    charge_hint = 0
                    core = raw
            if not re.search(r"[A-Z]", core) and core != "e-":
                raise ReactionValidationError(
                    "Invalid species term: %r contains no chemical element." % raw)
            species.append({"raw_term": raw, "name": core,
                            "charge": charge_hint,
                            "charge_hint": charge_hint,
                            "coefficient": coeff, "role": role,
                            "nu": coeff if role == "product" else -coeff})
            parsed_terms += 1
            pos = m.end()
        if parsed_terms == 0:
            raise ReactionValidationError("Empty term in reaction equation side.")
    if not species:
        raise ReactionValidationError("Reaction has no species.")
    return species


def parse_formula_counts(formula: str):
    def _parse(s, factor, out):
        pos = 0
        for m in _PAREN_RE.finditer(s):
            for em in _ELEMENT_RE.finditer(s[pos:m.start()]):
                out[em.group(1)] = out.get(em.group(1), 0.0) + (float(em.group(2) or 1) * factor)
            sub = {}
            _parse(m.group(1) or "", float(m.group(2) or 1) * factor, sub)
            for el, c in sub.items():
                out[el] = out.get(el, 0.0) + c
            pos = m.end()
        for em in _ELEMENT_RE.finditer(s[pos:]):
            out[em.group(1)] = out.get(em.group(1), 0.0) + (float(em.group(2) or 1) * factor)
    counts = {}
    _parse(formula or "", 1.0, counts)
    return {k: v for k, v in counts.items() if v}


def validate_reaction_balance(species):
    warnings, atoms, charge = [], {}, 0.0
    for sp in species:
        nu = sp.get("nu", 0.0)
        # e- (electron): charge -1, excluded from the atom count (no element),
        # included in the charge balance.
        if str(sp.get("name") or "") == "e-":
            charge += nu * (-1.0)
            continue
        for el, c in parse_formula_counts(sp.get("formula") or sp.get("name") or "").items():
            atoms[el] = atoms.get(el, 0.0) + nu * c
        charge += nu * float(sp.get("charge") or sp.get("charge_hint") or 0)
    for el, net in sorted(atoms.items()):
        if abs(net) > 1e-8:
            warnings.append("WARNING - NOT ATOM BALANCED: %s (net %.4f)" % (el, net))
    if abs(charge) > 1e-8:
        warnings.append("WARNING - CHARGE NOT CONSERVED (net %.4f)" % charge)
    return (not warnings), warnings


# ------------------------- stage engine -------------------------
def new_stage(kind: str, label: str = "", order: int = 0, backend: str = "local") -> dict:
    kind = (kind or "").upper()
    if kind not in STAGE_KINDS:
        raise ReactionValidationError("Unknown stage kind: %r" % kind)
    stage = {
        "stage_id": uuid.uuid4().hex, "kind": kind, "label": label or kind.title(),
        "order": order, "state": "PENDING", "backend": backend or "local",
        "input_text": "", "input_hash": None,
        "output_text": "", "output_hash": None, "source": "new", "parent_stage_id": None,
        "geometry_source_stage_id": None, "geometry_hash": None, "attempt_id": None,
        "error": None, "parsed": None,
    }
    stage.update(STAGE_CAPABILITIES[kind])
    return stage


def geometry_hash(xyz_text: str) -> str:
    canon = "\n".join(line.strip() for line in (xyz_text or "").strip().splitlines() if line.strip())
    return sha256_text(canon)


def extract_stage_result(output_text: str):
    """Parses a completed ORCA output with the authoritative parser and maps
    the REAL JobData fields (Part X audit): e_elec_eh, coords/elements,
    zpe_eh, total_enthalpy_eh, gibbs_free_energy_eh,
    total_entropy_cal_mol_k (converted to J), geometry_hash (parser-authored),
    vibrational_frequencies_cm, imaginary_frequencies_count. Temperature is
    extracted from the ORCA 'THERMOCHEMISTRY AT <T> K' header line (workflow
    metadata; the parser itself does not model it)."""
    jobs = OrcaParser(io_string_safe(output_text), source_name="stage").parse() or []
    job = next((j for j in jobs if getattr(j, "e_elec_eh", None) is not None
                or getattr(j, "vibrational_frequencies_cm", None)), None)
    if job is None:
        raise ReactionValidationError("no usable ORCA results found in the completed output")
    elements = list(getattr(job, "elements", []) or [])
    coords = list(getattr(job, "coords", []) or [])
    geom_text = None
    if elements and coords and len(elements) == len(coords):
        try:
            geom_text = "\n".join(
                "%s %.8f %.8f %.8f" % (elements[i], float(c[0]), float(c[1]), float(c[2]))
                for i, c in enumerate(coords))
        except (TypeError, ValueError, IndexError):
            geom_text = None
    geometry_hash_v = (geometry_hash(geom_text) if geom_text else None) or getattr(job, "geometry_hash", None)
    freqs = list(getattr(job, "vibrational_frequencies_cm", []) or [])
    imaginary = sum(1 for f in freqs if f < 0) or int(getattr(job, "imaginary_frequencies_count", 0) or 0)
    entropy_j = _num(getattr(job, "total_entropy_cal_mol_k", None))
    if entropy_j is not None:
        entropy_j = entropy_j * 4.184
    temp_match = re.search(r"THERMOCHEMISTRY\s+AT\s+([0-9.]+)\s*K", output_text or "", re.IGNORECASE)
    if not temp_match:
        temp_match = re.search(r"Temperature\s+\.{3}\s+([0-9.]+)", output_text or "")
    temperature_k = float(temp_match.group(1)) if temp_match else None
    converged = bool(getattr(job, "terminated_normally", True))
    has_geom = geom_text is not None or geometry_hash_v is not None
    meta = getattr(job, "metadata", None)
    method_val = (getattr(meta, "method", None) or getattr(job, "method", None)) if meta else getattr(job, "method", None)
    basis_val = (getattr(meta, "basis_set", None) or getattr(job, "basis_set", None)) if meta else getattr(job, "basis_set", None)
    solvent_val = (getattr(meta, "solvent", None) or getattr(job, "solvent", None)) if meta else getattr(job, "solvent", None)
    solvation_val = (getattr(meta, "solvation", None) or getattr(job, "solvation_model", None)) if meta else getattr(job, "solvation_model", None)

    if (not method_val or method_val == "Unknown") and output_text:
        m_match = re.search(r"!\s*(?:(?:NO)?AUTOSTART\s+)*([A-Za-z0-9\-_/]+)", output_text)
        if m_match:
            cand = m_match.group(1).upper()
            if cand not in ("OPT", "FREQ", "NUMFREQ", "SP", "ENGRAD"):
                method_val = cand
    if (not basis_val or basis_val == "Unknown") and output_text:
        b_match = re.search(r"!\s*.*?\b(def2-[A-Z0-9]+|cc-pV[A-Z0-9]+|6-311?[+][+]?G[*]*|STO-3G)\b", output_text, re.IGNORECASE)
        if b_match:
            basis_val = b_match.group(1)

    return {
        "energy_hartree": _num(getattr(job, "e_elec_eh", None)),
        "geometry_xyz": geom_text,
        "geometry_hash": geometry_hash_v,
        "has_geometry": has_geom,
        "converged": bool(converged) if getattr(job, "terminated_normally", True) else False,
        "frequencies": freqs,
        "imaginary_count": imaginary,
        "temperature_k": temperature_k,
        "zpe_hartree": _num(getattr(job, "zpe_eh", None)),
        "enthalpy_hartree": _num(getattr(job, "total_enthalpy_eh", None)),
        "gibbs_hartree": _num(getattr(job, "gibbs_free_energy_eh", None)),
        "entropy_j_mol_k": entropy_j,
        "method": method_val,
        "basis_set": basis_val,
        "solvent_model": solvation_val,
        "solvent": solvent_val,
        "stationary_point_status": getattr(job, "stationary_point_status", None),
        "terminated_normally": bool(getattr(job, "terminated_normally", True)),
    }


def stage_capabilities_valid(kind: str, result: dict, output_text: str = "") -> list:
    caps = STAGE_CAPABILITIES.get(kind, {})
    problems = []
    if caps.get("produces_geometry") and not (result.get("geometry_xyz") or result.get("geometry_hash")):
        problems.append("geometry-producing stage produced no validated geometry")
    if caps.get("produces_geometry") and caps.get("requires_converged_minimum"):
        if not result.get("converged"):
            problems.append("optimization did not terminate normally - unusable geometry")
        elif "THE OPTIMIZATION HAS CONVERGED" not in (output_text or ""):
            problems.append("optimization did not converge - unconverged geometry cannot be used")
    if caps.get("produces_electronic_energy") and result.get("energy_hartree") is None:
        problems.append("no electronic energy found")
    if caps.get("produces_thermochemistry") and (result.get("gibbs_hartree") is None
                                                 or result.get("enthalpy_hartree") is None):
        problems.append("frequency/thermal stage produced no complete thermochemistry")
    return problems


def validate_workflow_stages(stage_list: list):
    """Geometry handoff + stale detection over an ordered stage list.
    Pass 1: Find final valid geometry of the workflow.
    Pass 2: Compare each preceding non-geometry stage against the final geometry.
    """
    warnings = []
    latest_geom_stage = None
    latest_geom_hash = None
    ordered = sorted(stage_list, key=lambda s: s.get("order", 0))

    # Pass 1: Find latest valid geometry
    for st in reversed(ordered):
        if st.get("state") != "COMPLETE":
            continue
        caps = STAGE_CAPABILITIES.get(st.get("kind"), {})
        if caps.get("produces_geometry") and st.get("geometry_hash") and st.get("converged"):
            latest_geom_stage = st["stage_id"]
            latest_geom_hash = st["geometry_hash"]
            break

    if latest_geom_hash is None:
        for st in reversed(ordered):
            if st.get("state") == "COMPLETE" and st.get("geometry_hash"):
                latest_geom_stage = st["stage_id"]
                latest_geom_hash = st["geometry_hash"]
                break

    # Pass 2: Detect stale stages against latest geometry
    for st in ordered:
        if st.get("state") != "COMPLETE":
            continue
        caps = STAGE_CAPABILITIES.get(st.get("kind"), {})
        if not caps.get("produces_geometry") and st.get("geometry_hash") and latest_geom_hash:
            if st["geometry_hash"] != latest_geom_hash:
                if caps.get("produces_thermochemistry"):
                    st["thermal_stale"] = True
                    warnings.append("STALE THERMAL DATA: stage %s (%s) was computed on a geometry that a later "
                                    "optimization replaced." % (st.get("label"), st.get("kind")))
                elif st.get("kind") in ("SP", "OPT"):
                    st["electronic_stale"] = True
                    warnings.append("STALE ELECTRONIC RESULT: stage %s (%s) belongs to an older geometry."
                                    % (st.get("label"), st.get("kind")))
            else:
                if caps.get("produces_thermochemistry"):
                    st["thermal_stale"] = False
                elif st.get("kind") in ("SP", "OPT"):
                    st["electronic_stale"] = False

    return True, warnings, latest_geom_stage


def assemble_final_result(stage_list: list):
    """Species Final Result Assembler - never simply 'the last job output'."""
    ordered = sorted([s for s in stage_list if s.get("state") == "COMPLETE"], key=lambda s: s.get("order", 0))
    _, warnings, latest_geom = validate_workflow_stages(ordered)
    if not ordered:
        raise ReactionValidationError("no completed stages to assemble from")
    geom_stage = next((s for s in reversed(ordered)
                       if STAGE_CAPABILITIES.get(s.get("kind"), {}).get("produces_geometry")
                       and s.get("geometry_hash") and s.get("converged") and s["stage_id"] == latest_geom), None)
    if geom_stage is None:
        # fallback: a workflow of only FREQ/SP stages still has its own input
        # geometry (parsed and hashed by the authoritative parser)
        geom_stage = next((s for s in reversed(ordered) if s.get("geometry_hash")), None)
    if geom_stage is None:
        raise ReactionValidationError("no validated converged geometry available for the final result")
    geom_hash = geom_stage.get("geometry_hash")
    freq_stage = next((s for s in reversed(ordered)
                       if STAGE_CAPABILITIES.get(s.get("kind"), {}).get("produces_thermochemistry")
                       and not s.get("thermal_stale")
                       and (s.get("geometry_hash") in (None, geom_hash))), None)
    sp_stage = next((s for s in reversed(ordered)
                     if s.get("kind") == "SP" and not s.get("electronic_stale")
                     and (s.get("geometry_hash") in (None, geom_hash))), None)
    if sp_stage is None:
        sp_stage = next((s for s in reversed(ordered)
                         if s.get("kind") in ("OPT", "OPTTS") and not s.get("electronic_stale")
                         and (s.get("geometry_hash") in (None, geom_hash))), None)
    freq_res = freq_stage.get("parsed") if freq_stage else None
    sp_res = sp_stage.get("parsed") if sp_stage else None
    if freq_res is None and sp_res is None:
        raise ReactionValidationError("no valid electronic energy source available")
    result = {
        "final_geometry_source_stage_id": geom_stage["stage_id"],
        "final_geometry_source_label": geom_stage.get("label"),
        "geometry_hash": geom_hash,
        "final_thermal_source_stage_id": freq_stage["stage_id"] if freq_stage else None,
        "final_thermal_source_label": freq_stage.get("label") if freq_stage else None,
        "final_electronic_source_stage_id": (sp_stage or freq_stage)["stage_id"],
        "final_electronic_source_label": (sp_stage or freq_stage).get("label"),
        "composite": bool(sp_stage and freq_stage and sp_stage["stage_id"] != freq_stage["stage_id"]),
        "warnings": list(warnings),
        "temperature_k": (freq_res or sp_res or {}).get("temperature_k"),
        "zpe_hartree": freq_res.get("zpe_hartree") if freq_res else None,
        "entropy_j_mol_k": freq_res.get("entropy_j_mol_k") if freq_res else None,
        "method": (sp_res or freq_res or {}).get("method"),
        "basis_set": (sp_res or freq_res or {}).get("basis_set"),
        "solvent_model": (sp_res or freq_res or {}).get("solvent_model"),
        "solvent": (sp_res or freq_res or {}).get("solvent"),
        "imaginary_count": freq_res.get("imaginary_count") if freq_res else 0,
    }
    if freq_res:
        result.update({"E_freq": freq_res.get("energy_hartree"),
                       "H_freq": freq_res.get("enthalpy_hartree"),
                       "G_freq": freq_res.get("gibbs_hartree")})
    if sp_res:
        result["E_SP"] = sp_res.get("energy_hartree")
    if result["composite"] and freq_res:
        if None in (result.get("E_SP"), result.get("E_freq"), result.get("H_freq"), result.get("G_freq")):
            raise ReactionValidationError("composite assembly requires complete E/H/G from the frequency stage "
                                          "and an electronic energy from the SP stage")
        result["dH_thermal"] = result["H_freq"] - result["E_freq"]
        result["dG_thermal"] = result["G_freq"] - result["E_freq"]
        result["E_elec_final"] = result["E_SP"]
        result["H_final"] = result["E_SP"] + result["dH_thermal"]
        result["G_final"] = result["E_SP"] + result["dG_thermal"]
        result["E0_final"] = (result["E_SP"] + result["zpe_hartree"]) if result.get("zpe_hartree") is not None else None
        result["equation_H"] = "H_final = E_SP + (H_freq - E_freq)"
        result["equation_G"] = "G_final = E_SP + (G_freq - E_freq)"
        result["equation_E0"] = "E0_final = E_SP + ZPE_freq"
    elif freq_res:
        result["E_elec_final"] = result["E_freq"]
        result["H_final"] = result["H_freq"]
        result["G_final"] = result["G_freq"]
        result["E0_final"] = (result["E_freq"] + result["zpe_hartree"]) if result.get("zpe_hartree") is not None else None
        result["equation_H"] = "H_final = H_freq (direct)"
        result["equation_G"] = "G_final = G_freq (direct)"
        result["equation_E0"] = "E0_final = E_freq + ZPE_freq"
    else:
        result["E_elec_final"] = result["E_SP"]
        result["H_final"] = None
        result["G_final"] = None
        result["E0_final"] = None
        result["equation_H"] = None
        result["equation_G"] = None
        result["equation_E0"] = None
        result["warnings"].append("INCOMPLETE THERMOCHEMISTRY: no compatible frequency/thermal stage - "
                                  "G is not available from SP-only results.")
    result["E_final"] = result["E_elec_final"]
    result["complete_thermochemistry"] = result["G_final"] is not None and result["H_final"] is not None
    return result


# ------------------------- reaction thermodynamics -------------------------
def compute_reaction_thermodynamics(species_results, temperature_tolerance_k=0.01):
    warnings = []
    incomplete = [s for s in species_results if not s.get("final_result", {}).get("complete_thermochemistry")]
    if incomplete:
        raise ReactionValidationError("not all species have complete valid thermochemistry - "
                                      "reaction thermodynamics blocked (%d incomplete)" % len(incomplete))
    temps = {round(float(s["final_result"].get("temperature_k") or 0.0), 6)
             for s in species_results if s.get("final_result", {}).get("temperature_k")}
    if len(temps) > 1:
        raise ReactionValidationError("temperature mismatch across species (%s K) - resolve before computing "
                                      "reaction thermodynamics; no silent interpolation." % sorted(temps))
    temperature_k = temps.pop() if temps else None
    methods = {(s["final_result"].get("method"), s["final_result"].get("basis_set")) for s in species_results}
    solvents = {(s["final_result"].get("solvent_model"), s["final_result"].get("solvent")) for s in species_results}
    if len(methods) > 1:
        warnings.append("WARNING - MIXED LEVEL OF THEORY: %s" % sorted(str(m) for m in methods))
    if len(solvents) > 1:
        warnings.append("WARNING - MIXED SOLVENT MODELS: %s" % sorted(str(s) for s in solvents))
    deltas = {"E_elec": 0.0, "ZPE": 0.0, "H": 0.0, "G": 0.0}
    S_rxn = 0.0
    for s in species_results:
        fr = s["final_result"]
        nu = float(s["nu"])
        deltas["E_elec"] += nu * float(fr["E_final"])
        if fr.get("H_final") is not None:
            deltas["H"] += nu * float(fr["H_final"])
        if fr.get("G_final") is not None:
            deltas["G"] += nu * float(fr["G_final"])
        if fr.get("zpe_hartree") is not None:
            deltas["ZPE"] += nu * float(fr["zpe_hartree"])
        if fr.get("entropy_j_mol_k") is not None:
            S_rxn += nu * float(fr["entropy_j_mol_k"])
    deltas["S"] = S_rxn
    dE0_hartree = deltas["E_elec"] + deltas["ZPE"]
    for s in species_results:
        imag = s.get("final_result", {}).get("imaginary_count") or 0
        if imag and s.get("role") != "transition_state":
            warnings.append("WARNING - INVALID_MINIMUM: species %s has %d imaginary frequency(ies)."
                            % (s.get("display_name"), imag))
    dG_J = deltas["G"] * HARTREE_KJ_MOL * 1000.0
    dH_J = deltas["H"] * HARTREE_KJ_MOL * 1000.0
    if temperature_k and abs(dG_J - (dH_J - temperature_k * S_rxn)) > 1e-3:
        warnings.append("WARNING - G/H/S INCONSISTENCY beyond numerical tolerance.")
    ln_k = (-dG_J / (R_J_MOL_K * temperature_k)) if temperature_k else None
    log10_k = (ln_k / math.log(10.0)) if ln_k is not None else None
    k_value, k_note = None, None
    if ln_k is not None:
        if ln_k > 690.0:
            k_note = "K > 1e300 (use lnK / log10K)"
            k_value = float("inf")
        elif ln_k < -690.0:
            k_note = "K < 1e-300 (use lnK / log10K)"
            k_value = 0.0
        else:
            k_value = math.exp(ln_k)
    return {
        "temperature_k": temperature_k,
        "dE_elec_hartree": deltas["E_elec"], "dZPE_hartree": deltas["ZPE"],
        "dE0_hartree": dE0_hartree, "delta_E0_hartree": dE0_hartree,
        "dH_hartree": deltas["H"], "dS_j_mol_k": deltas["S"], "dG_hartree": deltas["G"],
        "delta_E_elec_hartree": deltas["E_elec"], "delta_ZPE_hartree": deltas["ZPE"],
        "delta_H_hartree": deltas["H"], "delta_S_j_mol_k": deltas["S"], "delta_G_hartree": deltas["G"],
        "dE_elec_kj_mol": deltas["E_elec"] * HARTREE_KJ_MOL, "dZPE_kj_mol": deltas["ZPE"] * HARTREE_KJ_MOL,
        "dE0_kj_mol": dE0_hartree * HARTREE_KJ_MOL, "delta_E0_kj_mol": dE0_hartree * HARTREE_KJ_MOL,
        "dH_kj_mol": deltas["H"] * HARTREE_KJ_MOL, "dG_kj_mol": deltas["G"] * HARTREE_KJ_MOL,
        "ln_k": ln_k, "log10_k": log10_k, "k": k_value, "k_note": k_note,
        "warnings": warnings, "species_count": len(species_results),
    }


# ------------------------- durable store -------------------------
class ReactionStore:
    """JSON file store (atomic writes) for reactions/stages/thermo + report
    metadata. Survives backend restarts. Single active report per immutable
    thermodynamics result (documented policy AB8)."""

    def __init__(self, state_dir: str, now_provider=None):
        self.state_dir = state_dir
        self.reports_dir = os.path.join(state_dir, "thermo_reports")
        os.makedirs(self.reports_dir, exist_ok=True)
        self.reactions_path = os.path.join(state_dir, "reaction_workflows.json")
        self.reports_path = os.path.join(state_dir, "thermo_reports_meta.json")
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        from services.local_orca_service import CrossProcessFileLock
        self.lock_path = os.path.join(state_dir, ".reaction_store.lock")
        self._lock = CrossProcessFileLock(self.lock_path)
        self._last_cleanup = 0.0

    def _read(self, path, default):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def _write_atomic(self, path, data):
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def list_reactions(self, owner):
        with self._lock:
            items = self._read(self.reactions_path, [])
        return [r for r in items if owner is None or r.get("owner") == owner]

    def get_reaction(self, owner, reaction_id):
        with self._lock:
            items = self._read(self.reactions_path, [])
        for r in items:
            if r.get("reaction_id") == reaction_id and (owner is None or r.get("owner") == owner):
                return r
        return None

    def save_reaction(self, reaction: dict):
        with self._lock:
            items = self._read(self.reactions_path, [])
            items = [r for r in items if r.get("reaction_id") != reaction.get("reaction_id")]
            items.append(reaction)
            self._write_atomic(self.reactions_path, items)

    def save_report_metadata(self, meta: dict):
        with self._lock:
            items = self._read(self.reports_path, [])
            items = [r for r in items if r.get("report_id") != meta.get("report_id")]
            items.append(meta)
            self._write_atomic(self.reports_path, items)

    def get_report(self, report_id: str):
        with self._lock:
            items = self._read(self.reports_path, [])
        for r in items:
            if r.get("report_id") == report_id:
                return r
        return None

    def replace_active_report(self, meta: dict):
        with self._lock:
            items = self._read(self.reports_path, [])
            for r in [x for x in items if x.get("result_id") == meta.get("result_id")]:
                try:
                    os.unlink(os.path.join(self.reports_dir, r["stored_report_id"]))
                except OSError:
                    pass
            items = [r for r in items if r.get("result_id") != meta.get("result_id")]
            items.append(meta)
            self._write_atomic(self.reports_path, items)

    def cleanup_expired_reports(self, now_provider=None):
        now = (now_provider or self.now_provider)()
        removed = []
        with self._lock:
            items = self._read(self.reports_path, [])
            keep = []
            for r in items:
                try:
                    expires = datetime.fromisoformat(r["expires_at"])
                except (KeyError, ValueError):
                    keep.append(r)
                    continue
                if now >= expires:
                    try:
                        os.unlink(os.path.join(self.reports_dir, r["stored_report_id"]))
                        removed.append(r["report_id"])
                    except OSError:
                        pass
                else:
                    keep.append(r)
            if removed:
                self._write_atomic(self.reports_path, keep)
        return removed

    def cleanup_if_due(self, now_provider=None):
        import time as _t
        now_t = _t.time()
        if now_t - self._last_cleanup >= _REPORT_CLEANUP_MIN_INTERVAL_S:
            self._last_cleanup = now_t
            return self.cleanup_expired_reports(now_provider)
        return []

    def reserve_execution_slot(self, backend: str = "local", max_concurrency: int = None, owner: str = None):
        """Atomically reserves an execution slot respecting backend concurrency limits, FIFO order, and Kaggle enablement."""
        if backend == "kaggle":
            try:
                from services.local_orca_service import get_local_orca_settings
                if not get_local_orca_settings(state_dir=self.state_dir).get("kaggle_enabled", False):
                    return None
            except Exception:
                return None

        max_concurrency = max_concurrency or (
            DEFAULT_LOCAL_ORCA_CONCURRENCY if backend == "local" else DEFAULT_KAGGLE_CONCURRENCY
        )
        now_iso = utcnow_iso(self.now_provider)
        with self._lock:
            items = self._read(self.reactions_path, [])
            active_count = 0
            candidates = []
            for r in items:
                if r.get("state") in ("PAUSED", "CANCELLED"):
                    continue
                for sp in r.get("species", []):
                    if sp.get("state") in ("PAUSED", "CANCELLED"):
                        continue
                    for st in sp.get("stages", []):
                        st_backend = st.get("backend") or "local"
                        if st_backend == backend and st.get("state") in ACTIVE_EXECUTION_STATES:
                            active_count += 1
                        elif st_backend == backend and st.get("state") in ("READY", "QUEUED"):
                            if owner is None or r.get("owner") == owner:
                                candidates.append((r, sp, st))
            if active_count >= max_concurrency or not candidates:
                return None
            candidates.sort(key=lambda x: x[2].get("ready_at") or x[0].get("created_at") or "")
            target_r, target_sp, target_st = candidates[0]
            target_st["state"] = "SUBMITTING"
            target_st["submitted_at"] = now_iso
            target_sp["state"] = "RUNNING"
            target_r["state"] = "RUNNING"
            self._write_atomic(self.reactions_path, items)
            return (target_r, target_sp, target_st)

    def pause_reaction(self, owner: str, reaction_id: str):
        """Pauses queued and ready stages of a reaction."""
        with self._lock:
            items = self._read(self.reactions_path, [])
            target = None
            for r in items:
                if r.get("reaction_id") == reaction_id and (owner is None or r.get("owner") == owner):
                    target = r
                    break
            if not target:
                return None
            target["state"] = "PAUSED"
            for sp in target.get("species", []):
                if sp.get("state") in ("PENDING", "RUNNING"):
                    sp["state"] = "PAUSED"
                for st in sp.get("stages", []):
                    if st.get("state") in ("READY", "QUEUED"):
                        st["state"] = "PAUSED"
            self._write_atomic(self.reactions_path, items)
            return target

    def resume_reaction(self, owner: str, reaction_id: str):
        """Resumes a paused reaction, re-enabling ready stages."""
        now_iso = utcnow_iso(self.now_provider)
        with self._lock:
            items = self._read(self.reactions_path, [])
            target = None
            for r in items:
                if r.get("reaction_id") == reaction_id and (owner is None or r.get("owner") == owner):
                    target = r
                    break
            if not target:
                return None
            target["state"] = "RUNNING" if any(st.get("state") in ACTIVE_EXECUTION_STATES for sp in target.get("species", []) for st in sp.get("stages", [])) else "READY"
            for sp in target.get("species", []):
                if sp.get("state") == "PAUSED":
                    sp["state"] = "PENDING"
                for st in sp.get("stages", []):
                    if st.get("state") == "PAUSED":
                        st["state"] = "READY"
                        st["ready_at"] = now_iso
            self._write_atomic(self.reactions_path, items)
            return target

    def cancel_reaction(self, owner: str, reaction_id: str):
        """Cancels remaining non-complete stages of a reaction."""
        with self._lock:
            items = self._read(self.reactions_path, [])
            target = None
            for r in items:
                if r.get("reaction_id") == reaction_id and (owner is None or r.get("owner") == owner):
                    target = r
                    break
            if not target:
                return None
            target["state"] = "CANCELLED"
            for sp in target.get("species", []):
                if sp.get("state") not in ("COMPLETE", "FAILED"):
                    sp["state"] = "CANCELLED"
                for st in sp.get("stages", []):
                    if st.get("state") not in ("COMPLETE", "FAILED"):
                        st["state"] = "CANCELLED"
            self._write_atomic(self.reactions_path, items)
            return target

    def get_queue_summary(self, backend: str = None, owner: str = None):
        """Aggregates queue metrics across all reactions."""
        with self._lock:
            items = self._read(self.reactions_path, [])
            counts = {
                "active": 0, "queued": 0, "ready": 0, "complete": 0,
                "failed": 0, "cancelled": 0, "blocked": 0, "paused": 0, "total": 0
            }
            for r in items:
                if owner is not None and r.get("owner") != owner:
                    continue
                for sp in r.get("species", []):
                    for st in sp.get("stages", []):
                        st_b = st.get("backend") or "local"
                        if backend is not None and st_b != backend:
                            continue
                        counts["total"] += 1
                        state = st.get("state", "PENDING")
                        if state in ACTIVE_EXECUTION_STATES:
                            counts["active"] += 1
                        elif state in ("QUEUED", "READY"):
                            counts["queued"] += 1
                            if state == "READY":
                                counts["ready"] += 1
                        elif state == "COMPLETE":
                            counts["complete"] += 1
                        elif state == "FAILED":
                            counts["failed"] += 1
                        elif state == "CANCELLED":
                            counts["cancelled"] += 1
                        elif state == "BLOCKED_BY_DEPENDENCY":
                            counts["blocked"] += 1
                        elif state == "PAUSED":
                            counts["paused"] += 1
            return counts


DEFAULT_SHARED_STAGES = [
    {"kind": "OPT", "label": "Geometry Optimization", "order": 0},
    {"kind": "FREQ", "label": "Frequency & Thermochemistry", "order": 1},
]


def compute_workflow_hash(stages: list) -> str:
    simplified = [
        {
            "kind": (s.get("kind") or "").upper(),
            "label": s.get("label") or "",
            "order": s.get("order", 0),
            "options": s.get("options") or {},
        }
        for s in sorted(stages, key=lambda x: x.get("order", 0))
    ]
    return sha256_text(json.dumps(simplified, sort_keys=True))


# ------------------------- reaction CRUD / workflow ops -------------------------
def create_reaction(owner: str, equation: str, store: ReactionStore, display_name: str = "") -> dict:
    species_terms = parse_reaction_equation(equation)
    balanced, balance_warnings = validate_reaction_balance(
        [{**t, "formula": t["name"]} for t in species_terms])
    now = utcnow_iso(store.now_provider)
    shared_wf_id = uuid.uuid4().hex
    shared_wf = {
        "shared_workflow_id": shared_wf_id,
        "workflow_revision": 1,
        "workflow_hash": compute_workflow_hash(DEFAULT_SHARED_STAGES),
        "stages": list(DEFAULT_SHARED_STAGES),
    }
    reaction = {
        "reaction_id": uuid.uuid4().hex, "owner": owner,
        "display_name": display_name or equation.strip(),
        "equation": equation.strip(), "state": "READY",
        "created_at": now, "updated_at": now,
        "balance_valid": balanced, "balance_warnings": balance_warnings,
        "shared_workflow": shared_wf,
        "species": [],
    }
    for term in species_terms:
        norm = term["name"].strip().upper().replace(" ", "")
        initial_geom = STANDARD_3D_GEOMETRIES.get(norm)
        reaction["species"].append({
            "initial_geometry": initial_geom,
            "species_id": uuid.uuid4().hex, "reaction_id": reaction["reaction_id"],
            "display_name": term["name"], "formula": term["name"], "role": term["role"],
            "nu": term["nu"], "stoichiometric_coefficient": term["coefficient"],
            "charge": term.get("charge", term.get("charge_hint", 0)), "multiplicity": 1,
            "workflow_id": uuid.uuid4().hex,
            "workflow_source": "SHARED",
            "workflow_hash": shared_wf["workflow_hash"],
            "stages": [], "latest_valid_geometry_stage_id": None,
            "immediately_previous_stage_id": None, "state": "PENDING",
            "final_result": None, "thermochemistry_status": "PENDING",
            "provenance": {"created_at": now},
        })
    store.save_reaction(reaction)
    return reaction


def set_shared_workflow(reaction: dict, stage_templates: list, store: ReactionStore = None) -> dict:
    if len(stage_templates) > MAX_STAGES_PER_SPECIES:
        raise ReactionValidationError("Shared workflow exceeds limit of %d stages." % MAX_STAGES_PER_SPECIES)
    cleaned = []
    for idx, st in enumerate(stage_templates):
        kind = (st.get("kind") or "").upper()
        if kind not in STAGE_KINDS:
            raise ReactionValidationError("Invalid stage kind in template: %s" % kind)
        cleaned.append({
            "kind": kind,
            "label": st.get("label") or ("%s %d" % (kind.title(), idx + 1)),
            "order": idx,
            "options": st.get("options") or {},
        })
    sw = reaction.get("shared_workflow") or {}
    shared_wf = {
        "shared_workflow_id": sw.get("shared_workflow_id") or uuid.uuid4().hex,
        "workflow_revision": sw.get("workflow_revision", 0) + 1,
        "workflow_hash": compute_workflow_hash(cleaned),
        "stages": cleaned,
    }
    reaction["shared_workflow"] = shared_wf
    if store is not None:
        store.save_reaction(reaction)
    return shared_wf


def apply_shared_workflow_to_species(reaction: dict, species_id: str, store: ReactionStore = None) -> dict:
    species = _get_species(reaction, species_id)
    shared_wf = reaction.get("shared_workflow")
    if not shared_wf or not shared_wf.get("stages"):
        shared_wf = set_shared_workflow(reaction, DEFAULT_SHARED_STAGES)
    stage_templates = shared_wf["stages"]
    now_iso = utcnow_iso(store.now_provider if store else None)
    new_stages_list = []
    prev_id = None
    for idx, tmpl in enumerate(stage_templates):
        st = new_stage(tmpl["kind"], tmpl.get("label", ""), order=idx)
        st["parent_stage_id"] = prev_id
        if idx == 0:
            st["state"] = "READY"
            st["ready_at"] = now_iso
        else:
            st["state"] = "BLOCKED_BY_DEPENDENCY"
        prev_id = st["stage_id"]
        new_stages_list.append(st)
    species["stages"] = new_stages_list
    species["workflow_source"] = "SHARED"
    species["workflow_hash"] = shared_wf["workflow_hash"]
    species["state"] = "PENDING"
    species["latest_valid_geometry_stage_id"] = None
    species["immediately_previous_stage_id"] = new_stages_list[-1]["stage_id"] if new_stages_list else None
    if store is not None:
        store.save_reaction(reaction)
    return species


def apply_shared_workflow_to_all(reaction: dict, store: ReactionStore = None, overwrite_custom: bool = False) -> dict:
    for sp in reaction.get("species", []):
        if overwrite_custom or sp.get("workflow_source") != "CUSTOM":
            apply_shared_workflow_to_species(reaction, sp["species_id"], store=None)
    if store is not None:
        store.save_reaction(reaction)
    return reaction


def set_species_custom_workflow(reaction: dict, species_id: str, stages: list, store: ReactionStore = None) -> dict:
    if len(stages) > MAX_STAGES_PER_SPECIES:
        raise ReactionValidationError("Custom workflow exceeds limit of %d stages." % MAX_STAGES_PER_SPECIES)
    species = _get_species(reaction, species_id)
    now_iso = utcnow_iso(store.now_provider if store else None)
    new_stages_list = []
    prev_id = None
    for idx, s_def in enumerate(stages):
        kind = (s_def.get("kind") or "").upper()
        if kind not in STAGE_KINDS:
            raise ReactionValidationError("Invalid stage kind: %s" % kind)
        st = new_stage(kind, s_def.get("label", ""), order=idx)
        st["parent_stage_id"] = prev_id
        if idx == 0:
            st["state"] = "READY"
            st["ready_at"] = now_iso
        else:
            st["state"] = "BLOCKED_BY_DEPENDENCY"
        prev_id = st["stage_id"]
        new_stages_list.append(st)
    species["stages"] = new_stages_list
    species["workflow_source"] = "CUSTOM"
    species["workflow_hash"] = compute_workflow_hash([{"kind": s["kind"], "label": s["label"], "order": s["order"]} for s in new_stages_list])
    species["state"] = "PENDING"
    species["latest_valid_geometry_stage_id"] = None
    species["immediately_previous_stage_id"] = new_stages_list[-1]["stage_id"] if new_stages_list else None
    if store is not None:
        store.save_reaction(reaction)
    return species


def _get_species(reaction, species_id):
    for s in reaction["species"]:
        if s["species_id"] == species_id:
            return s
    raise ReactionValidationError("species %s not found" % species_id)


def _get_stage(reaction, stage_id):
    for sp in reaction["species"]:
        for st in sp["stages"]:
            if st["stage_id"] == stage_id:
                return st
    raise ReactionValidationError("stage %s not found" % stage_id)


def add_stage(reaction: dict, species_id: str, kind: str, label: str = "") -> dict:
    species = _get_species(reaction, species_id)
    stages = species["stages"]
    if len(stages) >= MAX_STAGES_PER_SPECIES:
        raise ReactionValidationError("stage limit reached (%d stages per species)." % MAX_STAGES_PER_SPECIES)
    stage = new_stage(kind, label or "%s %d" % (kind.title(), len(stages) + 1), order=len(stages))
    prev = stages[-1] if stages else None
    stage["parent_stage_id"] = prev["stage_id"] if prev else None
    if not prev or prev.get("state") == "COMPLETE":
        stage["state"] = "READY"
        stage["ready_at"] = utcnow_iso()
    else:
        stage["state"] = "BLOCKED_BY_DEPENDENCY"
    stages.append(stage)
    species["immediately_previous_stage_id"] = prev["stage_id"] if prev else None
    species["state"] = "PENDING"
    reaction["state"] = "READY"
    return stage


def set_stage_input(reaction: dict, stage_id: str, input_text: str):
    stage = _get_stage(reaction, stage_id)
    if stage.get("state") not in ("PENDING", "READY", "FAILED", "BLOCKED_BY_DEPENDENCY"):
        raise ReactionValidationError("stage %s is %s - only PENDING/READY/FAILED stages accept new input."
                                      % (stage_id, stage.get("state")))
    stage["input_text"] = input_text
    stage["input_hash"] = sha256_text(input_text)
    stage["attempt_id"] = uuid.uuid4().hex
    return stage


def complete_stage_with_output(reaction: dict, species_id: str, stage_id: str, output_text: str,
                               store: ReactionStore = None):
    species = _get_species(reaction, species_id)
    stage = _get_stage(reaction, stage_id)
    if stage.get("state") in ("COMPLETE", "RUNNING"):
        raise ReactionValidationError("stage %s is already %s." % (stage_id, stage["state"]))
    stage["state"] = "VALIDATING"
    stage["output_text"] = output_text
    stage["output_hash"] = sha256_text(output_text)
    try:
        result = extract_stage_result(output_text)
    except Exception as exc:
        stage["state"] = "FAILED"
        stage["error"] = str(exc)[:300]
        species["state"] = "FAILED"
        reaction["state"] = "WAITING"
        if store is not None:
            store.save_reaction(reaction)
        return stage
    problems = stage_capabilities_valid(stage["kind"], result, output_text)
    if problems:
        stage["state"] = "FAILED"
        stage["error"] = "; ".join(problems)[:400]
        species["state"] = "FAILED"
        reaction["state"] = "WAITING"
    elif stage["kind"] in ("FREQ", "NUMFREQ", "OPT_FREQ", "OPTTS_FREQ") and result.get("imaginary_count"):
        if stage.get("transition_state_stage"):
            stage["imaginary_warning"] = "%d imaginary mode(s) expected for a transition state." % result["imaginary_count"]
            stage["state"] = "COMPLETE"
            stage["parsed"] = result
            if stage.get("produces_geometry") and stage.get("geometry_hash"):
                species["latest_valid_geometry_stage_id"] = stage["stage_id"]
        else:
            stage["state"] = "FAILED"
            stage["error"] = ("INVALID_MINIMUM: %d imaginary frequency(ies) - not a minimum. "
                              "Imaginary frequencies are never flipped." % result["imaginary_count"])
            species["state"] = "FAILED"
            reaction["state"] = "WAITING"
    else:
        stage["parsed"] = result
        stage["geometry_hash"] = result.get("geometry_hash") or stage.get("geometry_hash")
        stage["converged"] = bool(result.get("converged"))
        stage["terminated_normally"] = bool(result.get("terminated_normally", True))
        stage["state"] = "COMPLETE"
        if stage.get("produces_geometry") and stage.get("geometry_hash"):
            species["latest_valid_geometry_stage_id"] = stage["stage_id"]

        # Advance next stage in species dependency chain
        st_idx = next((i for i, s in enumerate(species["stages"]) if s["stage_id"] == stage_id), -1)
        if st_idx >= 0 and st_idx + 1 < len(species["stages"]):
            nxt = species["stages"][st_idx + 1]
            if nxt.get("state") == "BLOCKED_BY_DEPENDENCY":
                # Propagate optimized geometry if available
                opt_xyz = stage.get("parsed", {}).get("geometry_xyz") or species.get("initial_geometry")
                if opt_xyz and nxt.get("stage_options"):
                    import chem_core as core
                    opts = dict(nxt["stage_options"])
                    opts["coords"] = opt_xyz
                    nxt["input_text"] = core.generate_orca_6_input(opts)
                    nxt["input_hash"] = sha256_text(nxt["input_text"])
                nxt["state"] = "READY"
                nxt["ready_at"] = utcnow_iso(store.now_provider if store else None)
                nxt["parent_stage_id"] = stage["stage_id"]

        all_sp_stages_done = all(s.get("state") == "COMPLETE" for s in species["stages"])
        if all_sp_stages_done and species["stages"]:
            assemble_species_result(reaction, species_id, store=None)

        species["state"] = "COMPLETE" if all(s.get("state") == "COMPLETE" for s in species["stages"]) else "RUNNING"
        reaction["state"] = "WAITING"

        # Check if entire reaction completed -> Auto Thermo + PDF
        all_species_done = all(sp.get("state") == "COMPLETE" and sp.get("final_result") for sp in reaction["species"])
        if all_species_done and reaction["species"] and store is not None:
            try:
                thermo = compute_and_store_thermodynamics(reaction, store)
                try:
                    from services import thermo_report_service as _trs
                    meta = _trs.create_report(store, reaction.get("owner"), reaction, thermo)
                    reaction["thermo_report_id"] = meta.get("report_id")
                except Exception:
                    pass
            except Exception:
                pass

    if store is not None:
        validate_workflow_stages(species["stages"])
        store.save_reaction(reaction)
    return stage


def assemble_species_result(reaction: dict, species_id: str, store: ReactionStore = None):
    species = _get_species(reaction, species_id)
    result = assemble_final_result(stage_list=species["stages"])
    species["final_result"] = result
    species["thermochemistry_status"] = "COMPLETE" if result.get("complete_thermochemistry") else "PARTIAL"
    species["state"] = "COMPLETE"
    if store is not None:
        store.save_reaction(reaction)
    return result


def compute_and_store_thermodynamics(reaction: dict, store: ReactionStore):
    species_results = []
    for sp in reaction["species"]:
        if not sp.get("final_result"):
            assemble_species_result(reaction, sp["species_id"])
        species_results.append({"display_name": sp["display_name"], "nu": sp["nu"],
                                "role": sp["role"], "final_result": sp["final_result"]})
    thermo = compute_reaction_thermodynamics(species_results)
    thermo["result_id"] = uuid.uuid4().hex
    thermo["computed_at"] = utcnow_iso(store.now_provider)
    thermo["equation"] = reaction["equation"]
    thermo["species_provenance"] = [
        {"display_name": sp["display_name"], "nu": sp["nu"],
         "geometry_source": sp["final_result"].get("final_geometry_source_label"),
         "electronic_source": sp["final_result"].get("final_electronic_source_label"),
         "thermal_source": sp["final_result"].get("final_thermal_source_label"),
         "method": sp["final_result"].get("method"), "basis_set": sp["final_result"].get("basis_set"),
         "equation_G": sp["final_result"].get("equation_G")}
        for sp in reaction["species"]]
    reaction["state"] = "COMPLETE"
    reaction["thermodynamics"] = thermo
    store.save_reaction(reaction)
    return thermo

# ------------------------- Standard 3D Geometries & Resolution -------------------------
STANDARD_3D_GEOMETRIES = {
    "H2": "H  0.000000  0.000000  0.000000\nH  0.000000  0.000000  0.741440",
    "O2": "O  0.000000  0.000000  0.000000\nO  0.000000  0.000000  1.207500",
    "N2": "N  0.000000  0.000000  0.000000\nN  0.000000  0.000000  1.097700",
    "F2": "F  0.000000  0.000000  0.000000\nF  0.000000  0.000000  1.411900",
    "CL2": "Cl 0.000000  0.000000  0.000000\nCl 0.000000  0.000000  1.987900",
    "BR2": "Br 0.000000  0.000000  0.000000\nBr 0.000000  0.000000  2.281100",
    "I2": "I  0.000000  0.000000  0.000000\nI  0.000000  0.000000  2.666300",
    "HF": "F  0.000000  0.000000  0.000000\nH  0.000000  0.000000  0.916800",
    "HCL": "Cl 0.000000  0.000000  0.000000\nH  0.000000  0.000000  1.274600",
    "HBR": "Br 0.000000  0.000000  0.000000\nH  0.000000  0.000000  1.414400",
    "HI": "I  0.000000  0.000000  0.000000\nH  0.000000  0.000000  1.609200",
    "CO": "C  0.000000  0.000000  0.000000\nO  0.000000  0.000000  1.128300",
    "CO2": "C  0.000000  0.000000  0.000000\nO  0.000000  0.000000  1.162100\nO  0.000000  0.000000 -1.162100",
    "H2O": "O  0.000000  0.000000  0.117300\nH  0.000000  0.757200 -0.469200\nH  0.000000 -0.757200 -0.469200",
    "H2S": "S  0.000000  0.000000  0.102200\nH  0.000000  0.961600 -0.817500\nH  0.000000 -0.961600 -0.817500",
    "SO2": "S  0.000000  0.000000  0.360000\nO  0.000000  1.230000 -0.360000\nO  0.000000 -1.230000 -0.360000",
    "NH3": "N  0.000000  0.000000  0.116500\nH  0.000000  0.939700 -0.271800\nH  0.813800 -0.469900 -0.271800\nH -0.813800 -0.469900 -0.271800",
    "CH4": "C  0.000000  0.000000  0.000000\nH  0.627600  0.627600  0.627600\nH -0.627600 -0.627600  0.627600\nH -0.627600  0.627600 -0.627600\nH  0.627600 -0.627600 -0.627600",
    "HCN": "H  0.000000  0.000000 -1.064000\nC  0.000000  0.000000  0.000000\nN  0.000000  0.000000  1.156000",
    "NO": "N  0.000000  0.000000  0.000000\nO  0.000000  0.000000  1.150800",
    "NO2": "N  0.000000  0.000000  0.300000\nO  0.000000  1.080000 -0.300000\nO  0.000000 -1.080000 -0.300000",
}


def resolve_species_3d_geometry(name_or_formula: str, smiles: str = None) -> tuple[str | None, str | None]:
    """Resolve chemical species name, formula, or SMILES to standard 3D Cartesian coordinates (XYZ text)."""
    norm = name_or_formula.strip().upper().replace(" ", "")
    if norm in STANDARD_3D_GEOMETRIES:
        return STANDARD_3D_GEOMETRIES[norm], None

    import chem_core as core
    candidate_smiles = smiles or name_or_formula
    try:
        from rdkit import Chem
        mol = Chem.MolFromSmiles(candidate_smiles)
        if mol is not None:
            xyz = core.xyz_from_smiles(candidate_smiles)
            if xyz:
                return xyz, None
    except Exception:
        pass

    return None, f"Could not generate 3D coordinates for '{name_or_formula}'"


def generate_unified_reaction_inputs(reaction: dict, workflow_config: dict, store: ReactionStore = None) -> dict:
    """Configures stages for all species and generates standard ORCA 6 input decks."""
    import chem_core as core
    method = workflow_config.get("method", "B3LYP")
    basis = workflow_config.get("basis", "def2-SVP")
    disp = workflow_config.get("disp", "D3BJ")
    solv_model = workflow_config.get("solv_model", "none")
    solvent = workflow_config.get("solvent", "Water")
    cores = int(workflow_config.get("cores", 4))
    ram = int(workflow_config.get("ram", 2000))
    backend = workflow_config.get("backend", "local")
    target_device = workflow_config.get("target_device")

    raw_stages = workflow_config.get("stages") or [
        {"kind": "OPT", "label": "Geometry Optimization", "order": 0},
        {"kind": "FREQ", "label": "Frequency & Thermochemistry", "order": 1},
    ]

    for sp in reaction.get("species", []):
        # 1. Ensure 3D initial geometry
        if not sp.get("initial_geometry"):
            coords, _ = resolve_species_3d_geometry(sp["formula"])
            if coords:
                sp["initial_geometry"] = coords

        sp_coords = sp.get("initial_geometry") or ""

        # 2. Build stages
        sp["stages"] = []
        for idx, st_def in enumerate(raw_stages):
            kind = st_def.get("kind", "OPT").upper()
            st = new_stage(kind=kind, label=st_def.get("label", kind), order=idx, backend=backend)
            st["target_device"] = target_device

            calc_type = "opt" if kind == "OPT" else ("freq" if kind == "FREQ" else ("opt_freq" if kind in ("OPT_FREQ", "OPT_AND_FREQ") else "sp"))
            
            # First stage gets initial geometry and becomes READY
            if idx == 0:
                inp_payload = {
                    "calc_type": calc_type,
                    "theory": method,
                    "basis": basis,
                    "disp": disp,
                    "solv_model": solv_model,
                    "solvent": solvent,
                    "charge": sp.get("charge", 0),
                    "mult": sp.get("multiplicity", 1),
                    "cores": cores,
                    "ram": ram,
                    "coords": sp_coords,
                }
                st["input_text"] = core.generate_orca_6_input(inp_payload)
                st["input_hash"] = sha256_text(st["input_text"])
                st["state"] = "READY"
                st["ready_at"] = utcnow_iso(store.now_provider if store else None)
            else:
                st["state"] = "BLOCKED_BY_DEPENDENCY"
                st["stage_options"] = {
                    "calc_type": calc_type,
                    "theory": method,
                    "basis": basis,
                    "disp": disp,
                    "solv_model": solv_model,
                    "solvent": solvent,
                    "charge": sp.get("charge", 0),
                    "mult": sp.get("multiplicity", 1),
                    "cores": cores,
                    "ram": ram,
                }

            sp["stages"].append(st)

        sp["state"] = "PENDING"

    reaction["state"] = "READY"
    if store is not None:
        store.save_reaction(reaction)
    return reaction


def build_reaction_outputs_zip(reaction: dict, store: ReactionStore = None) -> bytes:
    """Builds a ZIP archive containing all inputs, outputs, and geometries for all species in the reaction."""
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        summary = {
            "reaction_id": reaction.get("reaction_id"),
            "equation": reaction.get("equation"),
            "state": reaction.get("state"),
            "thermodynamics": reaction.get("thermodynamics"),
            "created_at": reaction.get("created_at"),
            "species_count": len(reaction.get("species", [])),
        }
        zf.writestr("reaction_summary.json", json.dumps(summary, indent=2, ensure_ascii=False))

        for sp in reaction.get("species", []):
            sp_name = re.sub(r'[^A-Za-z0-9_\-\.]', '_', sp.get("display_name") or sp.get("formula") or "species")
            if sp.get("initial_geometry"):
                zf.writestr(f"geometries/{sp_name}_initial.xyz", sp["initial_geometry"])
            if (sp.get("final_result") or {}).get("geometry_xyz"):
                zf.writestr(f"geometries/{sp_name}_optimized.xyz", sp["final_result"]["geometry_xyz"])

            for idx, st in enumerate(sp.get("stages", [])):
                st_kind = st.get("kind", "STAGE")
                if st.get("input_text"):
                    zf.writestr(f"inputs/{sp_name}_step{idx+1}_{st_kind}.inp", st["input_text"])
                if st.get("output_text"):
                    zf.writestr(f"outputs/{sp_name}_step{idx+1}_{st_kind}.out", st["output_text"])

    return buf.getvalue()
