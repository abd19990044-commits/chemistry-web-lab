"""Stopped Kaggle windows and durable Cloudflare metadata must converge."""
from copy import deepcopy

import pytest

from orca_orchestrator.cloudflare_controller import (
    CloudflareController, CloudflareJobRecord, InMemoryCloudflareBackend,
    LocalWorkflowState, decide_reconciliation,
)
from orca_orchestrator.config import StoreConfig
from orca_orchestrator.credentials import KaggleCredentials
from orca_orchestrator.errors import NetworkError
from orca_orchestrator.kaggle_api import KernelStatus, classify_status
from orca_orchestrator.models import JobManifest
from orca_orchestrator.reconciler import Observation, Reconciler, decide
from orca_orchestrator.service import OrchestratorService
from orca_orchestrator.states import JobState, Trigger
from orca_orchestrator.store import JobStore


@pytest.fixture
def store(tmp_path):
    db = JobStore(StoreConfig(state_dir=str(tmp_path), db_filename="sync.sqlite3"))
    yield db
    db.close()


def running_job():
    job = JobManifest.create(job_id="chem-tools-sync-test", owner="researcher",
                             title="Sync", input_filename="job.inp", original_input_sha256="abc")
    job.enter_state(JobState.RUNNING)
    return job


CREDS = KaggleCredentials(username="researcher", key="0123456789abcdef0123456789abcdef")


@pytest.mark.parametrize("token,expected", [
    ("CANCEL_ACKNOWLEDGED", "cancelled"), ("CANCEL_REQUESTED", "running"),
    ("ERROR", "error"), ("COMPLETE", "complete"),
])
def test_current_kaggle_sdk_status_tokens(token, expected):
    assert classify_status(f'researcher/job has status "{token}"') == expected


@pytest.mark.parametrize("state,trigger", [("cancelled", Trigger.CANCEL), ("error", Trigger.ORCA_FATAL)])
@pytest.mark.parametrize("checkpoint", [None, "verified-checkpoint"])
def test_known_stop_is_not_hidden_by_output_download_failure(state, trigger, checkpoint):
    job = running_job()
    job.verified_checkpoint_id = checkpoint
    obs = Observation(job_id=job.job_id, kernel_status=KernelStatus(job.current_slug, state),
                      error=NetworkError("outputs unavailable"))
    assert decide(job, obs).trigger == trigger


@pytest.mark.parametrize("remote,expected", [("cancelled", JobState.CANCELLED), ("error", JobState.FAILED)])
def test_reconciler_persists_remote_stop_despite_missing_output(monkeypatch, store, remote, expected):
    job = running_job()
    store.put_job(job)
    class Client:
        def __init__(self, creds):
            pass
        def kernel_exists(self, slug):
            return KernelStatus(slug, remote) if slug == job.current_slug else None
    monkeypatch.setattr("orca_orchestrator.reconciler.KaggleClient", Client)
    def unavailable(*args, **kwargs):
        raise NetworkError("output storage unavailable")
    monkeypatch.setattr("orca_orchestrator.reconciler.read_window", unavailable)
    result = Reconciler(store=store).reconcile(job.job_id, CREDS)
    assert result.state == expected
    assert store.require_job(job.job_id).state == expected
    assert result._extra["last_seen_remote_state"] == remote.upper()
    assert not result._extra["remote_status_unknown"]


class VersionedBackend(InMemoryCloudflareBackend):
    """Enforce the production Worker's version check, unlike the memory fallback."""
    is_durable = True
    def get_job(self, ref, owner):
        return deepcopy(super().get_job(ref, owner))
    def put_job(self, record):
        previous = self.get_job(record.internal_job_id, record.kaggle_username)
        if previous:
            assert record.version == previous.version, "stale Cloudflare write"
        saved = deepcopy(record)
        saved.version = previous.version + 1 if previous else 1
        saved.cf_sync_status = "SYNCED"
        super().put_job(saved)
        return deepcopy(saved)


def test_repeated_cloudflare_updates_keep_identity_version_and_observed_state(store):
    backend = VersionedBackend()
    controller = CloudflareController(client=backend, store=store)
    job = running_job()
    initial = controller.register_job(job)
    for version, remote in enumerate(["RUNNING", "ERROR", "CANCELLED"], start=2):
        job._extra["last_seen_remote_state"] = remote
        saved = controller.sync_job_state(job)
        assert saved.version == version
        assert saved.internal_job_id == initial.internal_job_id
        assert saved.last_seen_remote_state == remote
        assert job._extra["cf_version"] == version
        assert job.cf_sync_status == "SYNCED"


@pytest.mark.parametrize("remote,expected", [("cancelled", JobState.CANCELLED), ("error", JobState.FAILED), (None, JobState.CANCELLED)])
def test_cloudflare_login_updates_existing_local_job(store, remote, expected):
    backend = InMemoryCloudflareBackend()
    controller = CloudflareController(client=backend, store=store)
    job = running_job()
    store.put_job(job)
    controller.register_job(job)
    class Client:
        def kernel_exists(self, slug):
            return KernelStatus(slug, remote) if remote else None
    controller.reconcile_user_session(CREDS, Client())
    assert store.require_job(job.job_id).state == expected


def test_explicit_cancel_does_not_resume_checkpoint(store):
    job = running_job()
    job.verified_checkpoint_id = "verified-checkpoint"
    record = CloudflareJobRecord.from_job_manifest(job)
    decision = decide_reconciliation(record, KernelStatus(job.current_slug, "cancelled"), job)
    assert decision.action == "STOPPED"
    assert not decision.resume_required
    assert decision.local_state == LocalWorkflowState.REMOTE_STOPPED.value
    record.local_state = decision.local_state
    assert record.to_job_manifest().state == JobState.CANCELLED


def test_cloudflare_connection_failure_does_not_mark_remote_deleted(store):
    backend = InMemoryCloudflareBackend()
    controller = CloudflareController(client=backend, store=store)
    job = running_job()
    store.put_job(job)
    controller.register_job(job)
    class Client:
        def kernel_exists(self, slug):
            raise NetworkError("unavailable")
    controller.reconcile_user_session(CREDS, Client())
    assert store.require_job(job.job_id).state == JobState.RUNNING
    assert not backend.get_job(job.job_id, job.owner).remote_deleted


def test_sync_probes_unlisted_local_running_job(monkeypatch, store):
    service = OrchestratorService(store=store)
    job = running_job()
    store.put_job(job)
    monkeypatch.setattr(service.cf_controller, "reconcile_user_session", lambda *a: {})
    monkeypatch.setattr(service.cf_controller, "sync_job_state", lambda *a: None)
    monkeypatch.setattr("orca_orchestrator.service.ledger_mod.discover_jobs", lambda *a: [])
    calls = []
    def reconcile(job_id, creds, **kwargs):
        calls.append(job_id)
        stopped = store.require_job(job_id)
        stopped.enter_state(JobState.CANCELLED)
        stopped._extra["last_seen_remote_state"] = "CANCELLED"
        return store.put_job(stopped)
    monkeypatch.setattr(service.reconciler, "reconcile", reconcile)
    result = service.list_jobs(CREDS)
    assert calls == [job.job_id]
    assert result[0]["state"] == "CANCELLED"
    assert result[0]["remote_active"] is False
    assert result[0]["deleted_on_kaggle"] is False


def test_cancelled_remote_overrides_stale_running_ledger():
    from orca_orchestrator.ledger import LedgerRecord
    job = running_job()
    obs = Observation(job_id=job.job_id, kernel_status=KernelStatus(job.current_slug, "cancelled"),
                      record=LedgerRecord(slug=job.current_slug, job=deepcopy(job)))
    result = decide(job, obs)
    assert result.trigger == Trigger.CANCEL
    assert result.action not in {"rollback", "push_successor", "adopt_ledger"}


def test_legacy_browser_contract_carries_authoritative_remote_activity(store):
    from orca_orchestrator.legacy_compat import _legacy_job
    service = OrchestratorService(store=store)
    job = running_job()
    job.enter_state(JobState.CANCELLED)
    job._extra["last_seen_remote_state"] = "CANCELLED"
    output = _legacy_job(service.describe(job))
    assert output["status"] == "cancelled"
    assert output["remote_active"] is False
    assert output["remote_status_unknown"] is False
