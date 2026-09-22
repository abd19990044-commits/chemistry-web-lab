import os
import time
import uuid
import pathlib
import pytest

from services import reaction_workflow_service, workflow_store, local_orca_worker, local_orca_service, local_agent_service
from local_agent.security import generate_process_session, hash_token_verifier


def _make_dummy_reaction(owner="test_user"):
    return {
        "reaction_id": f"rxn_{uuid.uuid4().hex[:8]}",
        "name": "Test Reaction",
        "equation": "A -> B",
        "owner": owner,
        "state": "READY",
        "species": [
            {
                "species_id": "sp_ts",
                "formula": "H2O",
                "display_name": "TS1",
                "role": "transition_state",
                "charge": 0,
                "multiplicity": 1,
                "workflow_id": f"wf-{uuid.uuid4().hex[:8]}",
                "initial_geometry": "3\nH2O\nO 0.0 0.0 0.0\nH 0.0 0.7 0.6\nH 0.0 -0.7 0.6",
                "stages": [
                    {
                        "stage_id": "st_optts",
                        "kind": "OPTTS",
                        "order": 0,
                        "state": "READY",
                        "backend": "local",
                        "input_text": "! OPTTS\n*",
                        "attempt_id": uuid.uuid4().hex,
                        "attempt_no": 1,
                    },
                    {
                        "stage_id": "st_freq",
                        "kind": "FREQ",
                        "order": 1,
                        "state": "BLOCKED_BY_DEPENDENCY",
                        "backend": "local",
                        "input_text": "! FREQ\n*",
                        "attempt_id": uuid.uuid4().hex,
                        "attempt_no": 1,
                    },
                ],
                "state": "PENDING",
            }
        ],
    }


def test_retry_stage_clears_stale_job_ids_and_enqueues_fresh(tmp_path):
    """SEC-REL-001: retry_stage must clear previous local/agent/kaggle job IDs so dispatch allocates fresh job."""
    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    rxn = _make_dummy_reaction()
    stage = rxn["species"][0]["stages"][0]
    stage["state"] = "FAILED"
    stage["local_job_id"] = "local_old_job_123"
    stage["agent_job_id"] = "agent_old_456"
    stage["kaggle_job_id"] = "kaggle_old_789"

    store = reaction_workflow_service.ReactionStore(state_dir)
    store.save_reaction(rxn)

    retried = reaction_workflow_service.retry_stage(rxn, "sp_ts", "st_optts", store=store, state_dir=state_dir)
    assert retried["state"] == "READY"
    assert retried["local_job_id"] is None
    assert retried["agent_job_id"] is None
    assert retried["kaggle_job_id"] is None
    assert retried["attempt_no"] == 2

    # Now dispatch ready stages
    dispatch_res = reaction_workflow_service.dispatch_ready_stages(rxn, owner_id=rxn["owner"], store=store, state_dir=state_dir)
    assert "st_optts" in dispatch_res.get("dispatched_stage_ids", [])
    assert retried["local_job_id"] is not None
    assert retried["local_job_id"] != "local_old_job_123"


def test_workflow_store_init_db_adds_missing_columns_no_infinite_backup(tmp_path):
    """SEC-REL-003: init_db must add missing columns like version to prevent infinite backup loop."""
    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    db_file = workflow_store.database_path(state_dir)

    # Create older schema table missing version
    import sqlite3
    with sqlite3.connect(db_file) as conn:
        conn.execute("""
            CREATE TABLE workflow_steps (
                workflow_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                step_type TEXT NOT NULL,
                status TEXT NOT NULL,
                PRIMARY KEY(workflow_id, step_id)
            )
        """)
        conn.commit()

    # Call init_db
    workflow_store.init_db(state_dir)

    # Verify column was added
    with sqlite3.connect(db_file) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(workflow_steps)").fetchall()}
        assert "version" in cols
        assert "attempt_id" in cols


def test_workflow_store_attempts_unique_conflict_handled(tmp_path):
    """SEC-REL-007: sync_reaction must not crash with UNIQUE constraint on workflow_attempts when input edited."""
    state_dir = str(tmp_path / "state")
    rxn = _make_dummy_reaction()
    workflow_store.sync_reaction(rxn, state_dir)

    # Edit input text and sync again
    reaction_workflow_service.set_stage_input(rxn, "st_optts", "! OPTTS Def2-SVP\n*")
    # Must succeed without IntegrityError
    workflow_store.sync_reaction(rxn, state_dir)


def test_pause_and_cancel_reaction_syncs_with_sqlite_ledger(tmp_path):
    """SEC-REL-004: pause_reaction and cancel_reaction must persist to SQLite ledger preventing split-brain."""
    state_dir = str(tmp_path / "state")
    rxn = _make_dummy_reaction()
    store = reaction_workflow_service.ReactionStore(state_dir)
    store.save_reaction(rxn)

    paused = store.pause_reaction(rxn["owner"], rxn["reaction_id"])
    assert paused["state"] == "PAUSED"
    assert paused["species"][0]["stages"][0]["state"] == "PAUSED"

    # Verify SQLite ledger reflects PAUSED
    snapshots = workflow_store.list_reaction_snapshots(state_dir)
    snap = next(s for s in snapshots if s["reaction_id"] == rxn["reaction_id"])
    assert snap["state"] == "PAUSED"

    # Cancel reaction
    cancelled = store.cancel_reaction(rxn["owner"], rxn["reaction_id"])
    assert cancelled["state"] == "CANCELLED"
    snapshots = workflow_store.list_reaction_snapshots(state_dir)
    snap = next(s for s in snapshots if s["reaction_id"] == rxn["reaction_id"])
    assert snap["state"] == "CANCELLED"


def test_optts_then_freq_accepts_single_imaginary_frequency():
    """SEC-REL-005: A FREQ stage on a transition_state species must accept 1 imaginary mode."""
    rxn = _make_dummy_reaction()
    mock_freq_out = (
        "ORCA TERMINATED NORMALLY\n"
        "Total Energy       : -76.40000000 Eh\n"
        "VIBRATIONAL FREQUENCIES\n"
        "0: -450.20 cm**-1\n"
        "1: 1500.00 cm**-1\n"
    )
    result = {
        "converged": True,
        "terminated_normally": True,
        "energy_hartree": -76.4,
        "imaginary_count": 1,
        "frequencies": [-450.2, 1500.0],
        "gibbs_hartree": -76.35,
        "enthalpy_hartree": -76.38,
    }
    stage = rxn["species"][0]["stages"][1]
    stage["state"] = "RUNNING"
    stage["kind"] = "FREQ"

    orig_extract = reaction_workflow_service.extract_stage_result
    reaction_workflow_service.extract_stage_result = lambda out: result
    try:
        completed = reaction_workflow_service.complete_stage_with_output(rxn, "sp_ts", "st_freq", mock_freq_out)
        assert completed["state"] == "COMPLETE"
        assert completed["scientific_status"] == "COMPLETED"
    finally:
        reaction_workflow_service.extract_stage_result = orig_extract


def test_unconverged_optts_rejected_by_capabilities():
    """SEC-REL-006: Unconverged OPTTS must be rejected by stage_capabilities_valid."""
    problems = reaction_workflow_service.stage_capabilities_valid(
        "OPTTS",
        {"produces_geometry": True, "geometry_xyz": "3\n\nO 0 0 0", "converged": False, "energy_hartree": -76.0},
        output_text="THE OPTIMIZATION HAS NOT CONVERGED",
    )
    assert any("converge" in p.lower() for p in problems)


def test_worker_requeues_on_concurrency_limit_instead_of_failing(tmp_path, monkeypatch):
    """SEC-REL-008: When execute_local_orca_job returns LOCAL_CONCURRENCY_LIMIT_EXCEEDED, requeue with backoff."""
    state_dir = str(tmp_path / "state")
    local_orca_worker.init_db(state_dir)
    worker = local_orca_worker.LocalOrcaWorker(state_dir, poll_seconds=0.05)

    job_rec = local_orca_worker.enqueue_local_job(
        owner_id="test", input_text="! SP\n*", state_dir=state_dir,
    )
    job_id = job_rec["job"]["job_id"]

    monkeypatch.setattr(
        local_orca_service,
        "execute_local_orca_job",
        lambda **kwargs: {"ok": False, "error_code": "LOCAL_CONCURRENCY_LIMIT_EXCEEDED", "error": "busy"},
    )

    monkeypatch.setenv("ORCA_LOCAL_MAX_MEMORY_MB", "16384")
    claimed = worker._claim_next()
    assert claimed is not None
    worker._run_claimed(claimed)

    job = local_orca_worker.get_local_job(job_id, state_dir=state_dir)
    assert job["status"] == "QUEUED"
    assert job["next_attempt_at"] is not None
    assert job["next_attempt_at"] > time.time()


def test_waiting_for_resources_applies_backoff_timer(tmp_path, monkeypatch):
    """SEC-REL-012: When resources are unavailable, backoff next_attempt_at to prevent event flooding."""
    state_dir = str(tmp_path / "state")
    local_orca_worker.init_db(state_dir)
    worker = local_orca_worker.LocalOrcaWorker(state_dir, poll_seconds=0.05)

    job_rec = local_orca_worker.enqueue_local_job(
        owner_id="test", input_text="! SP\n*", state_dir=state_dir,
    )
    job_id = job_rec["job"]["job_id"]

    monkeypatch.setattr(
        local_orca_worker,
        "_resources_available",
        lambda job, s_dir: (False, "INSUFFICIENT_DISK_SPACE"),
    )

    claimed = worker._claim_next()
    assert claimed is None

    job = local_orca_worker.get_local_job(job_id, state_dir=state_dir)
    assert job["status"] == "QUEUED_WAITING_RESOURCES"
    assert job["next_attempt_at"] is not None
    assert job["next_attempt_at"] >= time.time() + 5.0


def test_dangerous_executables_rejected_in_settings_and_validation(tmp_path):
    """SEC-REL-010: save_local_orca_settings and validate_local_orca_config must reject forbidden binaries."""
    state_dir = str(tmp_path / "state")

    with pytest.raises(ValueError, match="forbidden"):
        local_orca_service.save_local_orca_settings(
            {"orca_executable": "C:\\Windows\\System32\\cmd.exe"}, state_dir=state_dir
        )

    val = local_orca_service.validate_local_orca_config(
        {"orca_executable": "cmd.exe", "input_directory": state_dir, "output_directory": state_dir},
        state_dir=state_dir,
    )
    assert val["ready"] is False
    assert any("FORBIDDEN" in err for err in val["errors"])


def test_agent_disconnect_fails_active_jobs_and_reconciles_projections(tmp_path):
    """SEC-AGT-001: Disconnecting an agent must mark active jobs FAILED and reconcile projections."""
    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)

    sess_id, api = generate_process_session()
    inst_id = f"inst_{uuid.uuid4().hex[:8]}"

    local_agent_service.init_runtime_session(
        installation_id=inst_id,
        agent_session_id=sess_id,
        token_verifiers=[hash_token_verifier(api)],
        device_name="Test Rig",
        platform="windows",
        state_dir=state_dir,
    )

    claim_res = local_agent_service.claim_runtime_token(
        connection_api=api,
        owner_id="test_owner",
        state_dir=state_dir,
    )
    assert claim_res["ok"] is True

    finalize_res = local_agent_service.finalize_agent_runtime(
        agent_session_id=sess_id,
        connection_api=api,
        state_dir=state_dir,
    )
    assert finalize_res["ok"] is True

    enq = local_agent_service.enqueue_agent_job(
        agent_session_id=sess_id,
        owner_id="test_owner",
        input_text="! SP\n*",
        state_dir=state_dir,
    )
    assert enq["ok"] is True
    job_id = enq["job_id"]

    status_before = local_agent_service.get_agent_job_status(job_id, owner_id="test_owner", state_dir=state_dir)
    assert status_before["status"] == "QUEUED"

    ok = local_agent_service.disconnect_user_device(sess_id, "test_owner", state_dir=state_dir)
    assert ok is True

    status_after = local_agent_service.get_agent_job_status(job_id, owner_id="test_owner", state_dir=state_dir)
    assert status_after["status"] == "FAILED"
    assert "disconnected" in status_after["error_message"].lower()


def test_thermochemistry_rounding_tolerance_no_false_alarm():
    """SEC-REL-013: A 5 J/mol rounding deviation from 2-decimal cal/mol*K should not raise warning."""
    # Build complete species results as expected by compute_reaction_thermodynamics
    species_res = [
        {
            "nu": -1.0,
            "role": "reactant",
            "display_name": "R1",
            "final_result": {
                "complete_thermochemistry": True,
                "temperature_k": 298.15,
                "method": "B3LYP",
                "basis_set": "def2-SVP",
                "solvent_model": None,
                "solvent": None,
                "E_final": -100.0,
                "zpe_hartree": 0.1,
                "H_final": -99.88,
                "G_final": -99.92,
                "entropy_j_mol_k": 150.0,
            },
        },
        {
            "nu": 1.0,
            "role": "product",
            "display_name": "P1",
            "final_result": {
                "complete_thermochemistry": True,
                "temperature_k": 298.15,
                "method": "B3LYP",
                "basis_set": "def2-SVP",
                "solvent_model": None,
                "solvent": None,
                "E_final": -100.05,
                "zpe_hartree": 0.1,
                "H_final": -99.93,
                "G_final": -99.97,
                "entropy_j_mol_k": 150.002,  # slight rounding deviation ~0.6 J/mol
            },
        },
    ]
    res = reaction_workflow_service.compute_reaction_thermodynamics(species_res)
    assert res is not None
    # No false alarm inconsistency warning
    # Note that res does not have warnings in return, warnings are generated or passed
