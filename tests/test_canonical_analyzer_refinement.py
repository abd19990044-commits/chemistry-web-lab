# -*- coding: utf-8 -*-
"""Comprehensive verification suite for Real Analyzer JSON to Canonical Scientific Data Model Refinement.

Validates:
1. Lossless normalization of real ORCA analyzer output (ORCA_Parsed_Data.json)
2. Duplicate energy alias consolidation into unified canonical fields
3. ZPE vs E0 semantic distinction and numerical validation (E0 = E_el + ZPE)
4. Raw vs derived spectral separation (discrete modes vs continuous convoluted curves)
5. Explicit 'not_calculated' and 'not_applicable' status semantics
6. Population analysis separation (Mulliken, Loewdin, Hirshfeld, Mayer)
7. Conceptual DFT reactivity indices preservation
8. Artifact provenance references without raw binary embedding
9. Idempotent canonical normalization and deterministic content hashing
10. Machine-readable scientific numerical comparison and schema validation
"""

import copy
import json
import math
import os
import tempfile
import pytest
from jsonschema import validate, Draft202012Validator

from tools.normalize_scientific_json import (
    normalize_to_canonical_schema,
    normalize_with_report,
    sanitize_secrets,
    compute_content_hash,
    compute_geometry_hash,
    compute_chemical_formula,
    is_canonical_record,
    SCHEMA_VERSION,
    DATASET_VERSION,
)
from tools.export_training_jsonl import (
    export_canonical_to_training_jsonl,
    validate_no_target_leakage,
    TargetLeakageError,
)
from tools.export_thermochemistry_json import export_to_thermochemistry_input
from orca_engine.thermochemistry import ThermochemistryEngine


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def canonical_schema():
    schema_path = os.path.join(REPO_ROOT, "schema", "canonical_scientific.schema.json")
    with open(schema_path, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def real_analyzer_data():
    data_path = os.path.join(REPO_ROOT, "orca_engine", "ORCA_Parsed_Data.json")
    with open(data_path, "r", encoding="utf-8") as f:
        return json.load(f)


class TestRealAnalyzerJsonNormalization:
    """Phase 1, 18, 25: Real Analyzer JSON ingestion and lossless transformation."""

    def test_all_14_real_molecules_normalize_and_validate(self, real_analyzer_data, canonical_schema):
        molecules = real_analyzer_data["molecules"]
        assert len(molecules) >= 14

        for mol_name, mol_dict in molecules.items():
            payload = {
                "name": mol_name,
                "sources": mol_dict.get("sources", []),
                "had_error_termination": mol_dict.get("had_error_termination", False),
                **mol_dict["jobs"][-1]
            }
            canonical, report = normalize_with_report(payload)
            validate(instance=canonical, schema=canonical_schema)

            assert canonical["record_type"] == "analysis_record"
            assert canonical["schema_version"] == SCHEMA_VERSION
            assert len(canonical["content_hash"]) == 64
            assert len(report["mapped_fields"]) > 0

    def test_full_analyzer_bundle_normalization(self, real_analyzer_data, canonical_schema):
        canonical, report = normalize_with_report(real_analyzer_data)
        validate(instance=canonical, schema=canonical_schema)
        assert canonical["record_type"] == "analysis_record"
        assert canonical["molecular_system"]["species"][0]["name"] in real_analyzer_data["molecules"]



class TestEnergyConsolidationAndZPESemantics:
    """Phase 2, 3, 12, 13: Consolidation of energy aliases and ZPE vs E0 distinction."""

    def test_duplicate_electronic_energy_aliases_collapse_to_one(self, canonical_schema):
        raw_aliases = {
            "name": "EnergyAliasTest",
            "e_elec_eh": -767.536960665877,
            "electronic_energy_hartree": -767.536960665877,
            "total_energy_eh": -767.536960665877,
            "scf_energy_hartree": -767.536960665877,
            "zpe_eh": 0.24998355,
            "zero_point_energy_hartree": 0.24998355,
            "zpe_hartree": 0.24998355,
            "electronic_zpe_eh": -767.286977115877,
            "total_enthalpy_eh": -767.27076277,
            "enthalpy_hartree": -767.27076277,
            "final_enthalpy_hartree": -767.27076277,
            "gibbs_free_energy_eh": -767.32833861,
            "gibbs_free_energy_hartree": -767.32833861,
            "final_gibbs_hartree": -767.32833861,
        }
        canonical, _ = normalize_with_report(raw_aliases)
        validate(instance=canonical, schema=canonical_schema)

        thermo = canonical["thermochemistry"]
        # Canonical quantities
        assert thermo["electronic_energy"]["value"] == -767.536960665877
        assert thermo["electronic_energy"]["unit"] == "hartree"
        assert thermo["zero_point_energy"]["value"] == 0.24998355
        assert thermo["zero_point_energy"]["unit"] == "hartree"
        assert thermo["enthalpy"]["value"] == -767.27076277
        assert thermo["gibbs_free_energy"]["value"] == -767.32833861

    def test_zpe_vs_e0_semantics_and_numerical_relation(self, canonical_schema):
        raw = {
            "name": "Naproxen",
            "e_elec_eh": -767.536960665877,
            "zpe_eh": 0.24998355,
            "electronic_zpe_eh": -767.286977115877,
            "total_enthalpy_eh": -767.27076277,
            "entropy_term_ts_eh": 0.05757584,
            "gibbs_free_energy_eh": -767.32833861,
        }
        canonical, _ = normalize_with_report(raw)
        validate(instance=canonical, schema=canonical_schema)

        thermo = canonical["thermochemistry"]
        # E0 is zero_point_corrected_energy, NOT zero_point_energy
        e0_obj = thermo["zero_point_corrected_energy"]
        assert e0_obj["value"] == -767.286977115877
        assert e0_obj["unit"] == "hartree"
        assert e0_obj["formula"] == "E_electronic + ZPE"
        assert e0_obj["components"]["electronic_energy"] == -767.536960665877
        assert e0_obj["components"]["zero_point_energy"] == 0.24998355

        # Numerical relation test: E0 = E_elec + ZPE
        expected_e0 = raw["e_elec_eh"] + raw["zpe_eh"]
        assert math.isclose(e0_obj["value"], expected_e0, abs_tol=1e-8)
        assert thermo["numerical_consistency"]["e0_relation_valid"] is True
        assert thermo["numerical_consistency"]["e0_delta_eh"] < 1e-6

        # Gibbs relation test: G = H - TS
        expected_g = raw["total_enthalpy_eh"] - raw["entropy_term_ts_eh"]
        assert math.isclose(thermo["gibbs_free_energy"]["value"], expected_g, abs_tol=1e-6)
        assert thermo["numerical_consistency"]["gibbs_relation_valid"] is True


class TestRawVsDerivedDataSeparation:
    """Phase 4, 5: Raw harmonic modes vs Lorentzian/Gaussian derived spectra."""

    def test_vibrational_raw_modes_separated_from_convoluted_spectrum(self, canonical_schema):
        raw = {
            "name": "Water_Freq",
            "vibrational_frequencies_cm": [1595.0, 3657.0, 3756.0],
            "ir_intensities_km_mol": [65.2, 3.4, 45.8],
            "convoluted_ir_spectrum": [
                {"wavenumber_cm": 400.0, "absorbance": 0.0, "absorbance_norm": 0.0, "transmittance_pct": 100.0},
                {"wavenumber_cm": 1595.0, "absorbance": 1.38, "absorbance_norm": 1.0, "transmittance_pct": 10.0},
                {"wavenumber_cm": 4000.0, "absorbance": 0.0, "absorbance_norm": 0.0, "transmittance_pct": 100.0},
            ]
        }
        canonical, _ = normalize_with_report(raw)
        validate(instance=canonical, schema=canonical_schema)

        vib = canonical["vibrational_spectroscopy"]
        assert vib["status"] == "calculated"
        assert len(vib["harmonic_modes"]) == 3
        assert vib["harmonic_modes"][0]["frequency_cm"] == 1595.0
        assert vib["harmonic_modes"][0]["intensity_km_mol"] == 65.2

        # Derived spectrum is explicitly labeled
        derived = vib["derived_spectrum"]
        assert derived["spectrum_type"] == "ir"
        assert derived["broadening_model"] == "lorentzian"
        assert derived["x_unit"] == "cm^-1"
        assert len(derived["data_points"]) == 3

    def test_uvvis_transitions_separated_from_convoluted_spectrum(self, canonical_schema):
        raw = {
            "name": "Dye_UV",
            "tddft_cm": [25000.0, 32000.0],
            "tddft_fosc": [0.45, 0.12],
            "uvvis_spectrum": [
                {"wavelength_nm": 300.0, "intensity": 0.05},
                {"wavelength_nm": 400.0, "intensity": 0.85},
            ]
        }
        canonical, _ = normalize_with_report(raw)
        validate(instance=canonical, schema=canonical_schema)

        uv = canonical["electronic_spectroscopy"]
        assert uv["status"] == "calculated"
        assert uv["transitions_count"] == 2
        assert uv["transitions"][0]["state"] == 1
        assert uv["transitions"][0]["wavelength_nm"] == 400.0
        assert uv["transitions"][0]["oscillator_strength"] == 0.45

        derived = uv["derived_spectrum"]
        assert derived["spectrum_type"] == "uv_vis"
        assert derived["broadening_model"] == "gaussian"
        assert derived["x_unit"] == "nm"
        assert len(derived["data_points"]) == 2


class TestExplicitAvailabilityAndPopulationAnalysis:
    """Phase 7, 8, 9: Explicit not_calculated metadata and population analyses."""

    def test_not_calculated_subsystems_have_explicit_status(self, canonical_schema):
        raw_sp = {
            "name": "SP_Only",
            "e_elec_eh": -100.5,
            "homo_ev": -7.2,
            "lumo_ev": -1.1
        }
        canonical, _ = normalize_with_report(raw_sp)
        validate(instance=canonical, schema=canonical_schema)

        # Uncalculated subsystems must not be fake 0 or empty strings
        assert canonical["electronic_spectroscopy"]["status"] == "not_calculated"
        assert canonical["electronic_spectroscopy"]["transitions"] == []
        assert canonical["nmr_spectroscopy"]["status"] == "not_calculated"
        assert canonical["population_analysis"]["status"] == "not_calculated"

    def test_population_analyses_methods_strictly_separated(self, canonical_schema):
        raw = {
            "name": "Charges_Test",
            "coords": [{"elem": "C", "x": 0, "y": 0, "z": 0}, {"elem": "O", "x": 0, "y": 0, "z": 1.2}],
            "mulliken_charges": [0.45, -0.45],
            "loewdin_charges": [0.30, -0.30],
            "hirshfeld_charges": [0.25, -0.25],
            "mayer_charges": [0.40, -0.40],
            "mayer_valences": [3.8, 1.9],
        }
        canonical, _ = normalize_with_report(raw)
        validate(instance=canonical, schema=canonical_schema)

        pop = canonical["population_analysis"]
        assert pop["status"] == "calculated"
        assert pop["methods"]["mulliken"]["charges"] == [0.45, -0.45]
        assert pop["methods"]["loewdin"]["charges"] == [0.30, -0.30]
        assert pop["methods"]["hirshfeld"]["charges"] == [0.25, -0.25]
        assert pop["methods"]["mayer"]["charges"] == [0.40, -0.40]
        assert pop["methods"]["mayer"]["valences"] == [3.8, 1.9]

        # Individual atoms also retain separated charge types
        atoms = canonical["molecular_system"]["species"][0]["atoms"]
        assert atoms[0]["atomic_charges"]["mulliken"] == 0.45
        assert atoms[0]["atomic_charges"]["loewdin"] == 0.30
        assert atoms[0]["atomic_charges"]["hirshfeld"] == 0.25
        assert atoms[0]["atomic_charges"]["mayer"] == 0.40
        assert atoms[0]["atomic_charges"]["mayer_valence"] == 3.8

    def test_conceptual_dft_reactivity_indices_preserved(self, canonical_schema):
        raw_cdft = {
            "name": "Naproxen",
            "sources": ["nap.out"],
            "homo_ev": -5.6987,
            "lumo_ev": -1.2146,
            "electronegativity_ev": 3.45665,
            "chemical_hardness_ev": 2.24205,
            "chemical_potential_ev": -3.45665,
            "chemical_softness_ev": 0.22301019156575458,
            "electrophilicity_index_ev": 2.6646214898195844,
            "electrodonating_power_ev": 4.673202739819585,
            "electroaccepting_power_ev": 1.2165527398195848,
            "net_electrophilicity_ev": 5.88975547963917,
        }
        canonical, _ = normalize_with_report(raw_cdft)
        validate(instance=canonical, schema=canonical_schema)

        cdft = canonical["electronic_structure"]["conceptual_dft"]
        assert cdft["electronegativity"]["value"] == 3.45665
        assert cdft["chemical_hardness"]["value"] == 2.24205
        assert cdft["chemical_potential"]["value"] == -3.45665
        assert cdft["chemical_softness"]["value"] == 0.22301019156575458
        assert cdft["electrophilicity_index"]["value"] == 2.6646214898195844
        assert cdft["electrodonating_power"]["value"] == 4.673202739819585
        assert cdft["electroaccepting_power"]["value"] == 1.2165527398195848
        assert cdft["net_electrophilicity"]["value"] == 5.88975547963917


class TestArtifactProvenanceAndQuality:
    """Phase 6, 14, 15: Artifact provenance references and diagnostics."""

    def test_source_artifacts_referenced_by_metadata(self, canonical_schema):
        raw = {
            "name": "Nap_Calc",
            "sources": ["data/orcafile/orca6.1/nap.out", "data/orcafile/orca6.1/nap.inp"],
            "orca_version": "6.1.0",
            "e_elec_eh": -767.53
        }
        canonical, _ = normalize_with_report(raw)
        validate(instance=canonical, schema=canonical_schema)

        arts = canonical["provenance"]["source_artifacts"]
        assert len(arts) == 2
        assert arts[0]["filename"] == "nap.out"
        assert arts[0]["artifact_type"] == "orca_output"
        assert arts[1]["filename"] == "nap.inp"
        assert arts[1]["artifact_type"] == "orca_input"

    def test_data_quality_diagnostics_preserved(self, canonical_schema):
        raw_ts = {
            "name": "NapBrBr_TS",
            "calculation_status": "COMPLETED",
            "termination_status": "NORMAL",
            "termination_message": "****ORCA TERMINATED NORMALLY****",
            "had_error_termination": False,
            "is_transition_state": True,
            "stationary_point_status": "TRANSITION_STATE",
        }
        canonical, _ = normalize_with_report(raw_ts)
        validate(instance=canonical, schema=canonical_schema)

        dq = canonical["data_quality"]
        assert dq["calculation_status"] == "COMPLETED"
        assert dq["termination_status"] == "NORMAL"
        assert dq["termination_message"] == "****ORCA TERMINATED NORMALLY****"
        assert dq["had_error_termination"] is False
        assert dq["is_transition_state"] is True
        assert dq["stationary_point_status"] == "TRANSITION_STATE"



class TestIdempotencyAndNumericalComparison:
    """Phase 18, 25: Idempotent normalization and lossless numerical integrity."""

    def test_canonical_normalization_is_strictly_idempotent(self, real_analyzer_data):
        for mol_name, mol_dict in real_analyzer_data["molecules"].items():
            payload = {
                "name": mol_name,
                "sources": mol_dict.get("sources", []),
                "had_error_termination": mol_dict.get("had_error_termination", False),
                **mol_dict["jobs"][-1]
            }
            c1, r1 = normalize_with_report(payload)
            c2, r2 = normalize_with_report(c1)

            assert c1["content_hash"] == c2["content_hash"]
            assert c1["record_id"] == c2["record_id"]
            assert c1["schema_version"] == c2["schema_version"]
            if c1.get("thermochemistry"):
                assert c1["thermochemistry"]["electronic_energy"] == c2["thermochemistry"]["electronic_energy"]
                assert c1["thermochemistry"]["zero_point_corrected_energy"] == c2["thermochemistry"]["zero_point_corrected_energy"]

    def test_numerical_data_integrity_comparison_source_vs_canonical(self, real_analyzer_data):
        """Phase 25: Machine-readable comparison table source vs canonical."""
        comparison_results = []
        for mol_name, mol_dict in real_analyzer_data["molecules"].items():
            src_job = mol_dict["jobs"][-1]
            c_mol, _ = normalize_with_report({
                "name": mol_name,
                **src_job
            })

            # Check electronic energy
            if src_job.get("e_elec_eh") is not None:
                src_val = float(src_job["e_elec_eh"])
                can_val = c_mol["thermochemistry"]["electronic_energy"]["value"]
                abs_diff = abs(src_val - can_val)
                assert abs_diff == 0.0, f"Precision loss in e_elec for {mol_name}"
                comparison_results.append({
                    "molecule": mol_name,
                    "field": "e_elec_eh",
                    "source_value": src_val,
                    "canonical_value": can_val,
                    "abs_diff": abs_diff,
                    "status": "EXACT"
                })

            # Check ZPE
            if src_job.get("zpe_eh") is not None:
                src_val = float(src_job["zpe_eh"])
                can_val = c_mol["thermochemistry"]["zero_point_energy"]["value"]
                abs_diff = abs(src_val - can_val)
                assert abs_diff == 0.0, f"Precision loss in zpe for {mol_name}"
                comparison_results.append({
                    "molecule": mol_name,
                    "field": "zpe_eh",
                    "source_value": src_val,
                    "canonical_value": can_val,
                    "abs_diff": abs_diff,
                    "status": "EXACT"
                })

            # Check HOMO
            if src_job.get("homo_ev") is not None:
                src_val = float(src_job["homo_ev"])
                can_val = c_mol["electronic_structure"]["frontier_orbitals"]["homo"]["value"]
                abs_diff = abs(src_val - can_val)
                assert abs_diff == 0.0, f"Precision loss in homo_ev for {mol_name}"
                comparison_results.append({
                    "molecule": mol_name,
                    "field": "homo_ev",
                    "source_value": src_val,
                    "canonical_value": can_val,
                    "abs_diff": abs_diff,
                    "status": "EXACT"
                })

        assert len(comparison_results) > 20
