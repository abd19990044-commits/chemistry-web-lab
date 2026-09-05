# -*- coding: utf-8 -*-
"""Canonical Scientific Data Model Normalization Engine for Chemistry Lab.

Translates ORCA output parser results, reaction definitions, calculation workflows,
and legacy editor states into normalized, versioned, machine-readable canonical records.
Guarantees lossless representation of all scientific quantities, explicit availability
metadata, ZPE vs E0 distinction, raw vs derived spectral separation, and strict idempotency.
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import math
import re
import uuid
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "orca-web-lab.1.0"
DATASET_VERSION = "2026.1"

ELEMENT_TO_Z = {
    "H": 1, "HE": 2, "LI": 3, "BE": 4, "B": 5, "C": 6, "N": 7, "O": 8, "F": 9, "NE": 10,
    "NA": 11, "MG": 12, "AL": 13, "SI": 14, "P": 15, "S": 16, "CL": 17, "AR": 18,
    "K": 19, "CA": 20, "SC": 21, "TI": 22, "V": 23, "CR": 24, "MN": 25, "FE": 26,
    "CO": 27, "NI": 28, "CU": 29, "ZN": 30, "GA": 31, "GE": 32, "AS": 33, "SE": 34,
    "BR": 35, "KR": 36, "RB": 37, "SR": 38, "Y": 39, "ZR": 40, "NB": 41, "MO": 42,
    "TC": 43, "RU": 44, "RH": 45, "PD": 46, "AG": 47, "CD": 48, "IN": 49, "SN": 50,
    "SB": 51, "TE": 52, "I": 53, "XE": 54, "CS": 55, "BA": 56, "LA": 57, "CE": 58,
    "PR": 59, "ND": 60, "PM": 61, "SM": 62, "EU": 63, "GD": 64, "TB": 65, "DY": 66,
    "HO": 67, "ER": 68, "TM": 69, "YB": 70, "LU": 71, "HF": 72, "TA": 73, "W": 74,
    "RE": 75, "OS": 76, "IR": 77, "PT": 78, "AU": 79, "HG": 80, "TL": 81, "PB": 82,
    "BI": 83, "PO": 84, "AT": 85, "RN": 86, "FR": 87, "RA": 88, "AC": 89, "TH": 90,
    "PA": 91, "U": 92, "NP": 93, "PU": 94, "AM": 95, "CM": 96, "BK": 97, "CF": 98,
    "ES": 99, "FM": 100, "MD": 101, "NO": 102, "LR": 103
}

ELEMENT_TO_MASS = {
    "H": 1.008, "HE": 4.0026, "LI": 6.94, "BE": 9.0122, "B": 10.81, "C": 12.011, "N": 14.007, "O": 15.999, "F": 18.998, "NE": 20.180,
    "NA": 22.990, "MG": 24.305, "AL": 26.982, "SI": 28.085, "P": 30.974, "S": 32.06, "CL": 35.45, "AR": 39.948,
    "K": 39.098, "CA": 40.078, "SC": 44.956, "TI": 47.867, "V": 50.942, "CR": 51.996, "MN": 54.938, "FE": 55.845,
    "CO": 58.933, "NI": 58.693, "CU": 63.546, "ZN": 65.38, "GA": 69.723, "GE": 72.630, "AS": 74.922, "SE": 78.971,
    "BR": 79.904, "KR": 83.798, "RB": 85.468, "SR": 87.62, "Y": 88.906, "ZR": 91.224, "NB": 92.906, "MO": 95.95,
    "TC": 98.0, "RU": 101.07, "RH": 102.91, "PD": 106.42, "AG": 107.87, "CD": 112.41, "IN": 114.82, "SN": 118.71,
    "SB": 121.76, "TE": 127.60, "I": 126.90, "XE": 131.29, "CS": 132.91, "BA": 137.33, "LA": 138.91, "CE": 140.12,
    "PR": 140.91, "ND": 144.24, "PM": 145.0, "SM": 150.36, "EU": 151.96, "GD": 157.25, "TB": 158.93, "DY": 162.50,
    "HO": 164.93, "ER": 167.26, "TM": 168.93, "YB": 173.05, "LU": 174.97, "HF": 178.49, "TA": 180.95, "W": 183.84,
    "RE": 186.21, "OS": 190.23, "IR": 192.22, "PT": 195.08, "AU": 196.97, "HG": 200.59, "TL": 204.38, "PB": 207.2,
    "BI": 208.98, "TH": 232.04, "PA": 231.04, "U": 238.03
}
ELEMENT_TO_MASS_BY_Z = {ELEMENT_TO_Z[k]: v for k, v in ELEMENT_TO_MASS.items() if k in ELEMENT_TO_Z}

SENSITIVE_KEY_PATTERNS = [
    re.compile(r"kaggle.*(?:key|token|secret|auth)", re.IGNORECASE),
    re.compile(r"cloudflare.*(?:key|token|secret|auth)", re.IGNORECASE),
    re.compile(r"(?:api[_-]?key|password|private[_-]?key|access[_-]?token|bearer[_-]?token)", re.IGNORECASE),
    re.compile(r"cookie", re.IGNORECASE),
]

KNOWN_LEGACY_KEYS = {
    "name", "title", "coords", "xyz_structure", "opt_coords", "coordinates",
    "formula", "chemical_formula", "smiles", "canonical_smiles", "isomeric_smiles",
    "inchi", "inchikey", "charge", "multiplicity", "atoms", "fragments", "bonds",
    "reactants", "products", "equation", "balance", "reaction_smiles", "conditions",
    "temperature_k", "pressure_atm", "solvent", "catalyst", "standard_state",
    "calc_type", "calculation_type", "method", "basis", "basis_set", "dispersion",
    "solvation", "status", "workflow", "isDualWorkflow", "isWorkflow", "steps",
    "workflow_name", "e_elec_eh", "electronic_energy", "energy", "zpe_eh", "zpe",
    "zero_point_energy", "zero_point_energy_hartree", "zpe_hartree", "electronic_zpe_eh",
    "zero_point_corrected_energy", "total_enthalpy_eh", "enthalpy", "enthalpy_hartree",
    "final_enthalpy_hartree", "gibbs_free_energy_eh", "gibbs", "gibbs_free_energy_hartree",
    "final_gibbs_hartree", "gibbs_free_energy", "total_entropy_cal_mol_k", "entropy",
    "entropy_cal_mol_k", "thermal_correction_eh", "entropy_term_ts_eh", "entropy_term_eh",
    "entropy_correction_minus_ts_eh", "entropy_correction_eh", "k_eq",
    "equilibrium_constant", "stationary_point_status", "sp_source", "freq_source",
    "opt_source", "zpe_source", "thermal_correction_source", "entropy_source",
    "spName", "primaryName", "primaryMode", "spMode", "content", "sp_content",
    "homo_ev", "lumo_ev", "homo_lumo_gap_ev", "alpha_homo_ev", "alpha_lumo_ev",
    "beta_homo_ev", "beta_lumo_ev", "dipole_moment_debye", "s2_actual", "s2_ideal",
    "spin_contamination", "electronegativity_ev", "chemical_hardness_ev",
    "chemical_potential_ev", "chemical_softness_ev", "electrophilicity_index_ev",
    "electrodonating_power_ev", "electroaccepting_power_ev", "net_electrophilicity_ev",
    "ionization_potential_ev", "electron_affinity_ev", "ir_spectrum",
    "vibrational_frequencies_cm", "vibrational_frequencies", "ir_frequencies_cm",
    "ir_intensities_km_mol", "convoluted_ir_spectrum", "ir_peak_assignments",
    "activeIRAssignments", "assignments", "experimental_ir_spectra",
    "loadedExperimentalIRSpectra", "tddft_cm", "tddft_fosc", "tddft_transitions",
    "tddft_states", "transitions", "uvvis_spectrum", "loadedExperimentalSpectra",
    "nmr", "nmr_result", "training_metadata", "data_quality", "source_type",
    "filename", "source_filename", "jobId", "job_id", "source_job_id",
    "source_result_manifest", "result_sha256", "source_url", "application_version",
    "orca_version", "ORCA_version", "parser_version", "git_sha", "generated_by",
    "imported_from", "record_id", "created_at", "updated_at", "schema_version",
    "dataset_version", "record_type", "content_hash", "split_group_key",
    "analysis_results", "molecular_system", "reaction", "geometries", "calculations",
    "workflows", "thermochemistry", "electronic_structure", "population_analysis",
    "vibrational_spectroscopy", "electronic_spectroscopy", "nmr_spectroscopy",
    "properties", "spectroscopy", "migration_report", "raw_text", "raw_object",
    "reaction_thermochemistry", "delta_g_kcal_mol", "delta_h_kcal_mol", "delta_e_kcal_mol",
    "imaginary_frequencies_count", "imaginary_frequencies_cm", "is_transition_state",
    "terminated_normally", "termination_message", "had_error_termination", "error_count",
    "thermochemistry_reliability", "atoms_count", "coords_unit", "hirshfeld_charges",
    "mulliken_charges", "mayer_charges", "mayer_valences", "loewdin_charges",
    "atomic_charges", "elements", "xyz", "latest_job", "jobs", "sources", "molecules",
    "levels_of_theory", "source_artifacts", "input_source", "output_source", "job",
    "targets", "derived_features", "canonical_record", "canonical_dataset"
}


def is_canonical_record(data: Any) -> bool:
    """Check if data is already a valid canonical scientific record."""
    if not isinstance(data, dict):
        return False
    return (
        data.get("schema_version") == SCHEMA_VERSION
        and "record_id" in data
        and "record_type" in data
        and ("molecular_system" in data or "reaction" in data or "analysis_results" in data or "geometries" in data or "thermochemistry" in data)
    )


def sanitize_secrets(obj: Any) -> tuple[Any, list[str]]:
    """Recursively strip sensitive credential tokens from data dictionaries.
    
    Returns (sanitized_object, list_of_stripped_keys).
    """
    stripped_keys: list[str] = []

    def _sanitize(item: Any) -> Any:
        if isinstance(item, dict):
            cleaned = {}
            for k, v in item.items():
                k_str = str(k)
                if any(p.search(k_str) for p in SENSITIVE_KEY_PATTERNS):
                    stripped_keys.append(k_str)
                    continue
                cleaned[k] = _sanitize(v)
            return cleaned
        elif isinstance(item, list):
            return [_sanitize(x) for x in item]
        elif isinstance(item, tuple):
            return tuple(_sanitize(x) for x in item)
        return item

    clean_obj = _sanitize(obj)
    return clean_obj, sorted(list(set(stripped_keys)))


def parse_xyz_string(xyz_str: str) -> list[dict[str, Any]]:
    """Parse raw XYZ coordinate string into atom list."""
    if not xyz_str or not isinstance(xyz_str, str):
        return []
    lines = [line.strip() for line in xyz_str.strip().splitlines() if line.strip()]
    if not lines:
        return []

    start_idx = 0
    if len(lines) > 2 and lines[0].isdigit():
        start_idx = 2

    atoms: list[dict[str, Any]] = []
    for line in lines[start_idx:]:
        parts = line.split()
        if len(parts) >= 4:
            elem = parts[0].capitalize()
            try:
                x = float(parts[1])
                y = float(parts[2])
                z = float(parts[3])
                atoms.append({
                    "element": elem,
                    "x": x,
                    "y": y,
                    "z": z,
                })
            except ValueError:
                continue
    return atoms


def compute_geometry_hash(coords: list[dict[str, Any]]) -> str | None:
    """Compute deterministic SHA-256 fingerprint of Cartesian coordinate table.
    
    Coordinates are serialized with 6 decimal places and sorted to provide
    a stable frame fingerprint across calculation steps.
    """
    if not coords:
        return None
    atom_strings = []
    for a in coords:
        elem = a.get("element", "").upper()
        x = float(a.get("x", 0.0))
        y = float(a.get("y", 0.0))
        z = float(a.get("z", 0.0))
        atom_strings.append(f"{elem}:{x:.6f}:{y:.6f}:{z:.6f}")
    atom_strings.sort()
    return hashlib.sha256("".join(atom_strings).encode("utf-8")).hexdigest()


def compute_chemical_formula(elements: Sequence[str]) -> str:
    """Compute standard Hill system chemical formula from element list."""
    if not elements:
        return "-"
    from collections import Counter
    counts = Counter(el.capitalize() for el in elements if el and el != "Da" and not el.endswith(":"))
    if not counts:
        return "-"
    order = []
    if "C" in counts:
        order.append("C")
        if "H" in counts:
            order.append("H")
    for el in sorted(counts.keys()):
        if el not in order:
            order.append(el)
    parts = []
    for el in order:
        c = counts[el]
        parts.append(f"{el}{c if c > 1 else ''}")
    return "".join(parts)


def compute_content_hash(record_dict: dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 fingerprint of scientific content.
    
    Excludes volatile metadata, timestamps, randomized ID references, and migration logs.
    Normalizes floats to 8 decimal places for cross-platform hashing determinism
    without altering the stored full-precision scientific values.
    """
    excluded_keys = {
        "record_id",
        "content_hash",
        "created_at",
        "updated_at",
        "migration_report",
        "system_id",
        "geometry_id",
        "species_id",
        "reaction_id",
        "calculation_id",
        "workflow_id",
        "analysis_record_id",
        "source_calculation_id",
        "species_ref",
        "geometry_refs",
        "input_geometry_id",
        "output_geometry_id",
        "split_group_key",
    }
    
    def _clean_for_hash(val: Any) -> Any:
        if isinstance(val, dict):
            return {
                k: _clean_for_hash(v)
                for k, v in sorted(val.items())
                if k not in excluded_keys and v is not None
            }
        elif isinstance(val, list):
            return [_clean_for_hash(item) for item in val]
        elif isinstance(val, float):
            return round(val, 8)
        return val

    hashable_payload = _clean_for_hash(record_dict)
    canonical_json = json.dumps(hashable_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def compute_deterministic_calculation_id(calc_dict: dict[str, Any], geometry_hash: str | None = None) -> str:
    """Compute a deterministic, reproducible calculation ID from scientific parameters.
    
    Distinguishes:
    - geometry / coordinates fingerprint (geometry_hash)
    - chemical formula and elements
    - charge and spin multiplicity
    - quantum chemical method and DFT functional
    - basis set and auxiliary basis set
    - dispersion correction and solvation model
    - relativistic treatment
    - calculation type (OPT, FREQ, NUMFREQ, SP, TDDFT)
    - temperature and pressure
    - ORCA major/minor engine version
    - workflow stage
    """
    elements = calc_dict.get("elements") or []
    if isinstance(elements, list):
        elem_str = ",".join(str(e).upper() for e in elements)
    else:
        elem_str = str(elements).upper()

    identity_components = [
        f"geo={geometry_hash or 'no_geom'}",
        f"formula={calc_dict.get('formula') or '-'}",
        f"elements={elem_str}",
        f"charge={int(calc_dict.get('charge', 0)) if calc_dict.get('charge') is not None else 0}",
        f"mult={int(calc_dict.get('multiplicity', 1)) if calc_dict.get('multiplicity') is not None else 1}",
        f"method={str(calc_dict.get('method') or '').strip().upper()}",
        f"functional={str(calc_dict.get('dft_functional') or calc_dict.get('functional') or '').strip().upper()}",
        f"basis={str(calc_dict.get('basis_set') or calc_dict.get('basis') or '').strip().upper()}",
        f"aux_basis={str(calc_dict.get('auxiliary_basis') or '').strip().upper()}",
        f"dispersion={str(calc_dict.get('dispersion') or '').strip().upper()}",
        f"solvation={str(calc_dict.get('solvation') or '').strip().upper()}",
        f"relativistic={str(calc_dict.get('relativistic') or '').strip().upper()}",
        f"calc_type={str(calc_dict.get('calc_type') or calc_dict.get('calculation_type') or 'SP').strip().upper()}",
        f"temp={(float(calc_dict['temperature_k']) if calc_dict.get('temperature_k') is not None else 298.15):.2f}",
        f"press={(float(calc_dict['pressure_atm']) if calc_dict.get('pressure_atm') is not None else 1.0):.4f}",
        f"orca_ver={str(calc_dict.get('orca_version') or '6.1').strip()}",
        f"stage={str(calc_dict.get('stage') or calc_dict.get('step_name') or '1').strip()}",
    ]
    raw_sig = "|".join(identity_components)
    digest = hashlib.sha256(raw_sig.encode("utf-8")).hexdigest()
    return f"calc_{digest[:16]}"


def compute_split_group_key(clean_input: dict[str, Any], geometry_hash: str | None = None) -> str:
    """Compute a deterministic, scientifically grounded split_group_key for ML dataset segregation.
    
    Priority:
    1. InChIKey (exact molecular connectivity and stereochemistry)
    2. Canonical SMILES (standardized 2D graph)
    3. Reaction SMILES / equation (for reaction datasets)
    4. Formula + geometry hash (for quantum structures without SMILES)
    """
    inchikey = clean_input.get("inchikey") or clean_input.get("inchi_key")
    if inchikey and isinstance(inchikey, str) and len(inchikey.strip()) == 27:
        return inchikey.strip()
        
    smiles = clean_input.get("smiles") or clean_input.get("canonical_smiles")
    if smiles and isinstance(smiles, str) and smiles.strip():
        return smiles.strip()
        
    rxn_smi = clean_input.get("reaction_smiles") or clean_input.get("equation")
    if rxn_smi and isinstance(rxn_smi, str) and rxn_smi.strip():
        return rxn_smi.strip()
        
    formula = clean_input.get("formula") or clean_input.get("chemical_formula")
    if formula and isinstance(formula, str) and formula.strip() and formula.strip() != "-":
        if geometry_hash:
            return f"grp_{formula.strip()}_{geometry_hash[:12]}"
        return f"grp_{formula.strip()}"
        
    if geometry_hash:
        return f"grp_geom_{geometry_hash[:12]}"
        
    name = clean_input.get("name") or clean_input.get("title") or "sample"
    return str(name)


def extract_molecular_graph(
    clean_input: dict[str, Any],
    raw_atoms: list[dict[str, Any]],
    smiles: str | None = None,
    mol_block: str | None = None
) -> dict[str, Any]:
    """Generate GNN-compatible molecular graph representation with explicit graph_source.
    
    Includes node features, edge lists, adjacency matrix, bond orders, formal charges,
    and aromaticity flags without inventing arbitrary bonds.
    """
    nodes = []
    for idx, a in enumerate(raw_atoms):
        elem = a.get("element", "C")
        z = a.get("atomic_number") or ELEMENT_TO_Z.get(elem.upper(), 6)
        nodes.append({
            "atom_index": idx,
            "element": elem,
            "atomic_number": z,
            "formal_charge": int(a.get("formal_charge", 0)),
            "is_aromatic": bool(a.get("aromatic", False)),
        })
    
    num_nodes = len(nodes)
    edges: list[dict[str, Any]] = []
    adj_matrix = [[0] * num_nodes for _ in range(num_nodes)]
    graph_source = "unavailable"

    rdkit_mol = None
    if smiles:
        try:
            from rdkit import Chem
            rdkit_mol = Chem.MolFromSmiles(smiles)
            if rdkit_mol and rdkit_mol.GetNumAtoms() == num_nodes:
                graph_source = "rdkit"
            else:
                rdkit_mol = None
        except Exception:
            rdkit_mol = None

    if not rdkit_mol and mol_block:
        try:
            from rdkit import Chem
            rdkit_mol = Chem.MolFromMolBlock(mol_block, removeHs=False)
            if rdkit_mol and rdkit_mol.GetNumAtoms() == num_nodes:
                graph_source = "rdkit"
            else:
                rdkit_mol = None
        except Exception:
            rdkit_mol = None

    if rdkit_mol and graph_source == "rdkit":
        for b in rdkit_mol.GetBonds():
            u = b.GetBeginAtomIdx()
            v = b.GetEndAtomIdx()
            btype = str(b.GetBondType())
            border = float(b.GetBondTypeAsDouble())
            is_arom = bool(b.GetIsAromatic())
            edges.append({
                "source": u,
                "target": v,
                "bond_type": btype,
                "bond_order": border,
                "is_aromatic": is_arom,
            })
            if u < num_nodes and v < num_nodes:
                adj_matrix[u][v] = 1
                adj_matrix[v][u] = 1
    elif isinstance(clean_input.get("bonds"), list) and clean_input["bonds"]:
        for b in clean_input["bonds"]:
            if isinstance(b, dict):
                u = int(b.get("atom_i", b.get("atom1", b.get("source", 0))))
                v = int(b.get("atom_j", b.get("atom2", b.get("target", 0))))
                order = float(b.get("order", b.get("bond_order", 1.0)))
                edges.append({
                    "source": u,
                    "target": v,
                    "bond_type": b.get("type", "SINGLE"),
                    "bond_order": order,
                    "is_aromatic": bool(b.get("aromatic", False)),
                })
                if u < num_nodes and v < num_nodes:
                    adj_matrix[u][v] = 1
                    adj_matrix[v][u] = 1
        if edges:
            graph_source = "parser"

    return {
        "graph_source": graph_source,
        "nodes": nodes,
        "edges": edges,
        "adjacency_matrix": adj_matrix if graph_source != "unavailable" else None,
        "num_nodes": num_nodes,
        "num_edges": len(edges),
    }


def check_duplicate_status(
    record_dict: dict[str, Any],
    existing_registry: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Perform exact-content, scientific-content, and calculation-identity duplicate detection.
    
    Returns structured duplicate status:
    - exact_duplicate: identical content_hash
    - same_calculation: identical calculation_id
    - distinct: independent calculation
    """
    if not existing_registry:
        return {
            "is_duplicate": False,
            "duplicate_type": None,
            "duplicate_of": None,
            "distinction_reason": "First instance in registry"
        }
    
    c_hash = record_dict.get("content_hash")
    calc_id = record_dict.get("calculation_id")
    
    if c_hash and c_hash in existing_registry.get("by_content_hash", {}):
        orig_id = existing_registry["by_content_hash"][c_hash]
        return {
            "is_duplicate": True,
            "duplicate_type": "EXACT_SCIENTIFIC_CONTENT",
            "duplicate_of": orig_id,
            "distinction_reason": f"Matches content hash {c_hash}"
        }
        
    if calc_id and calc_id in existing_registry.get("by_calc_id", {}):
        orig_id = existing_registry["by_calc_id"][calc_id]
        return {
            "is_duplicate": True,
            "duplicate_type": "SAME_CALCULATION_PARAMETERS",
            "duplicate_of": orig_id,
            "distinction_reason": f"Matches calculation identity {calc_id}"
        }
        
    return {
        "is_duplicate": False,
        "duplicate_type": None,
        "duplicate_of": None,
        "distinction_reason": "Unique scientific parameters and content hash"
    }


def normalize_to_canonical_schema(
    raw_data: Mapping[str, Any] | str | Any,
    record_type: str | None = None,
    provenance_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Transform arbitrary ORCA Web Lab data models into the Canonical Scientific Record."""
    record, _ = normalize_with_report(raw_data, record_type=record_type, provenance_override=provenance_override)
    return record


def normalize_with_report(
    raw_data: Mapping[str, Any] | str | Any,
    record_type: str | None = None,
    provenance_override: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Transform data into Canonical Record and return detailed migration report.
    
    Guarantees strict idempotency on already canonical records and lossless preservation
    of all scientific quantities from real ORCA analyzer JSON records.
    """
    # 0. Check if already a canonical scientific record
    if isinstance(raw_data, Mapping) and is_canonical_record(raw_data):
        canonical = copy.deepcopy(dict(raw_data))
        canonical, stripped_secrets = sanitize_secrets(canonical)
        
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        canonical["updated_at"] = now_iso
        if provenance_override:
            canonical["provenance"] = {**canonical.get("provenance", {}), **provenance_override}
            
        # Re-compute content hash to ensure integrity
        canonical["content_hash"] = compute_content_hash(canonical)
        
        report = {
            "mapped_fields": ["canonical_record_preserved"],
            "transformed_fields": [],
            "omitted_fields": stripped_secrets,
            "missing_fields": [],
            "warnings": [],
            "unsupported_fields": []
        }
        canonical["migration_report"] = report
        return canonical, report

    # 1. Ingest parsed Python objects (MoleculeData / JobData) or nested bundle
    clean_input: dict[str, Any] = {}
    if hasattr(raw_data, "name") and hasattr(raw_data, "jobs"):
        mol_data = raw_data
        job = mol_data.jobs[-1] if mol_data.jobs else None
        clean_input = {
            "name": mol_data.name,
            "filename": mol_data.sources[0] if mol_data.sources else f"{mol_data.name}.out",
            "sources": mol_data.sources if hasattr(mol_data, "sources") else [],
            "e_elec_eh": job.e_elec_eh if job else None,
            "zpe_eh": job.zpe_eh if job else None,
            "electronic_zpe_eh": job.electronic_zpe_eh() if job and hasattr(job, "electronic_zpe_eh") else None,
            "total_enthalpy_eh": job.total_enthalpy_eh if job else None,
            "gibbs_free_energy_eh": job.gibbs_free_energy_eh if job else None,
            "total_entropy_cal_mol_k": job.total_entropy_cal_mol_k if job else None,
            "entropy_term_ts_eh": getattr(job, "entropy_term_eh", None) if job else None,
            "entropy_correction_minus_ts_eh": getattr(job, "entropy_correction_eh", None) if job else None,
            "homo_ev": job.homo_ev if job else None,
            "lumo_ev": job.lumo_ev if job else None,
            "homo_lumo_gap_ev": job.homo_lumo_gap_ev if job else None,
            "alpha_homo_ev": getattr(job, "alpha_homo_ev", None) if job else None,
            "alpha_lumo_ev": getattr(job, "alpha_lumo_ev", None) if job else None,
            "beta_homo_ev": getattr(job, "beta_homo_ev", None) if job else None,
            "beta_lumo_ev": getattr(job, "beta_lumo_ev", None) if job else None,
            "dipole_moment_debye": job.dipole_moment_debye if job else None,
            "coords": [{"elem": el, "x": c[0], "y": c[1], "z": c[2]} for el, c in zip(job.elements, job.coords)] if job and job.elements and job.coords else [],
            "elements": job.elements if job else [],
            "mulliken_charges": getattr(job, "mulliken_charges", None) if job else None,
            "loewdin_charges": getattr(job, "loewdin_charges", None) if job else None,
            "hirshfeld_charges": getattr(job, "hirshfeld_charges", None) if job else None,
            "mayer_charges": getattr(job, "mayer_charges", None) if job else None,
            "mayer_valences": getattr(job, "mayer_valences", None) if job else None,
            "atomic_charges": getattr(job, "atomic_charges", None) if job else None,
            "ir_spectrum": job.ir_spectrum if job else [],
            "vibrational_frequencies_cm": job.vibrational_frequencies_cm if job else [],
            "ir_intensities_km_mol": job.ir_intensities_km_mol if job else [],
            "imaginary_frequencies_cm": getattr(job, "imaginary_frequencies_cm", []) if job else [],
            "imaginary_frequencies_count": getattr(job, "imaginary_frequencies_count", 0) if job else 0,
            "is_transition_state": getattr(job, "is_transition_state", False) if job else False,
            "tddft_cm": job.tddft_cm if job else [],
            "tddft_fosc": job.tddft_fosc if job else [],
            "nmr_result": getattr(job, "nmr_result", None) if job else None,
            "orca_version": job.metadata.orca_version if job and job.metadata else "6.1.0",
            "method": job.metadata.method if job and job.metadata else "DFT",
            "basis_set": job.metadata.basis_set if job and job.metadata else "def2-SVP",
            "dispersion": job.metadata.dispersion if job and job.metadata else "None",
            "solvation": job.metadata.solvation if job and job.metadata else "None",
            "solvent": job.metadata.solvent if job and job.metadata else "None",
            "charge": job.metadata.charge if job and job.metadata else 0,
            "multiplicity": job.metadata.multiplicity if job and job.metadata else 1,
            "temperature_k": job.metadata.temperature_k if job and job.metadata else 298.15,
            "pressure_atm": job.metadata.pressure_atm if job and job.metadata else 1.0,
            "source_type": "orca_analyzer",
            "terminated_normally": getattr(job, "terminated_normally", True) if job else True,
            "termination_message": getattr(job, "termination_message", None) if job else None,
            "had_error_termination": getattr(job, "had_error_termination", False) if job else False,
            "error_count": len(getattr(job, "error_messages", [])) if job else 0,
            "stationary_point_status": getattr(job, "stationary_point_status", "MINIMUM") if job else "MINIMUM",
            "thermochemistry_reliability": getattr(job, "thermochemistry_reliability", "RELIABLE") if job else "RELIABLE",
            "chemical_hardness_ev": getattr(job, "chemical_hardness_ev", None) if job else None,
            "chemical_potential_ev": getattr(job, "chemical_potential_ev", None) if job else None,
            "chemical_softness_ev": getattr(job, "chemical_softness_ev", None) if job else None,
            "electronegativity_ev": getattr(job, "electronegativity_ev", None) if job else None,
            "electrophilicity_index_ev": getattr(job, "electrophilicity_index_ev", None) if job else None,
            "electrodonating_power_ev": getattr(job, "electrodonating_power_ev", None) if job else None,
            "electroaccepting_power_ev": getattr(job, "electroaccepting_power_ev", None) if job else None,
            "net_electrophilicity_ev": getattr(job, "net_electrophilicity_ev", None) if job else None,
            "ionization_potential_ev": getattr(job, "ionization_potential_ev", None) if job else None,
            "electron_affinity_ev": getattr(job, "electron_affinity_ev", None) if job else None,
            "s2_actual": getattr(job, "s2_actual", None) if job else None,
            "s2_ideal": getattr(job, "s2_ideal", None) if job else None,
            "spin_contamination": getattr(job, "spin_contamination", None) if job else None,
        }
    elif hasattr(raw_data, "metadata") and hasattr(raw_data, "e_elec_eh"):
        job = raw_data
        clean_input = {
            "name": "orca_job",
            "e_elec_eh": job.e_elec_eh,
            "zpe_eh": job.zpe_eh,
            "electronic_zpe_eh": job.electronic_zpe_eh() if hasattr(job, "electronic_zpe_eh") else None,
            "total_enthalpy_eh": job.total_enthalpy_eh,
            "gibbs_free_energy_eh": job.gibbs_free_energy_eh,
            "total_entropy_cal_mol_k": job.total_entropy_cal_mol_k,
            "entropy_term_ts_eh": getattr(job, "entropy_term_eh", None),
            "entropy_correction_minus_ts_eh": getattr(job, "entropy_correction_eh", None),
            "homo_ev": job.homo_ev,
            "lumo_ev": job.lumo_ev,
            "homo_lumo_gap_ev": job.homo_lumo_gap_ev,
            "alpha_homo_ev": getattr(job, "alpha_homo_ev", None),
            "alpha_lumo_ev": getattr(job, "alpha_lumo_ev", None),
            "beta_homo_ev": getattr(job, "beta_homo_ev", None),
            "beta_lumo_ev": getattr(job, "beta_lumo_ev", None),
            "dipole_moment_debye": job.dipole_moment_debye,
            "coords": [{"elem": el, "x": c[0], "y": c[1], "z": c[2]} for el, c in zip(job.elements, job.coords)] if job.elements and job.coords else [],
            "elements": job.elements,
            "mulliken_charges": getattr(job, "mulliken_charges", None),
            "loewdin_charges": getattr(job, "loewdin_charges", None),
            "hirshfeld_charges": getattr(job, "hirshfeld_charges", None),
            "mayer_charges": getattr(job, "mayer_charges", None),
            "mayer_valences": getattr(job, "mayer_valences", None),
            "atomic_charges": getattr(job, "atomic_charges", None),
            "ir_spectrum": job.ir_spectrum,
            "vibrational_frequencies_cm": job.vibrational_frequencies_cm,
            "ir_intensities_km_mol": job.ir_intensities_km_mol,
            "imaginary_frequencies_cm": getattr(job, "imaginary_frequencies_cm", []),
            "imaginary_frequencies_count": getattr(job, "imaginary_frequencies_count", 0),
            "is_transition_state": getattr(job, "is_transition_state", False),
            "tddft_cm": job.tddft_cm,
            "tddft_fosc": job.tddft_fosc,
            "nmr_result": getattr(job, "nmr_result", None),
            "orca_version": job.metadata.orca_version if job.metadata else "6.1.0",
            "method": job.metadata.method if job.metadata else "DFT",
            "basis_set": job.metadata.basis_set if job.metadata else "def2-SVP",
            "dispersion": job.metadata.dispersion if job.metadata else "None",
            "solvation": job.metadata.solvation if job.metadata else "None",
            "solvent": job.metadata.solvent if job.metadata else "None",
            "charge": job.metadata.charge if job.metadata else 0,
            "multiplicity": job.metadata.multiplicity if job.metadata else 1,
            "temperature_k": job.metadata.temperature_k if job.metadata else 298.15,
            "pressure_atm": job.metadata.pressure_atm if job.metadata else 1.0,
            "source_type": "orca_analyzer",
            "terminated_normally": getattr(job, "terminated_normally", True),
            "termination_message": getattr(job, "termination_message", None),
            "had_error_termination": getattr(job, "had_error_termination", False),
            "error_count": len(getattr(job, "error_messages", [])),
            "stationary_point_status": getattr(job, "stationary_point_status", "MINIMUM"),
            "thermochemistry_reliability": getattr(job, "thermochemistry_reliability", "RELIABLE"),
            "chemical_hardness_ev": getattr(job, "chemical_hardness_ev", None),
            "chemical_potential_ev": getattr(job, "chemical_potential_ev", None),
            "chemical_softness_ev": getattr(job, "chemical_softness_ev", None),
            "electronegativity_ev": getattr(job, "electronegativity_ev", None),
            "electrophilicity_index_ev": getattr(job, "electrophilicity_index_ev", None),
            "electrodonating_power_ev": getattr(job, "electrodonating_power_ev", None),
            "electroaccepting_power_ev": getattr(job, "electroaccepting_power_ev", None),
            "net_electrophilicity_ev": getattr(job, "net_electrophilicity_ev", None),
            "ionization_potential_ev": getattr(job, "ionization_potential_ev", None),
            "electron_affinity_ev": getattr(job, "electron_affinity_ev", None),
            "s2_actual": getattr(job, "s2_actual", None),
            "s2_ideal": getattr(job, "s2_ideal", None),
            "spin_contamination": getattr(job, "spin_contamination", None),
        }
    elif isinstance(raw_data, str):
        try:
            clean_input = json.loads(raw_data)
        except Exception:
            clean_input = {"raw_text": raw_data}
    elif isinstance(raw_data, Mapping):
        # Check if it's a multi-molecule dictionary from ORCA_Parsed_Data.json
        if "molecules" in raw_data and isinstance(raw_data["molecules"], dict) and len(raw_data["molecules"]) > 0:
            first_key = str(next(iter(raw_data["molecules"])))
            mol_item = raw_data["molecules"][first_key]
            if isinstance(mol_item, dict) and "jobs" in mol_item and len(mol_item["jobs"]) > 0:
                latest_job = mol_item["jobs"][-1]
                clean_input = {
                    "name": first_key,
                    "filename": mol_item.get("filename") or (mol_item.get("sources", [None])[0] if isinstance(mol_item.get("sources"), list) and mol_item.get("sources") else None),
                    "job_id": mol_item.get("job_id") or raw_data.get("job_id"),
                    "available_files": mol_item.get("available_files") or raw_data.get("available_files", []),
                    "sources": mol_item.get("sources", []),
                    "had_error_termination": mol_item.get("had_error_termination", False),
                    **latest_job
                }
            else:
                clean_input = dict(raw_data)
        elif "latest_job" in raw_data and isinstance(raw_data["latest_job"], dict):
            raw_mol = raw_data.get("molecule")
            mol_name = raw_mol.get("name") if isinstance(raw_mol, dict) else (raw_mol if isinstance(raw_mol, str) else None)
            clean_input = {
                "name": raw_data.get("name") if isinstance(raw_data.get("name"), str) else (mol_name or "orca_job"),
                "filename": raw_data.get("filename"),
                "job_id": raw_data.get("job_id"),
                "available_files": raw_data.get("available_files", []),
                "sources": raw_data.get("sources", []),
                **raw_data["latest_job"]
            }
        elif "jobs" in raw_data and isinstance(raw_data["jobs"], list) and len(raw_data["jobs"]) > 0:
            latest_job = raw_data["jobs"][-1]
            if isinstance(latest_job, dict):
                raw_mol = raw_data.get("molecule")
                mol_name = raw_mol.get("name") if isinstance(raw_mol, dict) else (raw_mol if isinstance(raw_mol, str) else None)
                clean_input = {
                    "name": raw_data.get("name") if isinstance(raw_data.get("name"), str) else (mol_name or latest_job.get("molecule") if isinstance(latest_job.get("molecule"), str) else "orca_job"),
                    "filename": raw_data.get("filename"),
                    "job_id": raw_data.get("job_id"),
                    "available_files": raw_data.get("available_files", []),
                    "sources": raw_data.get("sources", []),
                    **latest_job
                }
            else:
                clean_input = dict(raw_data)
        else:
            clean_input = dict(raw_data)
    else:
        clean_input = {"raw_object": str(raw_data)}

    clean_input, stripped_secrets = sanitize_secrets(clean_input)
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    mapped_fields: list[str] = []
    transformed_fields: list[str] = []
    omitted_fields: list[str] = list(stripped_secrets)
    missing_fields: list[str] = []
    warnings: list[str] = []
    unsupported_fields: list[str] = []

    KNOWN_UI_KEYS = {"viewport", "button_state", "css_class", "button_color", "dom_selector", "canvas_x", "zoom", "pan", "selection", "canvas_view", "ui_state", "highlighted"}
    # Detect unmapped/unsupported keys
    for k in clean_input.keys():
        k_str = str(k)
        if k_str in KNOWN_UI_KEYS:
            omitted_fields.append(k_str)
        elif k not in KNOWN_LEGACY_KEYS and k not in stripped_secrets:
            unsupported_fields.append(k_str)
            warnings.append(f"Unmapped property '{k}' was not part of the standard schema and was preserved in migration report.")

    # 2. Determine Record Type (Domain Entity Separation)
    if not record_type:
        src_type_hint = clean_input.get("source_type") or ""
        if src_type_hint in ("orca_analyzer", "orca_output_parser") or "e_elec_eh" in clean_input or "latest_job" in clean_input or "homo_ev" in clean_input:
            record_type = "analysis_record"
        elif ("reactants" in clean_input or (isinstance(clean_input.get("reaction"), dict) and "reactants" in clean_input["reaction"])) and not clean_input.get("delta_g_kcal_mol") and not clean_input.get("thermochemistry"):
            record_type = "reaction_definition"
        elif "workflows" in clean_input or clean_input.get("isDualWorkflow") or clean_input.get("isWorkflow"):
            record_type = "workflow_record"
        elif "fragments" in clean_input or (isinstance(clean_input.get("atoms"), list) and len(clean_input.get("atoms", [])) > 0 and "molId" in clean_input["atoms"][0]):
            record_type = "multi_fragment_system"
        elif "spectroscopy" in clean_input or "peak_assignments" in clean_input:
            record_type = "spectroscopy_dataset"
        elif "reaction_thermochemistry" in clean_input or "delta_g_kcal_mol" in clean_input:
            record_type = "thermochemistry_dataset"
        else:
            record_type = "analysis_record" if "energy" in clean_input or "e_elec_eh" in clean_input else "molecule"

    # Pre-parse coordinates for deterministic identity computation
    raw_coords_str = ""
    for candidate in [
        clean_input.get("xyz_structure"),
        clean_input.get("xyz"),
        clean_input.get("opt_coords"),
        clean_input.get("coordinates"),
        clean_input.get("coords"),
    ]:
        if isinstance(candidate, str) and candidate.strip():
            raw_coords_str = candidate
            break

    parsed_coords = parse_xyz_string(raw_coords_str) if raw_coords_str else []
    elements_input = clean_input.get("elements")
    coords_input = clean_input.get("coords") or clean_input.get("coordinates")
    if not parsed_coords and isinstance(coords_input, list):
        for idx, item in enumerate(coords_input):
            if isinstance(item, (list, tuple)) and len(item) >= 3:
                elem_val = "C"
                if isinstance(elements_input, list) and idx < len(elements_input):
                    elem_val = str(elements_input[idx])
                parsed_coords.append({"element": elem_val.capitalize(), "x": float(item[0]), "y": float(item[1]), "z": float(item[2])})
                transformed_fields.append("coords_tuple_to_object")
            elif isinstance(item, dict) and "x" in item and "y" in item and "z" in item:
                elem_val = item.get("elem") or item.get("element")
                if not elem_val and isinstance(elements_input, list) and idx < len(elements_input):
                    elem_val = str(elements_input[idx])
                elem_val = (elem_val or "C").capitalize()
                parsed_coords.append({
                    "element": elem_val,
                    "x": float(item["x"]),
                    "y": float(item["y"]),
                    "z": float(item["z"])
                })
                mapped_fields.append("coords_dict")

    # If coordinates exist with elements array
    if not parsed_coords and isinstance(clean_input.get("elements"), list) and isinstance(clean_input.get("coords"), list):
        for el, pt in zip(clean_input["elements"], clean_input["coords"], strict=False):
            if isinstance(pt, (list, tuple)) and len(pt) >= 3:
                parsed_coords.append({"element": el.capitalize(), "x": float(pt[0]), "y": float(pt[1]), "z": float(pt[2])})
                mapped_fields.append("elements_coords_parallel")

    geo_hash = compute_geometry_hash(parsed_coords) if parsed_coords else None
    calc_id = clean_input.get("calculation_id") or compute_deterministic_calculation_id(clean_input, geo_hash)
    id_tag = calc_id.replace("calc_", "")[:8]

    record_id = clean_input.get("record_id") or f"rec_{id_tag}"
    raw_rec_name = clean_input.get("name") or clean_input.get("title")
    if isinstance(raw_rec_name, str) and raw_rec_name.strip():
        record_name = raw_rec_name.strip()
    elif isinstance(raw_rec_name, dict):
        record_name = str(raw_rec_name.get("name") or f"Record_{id_tag}")
    else:
        record_name = f"Record_{id_tag}"

    # 3. Build Provenance & Source Artifacts
    default_src = "orca_analyzer" if record_type == "analysis_record" else ("reaction_definition" if record_type == "reaction_definition" else "user_defined")
    
    # Source artifacts list (references rather than embedding huge binaries)
    source_artifacts: list[dict[str, Any]] = []
    raw_sources = clean_input.get("sources") or ([clean_input["filename"]] if clean_input.get("filename") else [])
    if isinstance(raw_sources, list):
        for idx, src in enumerate(raw_sources):
            if isinstance(src, str) and src.strip():
                filename = src.replace("\\", "/").split("/")[-1]
                source_artifacts.append({
                    "artifact_id": f"art_{id_tag}_{idx + 1}",
                    "filename": filename,
                    "artifact_type": "orca_output" if filename.endswith((".out", ".log", ".txt")) else ("orca_input" if filename.endswith(".inp") else "generic_data"),
                    "storage_reference": src,
                    "sha256": None,
                    "size_bytes": None,
                })
        if source_artifacts:
            mapped_fields.append("source_artifacts")

    src_filename = clean_input.get("filename") or clean_input.get("source_filename")
    if not src_filename and source_artifacts:
        src_filename = source_artifacts[0]["filename"]

    prov: dict[str, Any] = {
        "source_type": clean_input.get("source_type") or default_src,
        "source_filename": src_filename,
        "source_job_id": clean_input.get("jobId") or clean_input.get("job_id") or clean_input.get("source_job_id"),
        "source_result_manifest": clean_input.get("source_result_manifest"),
        "result_sha256": clean_input.get("result_sha256"),
        "source_url": clean_input.get("source_url"),
        "application_version": "2026.1",
        "orca_version": clean_input.get("orca_version") or clean_input.get("ORCA_version") or "6.1.0",
        "parser_version": "2026.1",
        "git_sha": clean_input.get("git_sha"),
        "generated_by": "ORCA Web Lab Canonical Normalizer",
        "imported_from": clean_input.get("imported_from"),
        "source_artifacts": source_artifacts if source_artifacts else None,
    }
    if provenance_override:
        prov.update(provenance_override)

    # 4. Extract Geometries & Species
    geometries: list[dict[str, Any]] = []
    species_list: list[dict[str, Any]] = []
    fragments_list: list[dict[str, Any]] = []
    raw_atoms: list[dict[str, Any]] = []

    # Population charges maps by index
    mulliken_arr = clean_input.get("mulliken_charges")
    loewdin_arr = clean_input.get("loewdin_charges")
    hirshfeld_arr = clean_input.get("hirshfeld_charges")
    mayer_arr = clean_input.get("mayer_charges")
    mayer_val_arr = clean_input.get("mayer_valences")

    if isinstance(clean_input.get("atoms"), list):
        for idx, a in enumerate(clean_input["atoms"]):
            elem = (a.get("elem") or a.get("element") or "C").capitalize()
            x = float(a.get("x", 0.0))
            y = float(a.get("y", 0.0))
            z = float(a.get("z", 0.0))
            atom_id = int(a.get("id", idx + 1))
            frag_id = a.get("molId") or a.get("fragment_id")
            
            mul_c = float(mulliken_arr[idx]) if mulliken_arr and idx < len(mulliken_arr) else None
            loe_c = float(loewdin_arr[idx]) if loewdin_arr and idx < len(loewdin_arr) else None
            hir_c = float(hirshfeld_arr[idx]) if hirshfeld_arr and idx < len(hirshfeld_arr) else None
            may_c = float(mayer_arr[idx]) if mayer_arr and idx < len(mayer_arr) else None
            may_v = float(mayer_val_arr[idx]) if mayer_val_arr and idx < len(mayer_val_arr) else None

            raw_atoms.append({
                "atom_id": atom_id,
                "element": elem,
                "atomic_number": ELEMENT_TO_Z.get(elem.upper(), None),
                "formal_charge": int(a.get("formal_charge", 0)),
                "isotope": a.get("isotope", None),
                "coordinates": {
                    "x": x, "y": y, "z": z, "unit": "angstrom"
                },
                "fragment_id": frag_id,
                "aromatic": a.get("aromatic", None),
                "hybridization": a.get("hybridization", None),
                "partial_charge": a.get("partial_charge", mul_c),
                "atomic_charges": {
                    "mulliken": mul_c,
                    "loewdin": loe_c,
                    "hirshfeld": hir_c,
                    "mayer": may_c,
                    "mayer_valence": may_v,
                }
            })
            if not parsed_coords:
                parsed_coords.append({"element": elem, "x": x, "y": y, "z": z})
        mapped_fields.append("atoms")

    if not raw_atoms and parsed_coords:
        for idx, pt in enumerate(parsed_coords):
            elem = pt["element"].capitalize()
            mul_c = float(mulliken_arr[idx]) if mulliken_arr and idx < len(mulliken_arr) else None
            loe_c = float(loewdin_arr[idx]) if loewdin_arr and idx < len(loewdin_arr) else None
            hir_c = float(hirshfeld_arr[idx]) if hirshfeld_arr and idx < len(hirshfeld_arr) else None
            may_c = float(mayer_arr[idx]) if mayer_arr and idx < len(mayer_arr) else None
            may_v = float(mayer_val_arr[idx]) if mayer_val_arr and idx < len(mayer_val_arr) else None

            raw_atoms.append({
                "atom_id": idx + 1,
                "element": elem,
                "atomic_number": ELEMENT_TO_Z.get(elem.upper(), None),
                "formal_charge": 0,
                "isotope": None,
                "coordinates": {
                    "x": pt["x"], "y": pt["y"], "z": pt["z"], "unit": "angstrom"
                },
                "fragment_id": 1,
                "aromatic": None,
                "hybridization": None,
                "partial_charge": mul_c,
                "atomic_charges": {
                    "mulliken": mul_c,
                    "loewdin": loe_c,
                    "hirshfeld": hir_c,
                    "mayer": may_c,
                    "mayer_valence": may_v,
                }
            })
        transformed_fields.append("coordinates_to_atoms")

    if isinstance(clean_input.get("fragments"), list):
        for f in clean_input["fragments"]:
            fid = f.get("id", 1)
            fname = f.get("name", f"Fragment {fid}")
            fragments_list.append({
                "fragment_id": fid,
                "name": fname,
                "role": f.get("role", "substrate"),
                "color": f.get("color", None),
                "molecule_ref": f.get("molecule_ref", None),
            })
        mapped_fields.append("fragments")

    bonds_list: list[dict[str, Any]] = []
    if isinstance(clean_input.get("bonds"), list):
        for b in clean_input["bonds"]:
            ai = b.get("atom_i", b.get("atom1", 0))
            aj = b.get("atom_j", b.get("atom2", 0))
            order = float(b.get("order", 1.0))
            bonds_list.append({
                "atom_i": int(ai),
                "atom_j": int(aj),
                "order": order,
                "aromatic": b.get("aromatic", None),
                "conjugated": b.get("conjugated", None),
                "ring": b.get("ring", None),
            })
        mapped_fields.append("bonds")

    geo_hash = compute_geometry_hash(parsed_coords) if parsed_coords else None
    if parsed_coords:
        geometries.append({
            "geometry_id": f"geo_{record_id[:8]}_1",
            "species_ref": f"sp_{record_id[:8]}_1",
            "geometry_type": "optimized" if "opt" in str(clean_input.get("calc_type", "")).lower() or record_type == "analysis_record" else "input",
            "coordinate_units": clean_input.get("coords_unit", "angstrom") or "angstrom",
            "coordinates": parsed_coords,
            "geometry_hash": geo_hash,
            "source_calculation_id": clean_input.get("calculation_id", None),
        })
        mapped_fields.append("geometries")

    formula_str = (
        clean_input.get("formula")
        or clean_input.get("chemical_formula")
        or (compute_chemical_formula([a["element"] for a in parsed_coords]) if parsed_coords else None)
    )

    species_list.append({
        "species_id": f"sp_{record_id[:8]}_1",
        "name": record_name,
        "formula": formula_str,
        "canonical_smiles": clean_input.get("smiles") or clean_input.get("canonical_smiles"),
        "isomeric_smiles": clean_input.get("isomeric_smiles"),
        "inchi": clean_input.get("inchi"),
        "inchikey": clean_input.get("inchikey"),
        "charge": int(clean_input.get("charge", 0)) if clean_input.get("charge") is not None else 0,
        "multiplicity": int(clean_input.get("multiplicity", 1)) if clean_input.get("multiplicity") is not None else 1,
        "atoms": raw_atoms,
        "bonds": bonds_list,
        "geometry_refs": [g["geometry_id"] for g in geometries],
    })

    # 5. Reaction Definition Object
    reaction_obj = None
    raw_rxn_block = clean_input.get("reaction") if isinstance(clean_input.get("reaction"), dict) else {}
    
    if record_type in ("reaction_definition", "reaction") or "reactants" in clean_input or "equation" in clean_input or raw_rxn_block:
        r_participants: list[dict[str, Any]] = []
        p_participants: list[dict[str, Any]] = []

        eq_str = clean_input.get("equation") or raw_rxn_block.get("equation")
        raw_r = clean_input.get("reactants") or raw_rxn_block.get("reactants")
        raw_p = clean_input.get("products") or raw_rxn_block.get("products")

        if (not raw_r or not raw_p) and eq_str and "->" in eq_str:
            parts = eq_str.split("->")
            if not raw_r:
                raw_r = parts[0].strip()
            if not raw_p:
                raw_p = parts[1].strip()

        if raw_r is None:
            raw_r = []
        if raw_p is None:
            raw_p = []
        if isinstance(raw_r, str):
            for part in raw_r.split("+"):
                toks = part.strip().split()
                if toks:
                    coeff = float(toks[0]) if toks[0].replace(".", "", 1).isdigit() else 1.0
                    name = " ".join(toks[1:]) if toks[0].replace(".", "", 1).isdigit() else " ".join(toks)
                    r_participants.append({
                        "species_id": f"sp_r_{len(r_participants) + 1}",
                        "species_name": name,
                        "stoichiometric_coefficient": coeff,
                        "role": "reactant"
                    })
            transformed_fields.append("reactants_equation_string")
        elif isinstance(raw_r, list):
            for idx, item in enumerate(raw_r):
                r_participants.append({
                    "species_id": item.get("species_id") or item.get("id") or f"sp_r_{idx + 1}",
                    "species_name": item.get("species_name") or item.get("name", f"Reactant {idx + 1}"),
                    "stoichiometric_coefficient": float(item.get("stoichiometric_coefficient", item.get("coefficient", item.get("coeff", 1.0)))),
                    "role": "reactant"
                })
            mapped_fields.append("reactants_list")

        if isinstance(raw_p, str):
            for part in raw_p.split("+"):
                toks = part.strip().split()
                if toks:
                    coeff = float(toks[0]) if toks[0].replace(".", "", 1).isdigit() else 1.0
                    name = " ".join(toks[1:]) if toks[0].replace(".", "", 1).isdigit() else " ".join(toks)
                    p_participants.append({
                        "species_id": f"sp_p_{len(p_participants) + 1}",
                        "species_name": name,
                        "stoichiometric_coefficient": coeff,
                        "role": "product"
                    })
            transformed_fields.append("products_equation_string")
        elif isinstance(raw_p, list):
            for idx, item in enumerate(raw_p):
                p_participants.append({
                    "species_id": item.get("species_id") or item.get("id") or f"sp_p_{idx + 1}",
                    "species_name": item.get("species_name") or item.get("name", f"Product {idx + 1}"),
                    "stoichiometric_coefficient": float(item.get("stoichiometric_coefficient", item.get("coefficient", item.get("coeff", 1.0)))),
                    "role": "product"
                })
            mapped_fields.append("products_list")

        eq_str = clean_input.get("equation") or raw_rxn_block.get("equation")
        if not eq_str and (r_participants or p_participants):
            r_side = " + ".join(f"{r['stoichiometric_coefficient']} {r['species_name']}" for r in r_participants)
            p_side = " + ".join(f"{p['stoichiometric_coefficient']} {p['species_name']}" for p in p_participants)
            eq_str = f"{r_side} -> {p_side}"

        raw_bal = clean_input.get("balance") or raw_rxn_block.get("balance", {})
        reaction_obj = {
            "reaction_id": clean_input.get("reaction_id") or f"rxn_{record_id[:8]}",
            "equation": eq_str or "A -> B",
            "reaction_smiles": clean_input.get("reaction_smiles") or raw_rxn_block.get("reaction_smiles"),
            "conditions": {
                "temperature_k": float(clean_input.get("temperature_k", 298.15)) if clean_input.get("temperature_k") is not None else None,
                "pressure_atm": float(clean_input.get("pressure_atm", 1.0)) if clean_input.get("pressure_atm") is not None else None,
                "solvent": clean_input.get("solvent"),
                "catalyst": clean_input.get("catalyst"),
                "standard_state": clean_input.get("standard_state", "Gas Phase (1 atm)"),
            },
            "reactants": r_participants,
            "products": p_participants,
            "agents": [],
            "balance": {
                "balanced": bool(raw_bal.get("balanced", True)),
                "atom_delta": raw_bal.get("atom_delta", {}),
                "charge_delta": raw_bal.get("charge_delta", 0.0),
                "balance_status": raw_bal.get("balance_status", "balanced" if raw_bal.get("balanced", True) else "imbalanced"),
            }
        }

    # 6. Calculations and Workflows
    calculations_list: list[dict[str, Any]] = []
    workflows_list: list[dict[str, Any]] = []

    calc_id = clean_input.get("calculation_id") or compute_deterministic_calculation_id(clean_input, geo_hash)
    calc_type = clean_input.get("calc_type") or clean_input.get("calculation_type") or "OPT"

    calculations_list.append({
        "calculation_id": calc_id,
        "calculation_type": calc_type,
        "status": "COMPLETED" if clean_input.get("status") in ("COMPLETED", "SUCCESS", None) else clean_input.get("status", "COMPLETED"),
        "input_parameters": {
            "method": clean_input.get("method") or "B3LYP",
            "basis": clean_input.get("basis") or clean_input.get("basis_set") or "def2-SVP",
            "dispersion": clean_input.get("dispersion", "None"),
            "solvation": clean_input.get("solvation", "None"),
            "solvent": clean_input.get("solvent", "None"),
            "charge": int(clean_input.get("charge", 0)) if clean_input.get("charge") is not None else 0,
            "multiplicity": int(clean_input.get("multiplicity", 1)) if clean_input.get("multiplicity") is not None else 1,
            "temperature_k": float(clean_input.get("temperature_k", 298.15)) if clean_input.get("temperature_k") is not None else None,
            "pressure_atm": float(clean_input.get("pressure_atm", 1.0)) if clean_input.get("pressure_atm") is not None else None,
            "input_geometry_id": geometries[0]["geometry_id"] if geometries else None,
        },
        "analysis_record_id": f"an_{record_id[:8]}" if record_type == "analysis_record" else None,
        "orca_version": prov["orca_version"],
        "input_source": clean_input.get("input_source"),
        "output_source": clean_input.get("output_source"),
    })
    mapped_fields.append("calculations")

    if clean_input.get("workflow") or clean_input.get("isDualWorkflow") or clean_input.get("isWorkflow") or record_type == "workflow_record":
        wf_steps = []
        raw_steps = clean_input.get("steps") or [{"type": "OPT"}, {"type": "FREQ"}]
        for idx, s in enumerate(raw_steps):
            wf_steps.append({
                "step_id": f"step_{idx + 1}",
                "step_index": idx + 1,
                "calculation_type": s.get("type", s.get("calculation_type", "OPT")),
                "calculation_id": f"{calc_id}_s{idx + 1}",
                "depends_on": [f"step_{idx}"] if idx > 0 else [],
                "input_geometry_id": geometries[0]["geometry_id"] if geometries else None,
                "output_geometry_id": geometries[0]["geometry_id"] if geometries else None,
                "analysis_record_id": f"an_{record_id[:8]}_s{idx + 1}",
                "status": "COMPLETED"
            })
        workflows_list.append({
            "workflow_id": f"wf_{record_id[:8]}",
            "name": clean_input.get("workflow_name", "Multi-Step Optimization & Thermochemistry"),
            "status": "COMPLETED",
            "steps": wf_steps
        })
        mapped_fields.append("workflows")

    # 7. Energy Values Consolidation, ZPE vs E0 Semantics, & Thermochemistry
    def _extract_quantity(val_or_dict: Any) -> float | None:
        if val_or_dict is None:
            return None
        if isinstance(val_or_dict, (int, float)):
            return float(val_or_dict)
        if isinstance(val_or_dict, dict) and "value" in val_or_dict:
            v = val_or_dict["value"]
            return float(v) if v is not None else None
        return None

    def _first_valid_quantity(*keys: str) -> float | None:
        for k in keys:
            if k in clean_input:
                q = _extract_quantity(clean_input[k])
                if q is not None:
                    return q
        return None

    # Consolidate electronic energy from aliases
    e_elec = _first_valid_quantity(
        "e_elec_eh", "electronic_energy_hartree", "total_energy_eh",
        "scf_energy_hartree", "electronic_energy", "energy"
    )

    # Consolidate ZPE from aliases
    zpe = _first_valid_quantity(
        "zpe_eh", "zero_point_energy_hartree", "zpe_hartree",
        "zpe", "zero_point_energy"
    )

    # Critical distinction: electronic_zpe_eh is E0 = E_elec + ZPE
    e0_raw = _first_valid_quantity("electronic_zpe_eh", "zero_point_corrected_energy")
    e0 = e0_raw if e0_raw is not None else ((e_elec + zpe) if (e_elec is not None and zpe is not None) else None)

    # Consolidate enthalpy from aliases
    h_tot = _first_valid_quantity(
        "total_enthalpy_eh", "enthalpy_hartree", "final_enthalpy_hartree", "enthalpy"
    )

    # Consolidate Gibbs free energy from aliases
    g_tot = _first_valid_quantity(
        "gibbs_free_energy_eh", "gibbs_free_energy_hartree", "final_gibbs_hartree",
        "gibbs", "gibbs_free_energy"
    )

    # Consolidate entropy from aliases
    s_tot = _first_valid_quantity(
        "total_entropy_cal_mol_k", "entropy", "entropy_cal_mol_k"
    )

    # Entropy terms
    ts_term = _first_valid_quantity("entropy_term_ts_eh", "entropy_term_eh")
    minus_ts_term = _first_valid_quantity("entropy_correction_minus_ts_eh", "entropy_correction_eh")
    if minus_ts_term is None and ts_term is not None:
        minus_ts_term = -ts_term
    if ts_term is None and minus_ts_term is not None:
        ts_term = -minus_ts_term

    therm_corr = _first_valid_quantity("thermal_correction_eh", "thermal_correction")
    if therm_corr is None and h_tot is not None and e0 is not None:
        therm_corr = h_tot - e0


    # Thermochemistry numerical validation
    e0_valid = None
    e0_delta = None
    gibbs_valid = None
    gibbs_delta = None
    thermo_warnings: list[str] = []

    if e_elec is not None and zpe is not None and e0 is not None:
        e0_delta = abs(e0 - (e_elec + zpe))
        e0_valid = e0_delta < 1e-5
        if not e0_valid:
            thermo_warnings.append(f"ZPE-corrected energy discrepancy: E0={e0:.8f} != E_elec+ZPE={e_elec+zpe:.8f} (delta={e0_delta:.8f} Eh)")

    if g_tot is not None and h_tot is not None and ts_term is not None:
        gibbs_delta = abs(g_tot - (h_tot - ts_term))
        gibbs_valid = gibbs_delta < 1e-4
        if not gibbs_valid:
            thermo_warnings.append(f"Gibbs energy discrepancy: G={g_tot:.8f} != H-TS={h_tot-ts_term:.8f} (delta={gibbs_delta:.8f} Eh)")

    thermo_obj = None
    if e_elec is not None or zpe is not None or e0 is not None or g_tot is not None or h_tot is not None or clean_input.get("thermochemistry"):
        thermo_obj = {
            "status": "calculated" if (e_elec is not None or g_tot is not None) else "not_calculated",
            "electronic_energy": {"value": float(e_elec), "unit": "hartree", "source_calculation_id": calc_id, "semantic_type": "electronic_scf", "uncertainty": None} if e_elec is not None else None,
            "zero_point_energy": {"value": float(zpe), "unit": "hartree", "source_calculation_id": calc_id, "uncertainty": None} if zpe is not None else None,
            "zpe": {"value": float(zpe), "unit": "hartree", "source_calculation_id": calc_id, "uncertainty": None} if zpe is not None else None,
            "zero_point_corrected_energy": {
                "value": float(e0),
                "unit": "hartree",
                "formula": "E_electronic + ZPE",
                "components": {
                    "electronic_energy": float(e_elec) if e_elec is not None else None,
                    "zero_point_energy": float(zpe) if zpe is not None else None
                }
            } if e0 is not None else None,
            "thermal_correction": {"value": float(therm_corr), "unit": "hartree", "source_calculation_id": calc_id, "uncertainty": None} if therm_corr is not None else None,
            "enthalpy": {"value": float(h_tot), "unit": "hartree", "source_calculation_id": calc_id, "uncertainty": None} if h_tot is not None else None,
            "entropy": {"value": float(s_tot), "unit": "cal/mol/K", "source_calculation_id": calc_id, "uncertainty": None} if s_tot is not None else None,
            "entropy_term_ts": {"value": float(ts_term), "unit": "hartree", "source_calculation_id": calc_id, "uncertainty": None} if ts_term is not None else None,
            "entropy_correction_minus_ts": {"value": float(minus_ts_term), "unit": "hartree", "source_calculation_id": calc_id, "uncertainty": None} if minus_ts_term is not None else None,
            "gibbs_free_energy": {"value": float(g_tot), "unit": "hartree", "source_calculation_id": calc_id, "uncertainty": None} if g_tot is not None else None,
            "equilibrium_constant": clean_input.get("k_eq") or clean_input.get("equilibrium_constant"),
            "temperature_k": float(clean_input.get("temperature_k", 298.15)) if clean_input.get("temperature_k") is not None else None,
            "pressure_atm": float(clean_input.get("pressure_atm", 1.0)) if clean_input.get("pressure_atm") is not None else None,
            "standard_state": clean_input.get("standard_state", "Gas Phase (1 atm)"),
            "stationary_point_status": clean_input.get("stationary_point_status", "MINIMUM"),
            "numerical_consistency": {
                "e0_relation_valid": e0_valid,
                "e0_delta_eh": e0_delta,
                "gibbs_relation_valid": gibbs_valid,
                "gibbs_delta_eh": gibbs_delta,
                "warnings": thermo_warnings,
            },
            "composite_sources": {
                "electronic_energy_source": clean_input.get("sp_source") or clean_input.get("spName") or None,
                "zpe_source": clean_input.get("zpe_source") or clean_input.get("freq_source") or None,
                "vibrational_source": clean_input.get("freq_source") or clean_input.get("primaryName") or None,
                "thermal_correction_source": clean_input.get("thermal_correction_source") or None,
                "entropy_source": clean_input.get("entropy_source") or None,
                "geometry_source": clean_input.get("opt_source") or None
            }
        }
        mapped_fields.append("thermochemistry")

    # 8. Electronic Structure Block & Frontier Orbitals
    homo_val = _extract_quantity(clean_input.get("homo_ev"))
    lumo_val = _extract_quantity(clean_input.get("lumo_ev"))
    gap_val = _extract_quantity(clean_input.get("homo_lumo_gap_ev"))
    if gap_val is None and homo_val is not None and lumo_val is not None:
        gap_val = lumo_val - homo_val

    alpha_homo_val = _extract_quantity(clean_input.get("alpha_homo_ev"))
    alpha_lumo_val = _extract_quantity(clean_input.get("alpha_lumo_ev"))
    beta_homo_val = _extract_quantity(clean_input.get("beta_homo_ev"))
    beta_lumo_val = _extract_quantity(clean_input.get("beta_lumo_ev"))

    dipole_val = _extract_quantity(clean_input.get("dipole_moment_debye"))

    electronic_structure_obj = None
    if e_elec is not None or homo_val is not None or lumo_val is not None or dipole_val is not None or clean_input.get("electronic_structure"):
        orbitals_status = "unrestricted" if (alpha_homo_val is not None or (clean_input.get("multiplicity") or 1) > 1) else ("restricted" if homo_val is not None else "not_calculated")
        
        conceptual_dft_obj = {
            "electronegativity": {"value": _extract_quantity(clean_input.get("electronegativity_ev")), "unit": "eV"} if clean_input.get("electronegativity_ev") is not None else None,
            "chemical_hardness": {"value": _extract_quantity(clean_input.get("chemical_hardness_ev")), "unit": "eV"} if clean_input.get("chemical_hardness_ev") is not None else None,
            "chemical_potential": {"value": _extract_quantity(clean_input.get("chemical_potential_ev")), "unit": "eV"} if clean_input.get("chemical_potential_ev") is not None else None,
            "chemical_softness": {"value": _extract_quantity(clean_input.get("chemical_softness_ev")), "unit": "eV^-1"} if clean_input.get("chemical_softness_ev") is not None else None,
            "electrophilicity_index": {"value": _extract_quantity(clean_input.get("electrophilicity_index_ev")), "unit": "eV"} if clean_input.get("electrophilicity_index_ev") is not None else None,
            "electrodonating_power": {"value": _extract_quantity(clean_input.get("electrodonating_power_ev")), "unit": "eV"} if clean_input.get("electrodonating_power_ev") is not None else None,
            "electroaccepting_power": {"value": _extract_quantity(clean_input.get("electroaccepting_power_ev")), "unit": "eV"} if clean_input.get("electroaccepting_power_ev") is not None else None,
            "net_electrophilicity": {"value": _extract_quantity(clean_input.get("net_electrophilicity_ev")), "unit": "eV"} if clean_input.get("net_electrophilicity_ev") is not None else None,
            "ionization_potential": {"value": _extract_quantity(clean_input.get("ionization_potential_ev")), "unit": "eV"} if clean_input.get("ionization_potential_ev") is not None else None,
            "electron_affinity": {"value": _extract_quantity(clean_input.get("electron_affinity_ev")), "unit": "eV"} if clean_input.get("electron_affinity_ev") is not None else None,
        }

        electronic_structure_obj = {
            "status": "calculated" if (e_elec is not None or homo_val is not None) else "not_calculated",
            "electronic_energy": {"value": float(e_elec), "unit": "hartree", "semantic_type": "electronic_scf", "source_calculation_id": calc_id, "uncertainty": None} if e_elec is not None else None,
            "frontier_orbitals": {
                "homo": {"value": float(homo_val), "unit": "eV"} if homo_val is not None else None,
                "lumo": {"value": float(lumo_val), "unit": "eV"} if lumo_val is not None else None,
                "homo_lumo_gap": {"value": float(gap_val), "unit": "eV"} if gap_val is not None else None,
                "alpha_homo": {"value": float(alpha_homo_val), "unit": "eV"} if alpha_homo_val is not None else None,
                "alpha_lumo": {"value": float(alpha_lumo_val), "unit": "eV"} if alpha_lumo_val is not None else None,
                "beta_homo": {"value": float(beta_homo_val), "unit": "eV"} if beta_homo_val is not None else None,
                "beta_lumo": {"value": float(beta_lumo_val), "unit": "eV"} if beta_lumo_val is not None else None,
                "orbitals_status": orbitals_status
            },
            "conceptual_dft": conceptual_dft_obj,
            "dipole_moment": {
                "total_debye": float(dipole_val) if dipole_val is not None else None,
                "components_debye": None
            },
            "spin": {
                "status": "calculated" if clean_input.get("s2_actual") is not None else "not_applicable",
                "s2_actual": clean_input.get("s2_actual"),
                "s2_ideal": clean_input.get("s2_ideal"),
                "spin_contamination": clean_input.get("spin_contamination")
            }
        }
        mapped_fields.append("electronic_structure")

    # 9. Population Analysis Block (Explicit methods)
    has_pop = any(clean_input.get(k) is not None for k in ("mulliken_charges", "loewdin_charges", "hirshfeld_charges", "mayer_charges"))
    population_analysis_obj = {
        "status": "calculated" if has_pop else "not_calculated",
        "methods": {
            "mulliken": {
                "status": "available" if clean_input.get("mulliken_charges") is not None else "not_calculated",
                "charges": [float(x) for x in clean_input["mulliken_charges"]] if clean_input.get("mulliken_charges") is not None else None
            },
            "loewdin": {
                "status": "available" if clean_input.get("loewdin_charges") is not None else "not_calculated",
                "charges": [float(x) for x in clean_input["loewdin_charges"]] if clean_input.get("loewdin_charges") is not None else None
            },
            "hirshfeld": {
                "status": "available" if clean_input.get("hirshfeld_charges") is not None else "not_calculated",
                "charges": [float(x) for x in clean_input["hirshfeld_charges"]] if clean_input.get("hirshfeld_charges") is not None else None
            },
            "mayer": {
                "status": "available" if clean_input.get("mayer_charges") is not None else "not_calculated",
                "charges": [float(x) for x in clean_input["mayer_charges"]] if clean_input.get("mayer_charges") is not None else None,
                "valences": [float(x) for x in clean_input["mayer_valences"]] if clean_input.get("mayer_valences") is not None else None
            }
        } if has_pop else {}
    }
    if has_pop:
        mapped_fields.append("population_analysis")

    # 10. Vibrational & IR Spectroscopy (Raw vs Derived separation)
    v_modes: list[dict[str, Any]] = []
    if isinstance(clean_input.get("ir_spectrum"), list) and clean_input["ir_spectrum"]:
        for idx, m in enumerate(clean_input["ir_spectrum"]):
            if isinstance(m, dict):
                f_val = float(m.get("frequency_cm", m.get("freq", 0.0)))
                i_val = float(m.get("intensity_km_mol", m.get("t2", 0.0)))
                v_modes.append({
                    "mode": int(m.get("mode", idx + 1)),
                    "frequency_cm": f_val,
                    "intensity_km_mol": i_val,
                    "is_imaginary": f_val < 0,
                    "symmetry": m.get("symmetry")
                })
        mapped_fields.append("ir_spectrum")
    elif isinstance(clean_input.get("vibrational_frequencies_cm"), list) and clean_input["vibrational_frequencies_cm"]:
        freqs = clean_input["vibrational_frequencies_cm"]
        intensities = clean_input.get("ir_intensities_km_mol", [0.0] * len(freqs))
        for idx, f in enumerate(freqs):
            f_val = float(f)
            i_val = float(intensities[idx]) if idx < len(intensities) else 0.0
            v_modes.append({
                "mode": idx + 1,
                "frequency_cm": f_val,
                "intensity_km_mol": i_val,
                "is_imaginary": f_val < 0,
                "symmetry": None
            })
        mapped_fields.append("vibrational_frequencies_cm")

    imag_freqs = [float(x) for x in clean_input.get("imaginary_frequencies_cm", [])]
    imag_count = int(clean_input.get("imaginary_frequencies_count", len(imag_freqs)))

    # Derived IR Spectrum (Lorentzian broadening sampled grid)
    derived_ir_spectrum = None
    raw_conv_ir = clean_input.get("convoluted_ir_spectrum")
    if isinstance(raw_conv_ir, list) and raw_conv_ir:
        data_points = []
        for pt in raw_conv_ir:
            if isinstance(pt, dict) and "wavenumber_cm" in pt:
                data_points.append({
                    "wavenumber_cm": float(pt["wavenumber_cm"]),
                    "absorbance": float(pt.get("absorbance", 0.0)),
                    "absorbance_norm": float(pt.get("absorbance_norm", 0.0)) if pt.get("absorbance_norm") is not None else None,
                    "transmittance_pct": float(pt.get("transmittance_pct", 100.0)) if pt.get("transmittance_pct") is not None else None,
                })
        derived_ir_spectrum = {
            "spectrum_type": "ir",
            "x_label": "wavenumber",
            "x_unit": "cm^-1",
            "y_label": "absorbance",
            "broadening_model": "lorentzian",
            "broadening_parameter": {
                "fwhm_cm": 15.0,
                "scaling_factor": 1.0,
                "start_cm": 400.0,
                "end_cm": 4000.0,
                "step_cm": 2.0
            },
            "data_points": data_points
        }
        mapped_fields.append("convoluted_ir_spectrum")

    peak_ass = clean_input.get("ir_peak_assignments") or clean_input.get("activeIRAssignments") or clean_input.get("assignments") or []
    exp_ir = clean_input.get("experimental_ir_spectra") or clean_input.get("loadedExperimentalIRSpectra") or []

    vibrational_spectroscopy_obj = {
        "status": "calculated" if (v_modes or imag_freqs) else "not_calculated",
        "imaginary_frequencies_count": imag_count,
        "imaginary_frequencies_cm": imag_freqs,
        "harmonic_modes": v_modes,
        "derived_spectrum": derived_ir_spectrum,
        "peak_assignments": peak_ass,
        "experimental_spectra": exp_ir,
    }
    if v_modes or derived_ir_spectrum or imag_freqs:
        mapped_fields.append("vibrational_spectroscopy")

    # 11. Electronic Spectroscopy (UV-Vis)
    uv_transitions: list[dict[str, Any]] = []
    if isinstance(clean_input.get("transitions"), list) and clean_input["transitions"]:
        for t in clean_input["transitions"]:
            if isinstance(t, dict):
                uv_transitions.append({
                    "state": int(t.get("state", len(uv_transitions) + 1)),
                    "energy_cm": float(t.get("energy_cm", 0.0)),
                    "energy_ev": float(t.get("energy_ev", 0.0)) if t.get("energy_ev") is not None else None,
                    "wavelength_nm": float(t.get("wavelength_nm", 0.0)) if t.get("wavelength_nm") is not None else None,
                    "oscillator_strength": float(t.get("oscillator_strength", 0.0))
                })
        mapped_fields.append("transitions")
    elif isinstance(clean_input.get("tddft_cm"), list) and clean_input["tddft_cm"]:
        for idx, (cm, fosc) in enumerate(zip(clean_input["tddft_cm"], clean_input.get("tddft_fosc", [0.0] * len(clean_input["tddft_cm"])), strict=False), 1):
            cm_f = float(cm)
            fosc_f = float(fosc)
            nm_f = (1.0e7 / cm_f) if cm_f > 0 else 0.0
            ev_f = (cm_f / 8065.544) if cm_f > 0 else 0.0
            uv_transitions.append({
                "state": idx,
                "energy_cm": cm_f,
                "energy_ev": round(ev_f, 4),
                "wavelength_nm": round(nm_f, 2),
                "oscillator_strength": round(fosc_f, 6)
            })
        mapped_fields.append("tddft_cm")

    derived_uv_spectrum = None
    raw_uv_curve = clean_input.get("uvvis_spectrum")
    if isinstance(raw_uv_curve, list) and raw_uv_curve:
        uv_points = []
        for pt in raw_uv_curve:
            if isinstance(pt, dict) and "wavelength_nm" in pt:
                uv_points.append({
                    "wavelength_nm": float(pt["wavelength_nm"]),
                    "intensity": float(pt.get("intensity", 0.0))
                })
        derived_uv_spectrum = {
            "spectrum_type": "uv_vis",
            "x_label": "wavelength",
            "x_unit": "nm",
            "y_label": "intensity",
            "broadening_model": "gaussian",
            "broadening_parameter": {
                "sigma_nm": 20.0,
                "wavelength_shift_nm": 0.0
            },
            "data_points": uv_points
        }
        mapped_fields.append("uvvis_spectrum")

    exp_uv = clean_input.get("loadedExperimentalSpectra") or []
    electronic_spectroscopy_obj = {
        "status": "calculated" if uv_transitions else "not_calculated",
        "transitions_count": len(uv_transitions),
        "transitions": uv_transitions,
        "derived_spectrum": derived_uv_spectrum,
        "experimental_spectra": exp_uv,
    }
    if uv_transitions or derived_uv_spectrum:
        mapped_fields.append("electronic_spectroscopy")

    # 12. NMR Spectroscopy
    nmr_raw = clean_input.get("nmr") or clean_input.get("nmr_result")
    if nmr_raw:
        if isinstance(nmr_raw, dict):
            nmr_spectroscopy_obj = {
                "status": "calculated",
                "nuclei": nmr_raw.get("atoms") or nmr_raw.get("nuclei", []),
                "references": nmr_raw.get("references", {}),
                "h1_spectrum": nmr_raw.get("h1_spectrum"),
                "c13_spectrum": nmr_raw.get("c13_spectrum"),
            }
        elif isinstance(nmr_raw, list):
            nmr_spectroscopy_obj = {
                "status": "calculated",
                "nuclei": nmr_raw,
                "references": {},
                "h1_spectrum": None,
                "c13_spectrum": None,
            }
        mapped_fields.append("nmr_spectroscopy")
    else:
        nmr_spectroscopy_obj = {
            "status": "not_calculated",
            "nuclei": [],
            "references": {},
            "h1_spectrum": None,
            "c13_spectrum": None,
        }

    # 13. Legacy Analysis Results & Spectroscopy Containers (Backward Compatibility)
    analysis_results_obj = None
    if record_type == "analysis_record" or e_elec is not None or homo_val is not None:
        analysis_results_obj = {
            "analysis_type": calc_type,
            "status": "SUCCESS" if clean_input.get("status") in ("COMPLETED", "SUCCESS", None) else str(clean_input.get("status")),
            "single_point_energy_eh": float(e_elec) if e_elec is not None else None,
            "zero_point_energy_eh": float(zpe) if zpe is not None else None,
            "zero_point_corrected_energy_eh": float(e0) if e0 is not None else None,
            "total_enthalpy_eh": float(h_tot) if h_tot is not None else None,
            "gibbs_free_energy_eh": float(g_tot) if g_tot is not None else None,
            "entropy_cal_mol_k": float(s_tot) if s_tot is not None else None,
            "dipole_moment_debye": float(dipole_val) if dipole_val is not None else None,
            "homo_ev": float(homo_val) if homo_val is not None else None,
            "lumo_ev": float(lumo_val) if lumo_val is not None else None,
            "homo_lumo_gap_ev": float(gap_val) if gap_val is not None else None,
            "converged": bool(clean_input.get("terminated_normally", True)) if clean_input.get("terminated_normally") is not None else True,
            "stationary_point_status": clean_input.get("stationary_point_status", "MINIMUM"),
            "imaginary_frequencies_count": imag_count,
        }
        mapped_fields.append("analysis_results")

    legacy_spectroscopy_obj = {
        "ir": {
            "theoretical_modes": [{"mode": m["mode"], "frequency_cm": m["frequency_cm"], "intensity_km_mol": m.get("intensity_km_mol")} for m in v_modes],
            "experimental_spectra": exp_ir,
            "peak_assignments": peak_ass,
        },
        "uv_vis": {
            "theoretical_transitions": [{"transition_index": t["state"], "wavenumber_cm": t["energy_cm"], "wavelength_nm": t.get("wavelength_nm"), "oscillator_strength": t["oscillator_strength"]} for t in uv_transitions],
            "experimental_spectra": exp_uv,
        },
        "nmr": {
            "nuclei": nmr_spectroscopy_obj.get("nuclei", []) if nmr_spectroscopy_obj else []
        }
    } if (v_modes or uv_transitions or nmr_raw) else None

    # 14. Data Quality, Diagnostics, & Status
    term_status_in = clean_input.get("termination_status")
    term_msg = clean_input.get("termination_message")
    term_normal = clean_input.get("terminated_normally")
    if term_normal is None:
        if term_status_in == "NORMAL" or (term_msg and "TERMINATED NORMALLY" in str(term_msg)):
            term_normal = True
    had_err = clean_input.get("had_error_termination", False)
    term_status = term_status_in or ("NORMAL" if term_normal else ("ABNORMAL" if had_err else "UNKNOWN"))

    score = 0.5
    if geometries and len(geometries[0]["coordinates"]) > 0:
        score += 0.2
    if thermo_obj and thermo_obj.get("electronic_energy") is not None:
        score += 0.2
    if species_list and species_list[0].get("formula"):
        score += 0.1
    score = min(1.0, round(score, 2))

    val_status = "verified" if score >= 0.8 else ("partially_verified" if score >= 0.5 else "unverified")

    data_quality_obj = {
        "validation_status": val_status,
        "completeness_score": score,
        "calculation_status": "COMPLETED" if term_normal is not False else "FAILED",
        "termination_status": term_status,
        "termination_message": clean_input.get("termination_message"),
        "had_error_termination": had_err,
        "error_count": int(clean_input.get("error_count", 0)),
        "stationary_point_status": clean_input.get("stationary_point_status", "MINIMUM"),
        "thermochemistry_reliability": clean_input.get("thermochemistry_reliability", "RELIABLE"),
        "is_transition_state": bool(clean_input.get("is_transition_state", False)),
        "parser_status": "SUCCESS",
        "scientific_validation_status": "VERIFIED" if (e0_valid is not False and gibbs_valid is not False) else "WARNINGS",
        "missing_fields": missing_fields,
        "warnings": warnings + thermo_warnings,
        "errors": []
    }

    # 15. Training Metadata (for downstream ML Dataset Export)
    training_meta = clean_input.get("training_metadata")
    if not training_meta:
        training_meta = {
            "task_type": "molecular_property_prediction" if record_type in ("molecule", "analysis_record") else ("reaction_energy_prediction" if record_type in ("reaction_definition", "reaction") else "vibrational_mode_prediction"),
            "input_modality": "coordinates" if geometries else "smiles",
            "target_modality": "energy" if thermo_obj else "spectrum",
            "label": thermo_obj.get("gibbs_free_energy", {}).get("value") if thermo_obj and thermo_obj.get("gibbs_free_energy") else None,
            "label_type": "float" if thermo_obj else "null",
            "quality_flags": ["valid_geometry" if geometries else "missing_geometry"],
            "split_hint": "train"
        }

    # Split Group Key (for molecular grouping without train/val/test data leakage)
    split_key = compute_split_group_key(clean_input, geo_hash)

    # ML-Ready Molecule representation
    molecule_obj = {
        "formula": formula_str,
        "canonical_smiles": clean_input.get("smiles") or clean_input.get("canonical_smiles"),
        "isomeric_smiles": clean_input.get("isomeric_smiles"),
        "inchi": clean_input.get("inchi"),
        "inchikey": clean_input.get("inchikey"),
        "charge": int(clean_input.get("charge", 0)) if clean_input.get("charge") is not None else 0,
        "multiplicity": int(clean_input.get("multiplicity", 1)) if clean_input.get("multiplicity") is not None else 1,
        "num_atoms": len(raw_atoms) if raw_atoms else clean_input.get("atoms_count", 0),
        "elements": [a["element"] for a in raw_atoms] if raw_atoms else [],
        "atomic_numbers": [a["atomic_number"] for a in raw_atoms if a.get("atomic_number") is not None] if raw_atoms else [],
    }

    # ML-Ready Geometry representation
    geom_coords = [[float(a["coordinates"]["x"]), float(a["coordinates"]["y"]), float(a["coordinates"]["z"])] for a in raw_atoms] if raw_atoms else []
    geometry_obj = {
        "atomic_numbers": [a["atomic_number"] for a in raw_atoms if a.get("atomic_number") is not None],
        "elements": [a["element"] for a in raw_atoms],
        "coordinates": {
            "values": geom_coords,
            "unit": "angstrom"
        },
        "geometry_type": "optimized" if "opt" in str(clean_input.get("calc_type", "")).lower() or record_type == "analysis_record" else "input",
        "geometry_hash": geo_hash,
        "num_atoms": len(raw_atoms)
    } if raw_atoms else None

    # ML-Ready Molecular Graph (GNN representation)
    graph_obj = extract_molecular_graph(
        clean_input,
        raw_atoms,
        smiles=clean_input.get("smiles") or clean_input.get("canonical_smiles"),
        mol_block=clean_input.get("mol_block") or clean_input.get("mol_file")
    )

    # ML-Ready Calculation definition
    calculation_obj = {
        "calculation_id": calc_id,
        "engine": "ORCA",
        "orca_version": prov.get("orca_version", "6.1.0"),
        "calculation_type": calc_type,
        "method": clean_input.get("method") or "DFT",
        "functional": clean_input.get("dft_functional") or clean_input.get("functional") or "B3LYP",
        "basis_set": clean_input.get("basis") or clean_input.get("basis_set") or "def2-SVP",
        "auxiliary_basis": clean_input.get("auxiliary_basis"),
        "dispersion": clean_input.get("dispersion", "None"),
        "solvation": {
            "model": clean_input.get("solvation", "None"),
            "solvent": clean_input.get("solvent", "None")
        } if clean_input.get("solvation") and clean_input.get("solvation") != "None" else None,
        "charge": int(clean_input.get("charge", 0)) if clean_input.get("charge") is not None else 0,
        "multiplicity": int(clean_input.get("multiplicity", 1)) if clean_input.get("multiplicity") is not None else 1,
        "temperature": {
            "value": float(clean_input["temperature_k"]) if clean_input.get("temperature_k") is not None else 298.15,
            "unit": "kelvin"
        },
        "pressure": {
            "value": float(clean_input["pressure_atm"]) if clean_input.get("pressure_atm") is not None else 1.0,
            "unit": "atm"
        },
    }

    # Electronic properties with explicit units
    cdft_en = _extract_quantity(clean_input.get("electronegativity_ev"))
    cdft_hard = _extract_quantity(clean_input.get("chemical_hardness_ev"))
    cdft_pot = _extract_quantity(clean_input.get("chemical_potential_ev"))
    cdft_soft = _extract_quantity(clean_input.get("chemical_softness_ev"))
    cdft_w = _extract_quantity(clean_input.get("electrophilicity_index_ev"))

    electronic_props_obj = {
        "total_energy": {"value": float(e_elec), "unit": "hartree"} if e_elec is not None else None,
        "homo": {"value": float(homo_val), "unit": "eV"} if homo_val is not None else None,
        "lumo": {"value": float(lumo_val), "unit": "eV"} if lumo_val is not None else None,
        "homo_lumo_gap": {"value": float(gap_val), "unit": "eV"} if gap_val is not None else None,
        "dipole_moment": {"value": float(dipole_val), "unit": "debye"} if dipole_val is not None else None,
        "conceptual_dft": {
            "electronegativity": {"value": float(cdft_en), "unit": "eV"} if cdft_en is not None else None,
            "chemical_hardness": {"value": float(cdft_hard), "unit": "eV"} if cdft_hard is not None else None,
            "chemical_potential": {"value": float(cdft_pot), "unit": "eV"} if cdft_pot is not None else None,
            "chemical_softness": {"value": float(cdft_soft), "unit": "eV^-1"} if cdft_soft is not None else None,
            "electrophilicity_index": {"value": float(cdft_w), "unit": "eV"} if cdft_w is not None else None,
        } if any(x is not None for x in [cdft_en, cdft_hard, cdft_pot, cdft_soft, cdft_w]) else None
    } if any(x is not None for x in [e_elec, homo_val, lumo_val, gap_val, dipole_val]) else None

    # Vibrational properties with explicit units
    vibrational_props_obj = {
        "harmonic_modes": [
            {
                "mode_number": int(m.get("mode", idx + 1)),
                "frequency": {"value": float(m["frequency_cm"]), "unit": "cm^-1"},
                "ir_intensity": {"value": float(m.get("intensity_km_mol", 0.0)), "unit": "km/mol"} if m.get("intensity_km_mol") is not None else None
            }
            for idx, m in enumerate(v_modes)
        ] if v_modes else [],
        "imaginary_frequencies_count": int(clean_input.get("imaginary_frequencies_count", 0)),
        "stationary_point_status": clean_input.get("stationary_point_status", "MINIMUM"),
    } if v_modes or clean_input.get("stationary_point_status") else None

    # Structured Charges by method
    charges_obj = {
        "mulliken": [{"atom_index": idx, "element": a["element"], "value": float(mulliken_arr[idx]), "unit": "elementary_charge"} for idx, a in enumerate(raw_atoms) if mulliken_arr and idx < len(mulliken_arr) and mulliken_arr[idx] is not None] if mulliken_arr else [],
        "loewdin": [{"atom_index": idx, "element": a["element"], "value": float(loewdin_arr[idx]), "unit": "elementary_charge"} for idx, a in enumerate(raw_atoms) if loewdin_arr and idx < len(loewdin_arr) and loewdin_arr[idx] is not None] if loewdin_arr else [],
        "hirshfeld": [{"atom_index": idx, "element": a["element"], "value": float(hirshfeld_arr[idx]), "unit": "elementary_charge"} for idx, a in enumerate(raw_atoms) if hirshfeld_arr and idx < len(hirshfeld_arr) and hirshfeld_arr[idx] is not None] if hirshfeld_arr else [],
        "mayer": [{"atom_index": idx, "element": a["element"], "charge": float(mayer_arr[idx]) if mayer_arr and idx < len(mayer_arr) and mayer_arr[idx] is not None else None, "valence": float(mayer_val_arr[idx]) if mayer_val_arr and idx < len(mayer_val_arr) and mayer_val_arr[idx] is not None else None} for idx, a in enumerate(raw_atoms) if (mayer_arr and idx < len(mayer_arr)) or (mayer_val_arr and idx < len(mayer_val_arr))] if (mayer_arr or mayer_val_arr) else [],
    } if (mulliken_arr or loewdin_arr or hirshfeld_arr or mayer_arr or mayer_val_arr) else None

    # Available artifacts
    available_arts = clean_input.get("available_files") or clean_input.get("source_artifacts") or []
    if not isinstance(available_arts, list):
        available_arts = [str(available_arts)]

    # ML Targets construction (lossless, standardized units, explicit physical dimensions)
    targets_obj = clean_input.get("targets")
    if targets_obj is None or not isinstance(targets_obj, dict):
        targets_obj = {}
        if e_elec is not None:
            targets_obj["electronic_energy_hartree"] = float(e_elec)
            targets_obj["electronic_energy_eh"] = float(e_elec)
            targets_obj["electronic_energy_ev"] = round(float(e_elec) * 27.211386245988, 6)
        if zpe is not None:
            targets_obj["zero_point_energy_hartree"] = float(zpe)
            targets_obj["zero_point_energy_eh"] = float(zpe)
            targets_obj["zero_point_energy_ev"] = round(float(zpe) * 27.211386245988, 6)
        if e0 is not None:
            targets_obj["energy_zpe_corrected_hartree"] = float(e0)
            targets_obj["energy_zpe_corrected_eh"] = float(e0)
            targets_obj["energy_zpe_corrected_ev"] = round(float(e0) * 27.211386245988, 6)
        if h_tot is not None:
            targets_obj["total_enthalpy_hartree"] = float(h_tot)
            targets_obj["total_enthalpy_eh"] = float(h_tot)
            targets_obj["total_enthalpy_kcal_mol"] = round(float(h_tot) * 627.50947406311, 4)
        if g_tot is not None:
            targets_obj["gibbs_free_energy_hartree"] = float(g_tot)
            targets_obj["gibbs_free_energy_eh"] = float(g_tot)
            targets_obj["gibbs_free_energy_kcal_mol"] = round(float(g_tot) * 627.50947406311, 4)
        if s_tot is not None:
            targets_obj["total_entropy_cal_mol_k"] = float(s_tot)
        if homo_val is not None:
            targets_obj["homo_ev"] = float(homo_val)
        if lumo_val is not None:
            targets_obj["lumo_ev"] = float(lumo_val)
        if gap_val is not None:
            targets_obj["homo_lumo_gap_ev"] = float(gap_val)
        if dipole_val is not None:
            targets_obj["dipole_moment_debye"] = float(dipole_val)

        cdft_dict = {}
        if cdft_en is not None:
            cdft_dict["electronegativity_ev"] = float(cdft_en)
        if cdft_hard is not None:
            cdft_dict["chemical_hardness_ev"] = float(cdft_hard)
        if cdft_pot is not None:
            cdft_dict["chemical_potential_ev"] = float(cdft_pot)
        if cdft_soft is not None:
            cdft_dict["chemical_softness_ev_inv"] = float(cdft_soft)
        if cdft_w is not None:
            cdft_dict["electrophilicity_index_ev"] = float(cdft_w)
        if cdft_dict:
            targets_obj["conceptual_dft"] = cdft_dict

        if v_modes:
            targets_obj["vibrational_frequencies_cm1"] = [float(m["frequency_cm"]) for m in v_modes]
            if any(m.get("intensity_km_mol") is not None for m in v_modes):
                targets_obj["ir_intensities_km_mol"] = [float(m.get("intensity_km_mol") or 0.0) for m in v_modes]
        targets_obj["imaginary_frequencies_count"] = imag_count
        targets_obj["is_transition_state"] = bool(clean_input.get("is_transition_state", False) or imag_count == 1)

        charges_dict = {}
        if mulliken_arr:
            charges_dict["mulliken"] = [float(x) if x is not None else 0.0 for x in mulliken_arr]
        if loewdin_arr:
            charges_dict["loewdin"] = [float(x) if x is not None else 0.0 for x in loewdin_arr]
        if hirshfeld_arr:
            charges_dict["hirshfeld"] = [float(x) if x is not None else 0.0 for x in hirshfeld_arr]
        if mayer_arr:
            charges_dict["mayer"] = [float(x) if x is not None else 0.0 for x in mayer_arr]
        if charges_dict:
            targets_obj["atomic_charges"] = charges_dict

        if uv_transitions:
            targets_obj["electronic_transitions"] = [
                {
                    "transition_index": t["state"],
                    "wavenumber_cm1": float(t["energy_cm"]),
                    "energy_ev": round(float(t["energy_cm"]) * 0.00012398419, 4),
                    "wavelength_nm": float(t["wavelength_nm"]) if t.get("wavelength_nm") is not None else None,
                    "oscillator_strength": float(t["oscillator_strength"])
                }
                for t in uv_transitions
            ]
        if not targets_obj:
            targets_obj = None

    # ML Derived Features construction (coordinates, masses, center of mass, gyration radius)
    derived_features_obj = clean_input.get("derived_features")
    if derived_features_obj is None or not isinstance(derived_features_obj, dict):
        if raw_atoms:
            atomic_numbers = [a["atomic_number"] for a in raw_atoms if a.get("atomic_number") is not None]
            elements = [a["element"] for a in raw_atoms]
            coords_list = [[float(a["coordinates"]["x"]), float(a["coordinates"]["y"]), float(a["coordinates"]["z"])] for a in raw_atoms]

            derived_features_obj = {
                "num_atoms": len(raw_atoms),
                "atomic_numbers": atomic_numbers,
                "elements": elements,
                "coordinates_angstrom": coords_list,
                "stoichiometry_formula": formula_str,
                "total_charge": int(clean_input.get("charge", 0)) if clean_input.get("charge") is not None else 0,
                "spin_multiplicity": int(clean_input.get("multiplicity", 1)) if clean_input.get("multiplicity") is not None else 1,
            }

            masses = [ELEMENT_TO_MASS.get(elem.upper(), ELEMENT_TO_MASS_BY_Z.get(z, 12.011)) for elem, z in zip(elements, atomic_numbers)]
            tot_mass = sum(masses)
            derived_features_obj["molecular_mass_amu"] = round(tot_mass, 4)

            if tot_mass > 0 and len(coords_list) == len(masses):
                cx = sum(m * c[0] for m, c in zip(masses, coords_list)) / tot_mass
                cy = sum(m * c[1] for m, c in zip(masses, coords_list)) / tot_mass
                cz = sum(m * c[2] for m, c in zip(masses, coords_list)) / tot_mass
                derived_features_obj["center_of_mass_angstrom"] = [round(cx, 5), round(cy, 5), round(cz, 5)]

                rg_sq = sum(m * ((c[0] - cx) ** 2 + (c[1] - cy) ** 2 + (c[2] - cz) ** 2) for m, c in zip(masses, coords_list)) / tot_mass
                derived_features_obj["radius_of_gyration_angstrom"] = round(math.sqrt(max(0.0, rg_sq)), 5)
        else:
            derived_features_obj = {
                "num_atoms": int(clean_input.get("atoms_count") or 0),
                "atomic_numbers": [],
                "elements": [],
                "coordinates_angstrom": [],
                "stoichiometry_formula": formula_str,
                "total_charge": int(clean_input.get("charge", 0)) if clean_input.get("charge") is not None else 0,
                "spin_multiplicity": int(clean_input.get("multiplicity", 1)) if clean_input.get("multiplicity") is not None else 1,
                "molecular_mass_amu": None,
                "center_of_mass_angstrom": None,
                "radius_of_gyration_angstrom": None,
            }

    # Refine training metadata with primary label and flags if needed
    if not clean_input.get("training_metadata"):
        primary_label = None
        if g_tot is not None:
            primary_label = float(g_tot)
        elif e_elec is not None:
            primary_label = float(e_elec)
        elif thermo_obj and thermo_obj.get("gibbs_free_energy"):
            primary_label = thermo_obj["gibbs_free_energy"].get("value")

        q_flags = []
        if geometries and len(geometries[0]["coordinates"]) > 0:
            q_flags.append("valid_geometry")
        if term_normal:
            q_flags.append("normal_termination")
        if imag_count == 0:
            q_flags.append("ground_state_minimum")
        elif imag_count == 1:
            q_flags.append("transition_state")
        if g_tot is not None:
            q_flags.append("complete_thermochemistry")
        if not q_flags:
            q_flags.append("unverified")

        training_meta = {
            "task_type": "molecular_property_prediction" if record_type in ("molecule", "analysis_record") else ("reaction_energy_prediction" if record_type in ("reaction_definition", "reaction") else "vibrational_mode_prediction"),
            "input_modality": "coordinates_and_graph" if geometries else "smiles",
            "target_modality": "quantum_properties" if (thermo_obj or targets_obj or e_elec is not None) else "spectrum",
            "label": primary_label,
            "label_type": "float" if primary_label is not None else "null",
            "quality_flags": q_flags,
            "split_hint": "train"
        }

    # Assemble canonical record
    canonical: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "dataset_version": DATASET_VERSION,
        "record_type": record_type,
        "record_id": record_id,
        "calculation_id": calc_id,
        "content_hash": "0" * 64,
        "split_group_key": split_key,
        "name": record_name,
        "created_at": clean_input.get("created_at") or now_iso,
        "updated_at": now_iso,
        "provenance": prov,
        "molecule": molecule_obj,
        "geometry": geometry_obj,
        "graph": graph_obj,
        "calculation": calculation_obj,
        "electronic_properties": electronic_props_obj,
        "vibrational_properties": vibrational_props_obj,
        "charges": charges_obj,
        "quality": data_quality_obj,
        "duplicate_status": clean_input.get("duplicate_status") or {
            "is_duplicate": False,
            "duplicate_type": None,
            "duplicate_of": None,
            "distinction_reason": "Primary canonical instance"
        },
        "source_artifacts": available_arts,
        "derived_features": derived_features_obj,
        "targets": targets_obj,
        "migration_report": {
            "mapped_fields": sorted(list(set(mapped_fields))),
            "transformed_fields": sorted(list(set(transformed_fields))),
            "omitted_fields": sorted(list(set(omitted_fields))),
            "missing_fields": sorted(list(set(missing_fields))),
            "warnings": sorted(list(set(warnings + thermo_warnings))),
            "unsupported_fields": sorted(list(set(unsupported_fields))),
        },
        "data_quality": data_quality_obj,
        "molecular_system": {
            "system_id": f"sys_{record_id[:8]}",
            "name": record_name,
            "total_charge": species_list[0]["charge"] if species_list else 0,
            "total_multiplicity": species_list[0]["multiplicity"] if species_list else 1,
            "stoichiometry_formula": formula_str,
            "atoms_count": len(raw_atoms) if raw_atoms else clean_input.get("atoms_count"),
            "fragments": fragments_list,
            "species": species_list,
        },
        "geometries": geometries,
        "reaction": reaction_obj,
        "calculations": calculations_list,
        "workflows": workflows_list,
        "analysis_results": analysis_results_obj,
        "electronic_structure": electronic_structure_obj,
        "population_analysis": population_analysis_obj,
        "thermochemistry": thermo_obj,
        "vibrational_spectroscopy": vibrational_spectroscopy_obj,
        "electronic_spectroscopy": electronic_spectroscopy_obj,
        "nmr_spectroscopy": nmr_spectroscopy_obj,
        "properties": clean_input.get("properties") or {
            "electronic": {
                "homo_ev": homo_val,
                "lumo_ev": lumo_val,
                "homo_lumo_gap_ev": gap_val,
                "dipole_moment_debye": dipole_val,
            },
            "thermodynamic": None,
            "spectroscopic": None,
            "reactivity_indices": {
                "electronegativity_ev": _extract_quantity(clean_input.get("electronegativity_ev")),
                "chemical_hardness_ev": _extract_quantity(clean_input.get("chemical_hardness_ev")),
                "electrophilicity_index_ev": _extract_quantity(clean_input.get("electrophilicity_index_ev")),
            }
        },
        "spectroscopy": legacy_spectroscopy_obj,
        "training_metadata": training_meta,
    }

    # Compute and set real deterministic content hash
    canonical["content_hash"] = compute_content_hash(canonical)

    migration_report = canonical["migration_report"]
    return canonical, migration_report
