# -*- coding: utf-8 -*-
"""True Separate-Process Agent Transport and Real ORCA 6.1.0 E2E Test Suite."""
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import httpx
import pytest

from services import local_orca_service, local_agent_service

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_ORCA_EXE = r"D:\orca6\Orca6.1.0.Win64\orca.exe"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def live_agent_system():
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    tmpdir = tempfile.mkdtemp()
    agent_data_dir = os.path.join(tmpdir, "agent_data")
    os.makedirs(agent_data_dir, exist_ok=True)

    env = os.environ.copy()
    env["CHEMISTRY_LAB_STATE_DIR"] = tmpdir
    env["PYTHONPATH"] = REPO + ";" + os.path.join(REPO, "orca_engine", "src")
    env["CHEMISTRY_LAB_AGENT_DATA_DIR"] = agent_data_dir

    # Start FastAPI server process
    server_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "api.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=REPO,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for server ready
    deadline = time.time() + 90
    ready = False
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base_url}/api/v1/ready", timeout=2)
            if r.status_code == 200:
                ready = True
                break
        except Exception:
            time.sleep(0.5)

    if not ready:
        server_proc.kill()
        pytest.skip("FastAPI server did not become ready within timeout")

    # Start separate Agent process
    agent_proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-m",
            "local_agent.agent",
            "--server",
            base_url,
            "--device-name",
            "Test Separate PC",
            "--token-count",
            "1",
            "--data-dir",
            agent_data_dir,
        ],
        cwd=REPO,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    pairing_txt = os.path.join(agent_data_dir, "chemistry_lab_pairing.txt")
    token = None
    sess_id = None
    t_txt = time.time() + 30
    while time.time() < t_txt:
        if os.path.isfile(pairing_txt):
            try:
                with open(pairing_txt, "r", encoding="utf-8") as f:
                    content = f.read()
                m = re.search(r"CLA_[A-Za-z0-9_-]+", content)
                s = re.search(r"sess_[A-Za-z0-9]+", content)
                if m and s:
                    token = m.group(0)
                    sess_id = s.group(0)
                    break
            except Exception:
                pass
        time.sleep(0.5)

    # Allow agent init_server_session to complete
    time.sleep(2)

    yield {
        "base_url": base_url,
        "token": token,
        "sess_id": sess_id,
        "pairing_txt": pairing_txt,
        "state_dir": tmpdir,
        "agent_data_dir": agent_data_dir,
    }

    # Teardown
    agent_proc.terminate()
    try:
        agent_proc.wait(timeout=5)
    except Exception:
        agent_proc.kill()

    server_proc.terminate()
    try:
        server_proc.wait(timeout=5)
    except Exception:
        server_proc.kill()


def test_separate_process_agent_claim_and_txt_cleanup(live_agent_system):
    base_url = live_agent_system["base_url"]
    token = live_agent_system["token"]
    pairing_txt = live_agent_system["pairing_txt"]

    assert token is not None, "Agent must generate and write Connection API"
    assert os.path.isfile(pairing_txt), "Pairing TXT must exist before claim"

    # Website claims API over HTTP
    claim_resp = httpx.post(
        f"{base_url}/api/v1/local-agent/runtime/claim",
        headers={"X-User-Id": "scientist_ahmed"},
        json={"connection_api": token, "custom_device_name": "Physical Rig B"},
        timeout=10,
    )
    assert claim_resp.status_code == 200, f"Claim failed: {claim_resp.text}"
    claim_data = claim_resp.json()
    assert claim_data["ok"] is True
    assert "runtime_session_secret" not in claim_data, "Browser must NOT receive runtime secret!"

    # Wait for agent finalize poll and assert pairing TXT is deleted immediately
    time.sleep(2)
    assert not os.path.isfile(pairing_txt), "Pairing TXT must be deleted immediately upon claim"


def test_separate_process_agent_real_orca_e2e(live_agent_system):
    if not os.path.isfile(REAL_ORCA_EXE):
        pytest.skip(f"Real ORCA not installed at {REAL_ORCA_EXE}")

    state_dir = live_agent_system["state_dir"]
    input_dir = os.path.join(state_dir, "inputs")
    output_dir = os.path.join(state_dir, "outputs")
    working_dir = os.path.join(state_dir, "work")
    for d in (input_dir, output_dir, working_dir):
        os.makedirs(d, exist_ok=True)

    raw_inp = """! HF STO-3G SP
* xyz 0 1
O 0.000000 0.000000 0.117790
H 0.000000 0.755453 -0.471161
H 0.000000 -0.755453 -0.471161
*
"""
    resource_spec = {"cpu_cores": 1, "ram_gb": 4.0, "disk_gb": 20.0}
    injected_inp = local_agent_service.inject_orca_resources(raw_inp, resource_spec)

    agent_settings = {
        "enabled": True,
        "orca_executable": REAL_ORCA_EXE,
        "input_directory": input_dir,
        "output_directory": output_dir,
        "working_directory": working_dir,
        "concurrency": 1,
        "timeout_seconds": 60,
    }
    local_orca_service.save_local_orca_settings(agent_settings, state_dir=state_dir)

    exec_res = local_orca_service.execute_local_orca_job(
        job_id="true_proc_e2e_water_sp",
        input_text=injected_inp,
        settings=agent_settings,
        state_dir=state_dir,
        timeout_seconds=60,
    )
    assert exec_res["ok"] is True
    assert exec_res["process_ok"] is True
    assert exec_res["parse_ok"] is True
    assert exec_res["scientific_ok"] is True
    assert exec_res["exit_code"] == 0

    parsed = exec_res.get("parsed", {})
    energy = (
        parsed.get("energy_hartree")
        or parsed.get("final_energy_hartree")
        or parsed.get("electronic_energy_hartree")
    )
    assert energy is not None
    assert abs(energy - (-74.963146775728)) < 1e-4
