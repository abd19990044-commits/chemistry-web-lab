"""Phase 2 acceptance tests for the durable server-local ORCA worker."""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import threading
import time

from services import local_orca_service
from services import local_orca_worker as worker_module
from services.local_orca_worker import (
    LocalOrcaWorker,
    enqueue_local_job,
    get_local_job,
    request_cancel_local_job,
)

try:
    from tests.test_local_orca_backend import _make_fake_orca_script
except ImportError:
    from test_local_orca_backend import _make_fake_orca_script


def _settings(tmp_path: pathlib.Path, state_dir: pathlib.Path):
    fake = _make_fake_orca_script(tmp_path)
    inp = tmp_path / "inputs"
    out = tmp_path / "outputs"
    inp.mkdir()
    out.mkdir()
    local_orca_service.save_local_orca_settings(
        {
            "orca_executable": str(fake),
            "input_directory": str(inp),
            "output_directory": str(out),
        },
        str(state_dir),
    )
    return fake


def test_durable_worker_claims_and_completes_job(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _settings(tmp_path, state_dir)
    monkeypatch.setenv("FAKE_ORCA_MODE", "VALID_OPT")

    first = enqueue_local_job(
        owner_id="owner-a",
        input_text="! Opt\n* xyz 0 1\n*",
        stage_kind="OPT",
        resources={"nprocs": 1, "maxcore_mb": 128},
        state_dir=str(state_dir),
        idempotency_key="phase2-submit-1",
    )
    replay = enqueue_local_job(
        owner_id="owner-a",
        input_text="! Opt\n* xyz 0 1\n*",
        stage_kind="OPT",
        resources={"nprocs": 1, "maxcore_mb": 128},
        state_dir=str(state_dir),
        idempotency_key="phase2-submit-1",
    )
    assert replay["replayed"] is True
    assert replay["job"]["job_id"] == first["job"]["job_id"]

    worker = LocalOrcaWorker(str(state_dir), poll_seconds=0.05)
    assert worker.run_once() is True
    job = get_local_job(first["job"]["job_id"], state_dir=str(state_dir))
    assert job["status"] == "COMPLETED"
    assert job["return_code"] == 0
    assert "ORCA TERMINATED NORMALLY" in (job["result"] or {}).get("output_text", "").upper()
    assert job["revision"] >= 3


def test_two_workers_cannot_claim_two_local_jobs(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _settings(tmp_path, state_dir)
    monkeypatch.setenv("FAKE_ORCA_MODE", "VALID_OPT")
    first = enqueue_local_job(owner_id="owner-a", input_text="! Opt\n*", resources={"nprocs": 1, "maxcore_mb": 128}, state_dir=str(state_dir), idempotency_key="job-1")
    second = enqueue_local_job(owner_id="owner-a", input_text="! Opt\n*", resources={"nprocs": 1, "maxcore_mb": 128}, state_dir=str(state_dir), idempotency_key="job-2")

    worker_a = LocalOrcaWorker(str(state_dir), poll_seconds=0.05)
    worker_b = LocalOrcaWorker(str(state_dir), poll_seconds=0.05)
    claim = worker_a._claim_next()
    assert claim is not None
    assert claim.job["job_id"] == first["job"]["job_id"]
    assert worker_b._claim_next() is None
    assert get_local_job(second["job"]["job_id"], state_dir=str(state_dir))["status"] == "QUEUED"


def test_new_worker_adopts_live_process_without_duplicate_execution(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _settings(tmp_path, state_dir)
    launch_file = tmp_path / "launches.log"
    monkeypatch.setenv("FAKE_ORCA_MODE", "VALID_OPT")
    monkeypatch.setenv("FAKE_ORCA_DELAY", "4.0")
    monkeypatch.setenv("FAKE_ORCA_LAUNCH_COUNT_FILE", str(launch_file))

    queued = enqueue_local_job(
        owner_id="owner-a", input_text="! Opt\n* xyz 0 1\n*", stage_kind="OPT",
        resources={"nprocs": 1, "maxcore_mb": 128}, state_dir=str(state_dir)
    )
    old_worker = LocalOrcaWorker(str(state_dir), poll_seconds=0.05)
    claim = old_worker._claim_next()
    assert claim is not None

    execution_result = {}

    def execute_old_process():
        execution_result.update(
            local_orca_service.execute_local_orca_job(
                job_id=claim.job["job_id"],
                input_text=claim.job["input_text"],
                attempt_id=claim.job["attempt_id"],
                stage_kind="OPT",
                state_dir=str(state_dir),
            )
        )

    old_process = threading.Thread(target=execute_old_process, daemon=True)
    old_process.start()
    active = None
    for _ in range(400):
        active = local_orca_service._read_active_process_file(str(state_dir))
        if active and active.get("pid"):
            break
        time.sleep(0.025)
    assert active and active.get("pid")
    new_worker = LocalOrcaWorker(str(state_dir), poll_seconds=0.05)
    old_worker._heartbeat(claim, active)

    # A second worker must not steal an unexpired lease, even when it can see
    # the same live ORCA process.
    assert new_worker.reconcile_jobs()["adopted"] == 0
    # Expire the persisted lease deterministically; wall-clock sleeps made this
    # fencing assertion flaky on busy Windows CI hosts.
    with sqlite3.connect(worker_module.db_path(str(state_dir))) as conn:
        conn.execute(
            "UPDATE local_jobs SET lease_expires_at=? WHERE job_id=?",
            (time.time() - 1.0, claim.job["job_id"]),
        )
        conn.commit()
    reconciliation = new_worker.reconcile_jobs()
    assert reconciliation["adopted"] == 1
    assert new_worker.run_once() is True
    old_process.join(timeout=5)
    assert not old_process.is_alive()

    job = get_local_job(queued["job"]["job_id"], state_dir=str(state_dir))
    assert job["status"] in {"COMPLETED", "COMPLETED_WITH_WARNINGS"}
    assert job["result"]["return_code_unknown"] is True
    assert len(launch_file.read_text(encoding="utf-8").splitlines()) == 1
    assert execution_result.get("ok") is True


def test_cancel_running_worker_job_kills_process_and_is_terminal(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _settings(tmp_path, state_dir)
    monkeypatch.setenv("FAKE_ORCA_MODE", "VALID_OPT")
    monkeypatch.setenv("FAKE_ORCA_DELAY", "10")

    queued = enqueue_local_job(owner_id="owner-a", input_text="! Opt\n*", stage_kind="OPT",
                               resources={"nprocs": 1, "maxcore_mb": 128}, state_dir=str(state_dir))
    worker = LocalOrcaWorker(str(state_dir), poll_seconds=0.05)
    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()
    job_id = queued["job"]["job_id"]
    for _ in range(100):
        current = get_local_job(job_id, state_dir=str(state_dir))
        if current["status"] in {"RUNNING", "CANCEL_REQUESTED"}:
            break
        time.sleep(0.03)
    result = request_cancel_local_job(job_id, owner_id="owner-a", state_dir=str(state_dir))
    assert result["ok"] is True
    for _ in range(120):
        current = get_local_job(job_id, state_dir=str(state_dir))
        if current["status"] in {"CANCELLED", "FAILED"}:
            break
        time.sleep(0.05)
    worker.stop_event.set()
    thread.join(timeout=5)
    current = get_local_job(job_id, state_dir=str(state_dir))
    assert current["status"] == "CANCELLED"


def test_recovery_required_job_blocks_new_local_execution(tmp_path, monkeypatch):
    """An ambiguous old PID is a safety hold, not a free resource slot."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _settings(tmp_path, state_dir)
    first = enqueue_local_job(
        owner_id="owner-a", input_text="! Opt\n*", stage_kind="OPT",
        resources={"nprocs": 1, "maxcore_mb": 128},
        state_dir=str(state_dir), idempotency_key="recovery-first",
    )
    second = enqueue_local_job(
        owner_id="owner-a", input_text="! Opt\n*", stage_kind="OPT",
        resources={"nprocs": 1, "maxcore_mb": 128},
        state_dir=str(state_dir), idempotency_key="recovery-second",
    )
    with worker_module._connect(worker_module.db_path(str(state_dir))) as conn:
        conn.execute(
            "UPDATE local_jobs SET status='RECOVERY_REQUIRED', lease_expires_at=NULL "
            "WHERE job_id=?",
            (first["job"]["job_id"],),
        )
        conn.commit()
    worker = LocalOrcaWorker(str(state_dir), poll_seconds=0.05)
    assert worker._claim_next() is None
    assert get_local_job(second["job"]["job_id"], state_dir=str(state_dir))["status"] == "QUEUED"


def test_completion_proven_before_late_cancel_wins_deterministically(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    queued = enqueue_local_job(
        owner_id="owner-a", input_text="! SP\n*", stage_kind="SP",
        resources={"nprocs": 1, "maxcore_mb": 128},
        state_dir=str(state_dir), idempotency_key="cancel-race",
    )
    worker = LocalOrcaWorker(str(state_dir), poll_seconds=0.05)
    claim = worker._claim_next()
    assert claim is not None, get_local_job(
        queued["job"]["job_id"], state_dir=str(state_dir)
    )
    process_finished_at = time.time()
    time.sleep(0.01)
    request_cancel_local_job(
        queued["job"]["job_id"], owner_id="owner-a", state_dir=str(state_dir)
    )
    assert worker._finalize(
        claim,
        {
            "ok": True,
            "exit_code": 0,
            "process_finished_at": process_finished_at,
            "output_text": "FINAL SINGLE POINT ENERGY -1.0\nORCA TERMINATED NORMALLY",
        },
    )
    assert get_local_job(
        queued["job"]["job_id"], state_dir=str(state_dir)
    )["status"] == "COMPLETED"
