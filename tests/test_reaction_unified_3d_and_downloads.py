# -*- coding: utf-8 -*-
"""Integration tests for Unified 3D Reaction Thermochemistry, Queued Execution, Diagram Image & Remote Artifacts."""
import io
import json
import zipfile
import pytest
from app import app, _reaction_store
from services.reaction_workflow_service import (
    resolve_species_3d_geometry, complete_stage_with_output,
    compute_and_store_thermodynamics
)
from services import local_agent_service as las


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_3d_coordinates_resolution():
    """Verify built-in and RDKit 3D coordinate generation for common species."""
    for species in ["H2", "O2", "N2", "H2O", "CO2", "CH4", "NH3"]:
        coords, err = resolve_species_3d_geometry(species)
        assert err is None, f"Error resolving {species}: {err}"
        assert coords is not None and len(coords.splitlines()) >= 2
        # Check standard XYZ format (Element x y z)
        first_line = coords.splitlines()[0].split()
        assert len(first_line) == 4
        assert float(first_line[1]) is not None


def test_unified_reaction_setup_endpoint(client):
    """Verify POST /api/v1/reactions/unified-setup generates unified ORCA inputs for all species."""
    payload = {
        "equation": "2 H2 + O2 -> 2 H2O",
        "workflow_config": {
            "method": "r2SCAN-3c",
            "basis": "def2-mSVP",
            "disp": "D4",
            "solv_model": "none",
            "stages": [
                {"kind": "OPT", "label": "Geometry Optimization", "order": 0},
                {"kind": "FREQ", "label": "Frequency & Thermochemistry", "order": 1}
            ],
            "cores": 4,
            "ram": 2000,
            "backend": "server_local",
        }
    }
    res = client.post("/api/v1/reactions/unified-setup", json=payload)
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    rxn = data["reaction"]
    assert len(rxn["species"]) == 3

    # All species have 3D geometry and matching method/basis in input_text
    for sp in rxn["species"]:
        assert sp["initial_geometry"] is not None
        assert len(sp["stages"]) == 2
        st0 = sp["stages"][0]
        assert st0["kind"] == "OPT"
        assert st0["state"] == "READY"
        assert "r2SCAN-3c" in st0["input_text"]
        assert "Opt" in st0["input_text"]

        st1 = sp["stages"][1]
        assert st1["kind"] == "FREQ"
        assert st1["state"] == "BLOCKED_BY_DEPENDENCY"


def test_diagram_image_and_outputs_zip_download(client):
    """Verify generating publication-quality PNG diagram and outputs ZIP."""
    # Create reaction
    payload = {
        "equation": "2 H2 + O2 -> 2 H2O",
        "workflow_config": {
            "method": "B3LYP",
            "basis": "def2-SVP",
            "stages": [{"kind": "FREQ", "label": "Frequency", "order": 0}]
        }
    }
    setup_res = client.post("/api/v1/reactions/unified-setup", json=payload)
    rxn_id = setup_res.get_json()["reaction"]["reaction_id"]

    # Mock completion for all species to compute thermodynamics
    # Sample minimal ORCA FREQ output snippet with valid energy and frequencies
    fake_freq_output = """
    ! B3LYP def2-SVP Freq
    FINAL SINGLE POINT ENERGY      -76.382450
    Total Enthalpy           ...   -76.350000 Eh
    Final Gibbs free energy  ...   -76.370000 Eh
    Total thermal energy     ...   -76.355000 Eh
    Total correction to G    ...     0.012450 Eh
    Total correction to H    ...     0.032450 Eh
    Non-vibrational thermal  ...     0.002840 Eh
    Total Entropy            ...    45.120000 cal/mol-K
    Zero point energy        ...     0.021000 Eh
    VIBRATIONAL FREQUENCIES
    -----------------------
    0:         0.00 cm**-1
    1:         0.00 cm**-1
    2:         0.00 cm**-1
    3:         0.00 cm**-1
    4:         0.00 cm**-1
    5:         0.00 cm**-1
    6:      1590.20 cm**-1
    7:      3650.10 cm**-1
    8:      3750.40 cm**-1
    CARTESIAN COORDINATES (ANGSTROEM)
    O   0.000000   0.000000   0.117300
    H   0.000000   0.757200  -0.469200
    H   0.000000  -0.757200  -0.469200
    THERMOCHEMISTRY AT 298.15 K
    ****ORCA TERMINATED NORMALLY****
    """

    rxn_data = setup_res.get_json()["reaction"]
    rxn_id = rxn_data["reaction_id"]
    rxn_owner = rxn_data["owner"]
    rxn = _reaction_store.get_reaction(rxn_owner, rxn_id)
    for sp in rxn["species"]:
        st = sp["stages"][0]
        # Complete stage
        complete_stage_with_output(rxn, sp["species_id"], st["stage_id"], fake_freq_output, _reaction_store)

    # Compute thermodynamics
    thermo = compute_and_store_thermodynamics(rxn, _reaction_store)
    assert thermo is not None

    # Test PNG Diagram generation endpoint
    img_res = client.get(f"/api/v1/reactions/{rxn_id}/thermodynamics/image")
    assert img_res.status_code == 200
    assert img_res.mimetype == "image/png"
    assert len(img_res.data) > 1000
    # Verify PNG header
    assert img_res.data[:8] == b"\x89PNG\r\n\x1a\n"

    # Test Download All Outputs ZIP endpoint
    zip_res = client.get(f"/api/v1/reactions/{rxn_id}/download-all-outputs")
    assert zip_res.status_code == 200
    assert zip_res.mimetype == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(zip_res.data))
    namelist = zf.namelist()
    assert "reaction_summary.json" in namelist
    summary = json.loads(zf.read("reaction_summary.json"))
    assert summary["reaction_id"] == rxn_id


def test_companion_agent_output_upload_and_download(client, tmp_path):
    """Verify companion agent uploading .out and .xyz and web user downloading them."""
    db_file = str(tmp_path / "test_agent_artifacts.db")

    # 1. Init runtime session
    las.init_runtime_session(
        installation_id="inst_art_test",
        agent_session_id="sess_art_test",
        token_verifiers=[las.hash_token_verifier("CLA_art_test_123")],
        state_dir=db_file,
    )
    # 2. Claim
    las.claim_runtime_token("CLA_art_test_123", owner_id="chemist_bob", state_dir=db_file)
    # 3. Finalize
    fin = las.finalize_agent_runtime("sess_art_test", "CLA_art_test_123", state_dir=db_file)
    secret = fin["runtime_session_secret"]

    # 4. Enqueue job
    enq = las.enqueue_agent_job("sess_art_test", "chemist_bob", "! Opt\n* xyz 0 1\nO 0 0 0\n*", "water_opt", state_dir=db_file)
    job_id = enq["job_id"]

    # 5. Agent completes job and attaches .out log and .xyz structure
    fake_out = "ORCA LOG CONTENT\nFINAL SINGLE POINT ENERGY -76.38\n*** ORCA TERMINATED NORMALLY ***"
    fake_xyz = "1\n\nO 0.000000 0.000000 0.000000"

    las.complete_agent_job(
        job_id=job_id,
        agent_session_id="sess_art_test",
        runtime_session_secret=secret,
        exit_code=0,
        stdout_tail=fake_out,
        output_text=fake_out,
        xyz_structure=fake_xyz,
        state_dir=db_file,
    )

    # 6. Retrieve artifacts
    art = las.get_job_artifacts(job_id=job_id, owner_id="chemist_bob", state_dir=db_file)
    assert art["ok"] is True
    assert art["output_text"] == fake_out
    assert art["xyz_structure"] == fake_xyz

    # 7. Build job artifacts zip
    zip_bytes, fname = las.build_job_artifacts_zip(job_id=job_id, owner_id="chemist_bob", state_dir=db_file)
    assert fname.endswith(".zip")
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    assert f"water_opt.out" in zf.namelist()
    assert f"water_opt.xyz" in zf.namelist()
    assert zf.read("water_opt.out").decode("utf-8") == fake_out
    assert zf.read("water_opt.xyz").decode("utf-8") == fake_xyz


def test_lowercase_equation_unified_setup_and_start_execution(client):
    """Verify lowercase reaction equations (e.g. 2 h2 + o2 -> 2 h2o) and start execution."""
    payload = {
        "equation": "2 h2 + o2 -> 2 h2o",
        "stages_preset": "opt_freq",
        "method": "B3LYP",
        "basis_set": "def2-SVP",
        "dispersion": "D3BJ",
        "solv_model": "none",
        "target_host": "server_local",
        "max_concurrency": 1
    }
    res = client.post("/api/v1/reactions/unified-setup", json=payload)
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert data["reaction_id"] is not None
    assert data["stages_count"] == 6

    rxn = data["reaction"]
    species_names = [s["formula"] for s in rxn["species"]]
    assert species_names == ["H2", "O2", "H2O"]

    # Start execution via start-execution endpoint
    rxn_id = data["reaction_id"]
    start_res = client.post(f"/api/v1/reactions/{rxn_id}/start-execution", json={"max_concurrency": 1})
    assert start_res.status_code == 200
    start_data = start_res.get_json()
    assert start_data["ok"] is True
    assert start_data["reaction"]["state"] == "RUNNING"

