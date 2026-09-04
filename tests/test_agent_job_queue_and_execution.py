# -*- coding: utf-8 -*-
"""Integration test for Companion Agent Bidirectional Job Queue."""
import pytest
from app import app
from services import local_agent_service as las


@pytest.fixture
def client(tmp_path):
    app.config["TESTING"] = True
    db_file = str(tmp_path / "test_queue.db")
    with app.test_client() as c:
        yield c, db_file


def test_agent_job_lifecycle_full_loop(client):
    c, db_path = client
    
    # 1. Initialize agent session
    init_res = las.init_runtime_session(
        installation_id="inst_test_queue",
        agent_session_id="sess_test_queue",
        token_verifiers=[las.hash_token_verifier("CLA_test_token_123")],
        device_name="Test Rig",
        platform="windows",
        backend_kind="local",
        state_dir=str(db_path),
    )
    assert init_res["ok"] is True

    # 2. Claim token via browser
    claim_res = las.claim_runtime_token(
        connection_api="CLA_test_token_123",
        owner_id="chemist_alice",
        custom_device_name="Alice Rig",
        state_dir=str(db_path),
    )
    assert claim_res["ok"] is True

    # 3. Finalize pairing
    fin_res = las.finalize_agent_runtime(
        agent_session_id="sess_test_queue",
        connection_api="CLA_test_token_123",
        state_dir=str(db_path),
    )
    assert fin_res["ok"] is True
    runtime_secret = fin_res["runtime_session_secret"]
    assert runtime_secret is not None

    # 4. Enqueue job
    enq_res = las.enqueue_agent_job(
        agent_session_id="sess_test_queue",
        owner_id="chemist_alice",
        input_text="! B3LYP def2-SVP Opt\n* xyz 0 1\nO 0 0 0\nH 0 0 1\nH 0 1 0\n*",
        job_name="water_opt",
        state_dir=str(db_path),
    )
    assert enq_res["ok"] is True
    job_id = enq_res["job_id"]
    assert enq_res["status"] == "QUEUED"

    # 5. Agent polls for next job
    poll_res = las.poll_next_agent_job(
        agent_session_id="sess_test_queue",
        runtime_session_secret=runtime_secret,
        state_dir=str(db_path),
    )
    assert poll_res["ok"] is True
    job = poll_res["job"]
    assert job is not None
    assert job["job_id"] == job_id
    assert job["job_name"] == "water_opt"
    assert "B3LYP" in job["input_text"]

    # Verify status transitioned to RUNNING
    status_mid = las.get_agent_job_status(job_id=job_id, owner_id="chemist_alice", state_dir=str(db_path))
    assert status_mid["status"] == "RUNNING"

    # 6. Agent updates progress
    prog_res = las.update_agent_job_progress(
        job_id=job_id,
        agent_session_id="sess_test_queue",
        runtime_session_secret=runtime_secret,
        stdout_chunk="SCF ITERATION 1: Energy = -76.382100\n",
        state_dir=str(db_path),
    )
    assert prog_res["ok"] is True

    # 7. Agent completes job
    comp_res = las.complete_agent_job(
        job_id=job_id,
        agent_session_id="sess_test_queue",
        runtime_session_secret=runtime_secret,
        exit_code=0,
        stdout_tail="SCF CONVERGED\nFINAL SINGLE POINT ENERGY -76.382450\n*** ORCA TERMINATED NORMALLY ***",
        parsed_results={"energy_hartree": -76.382450, "converged": True},
        state_dir=str(db_path),
    )
    assert comp_res["ok"] is True
    assert comp_res["status"] == "COMPLETED"

    # 8. Verify final job status query
    final_status = las.get_agent_job_status(job_id=job_id, owner_id="chemist_alice", state_dir=str(db_path))
    assert final_status["ok"] is True
    assert final_status["status"] == "COMPLETED"
    assert final_status["exit_code"] == 0
    assert "ORCA TERMINATED NORMALLY" in final_status["stdout_tail"]
    assert final_status["parsed_results"]["converged"] is True
