"""Persistent My Jobs visibility and cancellation for waiting calculations."""
from __future__ import annotations

from app import app
from services import local_agent_service as agent_service
from services import local_orca_worker


def _login(client, owner: str) -> None:
    with client.session_transaction() as browser_session:
        browser_session["user"] = {"sub": owner}


def _queued_agent_job(state_dir: str, owner: str = "chemist") -> str:
    token = "CLA_waiting_jobs_token"
    initialized = agent_service.init_runtime_session(
        installation_id="inst_waiting_jobs",
        agent_session_id="sess_waiting_jobs",
        token_verifiers=[agent_service.hash_token_verifier(token)],
        device_name="Waiting Jobs Rig",
        platform="windows",
        backend_kind="local",
        state_dir=state_dir,
    )
    assert initialized["ok"] is True
    claimed = agent_service.claim_runtime_token(
        connection_api=token,
        owner_id=owner,
        custom_device_name="Waiting Jobs Rig",
        state_dir=state_dir,
    )
    assert claimed["ok"] is True
    queued = agent_service.enqueue_agent_job(
        agent_session_id="sess_waiting_jobs",
        owner_id=owner,
        input_text="! B3LYP def2-SVP\n* xyz 0 1\nH 0 0 0\nH 0 0 1\n*",
        job_name="queued-agent-job",
        state_dir=state_dir,
    )
    assert queued["ok"] is True
    return queued["job_id"]


def test_my_jobs_lists_and_cancels_waiting_local_agent_job(tmp_path, monkeypatch):
    state_dir = str(tmp_path / "state")
    monkeypatch.setenv("CHEMISTRY_LAB_STATE_DIR", state_dir)
    job_id = _queued_agent_job(state_dir)

    app.config["TESTING"] = True
    with app.test_client() as client:
        _login(client, "chemist")
        listed = client.get("/api/v1/jobs")
        assert listed.status_code == 200
        waiting = next(job for job in listed.get_json()["jobs"] if job["jobId"] == job_id)
        assert waiting["backend"] == "local_agent"
        assert waiting["status"] == "queued"
        assert waiting["cancellable"] is True
        assert waiting["waitingReason"]

        cancelled = client.post(f"/api/v1/jobs/{job_id}/cancel", json={})
        assert cancelled.status_code == 200
        assert cancelled.get_json()["status"] == "CANCELLED"

        listed_again = client.get("/api/v1/jobs").get_json()["jobs"]
        final = next(job for job in listed_again if job["jobId"] == job_id)
        assert final["status"] == "cancelled"
        assert final["cancellable"] is False


def test_waiting_job_cancellation_is_owner_scoped(tmp_path, monkeypatch):
    state_dir = str(tmp_path / "state")
    monkeypatch.setenv("CHEMISTRY_LAB_STATE_DIR", state_dir)
    job_id = _queued_agent_job(state_dir, owner="alice")

    app.config["TESTING"] = True
    with app.test_client() as client:
        _login(client, "bob")
        listed_ids = {job["jobId"] for job in client.get("/api/v1/jobs").get_json()["jobs"]}
        assert job_id not in listed_ids
        denied = client.post(f"/api/v1/jobs/{job_id}/cancel", json={})
        assert denied.status_code == 404

    status = agent_service.get_agent_job_status(
        job_id=job_id, owner_id="alice", state_dir=state_dir
    )
    assert status["status"] == "QUEUED"


def test_unified_cancel_handles_waiting_server_local_job(tmp_path, monkeypatch):
    state_dir = str(tmp_path / "state")
    monkeypatch.setenv("CHEMISTRY_LAB_STATE_DIR", state_dir)
    monkeypatch.setenv("ORCA_STATE_DIR", state_dir)
    queued = local_orca_worker.enqueue_local_job(
        owner_id="chemist",
        input_text="! B3LYP def2-SVP\n* xyz 0 1\nH 0 0 0\nH 0 0 1\n*",
        job_name="queued-server-job",
        state_dir=state_dir,
    )
    job_id = queued["job"]["job_id"]

    app.config["TESTING"] = True
    with app.test_client() as client:
        _login(client, "chemist")
        cancelled = client.post(f"/api/v1/jobs/{job_id}/cancel", json={})
        assert cancelled.status_code == 200
        assert cancelled.get_json()["status"] == "CANCELLED"

    persisted = local_orca_worker.get_local_job(
        job_id, owner_id="chemist", state_dir=state_dir
    )
    assert persisted["status"] == "CANCELLED"


def test_kaggle_cancel_reports_completion_won_race(tmp_path, monkeypatch):
    """A late Cancel must not relabel an already-finished remote job."""
    import app as webapp
    import orca_orchestrator.service as service_module

    state_dir = str(tmp_path / "state")
    monkeypatch.setenv("CHEMISTRY_LAB_STATE_DIR", state_dir)
    monkeypatch.setenv("ORCA_STATE_DIR", state_dir)

    class Manifest:
        owner = "kaggle-owner"

        @staticmethod
        def to_dict():
            return {"application_owner": "chemist"}

    class Store:
        @staticmethod
        def get_job(job_id):
            return Manifest() if job_id == "chem-tools-race-finished" else None

    class Service:
        store = Store()

        @staticmethod
        def cancel(creds, job_id):
            return {"job_id": job_id, "state": "FINISHED"}

    monkeypatch.setattr(webapp, "ORCHESTRATOR_AVAILABLE", True)
    monkeypatch.setattr(service_module, "get_service", lambda: Service())
    monkeypatch.setattr(
        webapp,
        "_resolve_kaggle_credentials",
        lambda username, key, owner=None: ("kaggle-owner", "a" * 32),
    )

    app.config["TESTING"] = True
    with app.test_client() as client:
        _login(client, "chemist")
        response = client.post("/api/v1/jobs/chem-tools-race-finished/cancel", json={})
    assert response.status_code == 200
    assert response.get_json()["status"] == "FINISHED"


def test_frontend_renders_waiting_reason_and_cancel_action():
    source = (app.root_path + "/static/js/app.js")
    with open(source, "r", encoding="utf-8") as handle:
        javascript = handle.read()
    assert "CANCELLABLE_JOB_STATUSES" in javascript
    assert "isClientOnlyWaitingJob" in javascript
    assert "Cancelled before submission." in javascript
    assert '["FINISHED", "COMPLETED", "COMPLETED_WITH_WARNINGS"].includes(state)' in javascript
    assert "waitingReason" in javascript
    assert "/api/v1/jobs/${encodeURIComponent(job.jobId)}/cancel" in javascript
    assert 'cancelBtn.textContent = "Cancel"' in javascript
