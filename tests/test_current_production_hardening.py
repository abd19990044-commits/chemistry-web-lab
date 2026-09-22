from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def test_container_entrypoint_stops_survivor_before_waiting():
    """A dead child must make the container exit instead of hanging in wait."""
    entrypoint = (Path(__file__).parents[1] / "docker-entrypoint.sh").read_text(
        encoding="utf-8"
    )
    after_loop = entrypoint.split("done", 1)[1]
    assert after_loop.index("stop_children") < after_loop.index('wait "$web_pid"')


def test_unified_solvation_normalizes_combined_and_gas_values():
    from services.reaction_workflow_service import normalize_solvation

    assert normalize_solvation("CPCM", "CPCM(Water)") == ("cpcm", "Water")
    assert normalize_solvation("none", "gas") == ("none", "Water")
    assert normalize_solvation("SMD", "DMSO") == ("smd", "DMSO")


def test_unified_dispatch_submits_kaggle_stage_with_stable_identity(tmp_path, monkeypatch):
    import orca_orchestrator.credential_vault as vault
    import orca_orchestrator.service as orchestrator_service
    from services.reaction_workflow_service import ReactionStore, dispatch_ready_stages, new_stage

    class FakeVault:
        def load_credentials(self, owner):
            assert owner == "alice"
            return SimpleNamespace(username="alice", key="secret", api_token="")

    calls = []

    class FakeService:
        def submit(self, creds, **kwargs):
            calls.append((creds.username, kwargs))
            return SimpleNamespace(job_id="job-123", slug="job-123", url="https://kaggle/job-123")

    monkeypatch.setattr(vault, "get_vault_manager", lambda: FakeVault())
    monkeypatch.setattr(orchestrator_service, "get_service", lambda: FakeService())

    stage = new_stage("OPT", "Optimization", order=0, backend="kaggle")
    stage["state"] = "READY"
    stage["input_text"] = "! HF SP\n* xyz 0 1\nH 0 0 0\n*"
    stage["stage_options"] = {"dataset_sources": "alice/orca-6", "orca_link": ""}
    reaction = {"reaction_id": "rxn-1", "owner": "alice", "state": "READY", "species": [{
        "species_id": "sp-1", "workflow_id": "wf-1", "display_name": "H",
        "stages": [stage],
    }]}
    store = ReactionStore(str(tmp_path))

    result = dispatch_ready_stages(reaction, owner_id="alice", state_dir=str(tmp_path), store=store)

    assert result["dispatched_stage_ids"] == [stage["stage_id"]]
    assert stage["state"] == "SUBMITTED"
    assert stage["kaggle_job_id"] == "job-123"
    assert calls[0][1]["idempotency_key"].startswith("reaction:wf-1:step:")


def test_reaction_read_model_rebuilds_from_sqlite_snapshot(tmp_path):
    from services.reaction_workflow_service import ReactionStore, new_stage

    stage = new_stage("OPT", "Optimization", order=0, backend="server_local")
    reaction = {"reaction_id": "rxn-rebuild", "owner": "alice", "state": "READY", "species": [{
        "species_id": "sp-1", "workflow_id": "wf-rebuild", "display_name": "H",
        "stages": [stage],
    }]}
    store = ReactionStore(str(tmp_path))
    store.save_reaction(reaction)
    Path(store.reactions_path).unlink()

    restored = ReactionStore(str(tmp_path)).get_reaction("alice", "rxn-rebuild")

    assert restored is not None
    assert restored["species"][0]["workflow_id"] == "wf-rebuild"


def test_frontend_unified_contract_uses_backend_field_names():
    root = Path(__file__).resolve().parents[1]
    js = (root / "static" / "js" / "reaction-unified.js").read_text(encoding="utf-8")
    html = (root / "templates" / "index.html").read_text(encoding="utf-8")

    assert "unified-setup-target-host" in js
    assert "unified-setup-max-concurrency" in js
    assert "unified-setup-dispersion" in js
    assert 'value="kaggle"' in html
    assert 'value="server_local" selected' in html


def test_container_creates_worker_state_directory_before_dropping_privileges():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    assert "mkdir -p /data" in dockerfile
    assert "chown -R appuser:appuser /app /data" in dockerfile
    assert "docker-entrypoint.sh" in dockerfile


def test_kaggle_reconciliation_preserves_remote_unknown(tmp_path, monkeypatch):
    import orca_orchestrator.credential_vault as vault
    import orca_orchestrator.service as orchestrator_service
    from services.reaction_workflow_service import ReactionStore, new_stage, reconcile_kaggle_stages

    class FakeVault:
        def load_credentials(self, owner):
            return SimpleNamespace(username="alice", key="secret", api_token="")

    class FakeService:
        def status(self, creds, job_id):
            return {"remote_status_unknown": True, "remote_state": "UNKNOWN"}

    monkeypatch.setattr(vault, "get_vault_manager", lambda: FakeVault())
    monkeypatch.setattr(orchestrator_service, "get_service", lambda: FakeService())

    stage = new_stage("OPT", "Optimization", order=0, backend="kaggle")
    stage.update({"state": "SUBMITTED", "kaggle_job_id": "job-unknown"})
    reaction = {"reaction_id": "rxn-unknown", "owner": "alice", "state": "RUNNING", "species": [{
        "species_id": "sp-1", "workflow_id": "wf-1", "display_name": "H", "stages": [stage]
    }]}
    store = ReactionStore(str(tmp_path))
    result = reconcile_kaggle_stages(reaction, owner_id="alice", state_dir=str(tmp_path), store=store)

    assert result["ok"] is True
    assert stage["state"] == "SUBMITTED"
    assert stage["waiting_reason"] == "KAGGLE_STATUS_UNKNOWN"


def test_kaggle_runner_missing_secret_does_not_abort_current_orca_window(tmp_path, monkeypatch):
    """A missing User Secret must not kill the notebook before ORCA starts."""
    import sys

    from orca_orchestrator.runner import kernel_runner as runner

    class MissingSecrets:
        def get_secret(self, _label):
            raise ConnectionError("HTTP Error 400: Bad Request")

    monkeypatch.setitem(
        sys.modules,
        "kaggle_secrets",
        SimpleNamespace(UserSecretsClient=MissingSecrets),
    )
    monkeypatch.setattr(
        runner,
        "H",
        dict(runner.H, kaggle_username="tester", kaggle_key=None, kaggle_api_token=None),
    )
    monkeypatch.delenv("KAGGLE_KEY", raising=False)
    monkeypatch.delenv("KAGGLE_API_TOKEN", raising=False)
    events = []
    monkeypatch.setattr(
        runner, "emit", lambda event, message="", **fields: events.append((event, message, fields))
    )

    assert runner.install_credentials(str(tmp_path / "kaggle")) is False
    assert runner.CONTINUATION_CREDENTIALS_AVAILABLE is False
    assert any(event[0] == "continuation_credentials_unavailable" for event in events)


def test_kaggle_runner_probes_legacy_key_after_missing_api_token(tmp_path, monkeypatch):
    """HTTP 400 for API_TOKEN must not prevent a valid legacy key fallback."""
    import sys

    from orca_orchestrator.runner import kernel_runner as runner

    class MixedSecrets:
        def get_secret(self, label):
            if label in ("KAGGLE_API_TOKEN", "KAGGLE_TOKEN"):
                raise ConnectionError("HTTP Error 400: Bad Request")
            return {"KAGGLE_KEY": "k" * 32, "KAGGLE_USERNAME": "tester"}.get(label)

    monkeypatch.setitem(
        sys.modules,
        "kaggle_secrets",
        SimpleNamespace(UserSecretsClient=MixedSecrets),
    )
    monkeypatch.setattr(
        runner,
        "H",
        dict(runner.H, kaggle_username="", kaggle_key=None, kaggle_api_token=None),
    )
    monkeypatch.delenv("KAGGLE_KEY", raising=False)
    monkeypatch.delenv("KAGGLE_API_TOKEN", raising=False)
    monkeypatch.setattr(runner, "emit", lambda *args, **kwargs: None)

    assert runner.install_credentials(str(tmp_path / "kaggle")) is True
    assert (tmp_path / "kaggle" / "kaggle.json").exists()


def test_kaggle_runner_executes_current_orca_window_without_secret(
        tmp_path, monkeypatch):
    """The missing-secret case must reach the ORCA execution layer."""
    import importlib
    import sys

    from tests.test_repeated_execution_regression import setup_job, read_state, FakeExecution
    from orca_orchestrator.runner import kernel_runner as runner

    class MissingSecrets:
        def get_secret(self, _label):
            raise ConnectionError("HTTP Error 400: Bad Request")

    monkeypatch.setitem(
        sys.modules,
        "kaggle_secrets",
        SimpleNamespace(UserSecretsClient=MissingSecrets),
    )
    # Bind every runner lifecycle file to this test.  Reusing the repository's
    # default .kaggle-working directory makes a previous pytest process look
    # like a live duplicate for the heartbeat grace period.
    monkeypatch.setenv("ORCA_RUNNER_OUTPUT_DIR", str(tmp_path / "working"))
    monkeypatch.setenv("ORCA_RUNNER_SCRATCH_ROOT", str(tmp_path / "scratch"))
    runner = importlib.reload(runner)
    real_install = runner.install_credentials
    setup_job(runner, monkeypatch, tmp_path, job_kind="sp",
              inp_text="! HF SP\n* xyz 0 1\nH 0 0 0\n*")
    # setup_job disables credential lookup for the older runner tests; this
    # regression explicitly restores the real implementation.
    runner.install_credentials = real_install
    FakeExecution.script = [("ORCA TERMINATED NORMALLY\n", None)]

    assert runner.main() == 0
    assert len(FakeExecution.instances) == 1
    assert read_state(runner)["job"]["state"] == "FINISHED"


def test_kaggle_runner_fresh_exited_heartbeat_is_reclaimable(tmp_path, monkeypatch):
    """A completed prior run is not a live duplicate, even when very recent."""
    import json
    import time

    from orca_orchestrator.runner import kernel_runner as runner

    heartbeat = tmp_path / "HEARTBEAT.json"
    heartbeat.write_text(
        json.dumps({
            "run_token": "previous-run-token",
            "state": "EXITED",
            "at": time.time(),
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "HEARTBEAT_FILE", str(heartbeat))
    monkeypatch.setattr(runner, "RUN_TOKEN", "new-run-token")
    events = []
    monkeypatch.setattr(
        runner, "emit", lambda event, message="", **fields: events.append(event)
    )

    assert runner.claim_run() is True
    assert "completed_heartbeat_reclaimed" in events
    saved = json.loads(heartbeat.read_text(encoding="utf-8"))
    assert saved["run_token"] == "new-run-token"
