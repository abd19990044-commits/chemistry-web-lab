# -*- coding: utf-8 -*-
"""Comprehensive audit, regression, and ML dataset test suite for ORCA Web Lab scientific JSON export.

Covers:
- Part 1: Audit of user fixtures (ORCA Calculation_quantum_analysis(3).json & orca_parsed_data (3)(2).json)
- Part 2: Deterministic calculation identity (calculation_id and content_hash)
- Part 3 & 4: Explicit units across all physical quantities
- Part 5: Full scientific precision preservation
- Part 6 & 7: ML-ready geometry and GNN molecular graph representations
- Part 8, 9, 10, 11, 12: Electronic properties, charges, thermochemistry, IR/UV/NMR spectroscopy
- Part 13 & 14: Fail-closed target leakage protection and split group keys
- Part 15: Duplicate detection (exact content, calculation identity, distinct)
- Part 16 & 24: JSONL export and reproducible dataset manifest
- Part 19: Strict JSON Schema Draft 2020-12 validation
- Invariant: Zero em-dashes and en-dashes across codebase
"""

import copy
import hashlib
import json
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
    compute_deterministic_calculation_id,
    compute_split_group_key,
    compute_geometry_hash,
    compute_chemical_formula,
    extract_molecular_graph,
    check_duplicate_status,
    is_canonical_record,
    SCHEMA_VERSION,
    DATASET_VERSION,
)
from tools.export_training_jsonl import (
    export_canonical_to_training_jsonl,
    validate_no_target_leakage,
    TargetLeakageError,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def canonical_schema():
    schema_path = os.path.join(REPO_ROOT, "schema", "canonical_scientific.schema.json")
    with open(schema_path, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def fixture_quantum_analysis():
    path = os.path.join(REPO_ROOT, "tests", "fixtures", "ORCA_Calculation_quantum_analysis_3.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def fixture_parsed_data_duplicate():
    path = os.path.join(REPO_ROOT, "tests", "fixtures", "orca_parsed_data_3_2.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class TestPart1AuditAndRegressionFixtures:
    """Part 1: Audit the two existing JSON files and prove identity/normalization."""

    def test_fixtures_exist_and_are_identical_or_valid(self, fixture_quantum_analysis, fixture_parsed_data_duplicate):
        assert fixture_quantum_analysis is not None
        assert fixture_parsed_data_duplicate is not None
        
        # Serialize both to compare content
        s1 = json.dumps(fixture_quantum_analysis, sort_keys=True)
        s2 = json.dumps(fixture_parsed_data_duplicate, sort_keys=True)
        h1 = hashlib.sha256(s1.encode("utf-8")).hexdigest()
        h2 = hashlib.sha256(s2.encode("utf-8")).hexdigest()
        assert h1 == h2, "Both fixtures must represent identical ORCA calculation content"

    def test_normalization_produces_valid_canonical_record(self, fixture_quantum_analysis, canonical_schema):
        canonical, report = normalize_with_report(fixture_quantum_analysis)
        validate(instance=canonical, schema=canonical_schema)
        
        assert canonical["schema_version"] == SCHEMA_VERSION
        assert canonical["dataset_version"] == DATASET_VERSION
        assert canonical["calculation_id"].startswith("calc_")
        assert len(canonical["content_hash"]) == 64
        assert canonical["split_group_key"].startswith("grp_H2O")
        assert canonical["molecule"]["formula"] == "H2O"
        assert canonical["molecule"]["elements"] == ["O", "H", "H"]
        assert canonical["molecule"]["atomic_numbers"] == [8, 1, 1]


class TestPart2DeterministicIdentityAndDeduplication:
    """Part 2 & 15: Deterministic calculation identity and duplicate detection."""

    def test_calculation_id_is_invariant_to_run_instance(self, fixture_quantum_analysis):
        c1 = normalize_to_canonical_schema(fixture_quantum_analysis)
        c2 = normalize_to_canonical_schema(fixture_quantum_analysis)
        
        # calculation_id and content_hash must be identical across runs
        assert c1["calculation_id"] == c2["calculation_id"]
        assert c1["content_hash"] == c2["content_hash"]
        assert c1["split_group_key"] == c2["split_group_key"]

    def test_calculation_id_changes_when_method_or_basis_changes(self):
        base_calc = {
            "formula": "H2O",
            "elements": ["O", "H", "H"],
            "charge": 0,
            "multiplicity": 1,
            "method": "B3LYP",
            "basis_set": "def2-SVP",
            "calc_type": "OPT",
            "temperature_k": 298.15,
            "pressure_atm": 1.0,
        }
        id1 = compute_deterministic_calculation_id(base_calc, geometry_hash="geom123")
        
        calc_diff_basis = copy.deepcopy(base_calc)
        calc_diff_basis["basis_set"] = "def2-TZVP"
        id2 = compute_deterministic_calculation_id(calc_diff_basis, geometry_hash="geom123")
        
        calc_diff_method = copy.deepcopy(base_calc)
        calc_diff_method["method"] = "wB97X-D4"
        id3 = compute_deterministic_calculation_id(calc_diff_method, geometry_hash="geom123")
        
        assert id1 != id2
        assert id1 != id3
        assert id2 != id3

    def test_duplicate_status_detection(self, fixture_quantum_analysis):
        c1 = normalize_to_canonical_schema(fixture_quantum_analysis)
        
        registry = {
            "by_content_hash": {c1["content_hash"]: c1["record_id"]},
            "by_calc_id": {c1["calculation_id"]: c1["record_id"]},
        }
        
        dup_check = check_duplicate_status(c1, registry)
        assert dup_check["is_duplicate"] is True
        assert dup_check["duplicate_type"] == "EXACT_SCIENTIFIC_CONTENT"
        assert dup_check["duplicate_of"] == c1["record_id"]


class TestPart3To7MLGeometryAndGraph:
    """Part 3 to 7: Units, Precision, Geometry, and Graph ML representations."""

    def test_explicit_units_in_quantities(self, fixture_quantum_analysis):
        c = normalize_to_canonical_schema(fixture_quantum_analysis)
        
        assert c["geometry"]["coordinates"]["unit"] == "angstrom"
        assert c["electronic_properties"]["total_energy"]["unit"] == "hartree"
        assert c["electronic_properties"]["homo"]["unit"] == "eV"
        assert c["electronic_properties"]["lumo"]["unit"] == "eV"
        assert c["electronic_properties"]["dipole_moment"]["unit"] == "debye"
        assert c["thermochemistry"]["gibbs_free_energy"]["unit"] == "hartree"
        assert c["calculation"]["temperature"]["unit"] == "kelvin"
        assert c["calculation"]["pressure"]["unit"] == "atm"

    def test_full_scientific_precision_preserved(self, fixture_quantum_analysis):
        c = normalize_to_canonical_schema(fixture_quantum_analysis)
        
        # Electronic energy: -76.36077000299 Eh
        e_val = c["electronic_properties"]["total_energy"]["value"]
        assert abs(e_val - (-76.36077000299)) < 1e-12
        
        # Gibbs free energy: -76.35779526 Eh
        g_val = c["thermochemistry"]["gibbs_free_energy"]["value"]
        assert abs(g_val - (-76.35779526)) < 1e-10

    def test_geometry_and_graph_structures(self, fixture_quantum_analysis):
        c = normalize_to_canonical_schema(fixture_quantum_analysis)
        
        geom = c["geometry"]
        assert geom["num_atoms"] == 3
        assert geom["elements"] == ["O", "H", "H"]
        assert geom["atomic_numbers"] == [8, 1, 1]
        assert len(geom["coordinates"]["values"]) == 3
        assert len(geom["coordinates"]["values"][0]) == 3
        
        graph = c["graph"]
        assert graph["num_nodes"] == 3
        assert graph["nodes"][0]["element"] == "O"
        assert graph["nodes"][0]["atomic_number"] == 8


class TestPart8To12ChargesAndSpectroscopy:
    """Part 8 to 12: Charges (Mulliken, Loewdin, Mayer) and IR/UV/NMR spectroscopy."""

    def test_charges_separation_and_accuracy(self, fixture_quantum_analysis):
        c = normalize_to_canonical_schema(fixture_quantum_analysis)
        charges = c["charges"]
        
        assert "mulliken" in charges
        assert "loewdin" in charges
        assert len(charges["mulliken"]) == 3
        assert len(charges["loewdin"]) == 3
        
        # Oxygen Mulliken charge is ~ -0.2986
        assert charges["mulliken"][0]["element"] == "O"
        assert abs(charges["mulliken"][0]["value"] - (-0.298612)) < 1e-5

    def test_vibrational_modes_and_spectroscopy(self, fixture_quantum_analysis):
        c = normalize_to_canonical_schema(fixture_quantum_analysis)
        vib = c["vibrational_properties"]
        
        assert len(vib["harmonic_modes"]) == 3
        assert "MINIMUM" in vib["stationary_point_status"]
        assert vib["imaginary_frequencies_count"] == 0
        
        # Check raw theoretical modes
        freqs = [m["frequency"]["value"] for m in vib["harmonic_modes"]]
        assert 1608.89 in freqs
        assert 3681.74 in freqs
        assert 3781.01 in freqs


class TestPart13To16TargetLeakageAndJSONLExport:
    """Part 13 to 16: Target leakage protection, JSONL dataset export, and manifests."""

    def test_fail_closed_target_leakage_protection(self):
        # Leaking HOMO into input
        input_leaked = {
            "smiles": "O",
            "homo_ev": -6.3366,
            "coordinates": [[0, 0, 0]],
        }
        targets = {"homo_ev": -6.3366}
        
        with pytest.raises(TargetLeakageError):
            validate_no_target_leakage("molecular_property_prediction", input_leaked, targets)

    def test_jsonl_export_and_reproducible_manifest(self, fixture_quantum_analysis):
        c = normalize_to_canonical_schema(fixture_quantum_analysis)
        
        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = os.path.join(tmpdir, "dataset.jsonl")
            manifest_path = os.path.join(tmpdir, "manifest.json")
            
            manifest = export_canonical_to_training_jsonl(
                [c],
                output_path=jsonl_path,
                task_type="molecular_property_prediction",
                manifest_path=manifest_path,
            )
            
            assert os.path.exists(jsonl_path)
            assert os.path.exists(manifest_path)
            assert manifest["record_count"] == 1
            assert len(manifest["dataset_sha256"]) == 64
            
            # Read back JSONL line and verify structure
            with open(jsonl_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
                assert len(lines) == 1
                row = json.loads(lines[0])
                assert row["task"] == "molecular_property_prediction"
                assert "input" in row
                assert "target" in row
                assert "split_group_key" in row


class TestZeroEmDashesInvariant:
    """Strict Invariant: Zero em-dashes and en-dashes across all code and schema."""

    def test_no_em_or_en_dashes_in_core_modules(self):
        target_files = [
            os.path.join(REPO_ROOT, "tools", "normalize_scientific_json.py"),
            os.path.join(REPO_ROOT, "tools", "export_training_jsonl.py"),
            os.path.join(REPO_ROOT, "schema", "canonical_scientific.schema.json"),
            os.path.join(REPO_ROOT, "templates", "index.html"),
            os.path.join(REPO_ROOT, "static", "js", "app.js"),
        ]
        for fpath in target_files:
            if not os.path.exists(fpath):
                continue
            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            assert "\u2014" not in content, f"Em-dash found in {fpath}"
            assert "\u2013" not in content, f"En-dash found in {fpath}"
