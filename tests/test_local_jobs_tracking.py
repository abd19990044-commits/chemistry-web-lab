# -*- coding: utf-8 -*-
"""
Test suite verifying local jobs tracking resilience:
1. submit_job registers JobManifest into the orchestrator store.
2. /api/kaggle/sync surfaces registered jobs.
3. Frontend app.js static contract verification:
   - MAX_TOTAL_JOBS is 100
   - Terminal job pruning in addJob and submitWorkflowToQueue
   - Dynamic visibility of jobs-signed-in-area in renderJobs
   - Safe isolateLocalJobsTo preserving active calculations
   - mergeRemoteJobs preserves active statuses
"""
import os
import re
import pytest
import uuid
from app import app
from services import kaggle_service
from orca_orchestrator.service import get_service
from orca_orchestrator.credentials import parse as parse_credentials
from orca_orchestrator.states import JobState


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_submit_job_registers_in_orchestrator_store(monkeypatch):
    """Verifies that submitting a job saves the JobManifest to the store."""
    service = get_service()
    store = service.store

    # Generate unique slug and idempotency key per test run
    uniq = uuid.uuid4().hex[:8]
    test_slug = f"chem-tools-test-submit-{uniq}"
    test_url = f"https://www.kaggle.com/code/testuser/{test_slug}"
    test_idem_key = f"kaggle-submit:test-{uniq}"

    monkeypatch.setattr(
        kaggle_service.kaggle_runner,
        "build_job_dir",
        lambda **kwargs: "/tmp/mock_dir"
    )
    monkeypatch.setattr(
        kaggle_service.kaggle_runner,
        "push_job",
        lambda *args, **kwargs: {"job_id": test_slug, "url": test_url, "owner": "testuser"}
    )
    monkeypatch.setattr(
        kaggle_service,
        "save_credentials",
        lambda *args, **kwargs: None
    )

    res, code = kaggle_service.submit_job(
        kaggle_username="testuser",
        kaggle_key="a" * 32,
        dataset_sources_raw="testuser/orca-dataset",
        orca_link="",
        input_filename=f"test_{uniq}.inp",
        input_content=f"! B3LYP def2-SVP\n# {uniq}\n* xyz 0 1\nO 0 0 0\n*",
        job_name=f"Test Job {uniq}",
        idem_key=test_idem_key,
        store=store,
    )

    assert code == 200
    assert res["ok"] is True
    assert res["job_id"] == test_slug

    # Verify that the job was stored in the orchestrator SQLite store
    stored_manifest = store.get_job(test_slug)
    assert stored_manifest is not None
    assert stored_manifest.job_id == test_slug
    assert stored_manifest.owner == "testuser"
    assert stored_manifest.title == f"Test Job {uniq}"
    assert stored_manifest.state == JobState.RUNNING


def test_frontend_jobs_tracking_contract():
    """Verifies critical JavaScript contract rules in static/js/app.js."""
    js_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "js", "app.js")
    with open(js_path, "r", encoding="utf-8") as f:
        js = f.read()

    # 1. Capacity limit is 100
    assert "const MAX_TOTAL_JOBS = 100;" in js, "MAX_TOTAL_JOBS must be expanded to 100"

    # 2. Terminal statuses defined
    assert 'const TERMINAL_STATUSES = ["complete", "error", "cancelled"];' in js

    # 3. addJob evicts oldest terminal jobs rather than silently dropping
    assert "while (jobs.length >= MAX_TOTAL_JOBS)" in js
    assert "TERMINAL_STATUSES.includes" in js

    # 4. renderJobs reveals jobsSignedInArea when jobs exist
    assert "if (jobs.length > 0 || currentKaggle)" in js
    assert 'jobsSignedInArea.classList.remove("hidden")' in js

    # 5. processKaggleQueue resilient unshift fallback
    assert "currentStoredJobs.findIndex(x => x.jobId === oldJobId || x.jobId === data.job_id)" in js
    # A missing credential/source on one queued item must not stop every other
    # queued item, and the reason must be visible instead of silently stuck.
    assert "One blocked entry must not starve unrelated queued jobs." in js
    assert 'queueReason: "CREDENTIALS_REQUIRED"' in js
    assert 'queueReason: "ORCA_SOURCE_REQUIRED"' in js
    assert "The API key is intentionally not restored to browser memory" in js
    assert "if (!creds.kaggle_username)" in js
    assert "const isLinkSource = orcaSourceKind === \"google_drive\" || orcaSourceKind === \"link\";" in js
    # Validation failures must release the submit button so a corrected retry
    # can actually reach the server.
    assert "function resetKaggleSubmitGuard()" in js
    assert js.count("resetKaggleSubmitGuard();") >= 7
    # Continuation must still recognize older OPT entries without stageType.
    assert "return Boolean(job.optimizedCoords && job.optimizedCoords.trim());" in js

    # 6. isolateLocalJobsTo protects active jobs from deletion
    assert '["running", "restarting", "submitting", "queued", "waiting_dependency"].includes(j.status)' in js

    # 7. No em-dash or en-dash in app.js
    assert "\u2014" not in js, "Found em-dash in app.js"
    assert "\u2013" not in js, "Found en-dash in app.js"


def test_my_jobs_lists_persisted_kaggle_jobs_without_browser_cache(tmp_path, monkeypatch):
    """A fresh browser session discovers durable jobs from the backend store."""
    import app as webapp
    import orca_orchestrator.service as service_module
    from orca_orchestrator.config import StoreConfig
    from orca_orchestrator.models import JobManifest
    from orca_orchestrator.store import JobStore

    store = JobStore(StoreConfig(state_dir=str(tmp_path / "orchestrator")))
    manifest = JobManifest.create(
        job_id="chem-tools-visible-1a2b3c4d",
        owner="kaggle-owner",
        title="Persistent calculation",
        input_filename="molecule.inp",
        original_input_sha256="hash",
    )
    store.put_job(manifest)

    class FakeService:
        def __init__(self):
            self.store = store

    monkeypatch.setattr(webapp, "ORCHESTRATOR_AVAILABLE", True)
    monkeypatch.setattr(service_module, "get_service", lambda: FakeService())
    monkeypatch.setattr(
        webapp,
        "_resolve_kaggle_credentials",
        lambda username, key, owner=None: ("kaggle-owner", "vault-key"),
    )
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as fresh_browser:
        with fresh_browser.session_transaction() as session:
            session["user"] = {"sub": "site-owner"}
        response = fresh_browser.get("/api/v1/jobs")
    assert response.status_code == 200
    jobs = response.get_json()["jobs"]
    visible = next(job for job in jobs if job["jobId"] == manifest.job_id)
    assert visible["backend"] == "kaggle"
    assert visible["name"] == "Persistent calculation"
    store.close()


def test_my_jobs_uses_persisted_application_owner_without_vault(tmp_path, monkeypatch):
    """Credential loss/rotation must not make an already-known job disappear."""
    import app as webapp
    import orca_orchestrator.service as service_module
    from orca_orchestrator.config import StoreConfig
    from orca_orchestrator.models import JobManifest
    from orca_orchestrator.store import JobStore

    store = JobStore(StoreConfig(state_dir=str(tmp_path / "orchestrator-owner")))
    visible = JobManifest.create(
        job_id="chem-tools-owner-visible-1",
        owner="remote-kaggle-user",
        title="Durably owned calculation",
        input_filename="molecule.inp",
        original_input_sha256="hash",
        _extra={"application_owner": "site-owner"},
    )
    hidden = JobManifest.create(
        job_id="chem-tools-other-owner-1",
        owner="remote-kaggle-user",
        title="Another tenant",
        input_filename="molecule.inp",
        original_input_sha256="hash",
        _extra={"application_owner": "other-site-owner"},
    )
    store.put_job(visible)
    store.put_job(hidden)

    class FakeService:
        def __init__(self):
            self.store = store

    monkeypatch.setattr(webapp, "ORCHESTRATOR_AVAILABLE", True)
    monkeypatch.setattr(service_module, "get_service", lambda: FakeService())
    monkeypatch.setattr(
        webapp, "_resolve_kaggle_credentials",
        lambda username, key, owner=None: ("", ""),
    )
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as fresh_browser:
        with fresh_browser.session_transaction() as session:
            session["user"] = {"sub": "site-owner"}
        response = fresh_browser.get("/api/v1/jobs")

    assert response.status_code == 200
    ids = {job["jobId"] for job in response.get_json()["jobs"]}
    assert visible.job_id in ids
    assert hidden.job_id not in ids
    store.close()


def test_kaggle_submit_accepts_owner_vault_resolution_without_browser_key(monkeypatch):
    """A Vault-backed browser session deliberately sends no API key to JS;
    the Flask boundary must resolve it before dispatching the submission."""
    import app as webapp
    import orca_orchestrator.legacy_compat as legacy

    captured = {}

    def fake_resolve(username, key, owner=None):
        captured["owner"] = owner
        return username or "vault-user", key or "vault-only-key"

    class FakeResult:
        job_id = "chem-tools-vault-1a2b3c4d"
        url = "https://www.kaggle.com/code/vault-user/chem-tools-vault-1a2b3c4d"
        title = "water"
        replayed = False

    class FakeService:
        def submit(self, creds, **kwargs):
            captured["kaggle_key"] = creds.key or creds.api_token
            captured["submit_kwargs"] = kwargs
            return FakeResult()

    monkeypatch.setattr(legacy, "_creds", lambda source: legacy.parse_credentials(
        "vault-user", "vault-only-key"))
    monkeypatch.setattr(legacy, "get_service", lambda: FakeService())
    monkeypatch.setattr(legacy, "enforce_capacity", lambda *args, **kwargs: None)
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as client:
        csrf = client.get("/").headers.get("X-CSRF-Token")
        response = client.post("/api/kaggle/submit", data={
            "kaggle_username": "vault-user",
            "kaggle_key": "",
            "dataset_sources": "vault-user/orca-dataset",
            "input_filename": "water.inp",
            "input_content": "! B3LYP SP\n* xyz 0 1\nO 0 0 0\n*",
        }, headers={"X-CSRF-Token": csrf or ""})
    assert response.status_code == 200
    assert captured["kaggle_key"] == "vault-only-key"
    assert captured["submit_kwargs"]["application_owner"] == "local_user"
    assert response.get_json()["job_id"] == "chem-tools-vault-1a2b3c4d"


def test_legacy_submit_preserves_workflow_identity(monkeypatch):
    """The browser's historical endpoint must persist stage provenance too."""
    import app as webapp
    import orca_orchestrator.legacy_compat as legacy

    class FakeResult:
        job_id = "chem-tools-workflow-1"
        url = "https://www.kaggle.com/code/user/chem-tools-workflow-1"
        title = "workflow-freq"
        replayed = False

    captured = {}

    class FakeService:
        def submit(self, creds, **kwargs):
            captured.update(kwargs)
            return FakeResult()

    monkeypatch.setattr(legacy, "_creds", lambda source: legacy.parse_credentials("user", "k" * 32))
    monkeypatch.setattr(legacy, "get_service", lambda: FakeService())
    monkeypatch.setattr(legacy, "enforce_capacity", lambda *args, **kwargs: None)
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as client:
        response = client.post("/api/kaggle/submit", data={
            "dataset_sources": "user/orca",
            "input_filename": "freq.inp",
            "input_content": "! B3LYP FREQ\n* xyz 0 1\nO 0 0 0\n*",
            "workflow_id": "chain-123",
            "parent_job_id": "chem-tools-opt-1",
            "step_index": "1",
            "step_count": "3",
            "step_name": "freq",
        })
    assert response.status_code == 200
    assert captured["workflow_id"] == "chain-123"
    assert captured["parent_job_id"] == "chem-tools-opt-1"
    assert captured["step_index"] == 1
    assert captured["step_count"] == 3
    assert captured["step_name"] == "freq"
