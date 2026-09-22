import pytest
from app import app as _flask_app

@pytest.fixture
def client():
    _flask_app.config["TESTING"] = True
    return _flask_app.test_client()
# -*- coding: utf-8 -*-
"""Focused regressions for the production reliability repairs."""
import os
import zipfile

from services import local_agent_service as agent_service
from orca_orchestrator.service import _safe_zip_file


def _paired_agent(tmp_path):
    state_dir = str(tmp_path)
    agent_service.init_runtime_session(
        installation_id="inst-reliability",
        agent_session_id="sess-reliability",
        token_verifiers=[agent_service.hash_token_verifier("CLA_reliability")],
        state_dir=state_dir,
    )
    agent_service.claim_runtime_token("CLA_reliability", "owner-reliability", state_dir=state_dir)
    result = agent_service.finalize_agent_runtime(
        "sess-reliability", "CLA_reliability", state_dir=state_dir)
    return state_dir, result["runtime_session_secret"]


def test_local_agent_submit_is_idempotent_and_completion_requires_normal_end(tmp_path):
    state_dir, secret = _paired_agent(tmp_path)
    first = agent_service.enqueue_agent_job(
        "sess-reliability", "owner-reliability", "! SP\n* xyz 0 1\nH 0 0 0\n*",
        job_name="same-job", state_dir=state_dir, idempotency_key="same-submit")
    replay = agent_service.enqueue_agent_job(
        "sess-reliability", "owner-reliability", "! SP\n* xyz 0 1\nH 0 0 0\n*",
        job_name="same-job", state_dir=state_dir, idempotency_key="same-submit")
    assert first["job_id"] == replay["job_id"]

    claimed = agent_service.poll_next_agent_job(
        "sess-reliability", secret, state_dir=state_dir)["job"]
    failed = agent_service.complete_agent_job(
        claimed["job_id"], "sess-reliability", secret, exit_code=0,
        stdout_tail="FINAL SINGLE POINT ENERGY -1.0", state_dir=state_dir)
    assert failed["status"] == "FAILED"


def test_download_archive_rejects_path_traversal(tmp_path):
    safe = tmp_path / "safe.zip"
    with zipfile.ZipFile(safe, "w") as archive:
        archive.writestr("molecule.out", "ok")
    assert _safe_zip_file(str(safe)) is True

    unsafe = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../../outside.txt", "must not extract")
    assert _safe_zip_file(str(unsafe)) is False


def test_frontend_does_not_put_kaggle_key_in_download_url():
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "static", "js", "app.js"), encoding="utf-8") as fh:
        source = fh.read()
    assert "kaggle_key=${encodeURIComponent" not in source
    assert 'downloadForm.method = "POST"' in source
    assert "const pollGuards = new Map()" in source


# ---------------------------------------------------------------------------
# Phase 2 Confirmed Findings Regressions
# ---------------------------------------------------------------------------

def test_sanitize_header_filename_prevents_injection_and_traversal():
    """F-030: Response filename sanitizer prevents CRLF, directory traversal, and quote escaping."""
    from app import sanitize_header_filename

    # CRLF injection
    assert "\r" not in sanitize_header_filename("bad\r\nfilename.out")
    assert "\n" not in sanitize_header_filename("bad\r\nfilename.out")

    # Traversal characters
    assert "/" not in sanitize_header_filename("../../etc/passwd")
    assert "\\" not in sanitize_header_filename("..\\..\\boot.ini")

    # Dangerous characters
    clean = sanitize_header_filename('report"with;special<chars>|?.out')
    assert '"' not in clean and "<" not in clean and ">" not in clean and "?" not in clean

    # Non-ASCII or empty fallbacks
    assert sanitize_header_filename("") == "download.bin"
    assert sanitize_header_filename(None) == "download.bin"
    assert sanitize_header_filename("   ...  ") == "download.bin"


def test_api_kaggle_download_rejects_query_credentials(client):
    """F-020: /api/kaggle/download rejects credentials passed in query parameters with 400."""
    res = client.get("/api/kaggle/download?job_id=chem-test-1234&kaggle_key=secretkey123&kaggle_username=tester")
    assert res.status_code == 400
    data = res.get_json()
    assert data["ok"] is False
    assert "query parameters is forbidden" in data["error"]


def test_reaction_start_execution_requires_admin_for_server_local(client, monkeypatch):
    """F-003: Anonymous/unauthenticated and non-admin users cannot run server-local reaction workflows."""
    from services.reaction_workflow_service import create_reaction, ReactionStore
    import tempfile

    monkeypatch.setenv("CHEMISTRY_LAB_MULTI_USER", "1")
    tmp = tempfile.mkdtemp()
    store = ReactionStore(tmp)
    monkeypatch.setattr("app._reaction_store", store)

    rxn = create_reaction("anon_user_1", "A -> B", store=store)
    # Add a stage configured with server_local backend
    rxn["species"][0]["stages"] = [{
        "stage_id": "stg_1",
        "kind": "OPT",
        "backend": "server_local",
        "state": "READY",
        "input_text": "! Opt\n* xyz 0 1\nH 0 0 0\n*",
    }]
    store.save_reaction(rxn)

    # Anonymous request without auth -> 401
    with client.session_transaction() as sess:
        sess["anon_id"] = "anon_user_1"
    res = client.post(f"/api/v1/reactions/{rxn['reaction_id']}/start-execution")
    assert res.status_code == 401
    assert res.get_json()["error"]["code"] == "UNAUTHORIZED"

    # Authenticated but non-admin request -> 403
    rxn_reg = create_reaction("regular_scientist", "A -> B", store=store)
    rxn_reg["species"][0]["stages"] = [{
        "stage_id": "stg_2",
        "kind": "OPT",
        "backend": "server_local",
        "state": "READY",
        "input_text": "! Opt\n* xyz 0 1\nH 0 0 0\n*",
    }]
    store.save_reaction(rxn_reg)

    with client.session_transaction() as sess:
        sess["user"] = {"sub": "regular_scientist"}
    res = client.post(f"/api/v1/reactions/{rxn_reg['reaction_id']}/start-execution")
    assert res.status_code == 403
    assert res.get_json()["error"]["code"] == "FORBIDDEN"

    # Authenticated admin request -> 200
    monkeypatch.setattr("app.is_admin_user", lambda owner: True)
    res = client.post(f"/api/v1/reactions/{rxn_reg['reaction_id']}/start-execution")
    assert res.status_code == 200
    assert res.get_json()["ok"] is True


def test_reaction_stage_retry_endpoint(client, monkeypatch):
    """F-015: /api/v1/reactions/<rxn_id>/stages/<stage_id>/retry resets state and increments attempt_no."""
    from services.reaction_workflow_service import create_reaction, ReactionStore
    import tempfile

    tmp = tempfile.mkdtemp()
    store = ReactionStore(tmp)
    monkeypatch.setattr("app._reaction_store", store)

    rxn = create_reaction("test_owner", "A -> B", store=store)
    rxn["species"][0]["stages"] = [{
        "stage_id": "stg_fail",
        "kind": "OPT",
        "backend": "server_local",
        "state": "FAILED",
        "attempt_no": 1,
        "attempt_id": "att_1",
        "error": "SCF did not converge",
        "input_text": "! Opt\n* xyz 0 1\nH 0 0 0\n*",
    }]
    store.save_reaction(rxn)

    with client.session_transaction() as sess:
        sess["user"] = {"sub": "test_owner"}

    res = client.post(f"/api/v1/reactions/{rxn['reaction_id']}/stages/stg_fail/retry")
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    retried_stage = body["stage"]
    assert retried_stage["state"] == "READY"
    assert retried_stage["attempt_no"] == 2
    assert retried_stage["attempt_id"] != "att_1"
    assert retried_stage.get("error") is None


def test_kaggle_submit_persists_before_push_and_handles_push_outcomes(monkeypatch, tmp_path):
    """F-004 & F-007: submit_job persists JobState.CREATED before push, handles 404 vs unknown on push failure."""
    from services import kaggle_service
    from orca_orchestrator.store import JobStore as OrchestratorStore
    from orca_orchestrator.config import StoreConfig
    from orca_orchestrator.states import JobState

    store = OrchestratorStore(StoreConfig(state_dir=str(tmp_path)))

    pushed_actions = []

    def mock_build_job_dir(**kwargs):
        return str(tmp_path / "mock_job")

    def mock_push_success(job_dir, u, k):
        # Verify that manifest was persisted in store BEFORE this call returned!
        jobs = store.list_active_jobs()
        assert len(jobs) == 1
        assert jobs[0].state == JobState.CREATED
        pushed_actions.append("pushed")
        return {"url": "https://kaggle.com/code/user/test-job", "job_id": "test-job", "owner": "user"}

    monkeypatch.setattr("kaggle_runner.build_job_dir", mock_build_job_dir)
    monkeypatch.setattr("kaggle_runner.push_job", mock_push_success)

    # 1. Successful submission flow
    res, code = kaggle_service.submit_job(
        kaggle_username="user",
        kaggle_key="key",
        dataset_sources_raw="ds/1",
        orca_link="",
        input_filename="mol.inp",
        input_content="! B3LYP",
        job_name="test-job",
        store=store,
    )
    assert code == 200
    assert res["ok"] is True
    # In-progress manifest updated to RUNNING
    m = store.get_job(res["job_id"])
    assert m is not None
    assert m.state == JobState.RUNNING

    # 2. Push failure where Kaggle probe returns 404 (definitively not landed) -> claim abandoned (F-004)
    class FakeKaggleClientNotFound:
        def __init__(self, creds):
            pass
        def kernel_exists(self, slug):
            return None  # 404 NotFound

    monkeypatch.setattr("orca_orchestrator.kaggle_api.KaggleClient", FakeKaggleClientNotFound)

    def mock_push_fail(job_dir, u, k):
        raise RuntimeError("Network reset during push")

    monkeypatch.setattr("kaggle_runner.push_job", mock_push_fail)

    res2, code2 = kaggle_service.submit_job(
        kaggle_username="user",
        kaggle_key="key",
        dataset_sources_raw="ds/1",
        orca_link="",
        input_filename="mol.inp",
        input_content="! B3LYP",
        job_name="test-job-fail-404",
        idem_key="idem_fail_404",
        store=store,
    )
    assert code2 in (502, 503)
    assert res2["ok"] is False
    assert res2["code"] == "SUBMIT_FAILED"
    # Verify idempotency claim was released, allowing clean retry
    replay, _ = store.begin_idempotent("idem_fail_404", {})
    assert replay is False

    # 3. Push failure where Kaggle probe raises exception (remote state indeterminate) -> claim retained as SUBMISSION_UNKNOWN
    class FakeKaggleClientUnreachable:
        def __init__(self, creds):
            pass
        def kernel_exists(self, slug):
            raise RuntimeError("Connection timed out to Kaggle API")

    monkeypatch.setattr("orca_orchestrator.kaggle_api.KaggleClient", FakeKaggleClientUnreachable)

    res3, code3 = kaggle_service.submit_job(
        kaggle_username="user",
        kaggle_key="key",
        dataset_sources_raw="ds/1",
        orca_link="",
        input_filename="mol.inp",
        input_content="! B3LYP",
        job_name="test-job-unknown",
        idem_key="idem_unknown_test",
        store=store,
    )
    assert code3 in (502, 503)
    assert res3["code"] == "SUBMISSION_UNKNOWN"
    # Idempotency claim was NOT abandoned - claim row is retained as in_progress!
    with store.transaction() as conn:
        row_u = conn.execute("SELECT * FROM idempotency WHERE key = ?", ("idem_unknown_test",)).fetchone()
    assert row_u is not None
    assert row_u["status"] == "in_progress"


def test_store_idempotency_does_not_prune_in_progress_by_age(tmp_path):
    """F-008: Store begin_idempotent prunes only terminal states ('done', 'failed'), never active 'in_progress' claims."""
    from orca_orchestrator.store import JobStore as OrchestratorStore
    from orca_orchestrator.config import StoreConfig

    store = OrchestratorStore(StoreConfig(state_dir=str(tmp_path), idempotency_ttl_seconds=10))

    # Insert an in_progress claim with an old timestamp (older than TTL)
    old_ts = 1000.0
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO idempotency(key, request_hash, status, created_at) VALUES(?,?,'in_progress',?)",
            ("key_in_progress_old", "hash1", old_ts),
        )
        conn.execute(
            "INSERT INTO idempotency(key, request_hash, status, created_at, completed_at, response_json) VALUES(?,?,'done',?,?,?)",
            ("key_done_old", "hash2", old_ts, old_ts + 1, "{}"),
        )

    # Calling begin_idempotent triggers pruning of records older than TTL
    replay, _ = store.begin_idempotent("key_fresh", {"payload": 1})
    assert replay is False

    with store.transaction() as conn:
        row_in_prog = conn.execute("SELECT status FROM idempotency WHERE key = 'key_in_progress_old'").fetchone()
        row_done = conn.execute("SELECT status FROM idempotency WHERE key = 'key_done_old'").fetchone()

    # The in_progress claim must NOT have been pruned!
    assert row_in_prog is not None
    assert row_in_prog["status"] == "in_progress"
    # The terminal 'done' claim was properly pruned!
    assert row_done is None


def test_init_runtime_session_blocks_spoofed_session_hijack(tmp_path):
    """F-018: An unauthenticated init cannot supersede an active installation session without the installation secret."""
    state_dir = str(tmp_path)

    # Legitimate agent initializes with an installation secret
    init1 = agent_service.init_runtime_session(
        installation_id="inst_victim",
        agent_session_id="sess_victim_1",
        token_verifiers=[agent_service.hash_token_verifier("CLA_victim")],
        state_dir=state_dir,
        installation_secret="super_secret_victim_proof",
    )
    assert init1["ok"] is True

    # Website claims the agent, moving it to ONLINE
    agent_service.claim_runtime_token("CLA_victim", "victim_owner", state_dir=state_dir)
    fin = agent_service.finalize_agent_runtime("sess_victim_1", "CLA_victim", state_dir=state_dir)
    assert fin["ok"] is True
    assert fin["status"] == "ONLINE"

    # Attacker attempts to spoof init for inst_victim without secret -> rejected with 403
    spoof_res = agent_service.init_runtime_session(
        installation_id="inst_victim",
        agent_session_id="sess_attacker",
        token_verifiers=[agent_service.hash_token_verifier("CLA_attacker")],
        state_dir=state_dir,
        installation_secret=None,
    )
    assert spoof_res["ok"] is False
    assert spoof_res["error_code"] == "INSTALLATION_AUTH_REQUIRED"

    # Legitimate agent restarts and provides the installation secret -> successfully supersedes
    restart_res = agent_service.init_runtime_session(
        installation_id="inst_victim",
        agent_session_id="sess_victim_2",
        token_verifiers=[agent_service.hash_token_verifier("CLA_victim_new")],
        state_dir=state_dir,
        installation_secret="super_secret_victim_proof",
    )
    assert restart_res["ok"] is True
    assert restart_res["agent_session_id"] == "sess_victim_2"


def test_inject_orca_resources_structure_and_comment_aware():
    """F-023: %pal and %maxcore rewriting does not corrupt comments or eat subsequent lines."""
    input_text = """! B3LYP def2-SVP
%pal nprocs 8 end # parallel block comment
%maxcore 4000 # per core memory
* xyz 0 1
O 0.0 0.0 0.0
H 0.0 0.7 0.5
H 0.0 -0.7 0.5
*
"""
    resources = {"cpu_cores": 4, "ram_gb": 8.0, "disk_gb": 20.0}
    updated = agent_service.inject_orca_resources(input_text, resources)

    # Injected new %pal with 4 cores
    assert "nprocs 4" in updated
    # Old 8 procs replaced
    assert "nprocs 8" not in updated
    # Coordinate block completely preserved!
    assert "O 0.0 0.0 0.0" in updated
    assert "H 0.0 0.7 0.5" in updated
    assert "H 0.0 -0.7 0.5" in updated

    # Multi-line with comments
    input_multiline = """! PBE0 def2-TZVP
%pal
  nprocs 16
end # multi line end with comment
* xyz 0 1
C 0.0 0.0 0.0
*
"""
    updated_multi = agent_service.inject_orca_resources(input_multiline, resources)
    assert "nprocs 4" in updated_multi
    assert "nprocs 16" not in updated_multi
    assert "C 0.0 0.0 0.0" in updated_multi


def test_hpc_adapter_sentinel_and_status_classification():
    """F-028 & F-033: Sentinel partition 'default' is normalized, and nonzero exit without unknown job maps to UNKNOWN."""
    from local_agent.hpc.base import validate_resources
    from local_agent.hpc.pbs import PbsAdapter
    from local_agent.hpc.slurm import SlurmAdapter
    import subprocess

    # F-033: 'default' partition is dropped so cluster native default is used
    res = validate_resources({"cpu_cores": 4, "partition": "default", "queue": "DEFAULT"})
    assert "partition" not in res
    assert "queue" not in res

    slurm = SlurmAdapter({})
    script = slurm.generate_sbatch_script("job1", "/usr/bin/orca", "mol.inp", {"cpu_cores": 4, "partition": "default"}, "/tmp")
    assert "--partition" not in script

    pbs = PbsAdapter({})
    pbs_script = pbs.generate_pbs_script("job1", "/usr/bin/orca", "mol.inp", {"cpu_cores": 4, "queue": "default"}, "/tmp")
    assert "#PBS -q" not in pbs_script

    # F-028: PBS qstat nonzero exit without unknown job returns UNKNOWN
    class FakeProcErr:
        def __init__(self, code, stderr):
            self.returncode = code
            self.stderr = stderr
            self.stdout = ""

    # Transient error (connection refused)
    pbs_adapter = PbsAdapter({})
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeProcErr(1, "qstat: Connection refused"))
    assert pbs_adapter.get_job_status("12345.pbs") == "UNKNOWN"

    # Job finished (unknown job id)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeProcErr(153, "qstat: Unknown Job Id 12345.pbs"))
    assert pbs_adapter.get_job_status("12345.pbs") == "COMPLETED"
    monkeypatch.undo()


def test_api_kaggle_delete_syncs_with_orchestrator_store(client, monkeypatch, tmp_path):
    """F-025: Deleting a Kaggle job removes it from both Kaggle and local orchestrator store."""
    from orca_orchestrator.store import JobStore as OrchestratorStore
    from orca_orchestrator.config import StoreConfig
    from orca_orchestrator.models import JobManifest

    store = OrchestratorStore(StoreConfig(state_dir=str(tmp_path)))
    manifest = JobManifest.create(
        job_id="chem-tools-del-test-1234",
        owner="testuser",
        title="del-test",
        input_filename="mol.inp",
        original_input_sha256="abc",
    )
    store.put_job(manifest)
    assert store.get_job("chem-tools-del-test-1234") is not None

    class MockService:
        def __init__(self):
            self.store = store

        def delete(self, creds, job_id):
            self.store.delete_job(job_id)
            return {"deleted": [job_id]}

    mock_svc = MockService()
    monkeypatch.setattr("orca_orchestrator.service.get_service", lambda: mock_svc)
    monkeypatch.setattr("orca_orchestrator.legacy_compat.get_service", lambda: mock_svc)
    monkeypatch.setattr("kaggle_runner.delete_job", lambda u, k, j: None)
    monkeypatch.setattr("kaggle_runner.is_valid_job_id", lambda j: True)

    with client.session_transaction() as sess:
        sess["user"] = {"sub": "testuser"}

    res = client.post("/api/kaggle/delete", json={
        "job_id": "chem-tools-del-test-1234",
        "kaggle_username": "testuser",
        "kaggle_key": "dummykey",
    })
    assert res.status_code == 200
    assert res.get_json()["ok"] is True
    # Local store must no longer have the job!
    assert store.get_job("chem-tools-del-test-1234") is None
