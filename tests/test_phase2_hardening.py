"""Persistence, parser, archive, and API contracts added in Phase 2."""
from __future__ import annotations

import os
import pathlib
import sqlite3
from types import SimpleNamespace
import zipfile

import pytest

from orca_orchestrator.errors import (
    ConcurrencyError,
    LeaseLostError,
    NetworkError,
    RateLimitError,
    SubmissionUnknownError,
)
from orca_orchestrator.kaggle_api import (
    KaggleClient,
    KernelStatus,
    PushResult,
    classify_status,
)
from orca_orchestrator.credentials import KaggleCredentials
from orca_orchestrator.retry import RetryPolicy, classify_subprocess_failure
from orca_orchestrator.result_store import ResultArtifactStore
from services.workflow_store import get_workflow, sync_reaction, transition_step, retry_step
from services.reaction_workflow_service import (
    ReactionStore, _select_orca_output_info, apply_local_worker_result,
    new_stage, retry_stage, generate_unified_reaction_inputs
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('owner/slug has status "KernelWorkerStatus.NEW_SCRIPT"', "queued"),
        ('owner/slug has status "KernelWorkerStatus.QUEUED"', "queued"),
        ('owner/slug has status "KernelWorkerStatus.RUNNING"', "running"),
        ('owner/slug has status "KernelWorkerStatus.COMPLETE"', "complete"),
        ('owner/slug has status "KernelWorkerStatus.ERROR"', "error"),
        ('owner/slug has status "KernelWorkerStatus.CANCELLED"', "cancelled"),
        ('owner/slug has status "KernelWorkerStatus.INCOMPLETE"', "unknown"),
        ('owner/slug has status "KernelWorkerStatus.ERROR_DETAIL"', "unknown"),
        ('owner/chem-tools-error-test has no structured status', "unknown"),
        ('{malformed json and no status field}', "unknown"),
    ],
)
def test_kaggle_status_parser_is_structured(raw, expected):
    assert classify_status(raw) == expected


def test_reaction_kaggle_result_prefers_scientific_out_over_runner_log(tmp_path):
    archive_path = tmp_path / "results.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("runner.log", "runner diagnostics" * 100)
        archive.writestr("nested/molecule.out", "ORCA TERMINATED NORMALLY")
        archive.writestr("other.out", "short unrelated output")
    with zipfile.ZipFile(archive_path) as archive:
        selected = _select_orca_output_info(archive, "molecule.inp")
    assert selected is not None
    assert selected.filename == "nested/molecule.out"


def test_scientific_output_matching_beats_incidental_out_extension(tmp_path):
    archive_path = tmp_path / "results.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("runner.out", "not ORCA" * 100)
        archive.writestr("molecule.log", "ORCA TERMINATED NORMALLY")
    with zipfile.ZipFile(archive_path) as archive:
        selected = _select_orca_output_info(archive, "molecule.inp")
    assert selected is not None
    assert selected.filename == "molecule.log"


def test_extract_opt_coords_prefers_converged_output_over_stale_xyz(tmp_path, monkeypatch):
    import services.kaggle_service as kaggle_service

    archive_path = tmp_path / "results.zip"
    output = """
CARTESIAN COORDINATES (ANGSTROEM)
---------------------------------
O  0.000000  0.000000  0.123456
H  0.000000  0.700000 -0.400000
H  0.000000 -0.700000 -0.400000

THE OPTIMIZATION HAS CONVERGED
ORCA TERMINATED NORMALLY
"""
    stale_xyz = "3\nstale input\nO 0 0 9.999\nH 0 1 9.999\nH 0 -1 9.999\n"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("runner.out", "runner diagnostics")
        archive.writestr("molecule.out", output)
        archive.writestr("molecule.xyz", stale_xyz)
        archive.writestr("molecule.inp", "! B3LYP Opt\n* xyz 0 1\nO 0 0 0\nH 0 1 0\nH 0 -1 0\n*\n")
    monkeypatch.setattr(
        kaggle_service.kaggle_runner,
        "fetch_job_results",
        lambda *args, **kwargs: (str(archive_path), None),
    )

    payload, status = kaggle_service.extract_opt_coords(
        "tester", "0" * 32, "chem-tools-geometry-1a2b3c4d"
    )
    assert status == 200, payload
    assert "0.123456" in payload["coords"]
    assert "9.999" not in payload["coords"]


def test_extract_opt_coords_rejects_unsafe_archive(tmp_path, monkeypatch):
    import services.kaggle_service as kaggle_service

    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../molecule.out", "ORCA TERMINATED NORMALLY")
        archive.writestr("molecule.inp", "! Opt\n* xyz 0 1\n*\n")
    monkeypatch.setattr(
        kaggle_service.kaggle_runner,
        "fetch_job_results",
        lambda *args, **kwargs: (str(archive_path), None),
    )
    payload, status = kaggle_service.extract_opt_coords(
        "tester", "0" * 32, "chem-tools-unsafe-1a2b3c4d"
    )
    assert status == 502
    assert payload["ok"] is False


def test_retry_policy_retries_transient_faults_with_bounded_backoff(monkeypatch):
    calls = []
    sleeps = []
    monkeypatch.setattr("orca_orchestrator.retry.time.sleep", sleeps.append)
    policy = RetryPolicy(max_attempts=4, base_delay=0, max_delay=0, deadline_seconds=5)

    def operation(attempt):
        calls.append(attempt.number)
        if len(calls) < 3:
            raise NetworkError("simulated connection reset")
        return "ok"

    assert policy.call("fault-injected", operation) == "ok"
    assert calls == [1, 2, 3]
    assert sleeps == [0, 0]


@pytest.mark.parametrize("message", [
    "connection reset by peer", "temporary failure in name resolution",
    "read timed out", "500 server error", "502 bad gateway", "503 service unavailable",
])
def test_kaggle_transport_faults_are_retryable(message):
    assert isinstance(classify_subprocess_failure(1, message), NetworkError)


def test_kaggle_rate_limit_is_typed_and_retryable():
    error = classify_subprocess_failure(1, "HTTP 429 too many requests")
    assert isinstance(error, RateLimitError)
    assert error.retry_after == 30.0


def test_ambiguous_kaggle_push_never_blindly_retries(tmp_path, monkeypatch):
    job_dir = tmp_path / "kernel"
    job_dir.mkdir()
    (job_dir / "kernel-metadata.json").write_text("{}", encoding="utf-8")
    client = KaggleClient(
        KaggleCredentials(username="owner", key="a" * 32),
        retry=RetryPolicy(max_attempts=4, base_delay=0, max_delay=0),
    )
    pushes = []

    def ambiguous_run(*_args, **_kwargs):
        pushes.append(1)
        raise NetworkError("response lost after upload")

    monkeypatch.setattr(client, "_run", ambiguous_run)
    monkeypatch.setattr(
        client,
        "kernel_exists",
        lambda _slug: (_ for _ in ()).throw(NetworkError("probe unavailable")),
    )
    with pytest.raises(SubmissionUnknownError):
        client.push_kernel(str(job_dir), expected_slug="chem-tools-ambiguous-1a2b3c4d", skip_if_active=False)
    assert len(pushes) == 1


def test_ambiguous_push_accepts_terminal_exact_slug_without_new_version(tmp_path, monkeypatch):
    job_dir = tmp_path / "kernel"
    job_dir.mkdir()
    (job_dir / "kernel-metadata.json").write_text("{}", encoding="utf-8")
    client = KaggleClient(
        KaggleCredentials(username="owner", key="a" * 32),
        retry=RetryPolicy(max_attempts=4, base_delay=0, max_delay=0),
    )
    pushes = []

    def lost_response(*_args, **_kwargs):
        pushes.append(1)
        raise NetworkError("response lost")

    monkeypatch.setattr(client, "_run", lost_response)
    monkeypatch.setattr(
        client,
        "kernel_exists",
        lambda slug: KernelStatus(slug=slug, status="complete", raw="COMPLETE"),
    )
    result = client.push_kernel(
        str(job_dir), expected_slug="chem-tools-complete-1a2b3c4d", skip_if_active=False
    )
    assert result.slug == "chem-tools-complete-1a2b3c4d"
    assert len(pushes) == 1


def test_initial_submission_bundle_survives_process_restart(tmp_path, monkeypatch):
    from orca_orchestrator.config import StoreConfig
    from orca_orchestrator.service import OrchestratorService
    from orca_orchestrator.states import JobState
    from orca_orchestrator.store import JobStore
    import orca_orchestrator.reconciler as reconciler_module
    import orca_orchestrator.service as service_module

    store = JobStore(StoreConfig(state_dir=str(tmp_path / "store")))
    service = OrchestratorService(store=store, start_watchdog=False)
    pushes = []

    def fake_build(directory, **_kwargs):
        pathlib.Path(directory, "kernel-metadata.json").write_text("{}", encoding="utf-8")
        pathlib.Path(directory, "molecule.inp").write_text("! HF", encoding="utf-8")

    class FakeClient:
        recovering = False

        def __init__(self, creds):
            self.creds = creds

        def push_kernel(self, job_dir, *, expected_slug, skip_if_active=True):
            pushes.append((expected_slug, pathlib.Path(job_dir).is_dir()))
            if not self.recovering:
                raise NetworkError("lost initial push response")
            return PushResult(
                slug=expected_slug, owner=self.creds.username,
                url=f"https://www.kaggle.com/code/{self.creds.username}/{expected_slug}",
                requested_slug=expected_slug,
            )

        def kernel_exists(self, slug):
            if not self.recovering:
                raise NetworkError("exact-slug probe unavailable")
            return None

    monkeypatch.setattr(service_module, "build_window_directory", fake_build)
    monkeypatch.setattr(service_module, "KaggleClient", FakeClient)
    creds = KaggleCredentials(username="owner", key="a" * 32)
    with pytest.raises(SubmissionUnknownError):
        service.submit(
            creds, input_filename="molecule.inp", input_content="! HF",
            dataset_sources=["owner/orca"], idempotency_key="durable-initial",
        )
    manifest = store.list_active_jobs()[0]
    submission_dir = manifest._extra["submission_dir"]
    assert manifest.state is JobState.UPLOADING
    assert pathlib.Path(submission_dir, "kernel-metadata.json").is_file()

    FakeClient.recovering = True
    monkeypatch.setattr(reconciler_module, "KaggleClient", FakeClient)
    recovered = service.reconciler.reconcile(manifest.job_id, creds, actor="startup-test")
    assert recovered.job_id == manifest.job_id
    assert recovered.state is JobState.QUEUED
    assert not pathlib.Path(submission_dir).exists()
    assert [slug for slug, existed in pushes if existed] == [manifest.job_id, manifest.job_id]
    service.shutdown()


def test_partial_kaggle_download_is_retried_in_clean_directory(tmp_path, monkeypatch):
    client = KaggleClient(
        KaggleCredentials(username="owner", key="a" * 32),
        retry=RetryPolicy(max_attempts=2, base_delay=0, max_delay=0),
    )
    calls = {"count": 0}

    def fake_run(*_args, **_kwargs):
        calls["count"] += 1
        out_dir = _kwargs.get("_out_dir")
        # fetch_output owns the temp directory, so find it from the CLI -p arg
        # instead of touching the real Kaggle environment.
        args = list(_args[0])
        out_dir = args[args.index("-p") + 1]
        if calls["count"] == 1:
            pathlib.Path(out_dir, "partial.out").write_text("truncated", encoding="utf-8")
            return SimpleNamespace(returncode=1, combined="connection reset", stdout="", stderr="connection reset")
        pathlib.Path(out_dir, "complete.out").write_text("complete", encoding="utf-8")
        return SimpleNamespace(returncode=0, combined="ok", stdout="", stderr="")

    monkeypatch.setattr(client, "_run", fake_run)
    out_dir = client.fetch_output("owner-job", timeout=1)
    assert calls["count"] == 2
    assert sorted(os.listdir(out_dir)) == ["complete.out"]


def test_workflow_steps_are_transactional_and_terminal_states_are_protected(tmp_path):
    state_dir = str(tmp_path / "state")
    reaction = {
        "reaction_id": "reaction-1",
        "owner": "owner-a",
        "species": [
            {
                "species_id": "species-1",
                "workflow_id": "workflow-1",
                "state": "RUNNING",
                "workflow_source": "CUSTOM",
                "workflow_hash": "hash",
                "stages": [
                    {"stage_id": "opt-1", "kind": "OPT", "order": 0, "state": "RUNNING", "attempt_id": "attempt-opt"},
                    {"stage_id": "freq-1", "kind": "FREQ", "order": 1, "state": "BLOCKED_BY_DEPENDENCY"},
                ],
            }
        ],
    }
    sync_reaction(reaction, state_dir)
    completed = transition_step(
        workflow_id="workflow-1", step_id="opt-1", new_state="COMPLETE",
        state_dir=state_dir, expected_states={"RUNNING"}, attempt_id="attempt-opt", attempt_no=1,
        output_artifacts={"geometry_hash": "xyz-hash"},
    )
    assert completed["ok"] is True
    workflow = get_workflow("workflow-1", state_dir)
    assert [step["status"] for step in workflow["steps"]] == ["COMPLETE", "READY"]
    assert any(event["event"] == "DEPENDENCY_RELEASED" for event in workflow["events"])

    late = transition_step(
        workflow_id="workflow-1", step_id="opt-1", new_state="RUNNING",
        state_dir=state_dir, expected_states={"COMPLETE"}, attempt_id="late-attempt", attempt_no=2,
    )
    assert late["ok"] is False
    assert late["error"] == "TERMINAL_STEP_PROTECTED"


def test_result_store_rejects_traversal_symlink_and_zip_bomb_budget(tmp_path, monkeypatch):
    source = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("../../outside.txt", "bad")
    store = ResultArtifactStore(str(tmp_path / "results"))
    with pytest.raises(ValueError):
        store.store("job-1", "owner-a", str(source))

    source.unlink()
    info = zipfile.ZipInfo("link")
    info.create_system = 3
    info.external_attr = (0o120777 << 16)
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(info, "target")
    with pytest.raises(ValueError):
        store.store("job-2", "owner-a", str(source))

    source.unlink()
    import orca_orchestrator.result_store as result_store
    monkeypatch.setattr(result_store, "MAX_ARCHIVE_EXTRACTED_BYTES", 4)
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("large.out", "12345")
    with pytest.raises(ValueError):
        store.store("job-3", "owner-a", str(source))


def test_generated_kaggle_source_never_embeds_long_lived_key(tmp_path, monkeypatch):
    import kaggle_runner

    monkeypatch.setenv("ORCA_ALLOW_REMOTE_CREDENTIAL_EMBEDDING", "1")
    job_dir = kaggle_runner.build_job_dir(
        kaggle_username="tester",
        kaggle_key="secret-token-" + "9" * 32,
        job_base_id="chem-tools-secret-check-1a2b3c4d",
        input_filename="molecule.inp",
        files_payload={"molecule.inp": "IyB0ZXN0"},
        dataset_sources=["tester/orca"],
    )
    try:
        source = pathlib.Path(job_dir, "script.py").read_text(encoding="utf-8")
        assert "secret-token-" + "9" * 32 not in source
        assert "KAGGLE_KEY = None" in source
        assert "KAGGLE_API_TOKEN = None" in source
    finally:
        import shutil
        shutil.rmtree(job_dir, ignore_errors=True)


def test_modern_orchestrator_header_never_embeds_credentials(monkeypatch):
    from orca_orchestrator.credentials import KaggleCredentials
    from orca_orchestrator.models import JobManifest
    from orca_orchestrator.runner.builder import build_header

    monkeypatch.setenv("ORCA_ALLOW_REMOTE_CREDENTIAL_EMBEDDING", "1")
    job = JobManifest.create(
        job_id="chem-tools-modern-secret-1a2b3c4d", owner="tester", title="secret",
        input_filename="molecule.inp", original_input_sha256="hash", job_kind="opt",
    )
    header = build_header(
        job=job, epoch=0, creds=KaggleCredentials(username="tester", key="a" * 32),
        inline_files={"molecule.inp": b"! HF\n* xyz 0 1\n*"},
    )
    assert header["kaggle_key"] is None
    assert header["kaggle_api_token"] is None


def test_server_local_api_is_durable_and_idempotent(tmp_path, monkeypatch):
    import app as webapp

    state_dir = tmp_path / "state"
    monkeypatch.setenv("CHEMISTRY_LAB_STATE_DIR", str(state_dir))
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as client:
        first = client.post(
            "/api/v1/local-orca/execute",
            headers={"Idempotency-Key": "api-local-1"},
            json={"input_text": "! HF\n* xyz 0 1\n*", "stage_kind": "SP"},
        )
        replay = client.post(
            "/api/v1/local-orca/execute",
            headers={"Idempotency-Key": "api-local-1"},
            json={"input_text": "! HF\n* xyz 0 1\n*", "stage_kind": "SP"},
        )
        assert first.status_code == 202
        assert replay.status_code == 202
        assert replay.get_json()["replayed"] is True
        job_id = first.get_json()["job_id"]
        listed = client.get("/api/v1/local-orca/jobs")
        assert listed.status_code == 200
        assert any(job["job_id"] == job_id for job in listed.get_json()["jobs"])


def test_local_worker_projection_releases_next_reaction_stage(tmp_path):
    from tests.test_local_orca_backend import opt_fixture

    state_dir = str(tmp_path / "state")
    workflow_id = "workflow-bridge"
    opt = new_stage("OPT", "Optimization", order=0, backend="server_local")
    opt["state"] = "RUNNING"
    opt["input_text"] = "! Opt\n* xyz 0 1\n*"
    freq = new_stage("FREQ", "Frequency", order=1, backend="server_local")
    freq["state"] = "BLOCKED_BY_DEPENDENCY"
    freq["stage_options"] = {"calc_type": "freq", "theory": "B3LYP", "basis": "def2-SVP", "coords": "H 0 0 0\nH 0 0 0.74"}
    reaction = {
        "reaction_id": "reaction-bridge", "owner": "owner-a", "state": "RUNNING",
        "equation": "H2 -> H2", "species": [{
            "species_id": "species-bridge", "workflow_id": workflow_id, "display_name": "H2",
            "state": "RUNNING", "stages": [opt, freq], "initial_geometry": "H 0 0 0\nH 0 0 0.74",
        }],
    }
    ReactionStore(state_dir).save_reaction(reaction)
    result = apply_local_worker_result(
        {"owner_id": "owner-a", "workflow_id": workflow_id, "step_id": opt["stage_id"]},
        {"ok": True, "output_text": opt_fixture(-76.4, coords_variant=1), "exit_code": 0},
        state_dir,
    )
    assert result["ok"] is True
    saved = ReactionStore(state_dir).get_reaction("owner-a", "reaction-bridge")
    assert saved["species"][0]["stages"][0]["state"] == "COMPLETE"
    assert saved["species"][0]["stages"][1]["state"] == "QUEUED"


def test_completed_stage_replay_repairs_successor_dispatch_after_projection_crash(tmp_path, monkeypatch):
    """A crash after stage persistence must not rerun the completed stage."""
    from tests.test_local_orca_backend import opt_fixture
    import services.local_orca_worker as local_worker

    state_dir = str(tmp_path / "state")
    workflow_id = "workflow-replay"
    opt = new_stage("OPT", "Optimization", order=0, backend="server_local")
    opt["state"] = "RUNNING"
    freq = new_stage("FREQ", "Frequency", order=1, backend="server_local")
    freq["state"] = "BLOCKED_BY_DEPENDENCY"
    freq["stage_options"] = {"calc_type": "freq", "theory": "B3LYP", "basis": "def2-SVP", "coords": "H 0 0 0\nH 0 0 0.74"}
    reaction = {
        "reaction_id": "reaction-replay", "owner": "owner-a", "state": "RUNNING",
        "equation": "H2 -> H2", "species": [{
            "species_id": "species-replay", "workflow_id": workflow_id, "display_name": "H2",
            "state": "RUNNING", "stages": [opt, freq], "initial_geometry": "H 0 0 0\nH 0 0 0.74",
        }],
    }
    ReactionStore(state_dir).save_reaction(reaction)

    original_enqueue = local_worker.enqueue_local_job
    calls = {"count": 0}

    def fail_once(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return {"ok": False, "error": "simulated projection crash"}
        return original_enqueue(*args, **kwargs)

    monkeypatch.setattr(local_worker, "enqueue_local_job", fail_once)
    job = {"owner_id": "owner-a", "workflow_id": workflow_id, "step_id": opt["stage_id"]}
    with pytest.raises(Exception):
        apply_local_worker_result(job, {"ok": True, "output_text": opt_fixture(-76.4, coords_variant=1)}, state_dir)
    saved_after_crash = ReactionStore(state_dir).get_reaction("owner-a", "reaction-replay")
    assert saved_after_crash["species"][0]["stages"][0]["state"] == "COMPLETE"
    assert saved_after_crash["species"][0]["stages"][1]["state"] == "READY"

    replay = apply_local_worker_result(job, {"ok": True}, state_dir)
    assert replay["ok"] is True
    saved_after_replay = ReactionStore(state_dir).get_reaction("owner-a", "reaction-replay")
    assert saved_after_replay["species"][0]["stages"][0]["state"] == "COMPLETE"
    assert saved_after_replay["species"][0]["stages"][1]["state"] == "QUEUED"


def test_workflow_step_retry_increments_attempt_and_preserves_history(tmp_path):
    """F-011 and F-015: Retrying a failed step increments monotonic attempt_no and preserves history."""
    import sqlite3
    state_dir = str(tmp_path / "state")
    workflow_id = "wf-retry-test"
    step_id = "step-1"

    # 1. Initialize workflow with a step in FAILED state
    reaction = {
        "reaction_id": "rxn-retry", "owner": "owner-u", "state": "RUNNING",
        "equation": "H2 -> H2",
        "species": [{
            "species_id": "sp-1", "workflow_id": workflow_id, "state": "FAILED",
            "stages": [{
                "stage_id": step_id, "kind": "OPT", "label": "Opt", "order": 0,
                "state": "FAILED", "error": "Geometry divergence", "retry_count": 0, "attempt_no": 1,
                "attempt_id": "attempt-orig",
            }],
        }],
    }
    sync_reaction(reaction, state_dir)

    # 2. Retry step
    ret1 = retry_step(workflow_id=workflow_id, step_id=step_id, state_dir=state_dir)
    assert ret1["ok"] is True
    assert ret1["attempt_no"] == 2
    assert ret1["new_state"] == "READY"

    wf = get_workflow(workflow_id, state_dir)
    assert wf["steps"][0]["status"] == "READY"
    assert wf["steps"][0]["retry_count"] == 1

    # 3. Simulate failure of attempt 2 and retry again -> attempt 3
    transition_step(workflow_id=workflow_id, step_id=step_id, new_state="FAILED", state_dir=state_dir, failure_reason="SCF failure")
    ret2 = retry_step(workflow_id=workflow_id, step_id=step_id, state_dir=state_dir)
    assert ret2["ok"] is True
    assert ret2["attempt_no"] == 3

    # 4. Verify all attempts are preserved in database without collision
    from services.workflow_store import database_path
    conn = sqlite3.connect(database_path(state_dir))
    attempts = conn.execute("SELECT attempt_no, status FROM workflow_attempts WHERE workflow_id=? AND step_id=? ORDER BY attempt_no", (workflow_id, step_id)).fetchall()
    conn.close()
    assert len(attempts) >= 3
    assert attempts[0][0] == 1
    assert attempts[1][0] == 2
    assert attempts[2][0] == 3


def test_reaction_stage_retry_and_generation_options(tmp_path):
    """F-014 and F-015: Complete stage options, attempt metadata, and reaction-level retry."""
    state_dir = str(tmp_path / "state")
    store = ReactionStore(state_dir)
    reaction = {
        "reaction_id": "rxn-opts", "owner": "owner-u", "state": "DRAFT",
        "equation": "H2 -> H2",
        "species": [{"species_id": "sp-h2", "formula": "H2", "initial_geometry": "H 0 0 0\nH 0 0 0.74"}],
    }
    wf_cfg = {
        "method": "PBE", "basis": "def2-TZVP", "backend": "local",
        "stages": [
            {"kind": "OPT", "label": "Opt", "order": 0},
            {"kind": "OPTTS", "label": "OptTS", "order": 1},
            {"kind": "FREQ", "label": "Freq", "order": 2},
        ],
    }
    configured = generate_unified_reaction_inputs(reaction, wf_cfg, store=store)
    stages = configured["species"][0]["stages"]
    assert len(stages) == 3

    # All stages have stage_options with explicit calc_type
    assert stages[0]["stage_options"]["calc_type"] == "opt"
    assert stages[1]["stage_options"]["calc_type"] == "optts"
    assert stages[2]["stage_options"]["calc_type"] == "freq"
    for st in stages:
        assert st["stage_options"]["theory"] == "PBE"
        assert st["stage_options"]["basis"] == "def2-TZVP"
        assert st["attempt_no"] == 1
        assert st["attempt_id"] is not None

    # Test retry_stage
    stages[0]["state"] = "FAILED"
    stages[0]["error"] = "Walltime exceeded"
    store.save_reaction(configured)

    ret = retry_stage(configured, "sp-h2", stages[0]["stage_id"], store=store, state_dir=state_dir)
    assert ret["state"] == "READY"
    assert ret["attempt_no"] == 2
    assert ret["retry_count"] == 1
    assert ret["error"] is None


def test_reaction_successor_dispatch_supports_local_backend(tmp_path):
    """F-002: Successor stages configured with backend='local' are durably enqueued."""
    from tests.test_local_orca_backend import opt_fixture
    state_dir = str(tmp_path / "state")
    workflow_id = "wf-local-backend"
    opt = new_stage("OPT", "Optimization", order=0, backend="local")
    opt["state"] = "RUNNING"
    opt["input_text"] = "! Opt\n* xyz 0 1\n*"
    freq = new_stage("FREQ", "Frequency", order=1, backend="local")
    freq["state"] = "BLOCKED_BY_DEPENDENCY"
    freq["stage_options"] = {"calc_type": "freq", "theory": "B3LYP", "basis": "def2-SVP", "coords": "H 0 0 0\nH 0 0 0.74"}
    reaction = {
        "reaction_id": "rxn-local-succ", "owner": "owner-b", "state": "RUNNING",
        "equation": "H2 -> H2", "species": [{
            "species_id": "sp-local", "workflow_id": workflow_id, "display_name": "H2",
            "state": "RUNNING", "stages": [opt, freq], "initial_geometry": "H 0 0 0\nH 0 0 0.74",
        }],
    }
    ReactionStore(state_dir).save_reaction(reaction)
    result = apply_local_worker_result(
        {"owner_id": "owner-b", "workflow_id": workflow_id, "step_id": opt["stage_id"]},
        {"ok": True, "output_text": opt_fixture(-76.4, coords_variant=1), "exit_code": 0},
        state_dir,
    )
    assert result["ok"] is True
    saved = ReactionStore(state_dir).get_reaction("owner-b", "rxn-local-succ")
    assert saved["species"][0]["stages"][0]["state"] == "COMPLETE"
    assert saved["species"][0]["stages"][1]["state"] == "QUEUED"


def test_job_store_rejects_missing_and_expired_fences(tmp_path):
    from orca_orchestrator.config import StoreConfig
    from orca_orchestrator.models import JobManifest
    from orca_orchestrator.store import JobStore

    store = JobStore(StoreConfig(state_dir=str(tmp_path / "store")))
    job = JobManifest.create(
        job_id="chem-tools-fence-1a2b3c4d", owner="owner", title="fence",
        input_filename="mol.inp", original_input_sha256="hash",
    )
    store.put_job(job)
    lease = store.acquire_lease(f"job:{job.job_id}", "worker-a", ttl_seconds=30)
    assert lease is not None
    store.release_lease(lease)
    fresh = store.require_job(job.job_id)
    with pytest.raises(LeaseLostError):
        store.put_job(fresh, expected_version=fresh._extra["_version"], fence=lease.fence)

    expired = store.acquire_lease(f"job:{job.job_id}", "worker-a", ttl_seconds=-1)
    assert expired is not None
    fresh = store.require_job(job.job_id)
    with pytest.raises(LeaseLostError):
        store.put_job(fresh, expected_version=fresh._extra["_version"], fence=expired.fence)
    store.close()


def test_kaggle_tombstone_prevents_eventual_consistency_resurrection(tmp_path):
    from orca_orchestrator.config import StoreConfig
    from orca_orchestrator.models import Event, JobManifest
    from orca_orchestrator.states import JobState, Trigger
    from orca_orchestrator.store import JobStore

    store = JobStore(StoreConfig(state_dir=str(tmp_path / "store")))
    job = JobManifest.create(
        job_id="chem-tools-deleted-1a2b3c4d", owner="owner", title="deleted",
        input_filename="mol.inp", original_input_sha256="hash",
    )
    store.put_job(job)
    event = Event.create(
        job_id=job.job_id, epoch=0, trigger=Trigger.SUBMIT,
        from_state=JobState.CREATED, to_state=JobState.UPLOADING,
        actor="test", reason="submitted",
    )
    store.append_event(event)
    store.tombstone_job(job.job_id, job.owner, detail={"state": "REMOTE_DELETE_PENDING"})
    assert store.get_job(job.job_id) is None
    assert store.is_tombstoned(job.job_id, job.owner)
    with pytest.raises(ConcurrencyError):
        store.put_job(job)
    tombstone = store.get_tombstone(job.job_id, job.owner)
    assert tombstone["detail"]["deleted_manifest"]["job_id"] == job.job_id
    assert tombstone["detail"]["deleted_events"][0]["event_id"] == event.event_id
    store.close()


def test_watchdog_reconciles_all_active_jobs_immediately_on_startup(tmp_path):
    from orca_orchestrator.config import StoreConfig
    from orca_orchestrator.credentials import CredentialBroker
    from orca_orchestrator.models import JobManifest
    from orca_orchestrator.states import JobState
    from orca_orchestrator.store import JobStore
    from orca_orchestrator.watchdog import Watchdog

    store = JobStore(StoreConfig(state_dir=str(tmp_path / "store")))
    job = JobManifest.create(
        job_id="chem-tools-startup-1a2b3c4d", owner="owner", title="startup",
        input_filename="mol.inp", original_input_sha256="hash", state=JobState.RUNNING,
    )
    store.put_job(job)
    broker = CredentialBroker()
    broker.remember(KaggleCredentials(username="owner", key="a" * 32))
    seen = []

    class FakeReconciler:
        def reconcile(self, job_id, creds, actor="system"):
            seen.append((job_id, creds.username, actor))
            return store.require_job(job_id)

    watchdog = Watchdog(store, reconciler=FakeReconciler(), broker=broker)
    report = watchdog.reconcile_all_active()
    assert report.examined == 1
    assert report.recovered == 1
    assert seen == [(job.job_id, "owner", "startup")]
    store.close()


def test_finished_result_collection_retries_delayed_kaggle_output(tmp_path):
    from orca_orchestrator.config import StoreConfig
    from orca_orchestrator.credentials import CredentialBroker
    from orca_orchestrator.models import JobManifest
    from orca_orchestrator.states import JobState
    from orca_orchestrator.store import JobStore
    from orca_orchestrator.watchdog import Watchdog

    store = JobStore(StoreConfig(state_dir=str(tmp_path / "store")))
    job = JobManifest.create(
        job_id="chem-tools-result-1a2b3c4d", owner="owner", title="result",
        input_filename="mol.inp", original_input_sha256="hash", state=JobState.FINISHED,
    )
    store.put_job(job)
    broker = CredentialBroker()
    broker.remember(KaggleCredentials(username="owner", key="a" * 32))
    attempts = []

    def delayed_callback(_creds, job_id):
        attempts.append(job_id)
        return None if len(attempts) == 1 else object()

    watchdog = Watchdog(
        store, broker=broker, result_callback=delayed_callback
    )
    first = watchdog.collect_finished_results()
    second = watchdog.collect_finished_results()
    assert first == {
        "examined": 1, "archived": 0, "deferred": 1,
        "skipped_no_credentials": 0,
    }
    assert second["archived"] == 1
    assert attempts == [job.job_id, job.job_id]
    store.close()


def test_local_agent_restart_never_requeues_an_ambiguous_running_process(tmp_path):
    from services import local_agent_service as agent

    state_dir = str(tmp_path / "state")
    token = "CLA_first-proof"
    first = agent.init_runtime_session(
        installation_id="inst-safe-restart", agent_session_id="sess-first",
        token_verifiers=[agent.hash_token_verifier(token)], state_dir=state_dir,
        installation_secret="installation-proof",
    )
    assert first["ok"]
    assert agent.claim_runtime_token(token, "owner", state_dir=state_dir)["ok"]
    finalized = agent.finalize_agent_runtime("sess-first", token, state_dir=state_dir)
    secret = finalized["runtime_session_secret"]
    queued = agent.enqueue_agent_job(
        "sess-first", "owner", "! HF\n* xyz 0 1\n*", state_dir=state_dir
    )
    assert agent.poll_next_agent_job("sess-first", secret, state_dir=state_dir)["job"]["job_id"] == queued["job_id"]

    restarted = agent.init_runtime_session(
        installation_id="inst-safe-restart", agent_session_id="sess-second",
        token_verifiers=[agent.hash_token_verifier("CLA_second-proof")], state_dir=state_dir,
        installation_secret="installation-proof",
    )
    assert restarted["ok"]
    persisted = agent.get_agent_job_status(queued["job_id"], owner_id="owner", state_dir=state_dir)
    assert persisted["status"] == "RECOVERY_REQUIRED"
    assert persisted["agent_session_id"] == "sess-first"


def test_local_agent_recovery_rebinds_only_same_installation_and_owner(tmp_path):
    from services import local_agent_service as agent

    state_dir = str(tmp_path / "state")
    first_token = "CLA_recovery_first"
    agent.init_runtime_session(
        installation_id="inst-recovery", agent_session_id="session-one",
        token_verifiers=[agent.hash_token_verifier(first_token)], state_dir=state_dir,
        installation_secret="installation-proof",
    )
    agent.claim_runtime_token(first_token, "owner-a", state_dir=state_dir)
    first_secret = agent.finalize_agent_runtime("session-one", first_token, state_dir=state_dir)["runtime_session_secret"]
    queued = agent.enqueue_agent_job(
        "session-one", "owner-a", "! HF\n* xyz 0 1\n*", state_dir=state_dir
    )
    agent.poll_next_agent_job("session-one", first_secret, state_dir=state_dir)

    second_token = "CLA_recovery_second"
    agent.init_runtime_session(
        installation_id="inst-recovery", agent_session_id="session-two",
        token_verifiers=[agent.hash_token_verifier(second_token)], state_dir=state_dir,
        installation_secret="installation-proof",
    )
    agent.claim_runtime_token(second_token, "owner-a", state_dir=state_dir)
    second_secret = agent.finalize_agent_runtime("session-two", second_token, state_dir=state_dir)["runtime_session_secret"]

    recovered = agent.register_agent_job_process(
        queued["job_id"], "session-two", second_secret,
        pid=12345, process_start_time=123.5,
        command_fingerprint="f" * 64, workspace=str(tmp_path / "run"),
        recovering=True, state_dir=state_dir,
    )
    assert recovered == {"ok": True, "status": "RUNNING", "recovered": True}
    status = agent.get_agent_job_status(queued["job_id"], "owner-a", state_dir=state_dir)
    assert status["status"] == "RUNNING"
    assert status["agent_session_id"] == "session-two"

    replay = agent.register_agent_job_process(
        queued["job_id"], "session-two", second_secret,
        pid=12345, process_start_time=123.5,
        command_fingerprint="f" * 64, workspace=str(tmp_path / "run"),
        recovering=True, state_dir=state_dir,
    )
    assert replay["ok"] is False
    assert replay["error"] == "JOB_NOT_RECOVERABLE"


def test_local_agent_process_survives_companion_restart_and_is_recovered(tmp_path, monkeypatch):
    import json
    import sys
    import threading
    import time

    from local_agent.agent import LocalCompanionAgent
    from local_agent.config import AgentConfig

    calls = []

    class Response:
        status_code = 200

        def __init__(self, payload=None):
            self._payload = payload or {"ok": True, "cancel_requested": False}

        def json(self):
            return self._payload

    def fake_post(url, **kwargs):
        calls.append((url, kwargs.get("json") or {}))
        return Response()

    monkeypatch.setattr("local_agent.agent.requests.post", fake_post)
    cfg = AgentConfig(
        server_url="http://server.invalid",
        data_dir=str(tmp_path / "agent"),
        device_name="test",
        orca_executable=sys.executable,
        orca_working_dir=str(tmp_path / "work"),
        heartbeat_interval_seconds=1,
        max_job_runtime_seconds=30,
    )
    first = LocalCompanionAgent(cfg)
    first.runtime_session_secret = "runtime-one"
    first.agent_session_id = "session-one"
    first.running = True
    job = {
        "job_id": "durable-agent-job",
        "job_name": "fake_orca",
        "input_text": (
            "import time\n"
            "print('ORCA STARTED', flush=True)\n"
            "time.sleep(3)\n"
            "print('ORCA TERMINATED NORMALLY', flush=True)\n"
        ),
    }
    thread = threading.Thread(target=first._run_job, args=(job,), daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline and not first.process_registry_path.is_file():
        time.sleep(0.05)
    assert first.process_registry_path.is_file()
    record = json.loads(first.process_registry_path.read_text(encoding="utf-8"))
    first._stop_event.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    import psutil
    assert psutil.pid_exists(record["pid"]), "fake ORCA must outlive the first companion"
    first.process_lock.release()

    second = LocalCompanionAgent(cfg)
    second.runtime_session_secret = "runtime-two"
    second.agent_session_id = "session-two"
    second.running = True
    try:
        assert second._recover_registered_process() is True
        assert not second.process_registry_path.exists()
        assert not second._completion_path(job["job_id"]).exists()
        assert any(payload.get("recovering") is True for _, payload in calls)
        completion_payloads = [payload for url, payload in calls if url.endswith("/complete")]
        assert completion_payloads
        assert completion_payloads[-1]["exit_code"] == 0
        assert "ORCA TERMINATED NORMALLY" in completion_payloads[-1]["output_text"]
    finally:
        second.process_lock.release()


def test_local_agent_handshake_uses_only_configured_safe_server(tmp_path, monkeypatch):
    from local_agent.agent import LocalCompanionAgent, _is_safe_server_url
    from local_agent.config import AgentConfig

    assert _is_safe_server_url("http://localhost:9876") is True
    assert _is_safe_server_url("https://example.invalid/lab") is True
    assert _is_safe_server_url("http://example.invalid/lab") is False
    assert _is_safe_server_url("https://user:pass@example.invalid/lab") is False

    seen = []

    class Response:
        status_code = 503

        def json(self):
            return {"ok": False}

    def fake_post(url, **kwargs):
        seen.append(url)
        return Response()

    monkeypatch.setattr("local_agent.agent.requests.post", fake_post)
    cfg = AgentConfig(
        server_url="http://localhost:9876",
        data_dir=str(tmp_path / "agent"),
        device_name="safe-handshake",
        orca_executable="orca",
    )
    agent = LocalCompanionAgent(cfg)
    try:
        assert agent.init_server_session() is False
        assert seen == ["http://localhost:9876/api/v1/local-agent/runtime/init"]
    finally:
        agent.process_lock.release()


def test_installation_credentials_fail_closed_on_corruption(tmp_path):
    from local_agent.security import load_or_create_installation_credentials

    data_dir = tmp_path / "agent"
    first = load_or_create_installation_credentials(data_dir)
    assert first == load_or_create_installation_credentials(data_dir)
    (data_dir / "installation_id.json").write_text("{truncated", encoding="utf-8")
    with pytest.raises(RuntimeError, match="corrupt"):
        load_or_create_installation_credentials(data_dir)


def test_local_agent_schema_upgrade_creates_verified_pre_migration_backup(tmp_path):
    from services.local_agent_service import _init_db

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    database = state_dir / "local_agent_registry.db"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE agent_installations ("
            "installation_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, platform TEXT NOT NULL, "
            "backend_kind TEXT NOT NULL, scheduler_type TEXT, created_at REAL NOT NULL, last_seen REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO agent_installations VALUES "
            "('legacy-install', 'Legacy', 'Windows', 'local', NULL, 1.0, 1.0)"
        )
        conn.commit()

    _init_db(str(database))

    backups = list((state_dir / "migration_backups").glob("*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        old_columns = {row[1] for row in backup.execute("PRAGMA table_info(agent_installations)")}
        assert "installation_secret_hash" not in old_columns
    with sqlite3.connect(database) as upgraded:
        columns = {row[1] for row in upgraded.execute("PRAGMA table_info(agent_installations)")}
        assert "installation_secret_hash" in columns
        assert upgraded.execute("SELECT version FROM agent_schema").fetchone()[0] == 2
        assert upgraded.execute(
            "SELECT display_name FROM agent_installations WHERE installation_id='legacy-install'"
        ).fetchone()[0] == "Legacy"

    # Idempotent startup: a current schema must not create another backup.
    _init_db(str(database))
    assert list((state_dir / "migration_backups").glob("*.bak")) == backups


def test_sqlite_migration_refuses_corrupt_database(tmp_path):
    from services.sqlite_migrations import CorruptDatabaseError, backup_before_schema_upgrade

    database = tmp_path / "corrupt.sqlite3"
    database.write_bytes(b"not-a-sqlite-database")
    with pytest.raises(CorruptDatabaseError):
        backup_before_schema_upgrade(
            str(database), component="test", target_version=2,
            required_schema={"jobs": ("job_id",)},
        )


def test_retry_stage_legacy_two_argument_signature_uses_real_step_id(tmp_path):
    state_dir = str(tmp_path / "state")
    store = ReactionStore(state_dir)
    stage = new_stage("SP", "SP", order=0, backend="local")
    stage.update({"state": "FAILED", "attempt_no": 1, "retry_count": 0})
    reaction = {
        "reaction_id": "rxn-old-retry", "owner": "owner", "state": "WAITING",
        "species": [{"species_id": "sp", "workflow_id": "wf-old-retry", "state": "FAILED", "stages": [stage]}],
    }
    store.save_reaction(reaction)
    retried = retry_stage(reaction, stage["stage_id"], store=store, state_dir=state_dir)
    assert retried["state"] == "READY"
    assert retried["attempt_no"] == 2
