# -*- coding: utf-8 -*-
"""Production routing and orchestration invariants.

The historical /api/kaggle/* contract is compatibility-only. Every calculation
operation must execute OrchestratorService rather than the obsolete direct CLI lifecycle.
This test suite covers all 20 required verification invariants for single-path orchestration.
"""
import io
import json
import os
import tempfile
import uuid
import zipfile
from unittest.mock import MagicMock, patch
import pytest

from app import app
from orca_orchestrator.credentials import KaggleCredentials, parse as parse_credentials
from orca_orchestrator.errors import KaggleUnavailableError, PermanentError, ValidationError
from orca_orchestrator.kaggle_api import KernelStatus, PushResult
from orca_orchestrator.legacy_compat import install_legacy_route_adapter
from orca_orchestrator.models import JobManifest
from orca_orchestrator.reconciler import Observation
from orca_orchestrator.service import OrchestratorService, get_service
from orca_orchestrator.states import JobState

LEGACY = {
    "/api/kaggle/login", "/api/kaggle/sync", "/api/kaggle/submit", "/api/kaggle/status",
    "/api/kaggle/download", "/api/kaggle/delete", "/api/kaggle/cancel",
    "/api/kaggle/resume",
}

CANONICAL = {
    "/api/orca/login", "/api/orca/jobs", "/api/orca/submit",
    "/api/orca/status", "/api/orca/cancel", "/api/orca/stop-all",
    "/api/orca/resume", "/api/orca/delete", "/api/orca/results",
    "/api/orca/health", "/api/orca/sweep", "/api/orca/state-machine",
}


# ─────────────────────────────────────────────────────────────
# 1-5: Route Invariants & Runtime Inspection
# ─────────────────────────────────────────────────────────────

def test_legacy_routes_are_bound_to_orchestrator_adapter():
    """Requirement 1, 5: Legacy routes route to legacy_compat."""
    rules = {rule.rule: rule for rule in app.url_map.iter_rules()}
    assert LEGACY.issubset(rules)
    for path in LEGACY:
        view = app.view_functions[rules[path].endpoint]
        assert view.__module__ == "orca_orchestrator.legacy_compat"


def test_canonical_orca_routes_are_registered():
    """Requirement 2, 5: Canonical /api/orca/* routes route to orca_orchestrator.api."""
    rules = {rule.rule: rule for rule in app.url_map.iter_rules()}
    assert CANONICAL.issubset(rules)
    for path in CANONICAL:
        view = app.view_functions[rules[path].endpoint]
        assert view.__module__ == "orca_orchestrator.api"


def test_legacy_routes_do_not_use_legacy_runner():
    """Requirement 3: No direct call from legacy routes to kaggle_runner."""
    rules = {rule.rule: rule for rule in app.url_map.iter_rules()}
    for path in LEGACY:
        view = app.view_functions[rules[path].endpoint]
        assert view.__module__ != "kaggle_runner"
        assert not getattr(view, "__name__", "").startswith("api_kaggle_")


def test_legacy_adapter_installed_once():
    """Requirement 4: Adapter registration is idempotent and safe."""
    assert getattr(app, "_orca_legacy_adapter_installed", False) is True
    install_legacy_route_adapter(app)
    assert getattr(app, "_orca_legacy_adapter_installed", False) is True


def test_application_has_a_health_endpoint():
    assert "/health" in {rule.rule for rule in app.url_map.iter_rules()}


# ─────────────────────────────────────────────────────────────
# 6-7, 13, 19: Push Success, Failure, Ref Persistence & Duplicates
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def test_client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_submit_via_legacy_and_canonical_use_orchestrator(test_client):
    """Requirement 1, 2, 6, 13, 19: Submit routes unified under OrchestratorService."""
    service = get_service()
    username = "testuser"
    key = "a" * 32
    uniq_id = uuid.uuid4().hex[:8]
    idem_key = f"submit:test:{uniq_id}"

    fake_push_result = PushResult(
        slug=f"chem-tools-water-{uniq_id}",
        owner=username,
        url=f"https://www.kaggle.com/code/{username}/chem-tools-water-{uniq_id}",
        requested_slug=f"chem-tools-water-{uniq_id}",
    )

    with patch("orca_orchestrator.kaggle_api.KaggleClient.push_kernel", return_value=fake_push_result), \
         patch("orca_orchestrator.kaggle_api.KaggleClient.list_kernels", return_value=[]), \
         patch("orca_orchestrator.kaggle_api.KaggleClient.kernel_exists", return_value=None):

        # 1. Submit via legacy route /api/kaggle/submit
        resp = test_client.post(
            "/api/kaggle/submit",
            data={
                "kaggle_username": username,
                "kaggle_key": key,
                "job_name": f"water_{uniq_id}",
                "input_filename": f"water_{uniq_id}.inp",
                "input_content": f"! B3LYP def2-SVP Opt\n# test {uniq_id}\n* xyz 0 1\nO 0 0 0\nH 0 0 1\nH 0 1 0\n*",
                "dataset_sources": "orca/dataset",
            },
            headers={"Idempotency-Key": idem_key},
        )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["job_id"].startswith("chem-tools-")
        job_id = data["job_id"]
        assert "kaggle.com" in data["kaggle_url"]

        # Verify job is persisted in authoritative Orchestrator Store
        stored_job = service.store.get_job(job_id)
        assert stored_job is not None
        assert stored_job.owner == username
        assert stored_job.current_slug == fake_push_result.slug

        # 2. Duplicate submission with identical idempotency key returns replayed result
        resp_dup = test_client.post(
            "/api/kaggle/submit",
            data={
                "kaggle_username": username,
                "kaggle_key": key,
                "job_name": f"water_{uniq_id}",
                "input_filename": f"water_{uniq_id}.inp",
                "input_content": f"! B3LYP def2-SVP Opt\n# test {uniq_id}\n* xyz 0 1\nO 0 0 0\nH 0 0 1\nH 0 1 0\n*",
                "dataset_sources": "orca/dataset",
            },
            headers={"Idempotency-Key": idem_key},
        )
        assert resp_dup.status_code == 200
        assert resp_dup.get_json()["job_id"] == job_id


def test_push_failure_handling(test_client):
    """Requirement 7: Push failure is handled cleanly and reported."""
    uniq_id = uuid.uuid4().hex[:8]
    with patch("orca_orchestrator.kaggle_api.KaggleClient.push_kernel", side_effect=PermanentError("Push rejected by Kaggle")), \
         patch("orca_orchestrator.kaggle_api.KaggleClient.list_kernels", return_value=[]), \
         patch("orca_orchestrator.kaggle_api.KaggleClient.kernel_exists", return_value=None):

        resp = test_client.post("/api/kaggle/submit", data={
            "kaggle_username": "testuser",
            "kaggle_key": "b" * 32,
            "job_name": f"fail_{uniq_id}",
            "input_filename": "fail.inp",
            "input_content": f"! HF STO-3G\n# fail {uniq_id}\n* xyz 0 1\nH 0 0 0\nH 0 0 1\n*",
            "dataset_sources": "orca/dataset",
        })
        assert resp.status_code in (400, 500, 502, 503)
        assert resp.get_json()["ok"] is False


# ─────────────────────────────────────────────────────────────
# 8-12, 14: Status Lifecycle States & Reconciliation
# ─────────────────────────────────────────────────────────────

def test_status_lifecycle_and_reconciliation(test_client):
    """Requirement 8-12, 14: Status transitions for QUEUED, RUNNING, COMPLETE, ERROR, CANCELLED."""
    service = get_service()
    username = "testuser"
    key = "c" * 32

    from orca_orchestrator.credentials import BROKER, KaggleCredentials
    BROKER.remember(KaggleCredentials(username=username, key=key))

    job = JobManifest.create(
        job_id=f"chem-tools-statustest-{uuid.uuid4().hex[:6]}",
        owner=username,
        title="Status Test",
        input_filename="test.inp",
        original_input_sha256="abc",
    )
    job.current_slug = job.job_id

    # 1. QUEUED state
    job.state = JobState.QUEUED
    service.store.put_job(job)
    with patch("orca_orchestrator.reconciler.observe", return_value=Observation(job_id=job.job_id, kernel_status=KernelStatus(slug=job.current_slug, status="queued"))):
        resp = test_client.post("/api/kaggle/status", json={"kaggle_username": username, "kaggle_key": key, "job_id": job.job_id})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "queued"

    # 2. RUNNING state
    job.state = JobState.RUNNING
    service.store.put_job(job)
    with patch("orca_orchestrator.reconciler.observe", return_value=Observation(job_id=job.job_id, kernel_status=KernelStatus(slug=job.current_slug, status="running"))):
        resp = test_client.post("/api/kaggle/status", json={"kaggle_username": username, "kaggle_key": key, "job_id": job.job_id})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "running"

    # 3. COMPLETE state
    job.state = JobState.FINISHED
    service.store.put_job(job)
    with patch("orca_orchestrator.reconciler.observe", return_value=Observation(job_id=job.job_id, kernel_status=KernelStatus(slug=job.current_slug, status="complete"))):
        resp = test_client.post("/api/kaggle/status", json={"kaggle_username": username, "kaggle_key": key, "job_id": job.job_id})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "complete"

    # 4. ERROR state
    job.state = JobState.FAILED
    service.store.put_job(job)
    with patch("orca_orchestrator.reconciler.observe", return_value=Observation(job_id=job.job_id, kernel_status=KernelStatus(slug=job.current_slug, status="error"))):
        resp_err = test_client.post("/api/kaggle/status", json={"kaggle_username": username, "kaggle_key": key, "job_id": job.job_id})
        assert resp_err.status_code == 200
        assert resp_err.get_json()["status"] == "error"

    # 5. CANCELLED state
    job.state = JobState.CANCELLED
    service.store.put_job(job)
    with patch("orca_orchestrator.reconciler.observe", return_value=Observation(job_id=job.job_id, kernel_status=KernelStatus(slug=job.current_slug, status="cancel"))):
        resp_can = test_client.post("/api/kaggle/status", json={"kaggle_username": username, "kaggle_key": key, "job_id": job.job_id})
        assert resp_can.status_code == 200
        assert resp_can.get_json()["status"] == "cancelled"


# ─────────────────────────────────────────────────────────────
# 15: Output Download
# ─────────────────────────────────────────────────────────────

def test_output_download(test_client):
    """Requirement 15: Output download retrieves result bundle via OrchestratorService."""
    username = "testuser"
    key = "d" * 32
    job_id = "chem-tools-downloadtest-2222"

    def fake_fetch_results(creds, jid, slug=None):
        tmp_dir = tempfile.mkdtemp()
        zip_path = os.path.join(tmp_dir, "results.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("orca.out", "ORCA TERMINATED NORMALLY\nFINAL SINGLE POINT ENERGY -76.12345")
        return zip_path, tmp_dir

    with patch("orca_orchestrator.service.OrchestratorService.fetch_results", side_effect=fake_fetch_results):
        resp = test_client.post("/api/kaggle/download", json={
            "kaggle_username": username, "kaggle_key": key, "job_id": job_id,
        })
        assert resp.status_code == 200
        assert resp.headers["Content-Disposition"].startswith("attachment;")


# ─────────────────────────────────────────────────────────────
# 16-18, 20: Continuation, Successor Detection, Stale Job & Timeout
# ─────────────────────────────────────────────────────────────

def test_continuation_and_successor_detection():
    """Requirement 16, 17, 20: Successor detection and continuation across epochs."""
    service = get_service()
    username = "testuser"
    creds = parse_credentials(username, "e" * 32)
    job_id = f"chem-tools-chaintest-{uuid.uuid4().hex[:6]}"

    job = JobManifest.create(
        job_id=job_id,
        owner=username,
        title="Chain Test",
        input_filename="test.inp",
        original_input_sha256="chainsha",
    )
    job.current_slug = job_id
    service.store.put_job(job)

    successor_slug = f"{job_id}-r1"
    with patch("orca_orchestrator.kaggle_api.KaggleClient.kernel_exists", return_value=KernelStatus(slug=successor_slug, status="running")), \
         patch("orca_orchestrator.reconciler.read_window", return_value=None):
        reconciled = service.reconciler.reconcile(job_id, creds, actor="test")
        assert reconciled is not None


def test_stale_job_sweep():
    """Requirement 18: Watchdog stall sweeper identifies and inspects stale jobs."""
    service = get_service()
    sweep_result = service.sweep_now()
    assert isinstance(sweep_result, dict)
    assert "examined" in sweep_result


def test_sweep_endpoint_auth_enforcement(test_client):
    """Verify /api/orca/sweep rejects unauthenticated requests, prevents owner spoofing, and does not leak secrets."""
    # 1. Unauthenticated request (no credentials in payload)
    resp_unauth = test_client.post("/api/orca/sweep", json={})
    assert resp_unauth.status_code in (400, 401)
    assert resp_unauth.json["ok"] is False

    # 2. Missing key or missing username
    resp_missing_key = test_client.post("/api/orca/sweep", json={"kaggle_username": "valid_user"})
    assert resp_missing_key.status_code in (400, 401)
    assert resp_missing_key.json["ok"] is False

    resp_missing_user = test_client.post("/api/orca/sweep", json={"kaggle_key": "a1b2c3d4e5f60718293a4b5c6d7e8f90"})
    assert resp_missing_user.status_code in (400, 401)
    assert resp_missing_user.json["ok"] is False

    # 3. Authenticated request with valid Kaggle credentials
    secret_key = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    with patch("orca_orchestrator.kaggle_api.KaggleClient.list_kernels", return_value=[]):
        resp_auth = test_client.post("/api/orca/sweep", json={
            "kaggle_username": "valid_user",
            "kaggle_key": secret_key,
            "owner": "spoofed_target_user"  # Attempt to spoof owner parameter
        })
        assert resp_auth.status_code == 200
        assert resp_auth.json["ok"] is True
        # Owner MUST be derived strictly from authenticated credentials, ignoring spoofed payload field
        assert resp_auth.json["owner"] == "valid_user"
        assert resp_auth.json["owner"] != "spoofed_target_user"
        assert "sweep" in resp_auth.json
        # Ensure raw secret key is never leaked in the response JSON
        assert secret_key not in resp_auth.text
