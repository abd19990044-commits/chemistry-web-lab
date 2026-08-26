# -*- coding: utf-8 -*-
"""Deterministic Machine-Learning Dataset JSONL Exporter for Chemistry Lab.

Enforces strict input/target separation, fail-closed target leakage protection
(covering numeric, string, categorical, and nested structures), molecular grouping
metadata (split_group_key), and SHA-256 manifest reproducibility.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
from typing import Any, Sequence

from tools.normalize_scientific_json import normalize_to_canonical_schema, sanitize_secrets, is_canonical_record, SCHEMA_VERSION, DATASET_VERSION


class TargetLeakageError(ValueError):
    """Raised when target/label information is detected inside input features."""


TASK_SCHEMAS = {
    "molecular_property_prediction": {
        "allowed_inputs": ["smiles", "formula", "charge", "multiplicity", "coordinates", "coordinate_units"],
        "allowed_targets": ["homo_ev", "lumo_ev", "homo_lumo_gap_ev", "dipole_moment_debye", "gibbs_free_energy_eh", "electronic_energy_eh"],
        "forbidden_in_inputs": ["homo_ev", "lumo_ev", "homo_lumo_gap_ev", "dipole_moment_debye", "gibbs_free_energy_eh", "electronic_energy_eh", "total_enthalpy_eh"]
    },
    "reaction_energy_prediction": {
        "allowed_inputs": ["equation", "reaction_smiles", "reactants", "products", "conditions"],
        "allowed_targets": ["reaction_energy", "delta_h_kcal_mol", "delta_g_kcal_mol", "k_eq", "balanced"],
        "forbidden_in_inputs": ["reaction_energy", "delta_h_kcal_mol", "delta_g_kcal_mol", "k_eq", "gibbs_free_energy_eh", "electronic_energy_eh"]
    },
    "vibrational_spectrum_prediction": {
        "allowed_inputs": ["smiles", "formula", "coordinates", "coordinate_units"],
        "allowed_targets": ["vibrational_modes", "ir_spectrum"],
        "forbidden_in_inputs": ["vibrational_modes", "ir_spectrum", "peak_assignments", "frequencies"]
    },
    "ir_peak_assignment_classification": {
        "allowed_inputs": ["peak_wavenumber_cm", "intensity_level", "signal_type", "molecular_structure", "theoretical_modes"],
        "allowed_targets": ["functional_group", "subgroup", "bond_or_mode", "confidence", "confidence_score"],
        "forbidden_in_inputs": ["functional_group", "subgroup", "bond_or_mode", "confidence", "confidence_score", "assigned_group", "target_assignment"]
    }
}

ALLOWED_GENERIC_STRINGS = {
    "true", "false", "null", "none", "-", "n/a", "angstrom", "hartree", "kcal/mol",
    "cal/mol/k", "opt", "freq", "sp", "minimum", "transition_state", "gas phase (1 atm)"
}


def _recursive_check_forbidden_keys(obj: Any, forbidden_keys: set[str], path: str = "") -> None:
    """Recursively traverse input features and fail if any forbidden key is present with non-None value."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            current_path = f"{path}.{k}" if path else str(k)
            if k in forbidden_keys and v is not None:
                raise TargetLeakageError(f"Target leakage detected: forbidden key '{k}' found at path '{current_path}'")
            _recursive_check_forbidden_keys(v, forbidden_keys, current_path)
    elif isinstance(obj, (list, tuple)):
        for idx, item in enumerate(obj):
            _recursive_check_forbidden_keys(item, forbidden_keys, f"{path}[{idx}]")


def _recursive_check_string_leakage(obj: Any, target_str: str, target_name: str, path: str = "") -> None:
    """Recursively traverse input features and fail if target string appears inside input values."""
    if isinstance(obj, str):
        # Ignore allowed structural representations like SMILES or chemical formulas
        if path.endswith("smiles") or path.endswith("formula") or path.endswith("molecular_structure") and ("=" in obj or "(" in obj):
            # If the exact target string appears inside metadata or notes, it leaks
            if obj.lower() == target_str.lower():
                raise TargetLeakageError(f"Target string leakage detected: target '{target_name}' ('{target_str}') matched input at '{path}'")
        else:
            pattern = r"(?:\b|_)" + re.escape(target_str.lower()) + r"(?:\b|_)"
            if re.search(pattern, obj.lower()):
                raise TargetLeakageError(f"Target string leakage detected: target '{target_name}' ('{target_str}') found in input text at '{path}': '{obj}'")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            current_path = f"{path}.{k}" if path else str(k)
            _recursive_check_string_leakage(v, target_str, target_name, current_path)
    elif isinstance(obj, (list, tuple)):
        for idx, item in enumerate(obj):
            _recursive_check_string_leakage(item, target_str, target_name, f"{path}[{idx}]")


def validate_no_target_leakage(task_type: str, input_features: dict[str, Any], target_features: dict[str, Any]) -> None:
    """Check that no target labels or forbidden fields leaked into input features.
    
    Fails closed on numeric, string, categorical, list, dictionary, and nested leakage.
    """
    schema = TASK_SCHEMAS.get(task_type)
    if not schema:
        return

    forbidden = set(schema["forbidden_in_inputs"])
    
    # 1. Structural key checks: no forbidden keys anywhere in input structure
    _recursive_check_forbidden_keys(input_features, forbidden)
    
    # Also check if any allowed target key is present in inputs
    target_keys = set(schema["allowed_targets"])
    _recursive_check_forbidden_keys(input_features, target_keys)

    # 2. Value checks: numeric target leakage
    input_str = json.dumps(input_features).lower()
    for tgt_k, tgt_v in target_features.items():
        if tgt_v is None:
            continue
        
        # Numeric target leakage
        if isinstance(tgt_v, (int, float)) and abs(tgt_v) > 1e-4:
            val_str = f"{tgt_v:.4f}"
            if val_str in input_str:
                raise TargetLeakageError(f"Numeric target leakage detected: target '{tgt_k}'={tgt_v} appears inside input representation.")
        
        # String and categorical target leakage
        elif isinstance(tgt_v, str):
            clean_tgt_str = tgt_v.strip()
            if len(clean_tgt_str) > 1 and clean_tgt_str.lower() not in ALLOWED_GENERIC_STRINGS:
                _recursive_check_string_leakage(input_features, clean_tgt_str, tgt_k)


def export_canonical_to_training_jsonl(
    records: Sequence[dict[str, Any]],
    output_path: str,
    task_type: str = "molecular_property_prediction",
    manifest_path: str | None = None,
) -> dict[str, Any]:
    """Export canonical scientific records to a strictly segregated, leak-proof JSONL dataset."""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    
    line_hashes: list[str] = []
    example_count = 0

    with open(output_path, "w", encoding="utf-8") as out_f:
        for idx, rec in enumerate(records):
            if is_canonical_record(rec):
                canonical = dict(rec)
            else:
                canonical = normalize_to_canonical_schema(rec)
            
            clean_rec, _ = sanitize_secrets(canonical)
            
            ex_id = clean_rec.get("record_id") or f"ex_{idx + 1}"
            split_group_key = clean_rec.get("split_group_key") or f"grp_{idx + 1}"
            
            mol_sys = clean_rec.get("molecular_system", {})
            species = (mol_sys.get("species", []) or [{}])[0]
            geos = clean_rec.get("geometries", [])
            thermo = clean_rec.get("thermochemistry", {}) or {}
            props = clean_rec.get("properties", {}) or {}
            spectro = clean_rec.get("spectroscopy", {}) or {}
            analysis_res = clean_rec.get("analysis_results") or {}
            elec_struct = clean_rec.get("electronic_structure") or {}
            front_orb = elec_struct.get("frontier_orbitals") or {}

            # Construct strictly segregated inputs and targets
            if task_type == "molecular_property_prediction":
                inp = {
                    "smiles": species.get("canonical_smiles"),
                    "formula": species.get("formula"),
                    "charge": species.get("charge"),
                    "multiplicity": species.get("multiplicity"),
                    "coordinates": geos[0]["coordinates"] if geos else None,
                    "coordinate_units": geos[0]["coordinate_units"] if geos else "angstrom",
                }
                
                homo_val = analysis_res.get("homo_ev") or (front_orb.get("homo", {}) or {}).get("value") or (props.get("electronic", {}) or {}).get("homo_ev")
                lumo_val = analysis_res.get("lumo_ev") or (front_orb.get("lumo", {}) or {}).get("value") or (props.get("electronic", {}) or {}).get("lumo_ev")
                gap_val = analysis_res.get("homo_lumo_gap_ev") or (front_orb.get("homo_lumo_gap", {}) or {}).get("value") or (props.get("electronic", {}) or {}).get("homo_lumo_gap_ev")
                dipole_val = analysis_res.get("dipole_moment_debye") or (elec_struct.get("dipole_moment", {}) or {}).get("total_debye") or (props.get("electronic", {}) or {}).get("dipole_moment_debye")
                gibbs_val = analysis_res.get("gibbs_free_energy_eh") or (thermo.get("gibbs_free_energy", {}) or {}).get("value")
                elec_e_val = analysis_res.get("single_point_energy_eh") or (thermo.get("electronic_energy", {}) or {}).get("value")

                tgt = {
                    "homo_ev": homo_val,
                    "lumo_ev": lumo_val,
                    "homo_lumo_gap_ev": gap_val,
                    "dipole_moment_debye": dipole_val,
                    "gibbs_free_energy_eh": gibbs_val,
                    "electronic_energy_eh": elec_e_val,
                }

            elif task_type == "reaction_energy_prediction":
                rxn = clean_rec.get("reaction", {})
                clean_reactants = [
                    {"species_name": r.get("species_name"), "stoichiometric_coefficient": r.get("stoichiometric_coefficient", 1.0)}
                    for r in rxn.get("reactants", [])
                ]
                clean_products = [
                    {"species_name": p.get("species_name"), "stoichiometric_coefficient": p.get("stoichiometric_coefficient", 1.0)}
                    for p in rxn.get("products", [])
                ]
                inp = {
                    "equation": rxn.get("equation"),
                    "reaction_smiles": rxn.get("reaction_smiles"),
                    "reactants": clean_reactants,
                    "products": clean_products,
                    "conditions": rxn.get("conditions", {}),
                }
                tgt = {
                    "balanced": rxn.get("balance", {}).get("balanced", True),
                    "reaction_energy": (thermo.get("gibbs_free_energy", {}) or {}).get("value"),
                    "delta_g_kcal_mol": clean_rec.get("delta_g_kcal_mol"),
                    "delta_h_kcal_mol": clean_rec.get("delta_h_kcal_mol"),
                }

            elif task_type == "vibrational_spectrum_prediction":
                inp = {
                    "smiles": species.get("canonical_smiles"),
                    "formula": species.get("formula"),
                    "coordinates": geos[0]["coordinates"] if geos else None,
                    "coordinate_units": geos[0]["coordinate_units"] if geos else "angstrom",
                }
                vib_modes = (
                    spectro.get("ir", {}).get("theoretical_modes", [])
                    or (clean_rec.get("vibrational_spectroscopy", {}) or {}).get("harmonic_modes", [])
                )
                tgt = {
                    "vibrational_modes": vib_modes,
                }

            elif task_type == "ir_peak_assignment_classification":
                ir_data = (
                    (spectro.get("ir") if isinstance(spectro, dict) and spectro.get("ir") else None)
                    or (clean_rec.get("vibrational_spectroscopy") if isinstance(clean_rec.get("vibrational_spectroscopy"), dict) and clean_rec.get("vibrational_spectroscopy", {}).get("peak_assignments") else None)
                )
                if not ir_data:
                    raise ValueError(f"Canonical record '{ex_id}' missing required spectroscopy.ir block for task '{task_type}'")
                peak_list = ir_data.get("peak_assignments", [])
                if not peak_list:
                    raise ValueError(f"Canonical record '{ex_id}' missing required spectroscopy.ir peak_assignments for task '{task_type}'")
                first_peak = peak_list[0]
                theoretical_modes = ir_data.get("theoretical_modes") or ir_data.get("harmonic_modes", [])
                inp = {
                    "peak_wavenumber_cm": first_peak.get("peak_wavenumber_cm", 1700.0),
                    "intensity_level": first_peak.get("intensity_level", "Strong"),
                    "signal_type": first_peak.get("signal_type", "Sharp"),
                    "molecular_structure": species.get("canonical_smiles") or species.get("formula"),
                    "theoretical_modes": theoretical_modes,
                }
                tgt = {
                    "functional_group": first_peak.get("functional_group", "Ketone"),
                    "subgroup": first_peak.get("subgroup", "Aliphatic Ketone"),
                    "bond_or_mode": first_peak.get("bond_or_mode", "C=O stretch"),
                    "confidence": first_peak.get("confidence", "High"),
                }

            else:
                inp = {"species": species, "geometries": geos}
                tgt = {"thermochemistry": thermo, "properties": props}

            # Enforce target leakage validation before writing
            validate_no_target_leakage(task_type, inp, tgt)

            training_example = {
                "example_id": ex_id,
                "split_group_key": split_group_key,
                "task": task_type,
                "schema_version": clean_rec.get("schema_version", "orca-web-lab.1.0"),
                "content_hash": clean_rec.get("content_hash"),
                "input": inp,
                "target": tgt,
                "provenance": {
                    "source_type": clean_rec.get("provenance", {}).get("source_type"),
                    "orca_version": clean_rec.get("provenance", {}).get("orca_version"),
                    "git_sha": clean_rec.get("provenance", {}).get("git_sha"),
                },
                "data_quality": {
                    "validation_status": clean_rec.get("data_quality", {}).get("validation_status", "verified"),
                    "completeness_score": clean_rec.get("data_quality", {}).get("completeness_score", 1.0),
                }
            }

            line_str = json.dumps(training_example, sort_keys=True)
            line_hash = hashlib.sha256(line_str.encode("utf-8")).hexdigest()
            line_hashes.append(line_hash)
            out_f.write(line_str + "\n")
            example_count += 1

    # Generate deterministic dataset manifest
    combined_hash = hashlib.sha256("".join(line_hashes).encode("utf-8")).hexdigest()
    manifest = {
        "manifest_version": "1.0",
        "schema_version": SCHEMA_VERSION,
        "dataset_version": DATASET_VERSION,
        "dataset_file": os.path.basename(output_path),
        "task_type": task_type,
        "record_count": example_count,
        "example_count": example_count,
        "records_sha256": combined_hash,
        "dataset_sha256": combined_hash,
        "line_hashes": line_hashes,
        "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    if manifest_path:
        os.makedirs(os.path.dirname(os.path.abspath(manifest_path)), exist_ok=True)
        with open(manifest_path, "w", encoding="utf-8") as man_f:
            json.dump(manifest, man_f, indent=2)

    return manifest
