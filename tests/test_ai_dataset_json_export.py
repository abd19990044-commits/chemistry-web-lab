# -*- coding: utf-8 -*-
"""Comprehensive verification suite for AI-Optimized Canonical JSON Dataset Exports.

Validates that the ORCA Web Lab Analyzer outputs canonical, machine-learning-ready
scientific records formatted specifically for AI model training:
1. Conforms 100% to schema/canonical_scientific.schema.json (Draft 2020-12).
2. Complete elimination of raw_text terminal dumps and web UI noise.
3. Full quantum targets coverage (Hartree, eV, kcal/mol, CDFT, frequencies, charges).
4. Full invariant derived features coverage (Z numbers, masses, center of mass, gyration radius).
5. GNN molecular graph compatibility (nodes, edges, adjacency matrix).
6. Deterministic split_group_key for leak-free train/val/test segregation.
7. Backend endpoints /api/orca/engine/parse and /api/orca/engine/export-ai-dataset verification.
"""

from __future__ import annotations

import json
import os
import pytest
from jsonschema import Draft202012Validator, validate

from tools.normalize_scientific_json import (
    normalize_to_canonical_schema,
    normalize_with_report,
    is_canonical_record,
    SCHEMA_VERSION,
    DATASET_VERSION,
)
from app import app


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def canonical_schema():
    schema_path = os.path.join(REPO_ROOT, "schema", "canonical_scientific.schema.json")
    with open(schema_path, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def sample_fixture_1():
    fixture_path = os.path.join(REPO_ROOT, "tests", "fixtures", "orca_parsed_data_1.json")
    with open(fixture_path, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def real_analyzer_molecules():
    data_path = os.path.join(REPO_ROOT, "orca_engine", "ORCA_Parsed_Data.json")
    with open(data_path, "r", encoding="utf-8") as f:
        return json.load(f).get("molecules", {})


class TestAiDatasetJsonStructure:
    """Validates schema adherence, targets, and derived features for AI training."""

    def test_fixture_1_produces_valid_ai_dataset_json(self, sample_fixture_1, canonical_schema):
        record, report = normalize_with_report(sample_fixture_1)
        validate(instance=record, schema=canonical_schema)

        # 1. Zero raw_text or debug tokens
        assert "raw_text" not in record
        assert "raw_object" not in record
        assert "session_id" not in record
        assert "cleanup_note" not in record

        # 2. Canonical identity & metadata
        assert record["schema_version"] == SCHEMA_VERSION
        assert record["hash_version"] == DATASED_VERSION if "hash_version" in record else record["schema_version"] == SCHEMA_VERSION
        assert record["dataset_version"] == DATASET_VERSION
        assert record["record_type"] == "analysis_record"
        assert len(record["hash_value"] if "hash_value" in record else record["content_hash"]) == 64
        assert record["split_group_key"] is not None

        # 3. Targets populated for molecular property prediction
        targets = record.get("targets")
        assert isinstance(targets, dict)
        assert targets["electronic_energy_hartree"] == pytest.approx(-76.321271814202, rel=1e-8)
        assert targets["electronic_energy_eh"] == pytest.approx(-76.321271814202, rel=1e-8)
        assert targets["electronic_energy_ev"] == pytest.approx(-76.321271814202 * 27.211386245988, rel=1e-5)
        assert targets["homo_ev"] == pytest.approx(-7.8403, rel=1e-4)
        assert targets["lumo_ev"] == pytest.approx(1.2984, rel=1e-4)
        assert targets["homo_lumo_gap_ev"] == pytest.approx(9.1387, rel=1e-4)
        assert targets["dipole_moment_debye"] == pytest.approx(2.004078, rel=1e-4)

        # CDFT reactivity indices in targets
        cdft = targets.get("conceptual_dft")
        assert isinstance(cdft, dict)
        assert "electronegativity_ev" in cdft
        assert "chemical_hardness_ev" in cdft
        assert "chemical_softness_ev_inv" in cdft
        assert "electrophilicity_index_ev" in cdft

        # Partial atomic charges in targets
        charges = targets.get("atomic_charges")
        assert isinstance(charges, dict)
        assert "mulliken" in charges
        assert len(charges["mulliken"]) == 3
        assert "loewdin" in charges
        assert len(charges["loewdin"]) == 3

        # 4. Derived features populated for tensor input
        derived = record.get("derived_features")
        assert isinstance(derived, dict)
        assert derived["num_atoms"] == 3
        assert derived["atomic_numbers"] == [8, 1, 1]
        assert derived["elements"] == ["O", "H", "H"]
        assert len(derived["coordinates_angstrom"]) == 3
        assert derived["molecular_mass_amu"] == pytest.approx(18.015, abs=0.01)
        assert len(derived["center_of_mass_angstrom"]) == 3
        assert derived["radius_of_gyration_angstrom"] > 0.0

        # 5. GNN molecular graph
        graph = record.get("graph")
        assert isinstance(graph, dict)
        assert len(graph["nodes"]) == 3
        assert "adjacency_matrix" in graph

        # 6. Training metadata
        meta = record.get("training_metadata")
        assert isinstance(meta, dict)
        assert meta["task_type"] == "molecular_property_prediction"
        assert meta["input_modality"] == "coordinates_and_graph"
        assert meta["target_modality"] == "quantum_properties"
        assert meta["label"] is not None
        assert meta["label_type"] == "float"
        assert "valid_geometry" in meta["quality_flags"]

    def test_all_14_real_molecules_produce_valid_targets_and_features(self, real_analyzer_molecules, canonical_schema):
        validator = Draft202012Validator(canonical_schema)
        for mol_name, mol_dict in real_analyzer_molecules.items():
            payload = {
                "name": mol_name,
                "sources": mol_dict.get("sources", []),
                **mol_dict["jobs"][-1]
            }
            canon = normalize_to_canonical_schema(payload)
            errors = list(validator.iter_errors(canon))
            assert not errors, f"Validation failed on {mol_name}: {[e.message for e in errors]}"
            assert "raw_text" not in canon
            assert canon["targets"] is not None
            assert canon["derived_features"] is not None
            assert canon["derived_features"]["num_atoms"] >= 0
            if mol_dict["jobs"][-1].get("coords"):
                assert canon["derived_features"]["num_atoms"] > 0


class TestBackendExportEndpoints:
    """Validates backend JSON export and parse responses."""

    @pytest.fixture
    def client(self):
        app.config["TESTING"] = True
        return app.test_client()

    def test_export_ai_dataset_endpoint_returns_attachment(self, client, sample_fixture_1, canonical_schema):
        res = client.post("/api/orca/engine/export-ai-dataset", json=sample_fixture_1)
        assert res.status_code == 200
        assert res.content_type == "application/json"
        assert "attachment" in res.headers.get("Content-Disposition", "")
        assert "_canonical_ai_dataset.json" in res.headers.get("Content-Disposition", "")

        dataset = res.get_json()
        assert dataset is not None
        assert is_canonical_record(dataset)
        assert "raw_text" not in dataset
        assert "targets" in dataset
        assert "derived_features" in dataset

        # Strict JSON Schema validation
        validate(instance=dataset, schema=canonical_schema)

    def test_parse_endpoint_includes_canonical_record(self, client, canonical_schema):
        minimal_orca = (
            "------------------------------------------------------------------------------\n"
            "* O   R   C   A *\n"
            "------------------------------------------------------------------------------\n"
            "                               Program Version 6.0.0\n"
            "Number of atoms                             ...     3\n"
            "Total Charge                                ...     0\n"
            "Multiplicity                                ...     1\n\n"
            "CARTESIAN COORDINATES (ANGSTROEM)\n"
            "---------------------------------\n"
            "  O      0.000000    0.000000    0.117269\n"
            "  H      0.000000    0.756968   -0.469076\n"
            "  H      0.000000   -0.756968   -0.469076\n"
            "FINAL SINGLE POINT ENERGY      -76.419948000000\n"
            "TOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds 100 msec\n"
            "****ORCA TERMINATED NORMALLY****\n"
        )
        res = client.post("/api/orca/engine/parse", json={"content": minimal_orca, "name": "water_test"})
        assert res.status_code == 200
        data = res.get_json()
        assert data["ok"] is True
        assert "canonical_record" in data
        assert data["canonical_record"] is not None

        canon = data["canonical_record"]
        assert canon["record_type"] == "analysis_record"
        assert canon["targets"]["electronic_energy_hartree"] == pytest.approx(-76.419948, abs=1e-5)
        assert canon["derived_features"]["num_atoms"] == 3
        validate(instance=canon, schema=canonical_schema)
