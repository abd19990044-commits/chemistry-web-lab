import io
import json
import os
import sys
import zipfile
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as webapp

@pytest.fixture
def client():
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        yield c

def test_engine_status(client):
    res = client.get("/api/orca/engine/status")
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert data["available"] is True
    assert "features" in data
    assert any("parser" in f for f in data["features"])
    assert "uvvis_gaussian_convolution" in data["features"]
    assert "reaction_thermochemistry" in data["features"]

def test_engine_samples(client):
    res = client.get("/api/orca/engine/samples")
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert len(data["samples"]) >= 3

    # Load specific sample
    sample_id = data["samples"][0]["id"]
    res_sample = client.get(f"/api/orca/engine/samples?id={sample_id}")
    assert res_sample.status_code == 200
    sample_data = res_sample.get_json()
    assert sample_data["ok"] is True
    assert "latest_job" in sample_data
    assert sample_data["latest_job"]["xyz"]

def test_engine_parse_text(client):
    orca_output = """
  ------------------------------------------------------------------------------
  * O   R   C   A *
  ------------------------------------------------------------------------------
                                 Program Version 6.0.0
                                 
  Number of atoms                             ...     3
  Total Charge                                ...     0
  Multiplicity                                ...     1

  CARTESIAN COORDINATES (ANGSTROEM)
  ---------------------------------
  O      0.000000    0.000000    0.117269
  H      0.000000    0.756968   -0.469076
  H      0.000000   -0.756968   -0.469076

  FINAL SINGLE POINT ENERGY      -76.421258412356
  
  ORBITAL ENERGIES
  NO   OCC          E(Eh)            E(eV)
   4   2.0000     -0.345678        -9.406
   5   0.0000      0.123456         3.359
   
  TOTAL DIPOLE MOMENT
  Magnitude (Debye)      :    1.8546
  
  Total Thermal Energy                         ...        0.024567 Eh
  Total Enthalpy                               ...      -76.395741 Eh
  Final Gibbs free energy                      ...      -76.417283 Eh
  
  ****ORCA TERMINATED NORMALLY****
"""
    res = client.post("/api/orca/engine/parse", json={"content": orca_output, "name": "water_test"})
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert data["name"] == "water_test"
    job = data["latest_job"]
    assert job["terminated_normally"] is True
    assert len(job["elements"]) == 3
    assert job["homo_ev"] == -9.406
    assert job["lumo_ev"] == 3.359
    assert pytest.approx(job["homo_lumo_gap_ev"], 0.01) == 12.765
    assert job["chemical_hardness_ev"] is not None
    assert job["electronegativity_ev"] is not None
    assert job["e_elec_eh"] == -76.421258412356

def test_engine_parse_file_upload(client):
    output_text = "Program Version 6.0.0\nFINAL SINGLE POINT ENERGY -100.123456\n****ORCA TERMINATED NORMALLY****\n"
    data = {
        "file": (io.BytesIO(output_text.encode("utf-8")), "sample.out")
    }
    res = client.post("/api/orca/engine/parse", data=data, content_type="multipart/form-data")
    assert res.status_code == 200
    res_data = res.get_json()
    assert res_data["ok"] is True
    assert res_data["latest_job"]["e_elec_eh"] == -100.123456

def test_engine_convolute(client):
    payload = {
        "tddft_cm": [25000.0, 30000.0],
        "tddft_fosc": [0.15, 0.45],
        "sigma_nm": 20.0,
        "wavelength_shift_nm": 0.0,
        "start_nm": 200.0,
        "end_nm": 600.0,
        "step_nm": 5.0,
    }
    res = client.post("/api/orca/engine/convolute", json=payload)
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert "spectrum" in data
    assert len(data["spectrum"]) > 10
    assert any(pt["intensity"] > 0 for pt in data["spectrum"])

def test_engine_thermochemistry(client):
    species_a = """
  CARTESIAN COORDINATES (ANGSTROEM)
  C  0.0 0.0 0.0
  FINAL SINGLE POINT ENERGY -38.0
  Total Enthalpy                               ...      -37.95000000 Eh
  Final Gibbs free energy                      ...      -37.98000000 Eh
  ****ORCA TERMINATED NORMALLY****
"""
    species_b = """
  CARTESIAN COORDINATES (ANGSTROEM)
  O  0.0 0.0 0.0
  FINAL SINGLE POINT ENERGY -75.0
  Total Enthalpy                               ...      -74.95000000 Eh
  Final Gibbs free energy                      ...      -74.98000000 Eh
  ****ORCA TERMINATED NORMALLY****
"""
    species_c = """
  CARTESIAN COORDINATES (ANGSTROEM)
  C  0.0 0.0 0.0
  O  0.0 0.0 1.2
  FINAL SINGLE POINT ENERGY -113.1
  Total Enthalpy                               ...     -113.05000000 Eh
  Final Gibbs free energy                      ...     -113.08000000 Eh
  ****ORCA TERMINATED NORMALLY****
"""
    payload = {
        "equation": "A + B -> C",
        "molecules": {
            "A": species_a,
            "B": species_b,
            "C": species_c
        },
        "temperature_k": 298.15,
        "pressure_atm": 1.0
    }
    res = client.post("/api/orca/engine/thermochemistry", json=payload)
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    r = data["result"]
    assert "delta_h_kcal_mol" in r
    assert "delta_g_kcal_mol" in r
    assert "equilibrium_constant_keq" in r
    assert r["delta_h_kcal_mol"] < 0  # exothermic reaction
    assert r["delta_electronic_kcal_mol"] < 0

def test_engine_analyze_job_mock(client, monkeypatch):
    import kaggle_runner
    from orca_orchestrator.service import OrchestratorService

    def mock_fetch(*args, **kwargs):
        return "mock_path.zip", None

    monkeypatch.setattr(kaggle_runner, "fetch_job_results", mock_fetch)
    monkeypatch.setattr(OrchestratorService, "fetch_results", lambda self, creds, job_id: "mock_path.zip")

    payload = {
        "kaggle_username": "mockuser",
        "kaggle_key": "mockkey0123456789abcdef01234567",
        "job_id": "chem-tools-test-12345678"
    }

    monkeypatch.setattr(webapp.os.path, "isfile", lambda p: True)
    
    class MockZipFile:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def infolist(self):
            class Item:
                filename = "orca_calc.out"
            return [Item()]
        def read(self, item):
            return b"Program Version 6.0.0\nFINAL SINGLE POINT ENERGY -42.123\n****ORCA TERMINATED NORMALLY****\n"

    monkeypatch.setattr(webapp.zipfile, "ZipFile", MockZipFile)

    res = client.post("/api/orca/engine/analyze-job", json=payload)
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert data["latest_job"]["e_elec_eh"] == -42.123


def test_engine_parse_zip_archive(client):
    """Test uploading a ZIP archive containing multiple calculation files."""
    calc1 = "Program Version 6.0.0\nFINAL SINGLE POINT ENERGY -50.123456\n****ORCA TERMINATED NORMALLY****\n"
    calc2 = "Program Version 6.0.0\nFINAL SINGLE POINT ENERGY -80.654321\n****ORCA TERMINATED NORMALLY****\n"

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("calc_a.out", calc1)
        zf.writestr("subfolder/calc_b.log", calc2)
        zf.writestr("readme.txt", "this is a note")

    zip_buffer.seek(0)
    data = {
        "file": (zip_buffer, "calculations_bundle.zip")
    }

    res = client.post("/api/orca/engine/parse", data=data, content_type="multipart/form-data")
    assert res.status_code == 200
    res_data = res.get_json()
    assert res_data["ok"] is True
    assert res_data["is_archive"] is True
    assert len(res_data["archive_entries"]) == 2
    assert "cleanup_note" in res_data


def test_session_heartbeat_and_leave(client):
    """Test session activity tracking and leave beacon."""
    res_hb = client.post("/api/session/heartbeat", json={"session_id": "test_sess_123"})
    assert res_hb.status_code == 200
    data_hb = res_hb.get_json()
    assert data_hb["ok"] is True
    assert data_hb["session_id"] == "test_sess_123"
    assert data_hb["ttl_seconds"] == 1800

    res_leave = client.post("/api/session/leave", json={"session_id": "test_sess_123"})
    assert res_leave.status_code == 200
    assert res_leave.get_json()["ok"] is True


def test_dynamic_orca_version_extraction_across_versions(client):
    """Test that ORCA versions (5.0.4, 6.0.0, 6.1.0) are accurately captured in parsed job records."""
    for version in ["5.0.4", "6.0.0", "6.1.0"]:
        sample_text = f"""
          * O   R   C   A *
          Program Version {version}  - RELEASE -
          FINAL SINGLE POINT ENERGY -100.123456
          ****ORCA TERMINATED NORMALLY****
        """
        res = client.post("/api/orca/engine/parse", json={"content": sample_text, "name": f"test_{version}"})
        assert res.status_code == 200
        data = res.get_json()
        assert data["ok"] is True
        job = data["latest_job"]
        assert job["orca_version"] == version


