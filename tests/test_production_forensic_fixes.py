# -*- coding: utf-8 -*-
"""
Tests for Production Forensic Fixes:
1. Kaggle Status Sync (Bug #1): RUNNING status reconciliation when output files don't exist yet.
2. Multi-Step Continuation (Bug #2): Stage 1 Opt completion -> Coordinate Extraction -> Stage 2 Freq continuation.
3. Phase 0 Local Development Defaults & ORCA_LOCAL_MODE mock execution.
"""
import json
import os
import shutil
import tempfile
import time
import zipfile
import pytest

from app import app
from orca_orchestrator.config import CONFIG, Config, StoreConfig
from orca_orchestrator.credentials import parse as parse_credentials
from orca_orchestrator.kaggle_api import KaggleClient, KernelStatus
from orca_orchestrator.models import JobManifest
from orca_orchestrator.reconciler import observe, decide, Reconciler
from orca_orchestrator.service import OrchestratorService
from orca_orchestrator.states import JobState, Trigger
from orca_orchestrator.store import JobStore


@pytest.fixture
def temp_state_dir():
    d = tempfile.mkdtemp(prefix="orca-test-state-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_bug1_running_status_sync_without_output_files(temp_state_dir):
    """Bug #1 Regression: A running Kaggle kernel with no output files yet must
    transition from QUEUED to RUNNING and never return stale 'queued'."""
    creds = parse_credentials("testuser", "testkey12345678901234567890123456789012")
    store = JobStore(StoreConfig(state_dir=temp_state_dir))
    reconciler = Reconciler(store)
    
    job_id = "chem-tools-test-water-12345678"
    job = JobManifest.create(
        job_id=job_id,
        owner="testuser",
        title="Water Opt",
        input_filename="water.inp",
        original_input_sha256="da39a3ee5e6b4b0d3255bfef95601890afd80709",
    )
    job.state = JobState.QUEUED
    store.put_job(job)
    
    class MockRunningClient:
        def __init__(self, c):
            self.creds = c
        def kernel_exists(self, slug):
            return KernelStatus(slug=slug, status="running", raw="status \"running\"")
        def fetch_ledger_files(self, slug):
            # No files available while running
            raise RuntimeError("No files downloaded")
        def status(self, slug):
            return KernelStatus(slug=slug, status="running")

    mock_client = MockRunningClient(creds)
    obs = observe(mock_client, job)
    
    assert obs.kernel_status is not None
    assert obs.kernel_status.status == "running"
    assert obs.error is None
    assert obs.record is None

    decision = decide(job, obs)
    assert decision.trigger in (Trigger.KERNEL_BOOT_FRESH, Trigger.KERNEL_BOOT_RESUME)
    assert not decision.is_noop

    # Act step
    from orca_orchestrator.legacy_compat import _legacy_status
    reconciled = reconciler._act(job, decision, obs, mock_client, fence=1, correlation_id="test", actor="system")
    assert reconciled.state in (JobState.READY, JobState.RUNNING)
    assert _legacy_status(reconciled.state) == "running"


def test_bug2_extract_opt_coords_and_multistep_transition(temp_state_dir):
    """Bug #2 Regression: Extract coordinates from completed parent stage and promote stage 2."""
    flask_client = app.test_client()

    # Enable local mock mode for complete end-to-end simulation
    os.environ["ORCA_LOCAL_MODE"] = "true"
    os.environ["ORCA_STATE_DIR"] = temp_state_dir
    base_state_dir = temp_state_dir
    
    # 1. Coordinate extraction endpoint test with mock data
    job_id = "chem-tools-test-opt-h2o"
    mock_slug_dir = os.path.join(base_state_dir, "mock_kaggle", job_id)
    os.makedirs(mock_slug_dir, exist_ok=True)
    
    out_text = """
================================================================================
                                ORCA 5.0.4
================================================================================
FINAL SINGLE POINT ENERGY      -76.432100
CARTESIAN COORDINATES (ANGSTROEM)
---------------------------------
  O      0.000000    0.000000    0.117200
  H      0.000000    0.757000   -0.468800
  H      0.000000   -0.757000   -0.468800
---------------------------------
*** OPTIMIZATION RUN DONE ***
VIBRATIONAL FREQUENCIES
-----------------------
   1:      1595.00 cm**-1
   2:      3657.00 cm**-1
   3:      3756.00 cm**-1
****ORCA TERMINATED NORMALLY****
"""
    zip_path = os.path.join(mock_slug_dir, "results.zip")
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("molecule.out", out_text)
        zf.writestr("molecule.xyz", "3\n\nO 0.000000 0.000000 0.117200\nH 0.000000 0.757000 -0.468800\nH 0.000000 -0.757000 -0.468800\n")
    
    with open(os.path.join(mock_slug_dir, "_mock_meta.json"), "w") as fh:
        json.dump({"status": "complete", "pushed_at": time.time() - 10, "slug": job_id, "owner": "testuser"}, fh)

    resp = flask_client.post("/api/kaggle/extract-opt-coords", json={
        "kaggle_username": "testuser",
        "kaggle_key": "testkey12345678901234567890123456789012",
        "job_id": job_id,
    })
    
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "coords" in data
    assert "O" in data["coords"]
    assert "0.1172" in data["coords"]
    
    # 2. Rebuild Stage 2 input via /api/orca/generate
    gen_resp = flask_client.post("/api/orca/generate", json={
        "calc_type": "freq",
        "theory": "B3LYP",
        "basis": "def2-SVP",
        "coords": data["coords"],
        "charge": 0,
        "mult": 1,
        "name": "Water_Stage2_Freq",
    })
    assert gen_resp.status_code == 200
    gen_data = gen_resp.get_json()
    assert "input_text" in gen_data
    assert "Freq" in gen_data["input_text"]
    assert "0.1172" in gen_data["input_text"]

    shutil.rmtree(mock_slug_dir, ignore_errors=True)
    os.environ.pop("ORCA_LOCAL_MODE", None)


def test_phase0_local_development_defaults_and_unauth():
    """Phase 0: Safe development configuration and unauthenticated requests handling."""
    flask_client = app.test_client()

    # Healthcheck works without credentials
    health_resp = flask_client.get("/health")
    assert health_resp.status_code == 200
    assert health_resp.get_json()["ok"] is True

    # Operation requiring Kaggle credentials returns clean 400/401 without crashing
    sync_resp = flask_client.post("/api/kaggle/sync", json={})
    assert sync_resp.status_code in (400, 401)
    sync_data = sync_resp.get_json()
    assert sync_data["ok"] is False
    assert "required" in sync_data["error"].lower() or "missing" in sync_data["error"].lower()


def test_kaggle_login_mixed_timestamp_types(temp_state_dir):
    """Regression test: /api/kaggle/login must not fail when sorting jobs with mixed float and string timestamps."""
    flask_client = app.test_client()
    os.environ["ORCA_LOCAL_MODE"] = "true"
    os.environ["ORCA_STATE_DIR"] = temp_state_dir
    base_state_dir = temp_state_dir
    
    # 1. Create a local mock kernel with a string last_run timestamp
    job_id = "chem-tools-test-login-mixed"
    mock_slug_dir = os.path.join(base_state_dir, "mock_kaggle", job_id)
    os.makedirs(mock_slug_dir, exist_ok=True)
    with open(os.path.join(mock_slug_dir, "_mock_meta.json"), "w") as fh:
        json.dump({
            "status": "complete",
            "pushed_at": "2026-08-21 18:00:00",
            "last_run": "2026-08-21T18:00:00",
            "slug": job_id,
            "owner": "testuser"
        }, fh)

    # 2. Call /api/kaggle/login
    resp = flask_client.post("/api/kaggle/login", json={
        "kaggle_username": "testuser",
        "kaggle_key": "testkey12345678901234567890123456789012"
    })
    
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "jobs" in data
    assert isinstance(data["jobs"], list)

    shutil.rmtree(mock_slug_dir, ignore_errors=True)
    os.environ.pop("ORCA_LOCAL_MODE", None)


def test_extract_opt_coords_prioritizes_parser_and_filters_trajectories(temp_state_dir):
    """Test that extract-opt-coords correctly ignores trajectory xyz files and extracts converged geometry."""
    flask_client = app.test_client()
    os.environ["ORCA_LOCAL_MODE"] = "true"
    os.environ["ORCA_STATE_DIR"] = temp_state_dir
    base_state_dir = temp_state_dir

    job_id = "chem-tools-test-trajectory-opt"
    mock_slug_dir = os.path.join(base_state_dir, "mock_kaggle", job_id)
    os.makedirs(mock_slug_dir, exist_ok=True)

    out_text = """
================================================================================
                                ORCA 5.0.4
================================================================================
FINAL SINGLE POINT ENERGY      -76.450000
---------------------------------
CARTESIAN COORDINATES (ANGSTROEM)
---------------------------------
  O      0.000000    0.000000    0.117200
  H      0.000000    0.757000   -0.468800
  H      0.000000   -0.757000   -0.468800
---------------------------------
*** OPTIMIZATION RUN DONE ***
****ORCA TERMINATED NORMALLY****
"""
    # Create a zip containing a trajectory file that has 2 frames concatenated
    trajectory_xyz = """3
Frame 1 initial
O 0.000000 0.000000 0.200000
H 0.000000 0.800000 -0.400000
H 0.000000 -0.800000 -0.400000
3
Frame 2 final converged
O 0.000000 0.000000 0.117200
H 0.000000 0.757000 -0.468800
H 0.000000 -0.757000 -0.468800
"""
    zip_path = os.path.join(mock_slug_dir, "results.zip")
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("molecule.out", out_text)
        zf.writestr("molecule_trj.xyz", trajectory_xyz)
        zf.writestr("ORIGINAL_input.xyz", "3\n\nO 0.0 0.0 0.5\nH 0.0 1.0 0.0\nH 0.0 -1.0 0.0\n")

    with open(os.path.join(mock_slug_dir, "_mock_meta.json"), "w") as fh:
        json.dump({"status": "complete", "pushed_at": time.time() - 10, "slug": job_id, "owner": "testuser"}, fh)

    resp = flask_client.post("/api/kaggle/extract-opt-coords", json={
        "kaggle_username": "testuser",
        "kaggle_key": "testkey12345678901234567890123456789012",
        "job_id": job_id,
    })

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "coords" in data
    # Must be the converged coordinates, not the initial unoptimized ones
    assert "0.1172" in data["coords"]
    # Atom count must be 3, not concatenated 6 or 9 atoms
    assert len(data["coords"].strip().splitlines()) == 3

    shutil.rmtree(mock_slug_dir, ignore_errors=True)
    os.environ.pop("ORCA_LOCAL_MODE", None)


def test_download_results_zip_integrity_and_validity(temp_state_dir):
    """Test that downloading results returns an uncorrupted, valid zip archive that can be opened."""
    import io
    flask_client = app.test_client()
    os.environ["ORCA_LOCAL_MODE"] = "true"
    os.environ["ORCA_STATE_DIR"] = temp_state_dir
    base_state_dir = temp_state_dir

    job_id = "chem-tools-test-download-valid"
    mock_slug_dir = os.path.join(base_state_dir, "mock_kaggle", job_id)
    os.makedirs(mock_slug_dir, exist_ok=True)

    zip_path = os.path.join(mock_slug_dir, "results.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("molecule.out", "ORCA calculation output content here\n****ORCA TERMINATED NORMALLY****\n")
        zf.writestr("molecule.xyz", "3\n\nO 0.0 0.0 0.1\nH 0.0 0.7 -0.4\nH 0.0 -0.7 -0.4\n")

    with open(os.path.join(mock_slug_dir, "_mock_meta.json"), "w") as fh:
        json.dump({"status": "complete", "pushed_at": time.time() - 10, "slug": job_id, "owner": "testuser"}, fh)

    resp = flask_client.post("/api/kaggle/download", json={
        "kaggle_username": "testuser",
        "kaggle_key": "testkey12345678901234567890123456789012",
        "job_id": job_id,
    })

    assert resp.status_code == 200
    assert resp.headers.get("Content-Type") == "application/zip"
    assert "attachment" in resp.headers.get("Content-Disposition", "")

    # Crucial check: verify that the returned payload is a 100% valid zip that opens without error
    data = resp.get_data()
    assert len(data) > 0
    with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
        namelist = zf.namelist()
        assert "molecule.out" in namelist
        assert "molecule.xyz" in namelist
        out_bytes = zf.read("molecule.out").decode("utf-8")
        assert "ORCA TERMINATED NORMALLY" in out_bytes

    # Test GET request format used by IDM and browser navigation
    resp_get = flask_client.get(f"/api/kaggle/download?kaggle_username=testuser&kaggle_key=testkey12345678901234567890123456789012&job_id={job_id}&mode=essential")
    assert resp_get.status_code == 200
    assert resp_get.headers.get("Content-Type") == "application/zip"
    assert "attachment" in resp_get.headers.get("Content-Disposition", "")
    data_get = resp_get.get_data()
    assert len(data_get) > 0
    with zipfile.ZipFile(io.BytesIO(data_get), "r") as zf_get:
        assert "molecule.out" in zf_get.namelist()

    shutil.rmtree(mock_slug_dir, ignore_errors=True)
    os.environ.pop("ORCA_LOCAL_MODE", None)


def test_download_repaired_zip_when_loose_files(temp_state_dir):
    """Test that if results.zip was missing or corrupt, loose files are bundled into a valid zip."""
    import io
    flask_client = app.test_client()
    os.environ["ORCA_LOCAL_MODE"] = "true"
    os.environ["ORCA_STATE_DIR"] = temp_state_dir
    base_state_dir = temp_state_dir

    job_id = "chem-tools-test-download-loose"
    mock_slug_dir = os.path.join(base_state_dir, "mock_kaggle", job_id)
    os.makedirs(mock_slug_dir, exist_ok=True)

    # Write loose files and a corrupt 0-byte results.zip to trigger fallback repair
    with open(os.path.join(mock_slug_dir, "loose_calc.out"), "w") as fh:
        fh.write("Loose calculation log content\n")
    with open(os.path.join(mock_slug_dir, "loose_calc.xyz"), "w") as fh:
        fh.write("1\n\nH 0.0 0.0 0.0\n")
    with open(os.path.join(mock_slug_dir, "results.zip"), "wb") as fh:
        fh.write(b"corrupt header not a real zip")

    with open(os.path.join(mock_slug_dir, "_mock_meta.json"), "w") as fh:
        json.dump({"status": "complete", "pushed_at": time.time() - 10, "slug": job_id, "owner": "testuser"}, fh)

    resp = flask_client.post("/api/kaggle/download", json={
        "kaggle_username": "testuser",
        "kaggle_key": "testkey12345678901234567890123456789012",
        "job_id": job_id,
    })

    assert resp.status_code == 200
    data = resp.get_data()
    assert len(data) > 0
    with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
        namelist = zf.namelist()
        assert any("loose_calc.out" in n for n in namelist)

    shutil.rmtree(mock_slug_dir, ignore_errors=True)
    os.environ.pop("ORCA_LOCAL_MODE", None)


def test_download_essential_mode_filters_huge_wavefunction_files(temp_state_dir):
    """Test that essential download mode extracts scientific outputs and omits multi-hundred MB GBW/densities."""
    import io
    flask_client = app.test_client()
    os.environ["ORCA_LOCAL_MODE"] = "true"
    os.environ["ORCA_STATE_DIR"] = temp_state_dir
    base_state_dir = temp_state_dir

    job_id = "chem-tools-test-download-essential"
    mock_slug_dir = os.path.join(base_state_dir, "mock_kaggle", job_id)
    os.makedirs(mock_slug_dir, exist_ok=True)

    zip_path = os.path.join(mock_slug_dir, "results.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("calc.out", "Final ORCA output\n****ORCA TERMINATED NORMALLY****\n")
        zf.writestr("calc.xyz", "3\n\nO 0.0 0.0 0.1\nH 0.0 0.7 -0.4\nH 0.0 -0.7 -0.4\n")
        zf.writestr("calc.property.txt", "dipole: 1.85\n")
        zf.writestr("huge_wavefunction.gbw", b"x" * 50000)
        zf.writestr("scratch_density.densities", b"y" * 50000)

    with open(os.path.join(mock_slug_dir, "_mock_meta.json"), "w") as fh:
        json.dump({"status": "complete", "pushed_at": time.time() - 10, "slug": job_id, "owner": "testuser"}, fh)

    # 1. Essential Mode
    resp_ess = flask_client.post("/api/kaggle/download", json={
        "kaggle_username": "testuser",
        "kaggle_key": "testkey12345678901234567890123456789012",
        "job_id": job_id,
        "mode": "essential"
    })
    assert resp_ess.status_code == 200
    data_ess = resp_ess.get_data()
    with zipfile.ZipFile(io.BytesIO(data_ess), "r") as zf:
        names = zf.namelist()
        assert "calc.out" in names
        assert "calc.xyz" in names
        assert "calc.property.txt" in names
        assert "huge_wavefunction.gbw" not in names
        assert "scratch_density.densities" not in names

    # 2. Full Mode
    resp_full = flask_client.post("/api/kaggle/download", json={
        "kaggle_username": "testuser",
        "kaggle_key": "testkey12345678901234567890123456789012",
        "job_id": job_id,
        "mode": "full"
    })
    assert resp_full.status_code == 200
    data_full = resp_full.get_data()
    with zipfile.ZipFile(io.BytesIO(data_full), "r") as zf:
        names_full = zf.namelist()
        assert "calc.out" in names_full
        assert "huge_wavefunction.gbw" in names_full

    shutil.rmtree(mock_slug_dir, ignore_errors=True)
    os.environ.pop("ORCA_LOCAL_MODE", None)


def test_extract_opt_coords_high_precision_decimals(temp_state_dir):
    """Test that coordinates with 10 or 12 decimal places are preserved completely without truncation."""
    flask_client = app.test_client()
    os.environ["ORCA_LOCAL_MODE"] = "true"
    os.environ["ORCA_STATE_DIR"] = temp_state_dir
    base_state_dir = temp_state_dir

    job_id = "chem-tools-test-high-prec-coords"
    mock_slug_dir = os.path.join(base_state_dir, "mock_kaggle", job_id)
    os.makedirs(mock_slug_dir, exist_ok=True)

    high_prec_xyz = """3
High precision water molecule
O   0.000000000000   0.000000000000   0.117289123456
H   0.000000000000   0.757123456789  -0.468898765432
H   0.000000000000  -0.757123456789  -0.468898765432
"""
    zip_path = os.path.join(mock_slug_dir, "results.zip")
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("molecule.xyz", high_prec_xyz)

    with open(os.path.join(mock_slug_dir, "_mock_meta.json"), "w") as fh:
        json.dump({"status": "complete", "pushed_at": time.time() - 10, "slug": job_id, "owner": "testuser"}, fh)

    resp = flask_client.post("/api/kaggle/extract-opt-coords", json={
        "kaggle_username": "testuser",
        "kaggle_key": "testkey12345678901234567890123456789012",
        "job_id": job_id,
    })

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    coords = data["coords"]

    # Verify that all 12 decimal places are retained verbatim
    assert "0.117289123456" in coords
    assert "0.757123456789" in coords
    assert "-0.468898765432" in coords

    # Generate input for next stage and verify coordinates in .inp retain full precision
    gen_resp = flask_client.post("/api/orca/generate", json={
        "calc_type": "sp",
        "theory": "wB97X-D4",
        "basis": "def2-TZVP",
        "coords": coords,
        "name": "stage3_sp"
    })
    assert gen_resp.status_code == 200
    gen_data = gen_resp.get_json()
    assert "0.117289123456" in gen_data["input_text"]
    assert "-0.468898765432" in gen_data["input_text"]

    shutil.rmtree(mock_slug_dir, ignore_errors=True)
    os.environ.pop("ORCA_LOCAL_MODE", None)





