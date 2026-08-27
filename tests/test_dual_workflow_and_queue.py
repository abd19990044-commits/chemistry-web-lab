# -*- coding: utf-8 -*-
"""Unit tests for Dual-Stage Chained Workflow and Kaggle extraction routes."""
import io
import json
import os
import sys
import zipfile
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as webapp
import kaggle_runner as K

KEY = "0" * 32
GOOD_ID = K.JOB_ID_PREFIX + "web-test1234"

SAMPLE_ORCA_OUTPUT = """
  ************************************************************
  *                        ORCA 6.0.0                        *
  ************************************************************
                     *** OPTIMIZATION RUN ***

-----------------------------------------------------------------------------
                        FINAL ENERGY EVALUATION
-----------------------------------------------------------------------------
FINAL SINGLE POINT ENERGY      -76.432891234567

----------------------------------
CARTESIAN COORDINATES (ANGSTROEM)
----------------------------------
  O      0.000000    0.000000    0.117215
  H      0.000000    0.756950   -0.468860
  H      0.000000   -0.756950   -0.468860

*** OPTIMIZATION CYCLE 4 ***
                  *** THE OPTIMIZATION HAS CONVERGED ***
"""

@pytest.fixture
def client():
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as client:
        yield client


def test_extract_opt_coords_endpoint(client, monkeypatch):
    """Verify POST /api/kaggle/extract-opt-coords correctly parses optimized geometry."""
    def fake_fetch_job_results(username, key, job_id):
        import tempfile
        tmp_dir = tempfile.mkdtemp()
        zip_path = os.path.join(tmp_dir, "results.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("job.out", SAMPLE_ORCA_OUTPUT + "\nORCA TERMINATED NORMALLY\n")
            zf.writestr("h2o_opt.inp", "! B3LYP def2-SVP Opt\n* xyz 0 1\nO 0 0 0\nH 0 0 0.96\nH 0 0 -0.96\n*\n")
            zf.writestr("JOB_NOTE.txt", "Job finished successfully.")
        return zip_path, tmp_dir

    monkeypatch.setattr(K, "fetch_job_results", fake_fetch_job_results)

    resp = client.post(
        "/api/kaggle/extract-opt-coords",
        data=json.dumps({
            "kaggle_username": "testuser",
            "kaggle_key": KEY,
            "job_id": GOOD_ID,
        }),
        content_type="application/json",
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "O" in data["coords"]
    assert "H" in data["coords"]
    assert data["atom_count"] == 3
    assert data["total_energy_eh"] is not None


def test_orca_generate_dual_inputs(client):
    """Verify generating both stage 1 (Opt) and stage 2 (SP/DLPNO-CCSD(T)) inputs."""
    water_xyz = "O 0.0 0.0 0.1\nH 0.0 0.7 -0.4\nH 0.0 -0.7 -0.4"
    
    # Stage 1: Geometry Optimization
    resp1 = client.post(
        "/api/orca/generate",
        data=json.dumps({
            "name": "water_opt",
            "calc_type": "opt",
            "theory": "B3LYP",
            "dispersion": "d3bj",
            "basis": "def2-SVP",
            "cores": 4,
            "ram": 2000,
            "coords": water_xyz,
        }),
        content_type="application/json",
    )
    assert resp1.status_code == 200
    d1 = resp1.get_json()
    assert "! B3LYP" in d1["input_text"]
    assert "Opt" in d1["input_text"]
    assert "def2-SVP" in d1["input_text"]

    # Stage 2: High-Level Single Point
    resp2 = client.post(
        "/api/orca/generate",
        data=json.dumps({
            "name": "water_sp",
            "calc_type": "sp",
            "theory": "DLPNO-CCSD(T)",
            "basis": "def2-TZVP",
            "scf_conv": "tightscf",
            "cores": 4,
            "ram": 4000,
            "coords": water_xyz,
        }),
        content_type="application/json",
    )
    assert resp2.status_code == 200
    d2 = resp2.get_json()
    assert "! DLPNO-CCSD(T)" in d2["input_text"]
    assert "def2-TZVP" in d2["input_text"]
