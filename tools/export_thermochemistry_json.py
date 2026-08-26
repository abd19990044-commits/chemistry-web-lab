# -*- coding: utf-8 -*-
"""Deterministic Exporter from Canonical Scientific Schema to ThermochemistryEngine Inputs.

Pure adapter that normalizes canonical scientific records into the exact dictionary
structure expected by orca_engine.thermochemistry.ThermochemistryEngine without
performing recalculations or modifying underlying physics.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from tools.normalize_scientific_json import normalize_to_canonical_schema, is_canonical_record


def export_to_thermochemistry_input(
    canonical_record: dict[str, Any],
    species_records: Mapping[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Convert a canonical reaction or molecular record into ThermochemistryEngine inputs."""
    if is_canonical_record(canonical_record):
        norm = canonical_record
    else:
        norm = normalize_to_canonical_schema(canonical_record)
    
    rxn = norm.get("reaction") or {}
    thermo = norm.get("thermochemistry") or {}
    mol_sys = norm.get("molecular_system") or {}
    species_list = mol_sys.get("species", [])
    
    equation = rxn.get("equation", "")
    reactants_list = rxn.get("reactants", [])
    products_list = rxn.get("products", [])

    molecules_payload: dict[str, Any] = {}
    
    # 1. Add species from canonical_record's molecular_system
    for sp in species_list:
        sp_name = sp.get("name", "molecule")
        sp_clean = sp_name.strip().lower()
        
        e_elec = (thermo.get("electronic_energy", {}) or {}).get("value") if thermo.get("electronic_energy") else None
        zpe = (thermo.get("zero_point_energy", {}) or {}).get("value") if thermo.get("zero_point_energy") else ((thermo.get("zpe", {}) or {}).get("value") if thermo.get("zpe") else None)
        h_tot = (thermo.get("enthalpy", {}) or {}).get("value") if thermo.get("enthalpy") else None
        g_tot = (thermo.get("gibbs_free_energy", {}) or {}).get("value") if thermo.get("gibbs_free_energy") else None
        s_tot = (thermo.get("entropy", {}) or {}).get("value") if thermo.get("entropy") else None

        molecules_payload[sp_clean] = {
            "name": sp_name,
            "formula": sp.get("formula"),
            "charge": sp.get("charge", 0),
            "multiplicity": sp.get("multiplicity", 1),
            "e_elec_eh": e_elec,
            "zpe_eh": zpe,
            "total_enthalpy_eh": h_tot,
            "gibbs_free_energy_eh": g_tot,
            "total_entropy_cal_mol_k": s_tot,
            "temperature_k": thermo.get("temperature_k", 298.15),
            "pressure_atm": thermo.get("pressure_atm", 1.0),
            "standard_state": thermo.get("standard_state", "Gas Phase (1 atm)"),
            "stationary_point_status": thermo.get("stationary_point_status", "MINIMUM"),
            "composite_sources": thermo.get("composite_sources", {}),
        }

    # 2. Add extra species records if supplied (e.g. multi-molecule reaction dictionary)
    if species_records:
        for name_key, sp_raw in species_records.items():
            if is_canonical_record(sp_raw):
                sp_norm = sp_raw
            else:
                sp_norm = normalize_to_canonical_schema(sp_raw)
            sp_thermo = sp_norm.get("thermochemistry") or {}
            sp_sys = sp_norm.get("molecular_system") or {}
            sp_first = (sp_sys.get("species") or [{}])[0]
            sp_name = sp_norm.get("name") or name_key
            
            sp_e_elec = (sp_thermo.get("electronic_energy", {}) or {}).get("value") if sp_thermo.get("electronic_energy") else None
            sp_zpe = (sp_thermo.get("zero_point_energy", {}) or {}).get("value") if sp_thermo.get("zero_point_energy") else ((sp_thermo.get("zpe", {}) or {}).get("value") if sp_thermo.get("zpe") else None)
            sp_h_tot = (sp_thermo.get("enthalpy", {}) or {}).get("value") if sp_thermo.get("enthalpy") else None
            sp_g_tot = (sp_thermo.get("gibbs_free_energy", {}) or {}).get("value") if sp_thermo.get("gibbs_free_energy") else None
            sp_s_tot = (sp_thermo.get("entropy", {}) or {}).get("value") if sp_thermo.get("entropy") else None

            molecules_payload[name_key.strip().lower()] = {
                "name": sp_name,
                "formula": sp_first.get("formula"),
                "charge": sp_first.get("charge", 0),
                "multiplicity": sp_first.get("multiplicity", 1),
                "e_elec_eh": sp_e_elec,
                "zpe_eh": sp_zpe,
                "total_enthalpy_eh": sp_h_tot,
                "gibbs_free_energy_eh": sp_g_tot,
                "total_entropy_cal_mol_k": sp_s_tot,
                "temperature_k": sp_thermo.get("temperature_k", 298.15),
                "pressure_atm": sp_thermo.get("pressure_atm", 1.0),
                "standard_state": sp_thermo.get("standard_state", "Gas Phase (1 atm)"),
                "stationary_point_status": sp_thermo.get("stationary_point_status", "MINIMUM"),
                "composite_sources": sp_thermo.get("composite_sources", {}),
            }

    return {
        "equation": equation,
        "reactants": reactants_list,
        "products": products_list,
        "conditions": rxn.get("conditions", {
            "temperature_k": 298.15,
            "pressure_atm": 1.0,
            "standard_state": "Gas Phase (1 atm)"
        }),
        "molecules": molecules_payload,
    }
