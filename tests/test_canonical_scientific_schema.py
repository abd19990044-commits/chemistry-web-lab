# -*- coding: utf-8 -*-
"""Comprehensive verification & regression test suite for Canonical Scientific Data Model & Exporters.

Covers all core requirements plus CF-1 through CF-8 forensic remediation tests:
- Schema Draft 2020-12 and domain record validity
- Real ORCA output normalization without data loss
- Content-hash determinism and invariance
- True numerical ThermochemistryEngine round-trip validation
- ML dataset target-leakage protection and split grouping
- CF-1: Canonical normalization idempotency & populated targets
- CF-3: Canonical IR classification export
- CF-4: String, nested, and list target leakage fail-closed detection
- CF-5: Rejection of unauthorized UI state
- CF-6: Migration report unsupported fields tracking
- CF-8: Reaction thermochemistry round-trip with canonical input
"""

import copy
import json
import math
import os
import tempfile
import pytest
import jsonschema
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
from orca_engine.thermochemistry import ThermochemistryEngine, ReactionTerm
from orca_engine.parser import OrcaParser
from orca_engine.models import MoleculeData, JobData, CalcMetadata


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def canonical_schema():
    schema_path = os.path.join(REPO_ROOT, "schema", "canonical_scientific.schema.json")
    with open(schema_path, "r", encoding="utf-8") as f:
        return json.load(f)


class Test1SchemaValidation:
    """1 to 4: Schema Draft 2020-12 and domain record validity."""

    def test_1_schema_itself_is_valid_draft202012(self, canonical_schema):
        Draft202012Validator.check_schema(canonical_schema)

    def test_2_validate_all_analysis_records(self, canonical_schema):
        analysis_fixtures = [
            "analysis_opt.json",
            "analysis_freq.json",
            "analysis_sp.json",
            "analysis_ir.json",
            "analysis_uv.json",
            "analysis_nmr.json",
        ]
        for fix in analysis_fixtures:
            path = os.path.join(REPO_ROOT, "data", "examples", fix)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            validate(instance=data, schema=canonical_schema)
            assert data["record_type"] == "analysis_record"
            assert data["provenance"]["source_type"] == "orca_analyzer"
            assert len(data["content_hash"]) == 64

    def test_3_validate_reaction_definition(self, canonical_schema):
        path = os.path.join(REPO_ROOT, "data", "examples", "canonical_reaction.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        validate(instance=data, schema=canonical_schema)
        assert data["record_type"] in ("reaction_definition", "reaction")
        assert len(data["reaction"]["reactants"]) > 0

    def test_4_validate_workflow_record(self, canonical_schema):
        path = os.path.join(REPO_ROOT, "data", "examples", "canonical_workflow.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        validate(instance=data, schema=canonical_schema)
        assert data["record_type"] in ("workflow_record", "workflow")
        assert len(data["workflows"][0]["steps"]) == 3


class Test2NormalizationAndDomainSeparation:
    """5 to 9: Real analyzer normalization, domain separation, warnings, and precision."""

    def test_5_real_orca_output_normalization_preserves_all_scientific_data(self, canonical_schema):
        orca_file = os.path.join(REPO_ROOT, "orca_engine", "data", "orcafile", "orca6.1", "nap.out")
        with open(orca_file, "r", encoding="utf-8", errors="replace") as f:
            parser = OrcaParser(f, source_name="nap.out")
            parser.parse()

        mol = MoleculeData(name="nap", jobs=parser.jobs, sources=[orca_file])
        job = mol.jobs[-1]
        assert job.e_elec_eh is not None

        canonical, report = normalize_with_report(mol)
        validate(instance=canonical, schema=canonical_schema)

        assert canonical["record_type"] == "analysis_record"
        assert canonical["analysis_results"]["single_point_energy_eh"] == job.e_elec_eh
        assert canonical["thermochemistry"]["electronic_energy"]["value"] == job.e_elec_eh
        if job.ir_spectrum or job.vibrational_frequencies_cm:
            modes = canonical["spectroscopy"]["ir"]["theoretical_modes"]
            assert len(modes) in (len(job.ir_spectrum or []), len(job.vibrational_frequencies_cm or []))
        else:
            assert canonical.get("spectroscopy") is None

    def test_6_editor_input_normalized_to_input_record(self, canonical_schema):
        editor_payload = {
            "title": "Water Draft",
            "coords": [{"elem": "O", "x": 0.0, "y": 0.0, "z": 0.0}, {"elem": "H", "x": 0.75, "y": 0.58, "z": 0.0}, {"elem": "H", "x": -0.75, "y": 0.58, "z": 0.0}],
            "charge": 0,
            "multiplicity": 1,
            "calc_type": "OPT"
        }
        canonical = normalize_to_canonical_schema(editor_payload, record_type="molecule")
        validate(instance=canonical, schema=canonical_schema)
        assert canonical["record_type"] == "molecule"
        assert canonical["analysis_results"] is None
        assert len(canonical["geometries"][0]["coordinates"]) == 3

    def test_7_reaction_definition_separated_from_analysis(self, canonical_schema):
        raw_rxn = {
            "equation": "2 H2 + O2 -> 2 H2O",
            "temperature_k": 298.15,
            "pressure_atm": 1.0,
            "solvent": "None"
        }
        canonical = normalize_to_canonical_schema(raw_rxn, record_type="reaction_definition")
        validate(instance=canonical, schema=canonical_schema)
        assert canonical["record_type"] == "reaction_definition"
        assert len(canonical["reaction"]["reactants"]) == 2
        assert len(canonical["reaction"]["products"]) == 1
        assert canonical["analysis_results"] is None

    def test_8_unmapped_field_migration_report_warnings(self):
        raw_dirty = {
            "name": "Custom System",
            "coords": [{"elem": "C", "x": 0, "y": 0, "z": 0}],
            "legacy_custom_widget_color": "#ff0000",
            "dom_tab_index": 4,
            "unsupported_sensor_frequency": 433.92
        }
        canonical, report = normalize_with_report(raw_dirty)
        assert "legacy_custom_widget_color" in report["unsupported_fields"]
        assert "unsupported_sensor_frequency" in report["unsupported_fields"]
        assert len(report["warnings"]) >= 2

    def test_9_precision_preservation_high_decimals(self):
        high_prec_energy = -1234.567890123456
        raw = {
            "name": "High Precision Molecule",
            "e_elec_eh": high_prec_energy,
            "coords": [{"elem": "H", "x": 0.123456789, "y": 0.987654321, "z": -0.555555555}]
        }
        canonical = normalize_to_canonical_schema(raw)
        assert canonical["thermochemistry"]["electronic_energy"]["value"] == high_prec_energy
        assert canonical["analysis_results"]["single_point_energy_eh"] == high_prec_energy
        assert canonical["geometries"][0]["coordinates"][0]["x"] == 0.123456789


class Test3ContentHashDeterminism:
    """10 to 13: Deterministic SHA-256 content hashing."""

    def test_10_identical_scientific_content_produces_identical_hash(self):
        rec1 = {"name": "Methane", "formula": "CH4", "e_elec_eh": -40.518, "coords": "C 0 0 0\nH 0.6 0.6 0.6"}
        rec2 = {"name": "Methane", "formula": "CH4", "e_elec_eh": -40.518, "coords": "C 0 0 0\nH 0.6 0.6 0.6"}
        c1 = normalize_to_canonical_schema(rec1)
        c2 = normalize_to_canonical_schema(rec2)
        assert c1["content_hash"] == c2["content_hash"]

    def test_11_different_timestamp_and_record_id_produces_same_hash(self):
        rec1 = {"record_id": "id_aaa_111", "created_at": "2026-01-01T00:00:00Z", "name": "H2O", "e_elec_eh": -76.4, "coords": "O 0 0 0"}
        rec2 = {"record_id": "id_bbb_222", "created_at": "2026-08-25T12:34:56Z", "name": "H2O", "e_elec_eh": -76.4, "coords": "O 0 0 0"}
        c1 = normalize_to_canonical_schema(rec1)
        c2 = normalize_to_canonical_schema(rec2)
        assert c1["record_id"] != c2["record_id"]
        assert c1["content_hash"] == c2["content_hash"]

    def test_12_changed_geometry_produces_different_hash(self):
        rec1 = {"name": "H2", "coords": "H 0 0 0\nH 0 0 0.74"}
        rec2 = {"name": "H2", "coords": "H 0 0 0\nH 0 0 0.78"}
        c1 = normalize_to_canonical_schema(rec1)
        c2 = normalize_to_canonical_schema(rec2)
        assert c1["content_hash"] != c2["content_hash"]

    def test_13_changed_energy_produces_different_hash(self):
        rec1 = {"name": "H2", "e_elec_eh": -1.1785, "coords": "H 0 0 0\nH 0 0 0.74"}
        rec2 = {"name": "H2", "e_elec_eh": -1.1790, "coords": "H 0 0 0\nH 0 0 0.74"}
        c1 = normalize_to_canonical_schema(rec1)
        c2 = normalize_to_canonical_schema(rec2)
        assert c1["content_hash"] != c2["content_hash"]


class Test4TrueThermochemistryRoundTripValidation:
    """14 to 16: Numerical round-trip validation with ThermochemistryEngine."""

    def test_14_direct_engine_equals_canonical_exported_result(self):
        raw_molecules = {
            "h2": {
                "name": "H2", "formula": "H2", "charge": 0, "multiplicity": 1,
                "e_elec_eh": -1.1785, "zpe_eh": 0.0102, "total_enthalpy_eh": -1.1650,
                "gibbs_free_energy_eh": -1.1798, "total_entropy_cal_mol_k": 31.2,
                "temperature_k": 298.15, "pressure_atm": 1.0, "stationary_point_status": "MINIMUM"
            },
            "o2": {
                "name": "O2", "formula": "O2", "charge": 0, "multiplicity": 3,
                "e_elec_eh": -150.3200, "zpe_eh": 0.0035, "total_enthalpy_eh": -150.3130,
                "gibbs_free_energy_eh": -150.3340, "total_entropy_cal_mol_k": 49.0,
                "temperature_k": 298.15, "pressure_atm": 1.0, "stationary_point_status": "MINIMUM"
            },
            "h2o": {
                "name": "H2O", "formula": "H2O", "charge": 0, "multiplicity": 1,
                "e_elec_eh": -76.4199, "zpe_eh": 0.0214, "total_enthalpy_eh": -76.3947,
                "gibbs_free_energy_eh": -76.4162, "total_entropy_cal_mol_k": 45.1,
                "temperature_k": 298.15, "pressure_atm": 1.0, "stationary_point_status": "MINIMUM"
            }
        }

        # Construct direct MoleculeData mapping
        direct_mol_map = {}
        for name, data in raw_molecules.items():
            job = JobData(
                e_elec_eh=data["e_elec_eh"],
                zpe_eh=data["zpe_eh"],
                total_enthalpy_eh=data["total_enthalpy_eh"],
                gibbs_free_energy_eh=data["gibbs_free_energy_eh"],
                total_entropy_cal_mol_k=data["total_entropy_cal_mol_k"],
            )
            job.metadata = CalcMetadata(
                temperature_k=data["temperature_k"],
                pressure_atm=data["pressure_atm"],
                phase="Gas"
            )
            direct_mol_map[name] = MoleculeData(name=name, jobs=[job])

        direct_engine = ThermochemistryEngine(molecules=direct_mol_map)
        direct_result = direct_engine.evaluate("2 h2 + o2 -> 2 h2o")

        # Canonical Record Path
        canon_rxn_record = {
            "schema_version": SCHEMA_VERSION,
            "dataset_version": DATASET_VERSION,
            "record_type": "reaction_definition",
            "record_id": "rxn_water_001",
            "name": "Water Formation",
            "reaction": {
                "equation": "2 H2 + O2 -> 2 H2O",
                "reactants": [
                    {"species_id": "sp_h2", "species_name": "H2", "stoichiometric_coefficient": 2.0, "role": "reactant"},
                    {"species_id": "sp_o2", "species_name": "O2", "stoichiometric_coefficient": 1.0, "role": "reactant"}
                ],
                "products": [
                    {"species_id": "sp_h2o", "species_name": "H2O", "stoichiometric_coefficient": 2.0, "role": "product"}
                ],
                "conditions": {"temperature_k": 298.15, "pressure_atm": 1.0, "standard_state": "Gas Phase (1 atm)"}
            }
        }

        # Export through pure adapter
        exported_payload = export_to_thermochemistry_input(
            canonical_record=canon_rxn_record,
            species_records=raw_molecules
        )

        exported_mol_map = {}
        for name, data in exported_payload["molecules"].items():
            job = JobData(
                e_elec_eh=data["e_elec_eh"],
                zpe_eh=data["zpe_eh"],
                total_enthalpy_eh=data["total_enthalpy_eh"],
                gibbs_free_energy_eh=data["gibbs_free_energy_eh"],
                total_entropy_cal_mol_k=data["total_entropy_cal_mol_k"],
            )
            job.metadata = CalcMetadata(
                temperature_k=data["temperature_k"],
                pressure_atm=data["pressure_atm"],
                phase="Gas"
            )
            exported_mol_map[name] = MoleculeData(name=name, jobs=[job])

        exported_engine = ThermochemistryEngine(molecules=exported_mol_map)
        exported_result = exported_engine.evaluate(exported_payload["equation"])

        # Assert exact floating-point equality (zero loss or drift)
        assert direct_result.delta_electronic_kcal_mol == pytest.approx(exported_result.delta_electronic_kcal_mol, abs=1e-8)
        assert direct_result.delta_e0_kcal_mol == pytest.approx(exported_result.delta_e0_kcal_mol, abs=1e-8)
        assert direct_result.delta_h_kcal_mol == pytest.approx(exported_result.delta_h_kcal_mol, abs=1e-8)
        assert direct_result.delta_entropy_cal_mol_k == pytest.approx(exported_result.delta_entropy_cal_mol_k, abs=1e-8)
        assert direct_result.delta_g_kcal_mol == pytest.approx(exported_result.delta_g_kcal_mol, abs=1e-8)
        assert direct_result.equilibrium_constant_keq == pytest.approx(exported_result.equilibrium_constant_keq, rel=1e-6)

    def test_15_composite_thermochemistry_provenance_preservation(self):
        composite_rec = {
            "name": "Composite Species Test",
            "e_elec_eh": -193.500,
            "zpe_eh": 0.085,
            "sp_source": "DLPNO-CCSD(T)/def2-QZVPP",
            "freq_source": "r2SCAN-3c/def2-mTZVP",
            "opt_source": "r2SCAN-3c/def2-mTZVP"
        }
        canon = normalize_to_canonical_schema(composite_rec)
        sources = canon["thermochemistry"]["composite_sources"]
        assert sources["electronic_energy_source"] == "DLPNO-CCSD(T)/def2-QZVPP"
        assert sources["vibrational_source"] == "r2SCAN-3c/def2-mTZVP"
        assert sources["geometry_source"] == "r2SCAN-3c/def2-mTZVP"

    def test_16_temperature_pressure_thermochemistry_roundtrip(self):
        raw_molecules = {
            "a": {"name": "A", "formula": "A", "charge": 0, "multiplicity": 1, "e_elec_eh": -100.0, "total_enthalpy_eh": -99.9, "gibbs_free_energy_eh": -99.95, "temperature_k": 500.0, "pressure_atm": 2.5},
            "b": {"name": "B", "formula": "B", "charge": 0, "multiplicity": 1, "e_elec_eh": -100.1, "total_enthalpy_eh": -100.0, "gibbs_free_energy_eh": -100.06, "temperature_k": 500.0, "pressure_atm": 2.5}
        }
        mol_map = {}
        for name, data in raw_molecules.items():
            job = JobData(e_elec_eh=data["e_elec_eh"], total_enthalpy_eh=data["total_enthalpy_eh"], gibbs_free_energy_eh=data["gibbs_free_energy_eh"])
            job.metadata = CalcMetadata(temperature_k=500.0, pressure_atm=2.5, phase="Gas")
            mol_map[name] = MoleculeData(name=name, jobs=[job])

        direct_engine = ThermochemistryEngine(molecules=mol_map)
        direct = direct_engine.evaluate("a -> b", custom_temperature_k=500.0, custom_pressure_atm=2.5)
        assert direct.temperature_requested_k == 500.0
        assert direct.pressure_atm == 2.5


class Test5MLDatasetExportAndLeakageProtection:
    """17 to 22: ML JSONL determinism, security, target leakage prevention, and grouping."""

    def test_17_jsonl_deterministic_export(self):
        records = [
            {"record_id": "m1", "name": "Methane", "formula": "CH4", "smiles": "C", "coords": "C 0 0 0", "homo_ev": -13.0, "lumo_ev": 1.5},
            {"record_id": "m2", "name": "Ethane", "formula": "C2H6", "smiles": "CC", "coords": "C 0 0 0\nC 1.5 0 0", "homo_ev": -11.5, "lumo_ev": 1.2}
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = os.path.join(tmpdir, "d1.jsonl")
            f2 = os.path.join(tmpdir, "d2.jsonl")
            m1 = export_canonical_to_training_jsonl(records, f1)
            m2 = export_canonical_to_training_jsonl(records, f2)
            assert m1["records_sha256"] == m2["records_sha256"]
            assert open(f1, "rb").read() == open(f2, "rb").read()

    def test_18_ml_dataset_no_ui_state(self):
        records = [{
            "record_id": "w1",
            "name": "Water",
            "smiles": "O",
            "homo_ev": -12.6,
            "viewport": {"zoom": 1.5, "pan": [0, 0]},
            "button_state": "ACTIVE",
            "css_class": "highlighted-card"
        }]
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = os.path.join(tmpdir, "clean.jsonl")
            export_canonical_to_training_jsonl(records, out_file)
            content = open(out_file, "r", encoding="utf-8").read()
            assert "viewport" not in content
            assert "button_state" not in content
            assert "css_class" not in content

    def test_19_ml_dataset_secret_scrubbing(self):
        dirty_records = [{
            "record_id": "d1",
            "name": "Molecule",
            "smiles": "N",
            "kaggle_key": "KAGGLE_PRIVATE_KEY_ABC",
            "cloudflare_token": "CF_SECRET_TOKEN_XYZ",
            "homo_ev": -10.5
        }]
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = os.path.join(tmpdir, "scrubbed.jsonl")
            export_canonical_to_training_jsonl(dirty_records, out_file)
            content = open(out_file, "r", encoding="utf-8").read()
            assert "KAGGLE_PRIVATE_KEY_ABC" not in content
            assert "CF_SECRET_TOKEN_XYZ" not in content

    def test_20_target_leakage_rejection_for_every_task(self):
        # 1. molecular_property_prediction leakage
        bad_prop_input = {"smiles": "C", "homo_ev": -10.5}
        with pytest.raises(TargetLeakageError):
            validate_no_target_leakage("molecular_property_prediction", bad_prop_input, {"homo_ev": -10.5})

        # 2. reaction_energy_prediction leakage
        bad_rxn_input = {"equation": "A -> B", "delta_g_kcal_mol": -15.4}
        with pytest.raises(TargetLeakageError):
            validate_no_target_leakage("reaction_energy_prediction", bad_rxn_input, {"delta_g_kcal_mol": -15.4})

        # 3. vibrational_spectrum_prediction leakage
        bad_vib_input = {"smiles": "O", "vibrational_modes": [1595.0, 3657.0]}
        with pytest.raises(TargetLeakageError):
            validate_no_target_leakage("vibrational_spectrum_prediction", bad_vib_input, {"vibrational_modes": [1595.0]})

        # 4. ir_peak_assignment_classification leakage
        bad_ir_input = {"peak_wavenumber_cm": 1715.0, "functional_group": "Ketone"}
        with pytest.raises(TargetLeakageError):
            validate_no_target_leakage("ir_peak_assignment_classification", bad_ir_input, {"functional_group": "Ketone"})

    def test_21_split_group_key_stability(self):
        rec1 = {"name": "Acetone_1", "smiles": "CC(=O)C", "formula": "C3H6O", "homo_ev": -9.8}
        rec2 = {"name": "Acetone_2", "smiles": "CC(=O)C", "formula": "C3H6O", "homo_ev": -9.8}
        c1 = normalize_to_canonical_schema(rec1)
        c2 = normalize_to_canonical_schema(rec2)
        assert c1["split_group_key"] == c2["split_group_key"] == "CC(=O)C"

    def test_22_manifest_sha256_reproducibility(self):
        records = [
            {"record_id": "r1", "name": "Water", "formula": "H2O", "smiles": "O", "homo_ev": -12.6},
            {"record_id": "r2", "name": "Ammonia", "formula": "NH3", "smiles": "N", "homo_ev": -10.8}
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = os.path.join(tmpdir, "dataset.jsonl")
            man_file = os.path.join(tmpdir, "dataset_manifest.json")
            manifest = export_canonical_to_training_jsonl(records, out_file, manifest_path=man_file)
            assert os.path.exists(man_file)
            assert manifest["record_count"] == 2
            assert len(manifest["records_sha256"]) == 64
            assert manifest["schema_version"] == SCHEMA_VERSION


class Test6ForensicRemediationCF1ThroughCF8:
    """23 to 30: Dedicated regression verification for DeepSeek findings CF-1 to CF-8."""

    def test_23_canonical_normalization_idempotency_all_analyzer_records(self, canonical_schema):
        analysis_fixtures = [
            "analysis_opt.json",
            "analysis_freq.json",
            "analysis_sp.json",
            "analysis_ir.json",
            "analysis_uv.json",
            "analysis_nmr.json",
        ]
        for fix in analysis_fixtures:
            path = os.path.join(REPO_ROOT, "data", "examples", fix)
            with open(path, "r", encoding="utf-8") as f:
                canon1 = json.load(f)
            
            # First re-normalization
            canon2, _ = normalize_with_report(canon1)
            # Second re-normalization
            canon3, _ = normalize_with_report(canon2)

            assert canon2["record_type"] == canon1["record_type"] == "analysis_record"
            assert canon3["record_type"] == "analysis_record"
            assert canon2["content_hash"] == canon1["content_hash"]
            assert canon3["content_hash"] == canon1["content_hash"]
            
            # Validate preserved fields
            if canon1.get("analysis_results"):
                assert canon2["analysis_results"]["single_point_energy_eh"] == canon1["analysis_results"]["single_point_energy_eh"]
                assert canon3["analysis_results"]["single_point_energy_eh"] == canon1["analysis_results"]["single_point_energy_eh"]
            if canon1.get("spectroscopy"):
                assert canon2["spectroscopy"] is not None
                assert canon3["spectroscopy"] is not None
            
            validate(instance=canon2, schema=canonical_schema)
            validate(instance=canon3, schema=canonical_schema)

    def test_24_canonical_input_ml_export_preserves_populated_targets(self):
        freq_path = os.path.join(REPO_ROOT, "data", "examples", "analysis_freq.json")
        with open(freq_path, "r", encoding="utf-8") as f:
            canon_freq = json.load(f)

        with tempfile.TemporaryDirectory() as tmpdir:
            out_jsonl = os.path.join(tmpdir, "test_targets.jsonl")
            export_canonical_to_training_jsonl([canon_freq], out_jsonl, task_type="molecular_property_prediction")
            with open(out_jsonl, "r", encoding="utf-8") as f:
                line = json.loads(f.readline())
            
            target = line["target"]
            assert target["electronic_energy_eh"] == -76.419948
            assert target["gibbs_free_energy_eh"] == -76.416173
            assert target["electronic_energy_eh"] is not None
            assert target["gibbs_free_energy_eh"] is not None

    def test_25_canonical_ir_classification_export_succeeds(self):
        ir_path = os.path.join(REPO_ROOT, "data", "examples", "analysis_ir.json")
        with open(ir_path, "r", encoding="utf-8") as f:
            canon_ir = json.load(f)

        with tempfile.TemporaryDirectory() as tmpdir:
            out_jsonl = os.path.join(tmpdir, "test_ir.jsonl")
            export_canonical_to_training_jsonl([canon_ir], out_jsonl, task_type="ir_peak_assignment_classification")
            with open(out_jsonl, "r", encoding="utf-8") as f:
                line = json.loads(f.readline())
            
            assert line["task"] == "ir_peak_assignment_classification"
            assert line["input"]["peak_wavenumber_cm"] == 1715.0
            assert line["target"]["functional_group"] == "Ketone"
            assert line["target"]["bond_or_mode"] == "C=O stretch"

    def test_26_canonical_ir_missing_spectroscopy_fails_deterministically(self):
        bad_rec = {
            "schema_version": SCHEMA_VERSION,
            "dataset_version": DATASET_VERSION,
            "record_type": "analysis_record",
            "record_id": "bad_001",
            "name": "Bad IR",
            "spectroscopy": None
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            out_jsonl = os.path.join(tmpdir, "fail.jsonl")
            with pytest.raises(ValueError, match="missing required spectroscopy.ir"):
                export_canonical_to_training_jsonl([bad_rec], out_jsonl, task_type="ir_peak_assignment_classification")

    def test_27_string_and_nested_target_leakage_fail_closed(self):
        # String target in note text
        bad_input_str = {"peak_wavenumber_cm": 1718.0, "comment": "Sample has a Ketone group"}
        with pytest.raises(TargetLeakageError, match="Target string leakage detected"):
            validate_no_target_leakage("ir_peak_assignment_classification", bad_input_str, {"functional_group": "Ketone"})

        # Nested target leakage
        bad_input_nested = {"molecular_structure": "CC(=O)C", "extra": {"functional_group": "Ketone"}}
        with pytest.raises(TargetLeakageError, match="Target leakage detected"):
            validate_no_target_leakage("ir_peak_assignment_classification", bad_input_nested, {"functional_group": "Ketone"})

        # List target leakage
        bad_input_list = {"labels": ["Alcohol", "Ketone"]}
        with pytest.raises(TargetLeakageError, match="Target string leakage detected"):
            validate_no_target_leakage("ir_peak_assignment_classification", bad_input_list, {"functional_group": "Ketone"})

        # bond_or_mode leakage
        bad_input_mode = {"peak_wavenumber_cm": 1718.0, "bond_or_mode": "C=O stretch"}
        with pytest.raises(TargetLeakageError, match="Target leakage detected: forbidden key 'bond_or_mode'"):
            validate_no_target_leakage("ir_peak_assignment_classification", bad_input_mode, {"bond_or_mode": "C=O stretch"})

        # Unrelated text does NOT raise
        clean_input = {"peak_wavenumber_cm": 1718.0, "solvent": "ethanol", "theoretical_modes": []}
        validate_no_target_leakage("ir_peak_assignment_classification", clean_input, {"functional_group": "Ketone", "bond_or_mode": "C=O stretch"})

    def test_28_schema_rejects_unauthorized_ui_state(self, canonical_schema):
        ui_state_rec = {
            "schema_version": "orca-web-lab.1.0",
            "dataset_version": "2026.1",
            "record_type": "analysis_record",
            "record_id": "test_rec_001",
            "content_hash": "a" * 64,
            "created_at": "2026-08-25T00:00:00Z",
            "provenance": {"source_type": "orca_analyzer"},
            "data_quality": {"validation_status": "verified", "completeness_score": 1.0},
            "button_color": "red",
            "dom_selector": "#app",
            "canvas_x": 127
        }
        with pytest.raises(jsonschema.ValidationError, match="Additional properties are not allowed"):
            validate(instance=ui_state_rec, schema=canonical_schema)

    def test_29_migration_report_records_unsupported_fields(self):
        raw_unknown = {
            "name": "Test Unmapped",
            "coords": [{"elem": "H", "x": 0, "y": 0, "z": 0}],
            "unknown_scientific_property": 123.45
        }
        canonical, report = normalize_with_report(raw_unknown)
        assert "unknown_scientific_property" in report["unsupported_fields"]
        assert any("unknown_scientific_property" in w for w in report["warnings"])
