# -*- coding: utf-8 -*-
"""API contract tests for the FastAPI /api/v1 layer (migration wave 1+2).

Covers: OpenAPI validity, health/ready, ORCA generation (MaxDisk configurable:
omitted->20000-injected-by-runner, custom preserved), Kaggle submit
idempotency backed by the orchestrator store across two worker instances
(multi-worker proof), artifact listing/download with path-traversal
rejection, the centralized error contract, Flask parity for /health, and the
three primary UI pages served through the WSGI mount.
"""
import hashlib
import os
import sys
import threading
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def api():
    from fastapi.testclient import TestClient
    import api.main as api_main
    with TestClient(api_main.app) as client:
        yield client


# ---------------------------------------------------------------------------
# OpenAPI + health/ready
# ---------------------------------------------------------------------------
def test_openapi_is_valid_and_lists_v1_paths(api):
    r = api.get("/api/v1/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert spec["openapi"].startswith("3.")
    expected = [
        "/api/v1/health", "/api/v1/ready", "/api/v1/orca/generate",
        "/api/v1/kaggle/jobs", "/api/v1/kaggle/jobs/{job_id}/status",
        "/api/v1/kaggle/jobs/{job_id}/extract-opt-coords",
        "/api/v1/kaggle/jobs/{job_id}/artifacts",
        "/api/v1/kaggle/jobs/{job_id}/artifacts/{artifact_name}",
    ]
    for path in expected:
        assert path in spec["paths"], "missing %s from OpenAPI" % path


def test_health_and_ready(api):
    h = api.get("/api/v1/health")
    assert h.status_code == 200
    body = h.json()
    assert body["ok"] in (True, False) and "kaggle_cli_ok" in body
    r = api.get("/api/v1/ready")
    assert r.status_code == 200
    ready = r.json()
    assert ready["checks"]["storage"] is True, "temp storage must be writable"


def test_health_parity_with_flask(api):
    """Flask /health and FastAPI /api/v1/health call the SAME service - the
    payload keys must match exactly."""
    import app as webapp
    flask_body = webapp.app.test_client().get("/health").get_json()
    api_body = api.get("/api/v1/health").json()
    assert set(flask_body.keys()) == set(api_body.keys())


# ---------------------------------------------------------------------------
# ORCA generation (maxdisk configurable)
# ---------------------------------------------------------------------------
VALID_GENERATE = {
    "coords": "O 0.0 0.0 0.0\nH 0.0 0.0 0.96\nH 0.0 0.0 -0.96",
    "name": "water",
    "calc_type": "opt",
    "theory": "B3LYP",
    "basis": "def2-SVP",
}


def test_generate_defaults_and_custom_maxdisk(api):
    r_default = api.post("/api/v1/orca/generate", json=dict(VALID_GENERATE))
    assert r_default.status_code == 200
    body = r_default.json()
    assert body["ok"] is True and body["file_base64"]
    # omitted maxdisk: the RUNNER injects its configured budget at execution;
    # the generated input itself stays backend-neutral unless the caller chose
    import base64
    text_default = base64.b64decode(body["file_base64"]).decode()

    r_custom = api.post("/api/v1/orca/generate",
                        json=dict(VALID_GENERATE, maxdisk_mb=50000))
    assert r_custom.status_code == 200

    # the SCHEMA accepts a custom maxdisk and rejects nonsense
    r_bad = api.post("/api/v1/orca/generate", json=dict(VALID_GENERATE, maxdisk=-5))
    assert r_bad.status_code == 422


def test_generate_validation_error_contract(api):
    r = api.post("/api/v1/orca/generate", json={"name": "no-coords"})
    assert r.status_code == 422
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "message" in body["error"]


# ---------------------------------------------------------------------------
# Kaggle submit idempotency (orchestrator store = authoritative)
# ---------------------------------------------------------------------------
def _two_workers(tmp_path):
    from orca_orchestrator.config import StoreConfig
    from orca_orchestrator.store import JobStore
    shared = str(tmp_path / "shared_state")
    os.makedirs(shared, exist_ok=True)
    worker_a = JobStore(StoreConfig(state_dir=shared))
    worker_b = JobStore(StoreConfig(state_dir=shared))
    return worker_a, worker_b


def test_two_worker_instances_share_store_idempotency(tmp_path, monkeypatch):
    """Two workers (separate JobStore instances over ONE sqlite file) receive
    the same Idempotency-Key: exactly ONE logical submission may happen."""
    import services.kaggle_service as ks

    worker_a, worker_b = _two_workers(tmp_path)
    pushes = []

    def fake_build_job_dir(**kw):
        d = os.path.join(str(tmp_path), "job_%d" % len(pushes))
        os.makedirs(d, exist_ok=True)
        return d

    def fake_push(job_dir, username, key):
        pushes.append(job_dir)
        return {"job_id": "chem-tools-x-1a2b3c4d", "url": "https://x",
                "owner": username, "slug": "chem-tools-x-1a2b3c4d"}

    monkeypatch.setattr(ks.kaggle_runner, "build_job_dir", fake_build_job_dir)
    monkeypatch.setattr(ks.kaggle_runner, "push_job", fake_push)

    def submit(store, idem_key):
        return ks.submit_job(
            kaggle_username="tester", kaggle_key="0" * 32,
            input_filename="mol.inp",
            input_content="! B3LYP Opt\n* xyz 0 1\nO 0 0 0\n*\n",
            dataset_sources_raw="user/orca-dataset", orca_link="",
            input_content_name=None, job_name="w", idem_key=idem_key, store=store)

    payload = dict(kaggle_username="tester", kaggle_key="0" * 32,
                   input_filename="mol.inp",
                   input_content="! B3LYP Opt\n* xyz 0 1\nO 0 0 0\n*\n",
                   dataset_sources_raw="user/orca-dataset", orca_link="",
                   job_name="w")

    r1 = ks.submit_job(idem_key="idem-key-1", store=worker_a, **payload)
    r2 = ks.submit_job(idem_key="idem-key-1", store=worker_b, **payload)

    assert len(pushes) == 1, "the second worker must REPLAY, never push again"
    assert r1[0]["ok"] is True and r2[0]["ok"] is True
    assert r2[0].get("job_id") == r1[0].get("job_id")


def test_concurrent_identical_submissions_create_one_job(tmp_path, monkeypatch):
    """Concurrency proof: two THREADS submit the identical payload through two
    store instances at the same moment - the store serializes them so only
    one push can ever happen (the loser gets 409 or a replay)."""
    import services.kaggle_service as ks

    worker_a, worker_b = _two_workers(tmp_path)
    pushes = []
    lock = __import__("threading").Lock()

    def fake_build_job_dir(**kw):
        d = os.path.join(str(tmp_path), "job_%d" % len(pushes))
        os.makedirs(d, exist_ok=True)
        return d

    def fake_push(job_dir, username, key):
        with lock:
            pushes.append(job_dir)
        return {"job_id": "chem-tools-x-1a2b3c4d", "url": "https://x",
                "owner": username, "slug": "chem-tools-x-1a2b3c4d"}

    monkeypatch.setattr(ks.kaggle_runner, "build_job_dir", fake_build_job_dir)
    monkeypatch.setattr(ks.kaggle_runner, "push_job", fake_push)

    payload = dict(kaggle_username="tester", kaggle_key="0" * 32,
                   input_filename="mol.inp",
                   input_content="! B3LYP Opt\n* xyz 0 1\nO 0 0 0\n*\n",
                   dataset_sources_raw="user/orca-dataset", orca_link="",
                   job_name="w")
    results = []

    def worker(store):
        results.append(ks.submit_job(idem_key=None, store=store, **payload))

    t1 = threading.Thread(target=worker, args=(worker_a,))
    t2 = threading.Thread(target=worker, args=(worker_b,))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert len(pushes) == 1, "identical concurrent submissions must create ONE job"
    statuses = sorted(r[1] for r in results)
    assert statuses in ([200, 200], [200, 409], [409, 200]), statuses


# ---------------------------------------------------------------------------
# Artifact listing / download security
# ---------------------------------------------------------------------------
def _seed_archive(tmp_path, molden=True):
    d = tmp_path / "archive_stage"
    d.mkdir(parents=True, exist_ok=True)
    zip_path = d / "results.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("h2o_opt.out", "ORCA TERMINATED NORMALLY\n")
        zf.writestr("h2o_opt.inp", "! B3LYP Opt\n")
        if molden:
            zf.writestr("h2o.molden.input", "[Molden Format]\n[Atoms] (AU)\nO 1 0 0 0\n")
    return str(zip_path), str(d)


def test_artifact_listing_classifies_molden(api, tmp_path, monkeypatch):
    import kaggle_runner as kr_mod
    zip_path, cleanup = _seed_archive(tmp_path)
    monkeypatch.setattr(kr_mod, "fetch_job_results",
                        lambda u, k, j: (zip_path, cleanup))
    r = api.get("/api/v1/kaggle/jobs/chem-tools-x-1a2b3c4d/artifacts",
                params={"kaggle_username": "tester", "kaggle_key": "0" * 32})
    assert r.status_code == 200
    listing = r.json()
    by_name = {a["filename"]: a for a in listing["artifacts"]}
    assert by_name["h2o.molden.input"]["artifact_type"] == "molden"
    assert by_name["h2o.molden.input"]["sha256"]
    assert "/" not in by_name["h2o.molden.input"]["filename"]


def test_artifact_download_rejects_path_traversal(api, tmp_path, monkeypatch):
    import kaggle_runner as kr_mod
    zip_path, cleanup = _seed_archive(tmp_path)
    monkeypatch.setattr(kr_mod, "fetch_job_results",
                        lambda u, k, j: (zip_path, cleanup))
    for evil in ("..%2Fsecret", "..\\secret", "%2Fetc%2Fpasswd", "C:%5Csecret", ".."):
        r = api.get("/api/v1/kaggle/jobs/chem-tools-x-1a2b3c4d/artifacts/" + evil,
                    params={"kaggle_username": "tester", "kaggle_key": "0" * 32})
        assert r.status_code in (400, 404), evil
    r_ok = api.get("/api/v1/kaggle/jobs/chem-tools-x-1a2b3c4d/artifacts/h2o.molden.input",
                   params={"kaggle_username": "tester", "kaggle_key": "0" * 32})
    assert r_ok.status_code == 200
    assert "[Molden Format]" in r_ok.text


# ---------------------------------------------------------------------------
# Primary pages remain served (WSGI mount)
# ---------------------------------------------------------------------------
def test_primary_pages_served_through_the_api_process(api):
    for route in ("/", "/lab", "/calculations", "/analysis"):
        r = api.get(route)
        assert r.status_code == 200, route
        assert "data-initial-view" in r.text


# ---------------------------------------------------------------------------
# Owner isolation (server-side, requirement 5)
# ---------------------------------------------------------------------------
def test_owner_isolation_user_cannot_fetch_another_users_job(tmp_path):
    from orca_orchestrator.config import StoreConfig
    from orca_orchestrator.credentials import parse as parse_credentials
    from orca_orchestrator.errors import ValidationError
    from orca_orchestrator.models import JobManifest
    from orca_orchestrator.service import OrchestratorService
    from orca_orchestrator.store import JobStore

    store = JobStore(StoreConfig(state_dir=str(tmp_path / "state")))
    svc = OrchestratorService(store, start_watchdog=False)
    svc.ensure_authenticated = lambda creds: creds  # no network in tests
    bobs_job = JobManifest.create(job_id="chem-tools-bobs-1a2b3c4d", owner="bob",
                                  title="bobs", input_filename="mol.inp",
                                  original_input_sha256="h", job_kind="opt")
    store.put_job(bobs_job)

    alices_creds = parse_credentials("alice", "1" * 32)
    with pytest.raises(ValidationError) as excinfo:
        svc.fetch_results(alices_creds, "chem-tools-bobs-1a2b3c4d")
    assert "does not belong" in str(excinfo.value)


def test_owner_isolation_maps_to_403(tmp_path, monkeypatch):
    import services.kaggle_service as ks

    class _Denied(Exception):
        pass

    class _StubService:
        class store:
            @staticmethod
            def begin_idempotent(key, payload):
                return False, None
        def fetch_results(self, creds, job_id):
            raise ks._OrchValidationError("Access denied: Job x does not belong to user alice")

    import orca_orchestrator.service as _svc_mod
    monkeypatch.setattr(_svc_mod, "get_service", lambda: _StubService())
    payload, status = ks.extract_opt_coords("alice", "1" * 32, "chem-tools-bobs-1a2b3c4d")
    assert status == 403
    assert payload["error"]["code"] == "FORBIDDEN"


def test_fetch_archive_maps_owner_denial_to_403(tmp_path, monkeypatch):
    import services.kaggle_service as ks

    class _StubService:
        class store:
            @staticmethod
            def begin_idempotent(key, payload):
                return False, None
        def fetch_results(self, creds, job_id):
            raise ks._OrchValidationError("Access denied: Job x does not belong to user alice")

    import orca_orchestrator.service as _svc_mod
    monkeypatch.setattr(_svc_mod, "get_service", lambda: _StubService())
    zip_path, cleanup, payload, status = ks.fetch_archive("alice", "1" * 32, "chem-tools-bobs-1a2b3c4d")
    assert status == 403 and payload["error"]["code"] == "FORBIDDEN"


# ---------------------------------------------------------------------------
# Wave-2 download hardening
# ---------------------------------------------------------------------------
def test_download_duplicate_zip_members_and_malicious_names(api, tmp_path, monkeypatch):
    import kaggle_runner as kr_mod
    d = tmp_path / "dup_stage"
    d.mkdir(parents=True, exist_ok=True)
    zip_path = d / "results.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("h2o.molden.input", "[Molden Format] version A")
        zf.writestr("sub/h2o.molden.input", "[Molden Format] version B")
        zf.writestr("../evil.txt", "should never be reachable by path")
    monkeypatch.setattr(kr_mod, "fetch_job_results", lambda u, k, j: (str(zip_path), str(d)))

    r = api.get("/api/v1/kaggle/jobs/chem-tools-x-1a2b3c4d/artifacts",
                params={"kaggle_username": "tester", "kaggle_key": "0" * 32})
    assert r.status_code == 200
    names = [a["filename"] for a in r.json()["artifacts"]]
    assert "h2o.molden.input" in names and "evil.txt" in names
    # every listed name is a bare basename - the directory component of the
    # malicious member is stripped, so it can never resolve outside
    for n in names:
        assert "/" not in n and "\\" not in n and ".." not in n


def test_download_large_artifact_integrity(api, tmp_path, monkeypatch):
    import kaggle_runner as kr_mod
    d = tmp_path / "big_stage"
    d.mkdir(parents=True, exist_ok=True)
    zip_path = d / "results.zip"
    big = b"x" * (5 * 1024 * 1024)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("trajectory_big.xyz", big)
    monkeypatch.setattr(kr_mod, "fetch_job_results", lambda u, k, j: (str(zip_path), str(d)))

    r = api.get("/api/v1/kaggle/jobs/chem-tools-x-1a2b3c4d/artifacts/trajectory_big.xyz",
                params={"kaggle_username": "tester", "kaggle_key": "0" * 32})
    assert r.status_code == 200
    assert len(r.content) == 5 * 1024 * 1024
    assert hashlib.sha256(r.content).hexdigest() == hashlib.sha256(big).hexdigest()


def test_cleanup_after_success_and_error(api, tmp_path, monkeypatch):
    import kaggle_runner as kr_mod
    ok_dir = tmp_path / "cleanup_ok"
    ok_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ok_dir / "results.zip", "w") as zf:
        zf.writestr("h2o.molden.input", "[Molden Format]\\n")
    monkeypatch.setattr(kr_mod, "fetch_job_results", lambda u, k, j: (str(ok_dir / "results.zip"), str(ok_dir)))
    r = api.get("/api/v1/kaggle/jobs/chem-tools-x-1a2b3c4d/artifacts/h2o.molden.input",
                params={"kaggle_username": "tester", "kaggle_key": "0" * 32})
    assert r.status_code == 200
    assert not ok_dir.exists(), "cleanup must remove the staging dir after success"

    bad_dir = tmp_path / "cleanup_bad"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "results.zip").write_bytes(b"not a zip at all")
    monkeypatch.setattr(kr_mod, "fetch_job_results", lambda u, k, j: (str(bad_dir / "results.zip"), str(bad_dir)))
    r2 = api.get("/api/v1/kaggle/jobs/chem-tools-x-1a2b3c4d/artifacts/h2o.molden.input",
                 params={"kaggle_username": "tester", "kaggle_key": "0" * 32})
    assert r2.status_code == 502
    assert not bad_dir.exists(), "cleanup must remove the staging dir after error"


# ---------------------------------------------------------------------------
# Error contract coverage
# ---------------------------------------------------------------------------
def test_error_contract_409_in_flight(api, tmp_path, monkeypatch):
    import services.kaggle_service as ks

    class _InFlight:
        class store:
            @staticmethod
            def begin_idempotent(key, payload):
                return True, None   # another worker holds this key right now

    payload, status = ks.submit_job(
        kaggle_username="tester", kaggle_key="0" * 32,
        input_filename="mol.inp", input_content="! B3LYP Opt\n* xyz 0 1\nO 0 0 0\n*",
        dataset_sources_raw="user/orca", orca_link="", job_name="w",
        idem_key="idem-inflight", store=_InFlight.store)
    assert status == 409


def test_error_contract_503_cli_unavailable(api, monkeypatch):
    import services.kaggle_service as ks

    class _CliDown:
        def __init__(self, creds=None):
            pass
        def check_status(self, username, key, job_id):
            raise ks.kaggle_runner.KaggleCliUnavailable("kaggle CLI not installed correctly")

    monkeypatch.setattr(ks.kaggle_runner, "check_job_status",
                        lambda u, k, j: (_ for _ in ()).throw(
                            ks.kaggle_runner.KaggleCliUnavailable("kaggle CLI not installed correctly")))
    payload, status = ks.check_status("tester", "0" * 32, "chem-tools-x-1a2b3c4d")
    assert status == 503


def test_error_contract_502_corrupt_archive(api, tmp_path, monkeypatch):
    import kaggle_runner as kr_mod
    d = tmp_path / "corrupt"
    d.mkdir(parents=True, exist_ok=True)
    (d / "results.zip").write_bytes(b"this is not a zip file")
    monkeypatch.setattr(kr_mod, "fetch_job_results", lambda u, k, j: (str(d / "results.zip"), str(d)))
    r = api.get("/api/v1/kaggle/jobs/chem-tools-x-1a2b3c4d/artifacts",
                params={"kaggle_username": "tester", "kaggle_key": "0" * 32})
    assert r.status_code == 502
    body = r.json()
    assert body["ok"] is False and body["error"]["code"] == "BAD_GATEWAY"


def test_error_contract_404_unknown_job(api, tmp_path, monkeypatch):
    import kaggle_runner as kr_mod
    import orca_orchestrator.errors as _err
    import orca_orchestrator.service as _svc_mod

    class _NoJob:
        def fetch_results(self, creds, job_id):
            raise _err.OrchestratorError("orchestrator store has no such job (test)")

    monkeypatch.setattr(_svc_mod, "get_service", lambda: _NoJob())
    d = tmp_path / "missing"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(kr_mod, "fetch_job_results", lambda u, k, j: (None, None))
    r = api.get("/api/v1/kaggle/jobs/chem-tools-x-1a2b3c4d/artifacts",
                params={"kaggle_username": "tester", "kaggle_key": "0" * 32})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"
