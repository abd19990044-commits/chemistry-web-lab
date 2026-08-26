# -*- coding: utf-8 -*-
"""Flask API endpoint tests for NMR spectrum analyzer."""

from __future__ import annotations

import json
import pytest

from app import app

SAMPLE_NMR_TEXT = """
  ------------------
  CHEMICAL SHIELDING
  ------------------

Nucleus  Element    Isotropic     Anisotropy
-------  -------  ------------  ------------
    0       C          173.900        25.100
    1       C          155.000        32.400
    2       H           30.500         5.100
    3       H           30.510         5.100
"""


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_api_nmr_parse_endpoint_with_and_without_references(client):
    # 1. Without references -> default to uncalibrated isotropic shielding mode
    res1 = client.post(
        "/api/orca/engine/nmr/parse",
        data=json.dumps({"content": SAMPLE_NMR_TEXT}),
        content_type="application/json",
    )
    assert res1.status_code == 200
    data1 = res1.get_json()
    assert data1["ok"] is True
    nmr1 = data1["nmr"]
    assert len(nmr1["atoms"]) == 4
    assert nmr1["has_h1"] is True
    assert nmr1["has_c13"] is True
    assert nmr1["h1_spectrum"]["is_reference_applied"] is False
    assert nmr1["c13_spectrum"]["is_reference_applied"] is False
    assert "Reference shielding not configured" in nmr1["h1_spectrum"]["warnings"][0]

    # 2. With references -> compute chemical shifts
    res2 = client.post(
        "/api/orca/engine/nmr/parse",
        data=json.dumps({"content": SAMPLE_NMR_TEXT, "reference_1h": 31.77, "reference_13c": 184.30}),
        content_type="application/json",
    )
    assert res2.status_code == 200
    data2 = res2.get_json()
    assert data2["ok"] is True
    nmr2 = data2["nmr"]
    assert nmr2["h1_spectrum"]["is_reference_applied"] is True
    assert nmr2["c13_spectrum"]["is_reference_applied"] is True


def test_api_nmr_spectrum_recalculate_and_compatibility(client):
    atoms = [
        {"atom_index": 0, "element": "C", "isotope": "13C", "isotropic_shielding": 173.9, "assignment": "C0"},
        {"atom_index": 1, "element": "C", "isotope": "13C", "isotropic_shielding": 155.0, "assignment": "C1"},
    ]

    # Level of theory match: B3LYP/TZVPP
    res = client.post(
        "/api/orca/engine/nmr/spectrum",
        data=json.dumps({
            "atoms": atoms,
            "nucleus": "13C",
            "reference_shielding": 184.30,
            "reference_method": "B3LYP",
            "reference_basis": "TZVPP",
            "method": "B3LYP",
            "basis_set": "TZVPP",
        }),
        content_type="application/json",
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    spec = data["spectrum"]
    assert spec["nucleus"] == "13C"
    assert spec["is_reference_applied"] is True
    assert len(spec["peaks"]) == 2
    assert spec["peaks"][0]["position_ppm"] == pytest.approx(10.40, abs=1e-2)

    # Level of theory mismatch: calculation is HF/SVP, reference is B3LYP/TZVPP
    res_mismatch = client.post(
        "/api/orca/engine/nmr/spectrum",
        data=json.dumps({
            "atoms": atoms,
            "nucleus": "13C",
            "reference_shielding": 184.30,
            "reference_method": "B3LYP",
            "reference_basis": "TZVPP",
            "method": "HF",
            "basis_set": "SVP",
        }),
        content_type="application/json",
    )
    assert res_mismatch.status_code == 200
    data_m = res_mismatch.get_json()
    spec_m = data_m["spectrum"]
    assert len(spec_m["warnings"]) > 0
    assert any("Level of theory mismatch" in w for w in spec_m["warnings"])


def test_api_nmr_export_csv(client):
    atoms = [
        {"atom_index": 0, "element": "H", "isotope": "1H", "isotropic_shielding": 30.5, "chemical_shift": 1.27, "assignment": "H0"},
    ]
    res = client.post(
        "/api/orca/engine/nmr/export/csv",
        data=json.dumps({"atoms": atoms}),
        content_type="application/json",
    )
    assert res.status_code == 200
    assert "text/csv" in res.content_type
    assert "atom_index,element,isotope" in res.data.decode("utf-8")


def test_api_nmr_export_json(client):
    payload = {"test": "nmr_export"}
    res = client.post(
        "/api/orca/engine/nmr/export/json",
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert res.status_code == 200
    assert "application/json" in res.content_type
    data = json.loads(res.data.decode("utf-8"))
    assert data["test"] == "nmr_export"
