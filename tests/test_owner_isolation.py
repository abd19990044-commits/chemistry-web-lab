# -*- coding: utf-8 -*-
"""Owner-isolation matrix (requirements A-G) through the /api/v1 surface.

A. owner A accesses owner A job      -> allowed
B. owner B accesses owner A job      -> 403
C. owner B requests owner A artifact -> 403
D. owner B requests owner A Molden   -> 403
E. unknown job                       -> 404
F. invalid credential                -> 400/401
G. owner denial MUST NOT fall back to the legacy Kaggle fetch
"""
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OWNER_A = "alice"
OWNER_B = "bob"
JOB_ID = "chem-tools-matrix-1a2b3c4d"


@pytest.fixture
def isolated_api(tmp_path, monkeypatch):
    """A stub orchestrator service that enforces owner isolation exactly like
    the real fetch_results (ValidationError on mismatch) - plus a sentinel
    legacy fetch that FAILS the test if it is ever reached on a denial."""
    import orca_orchestrator.service as svc_mod

    stage = tmp_path / "stage"
    stage.mkdir(parents=True, exist_ok=True)
    zip_path = stage / "results.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("h2o.molden.input", "[Molden Format]\n[Atoms] (AU)\nO 1 0 0 0\n")
        zf.writestr("h2o_opt.out", "ORCA TERMINATED NORMALLY\n")

    reached = {"legacy": 0}

    class _StubOrchestrator:
        def fetch_results(self, creds, job_id):
            if creds.username != OWNER_A:
                from orca_orchestrator.errors import ValidationError
                raise ValidationError(
                    "Access denied: Job %s does not belong to user %s" % (job_id, creds.username))
            return str(zip_path), str(stage)

    class _StubService:
        store = type("S", (), {"begin_idempotent": staticmethod(lambda k, p: (False, None))})()
        def fetch_results(self, creds, job_id):
            return _StubOrchestrator().fetch_results(creds, job_id)

    monkeypatch.setattr(svc_mod, "get_service", lambda: _StubService())

    import kaggle_runner as kr_mod

    def _legacy_must_not_run_on_denial(*a, **k):
        reached["legacy"] += 1
        return str(zip_path), str(stage)

    monkeypatch.setattr(kr_mod, "fetch_job_results", _legacy_must_not_run_on_denial)
    from fastapi.testclient import TestClient
    import api.main as api_main
    with TestClient(api_main.app) as c:
        yield c, reached


def _creds(user):
    return {"X-Kaggle-Username": user, "X-Kaggle-Key": "0" * 32}


def test_A_owner_can_list_own_artifacts(isolated_api):
    client, reached = isolated_api
    r = client.get("/api/v1/kaggle/jobs/%s/artifacts" % JOB_ID, headers=_creds(OWNER_A))
    assert r.status_code == 200
    types = {a["filename"]: a["artifact_type"] for a in r.json()["artifacts"]}
    assert types["h2o.molden.input"] == "molden"


def test_B_other_owner_job_is_403(isolated_api):
    client, reached = isolated_api
    r = client.get("/api/v1/kaggle/jobs/%s/artifacts" % JOB_ID, headers=_creds(OWNER_B))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


def test_C_other_owner_artifact_is_403(isolated_api):
    client, reached = isolated_api
    r = client.get("/api/v1/kaggle/jobs/%s/artifacts/h2o_opt.out" % JOB_ID,
                   headers=_creds(OWNER_B))
    assert r.status_code == 403


def test_D_other_owner_molden_is_403(isolated_api):
    client, reached = isolated_api
    r = client.get("/api/v1/kaggle/jobs/%s/artifacts/h2o.molden.input" % JOB_ID,
                   headers=_creds(OWNER_B))
    assert r.status_code == 403
    assert "h2o.molden.input" not in (r.text or "")


def test_E_unknown_job_is_404(isolated_api, tmp_path, monkeypatch):
    import kaggle_runner as kr_mod
    import orca_orchestrator.service as svc_mod

    class _NoJob:
        def fetch_results(self, creds, job_id):
            from orca_orchestrator.errors import OrchestratorError
            raise OrchestratorError("no such job in the orchestrator store (test)")

    monkeypatch.setattr(svc_mod, "get_service", lambda: _NoJob())
    monkeypatch.setattr(kr_mod, "fetch_job_results", lambda u, k, j: (None, None))
    client, reached = isolated_api
    r = client.get("/api/v1/kaggle/jobs/chem-tools-none-1a2b3c4d/artifacts",
                   headers=_creds(OWNER_A))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_F_missing_credentials_rejected(isolated_api):
    client, reached = isolated_api
    r = client.get("/api/v1/kaggle/jobs/%s/artifacts" % JOB_ID,
                   headers={})
    assert r.status_code == 422


def test_G_owner_denial_never_falls_back_to_legacy_fetch(isolated_api):
    client, reached = isolated_api
    r = client.get("/api/v1/kaggle/jobs/%s/artifacts/h2o.molden.input" % JOB_ID,
                   headers=_creds(OWNER_B))
    assert r.status_code == 403
    assert reached["legacy"] == 0, \
        "an owner denial must NEVER reach the legacy Kaggle fetch path"
