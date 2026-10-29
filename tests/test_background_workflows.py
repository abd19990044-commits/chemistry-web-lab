"""Background execution uses durable state, without browser polling or live Kaggle."""

from __future__ import annotations

import json
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_orchestrator.cloudflare_controller.client import InMemoryCloudflareBackend
from orca_orchestrator.credential_vault import EncryptedCredentialVaultManager
from orca_orchestrator.credentials import CredentialBroker, KaggleCredentials
from services.calculation_workflow_service import register_workflow
from services.reaction_workflow_service import ReactionStore

COORDS = "O 0 0 0\nH 0 0 0.96\nH 0.9 0 -0.3"
OUT = """* O   R   C   A *
Program Version 6.0.0
CARTESIAN COORDINATES (ANGSTROEM)
---------------------------------
O  0.000000  0.000000  0.117215
H  0.000000  0.756950 -0.468860
H  0.000000 -0.756950 -0.468860

*** THE OPTIMIZATION HAS CONVERGED ***
FINAL SINGLE POINT ENERGY -76.432891234567
****ORCA TERMINATED NORMALLY****
"""


def payload(backend="kaggle"):
    return {
        "request_id": "stable-test-chain",
        "name": "water",
        "backend": backend,
        "dataset_sources": ["alice/orca"],
        "stages": [
            {
                "type": kind,
                "config": {
                    "calc_type": kind,
                    "theory": "HF",
                    "basis": "def2-SVP",
                    "coords": COORDS,
                    "charge": 0,
                    "mult": 1,
                    "cores": 1,
                    "ram": 128,
                },
            }
            for kind in ("opt", "sp")
        ],
    }


def test_encrypted_credentials_survive_process_and_cloudflare_loss(tmp_path):
    key = bytes(range(32))
    secret = "test-background-secret-value"
    first = EncryptedCredentialVaultManager(
        client=InMemoryCloudflareBackend(), broker=CredentialBroker(), master_key=key, state_dir=str(tmp_path)
    )
    first.save_credentials("application-owner", KaggleCredentials(username="alice", api_token=secret))
    assert secret.encode() not in Path(first.local_storage.path).read_bytes()
    restarted = EncryptedCredentialVaultManager(
        client=InMemoryCloudflareBackend(), broker=CredentialBroker(), master_key=key, state_dir=str(tmp_path)
    )
    assert restarted.load_credentials("application-owner").api_token == secret
    assert restarted.load_job_credentials("alice").api_token == secret
    assert restarted.load_credentials("other-owner") is None


def test_forgetting_credentials_cannot_restore_an_old_cloud_record(tmp_path):
    class UnavailableDelete(InMemoryCloudflareBackend):
        def delete_credential_vault(self, owner):
            raise ConnectionError("offline")

    backend = UnavailableDelete()
    manager = EncryptedCredentialVaultManager(
        client=backend, broker=CredentialBroker(), master_key=bytes(range(32)), state_dir=str(tmp_path)
    )
    manager.save_credentials("owner", KaggleCredentials(username="alice", api_token="secret"))
    assert manager.delete_credentials("owner")
    assert manager.broker.get("alice") is None
    assert manager.load_credentials("owner") is None
    assert not manager.get_metadata("owner").exists


def test_local_connection_settings_are_encrypted_and_legacy_migrates(tmp_path):
    from services.local_orca_service import get_local_orca_settings, save_local_orca_settings

    secret_path = str(tmp_path / "private-installation" / "orca")
    legacy = tmp_path / "local_orca_settings.json"
    legacy.write_text(json.dumps({"orca_executable": secret_path}))
    assert get_local_orca_settings(str(tmp_path))["orca_executable"] == secret_path
    assert secret_path not in legacy.read_text()
    assert json.loads(legacy.read_text())["encrypted_connection_settings"] == 1
    save_local_orca_settings({"input_directory": str(tmp_path / "inputs")}, str(tmp_path))
    assert get_local_orca_settings(str(tmp_path))["orca_executable"] == secret_path


def test_workflow_registration_is_idempotent_and_credentials_never_enter_snapshot(tmp_path):
    store = ReactionStore(str(tmp_path))
    first = register_workflow("owner", payload(), store)
    second = register_workflow("owner", payload(), ReactionStore(str(tmp_path)))
    assert first["reaction_id"] == second["reaction_id"]
    assert len(store.list_reactions("owner")) == 1
    forged = payload()
    forged["stages"][0]["config"]["api_token"] = "do-not-store-this"
    with pytest.raises(ValueError, match="Credentials"):
        register_workflow("owner", forged, store)
    assert "do-not-store-this" not in Path(store.reactions_path).read_text()


def test_kaggle_chain_completes_after_supervisor_restart_without_browser(tmp_path, monkeypatch):
    import orca_orchestrator.credential_vault as vault
    import orca_orchestrator.service as service_module
    from services.background_workflows import BackgroundWorkflowSupervisor
    from services.calculation_workflow_service import describe_workflow

    creds = KaggleCredentials(username="alice", api_token="secret")
    manager = SimpleNamespace(load_credentials=lambda owner: creds if owner == "owner" else None)
    monkeypatch.setattr(vault, "get_vault_manager", lambda: manager)
    archive = tmp_path / "results.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("stage.out", OUT)
    calls = []
    finished = set()

    def submit(creds, **kwargs):
        calls.append(kwargs)
        number = len(calls)
        return SimpleNamespace(
            job_id=f"chem-tools-stage-{number}",
            slug=f"alice/stage-{number}",
            url=f"https://www.kaggle.com/code/alice/stage-{number}",
        )

    fake_service = SimpleNamespace(
        submit=submit,
        status=lambda creds, job_id: {"remote_state": "COMPLETE" if job_id in finished else "RUNNING"},
        archive_job_results=lambda *args, **kwargs: {"ok": True},
        result_store=SimpleNamespace(retrieve=lambda *args: (str(archive), {})),
        store=SimpleNamespace(
            get_job=lambda job_id: SimpleNamespace(input_filename="stage.inp"), list_active_jobs=lambda **kwargs: []
        ),
        watchdog=SimpleNamespace(collect_finished_results=lambda: {}),
    )
    monkeypatch.setattr(service_module, "get_service", lambda: fake_service)
    store = ReactionStore(str(tmp_path))
    reaction = register_workflow("owner", payload(), store)
    BackgroundWorkflowSupervisor(str(tmp_path)).run_once()
    assert len(calls) == 1
    finished.add("chem-tools-stage-1")
    # All subsequent progress comes from fresh workers and persisted state.
    BackgroundWorkflowSupervisor(str(tmp_path)).run_once()
    assert len(calls) == 2
    assert "0.117215" in calls[1]["input_content"]
    finished.add("chem-tools-stage-2")
    BackgroundWorkflowSupervisor(str(tmp_path)).run_once()
    restored = ReactionStore(str(tmp_path)).get_reaction("owner", reaction["reaction_id"])
    assert restored["state"] == "COMPLETE"
    assert all(job["status"] == "complete" for job in describe_workflow(restored)["jobs"])
    BackgroundWorkflowSupervisor(str(tmp_path)).run_once()
    assert len(calls) == 2


def test_paused_and_cancelled_workflows_never_dispatch(tmp_path, monkeypatch):
    import services.background_workflows as background

    store = ReactionStore(str(tmp_path))
    reaction = register_workflow("owner", payload(), store)
    store.pause_reaction("owner", reaction["reaction_id"])
    monkeypatch.setattr(background, "dispatch_ready_stages", lambda *args, **kwargs: pytest.fail("paused dispatch"))
    supervisor = background.BackgroundWorkflowSupervisor(str(tmp_path))
    supervisor._last_remote_pass = time.monotonic()
    assert supervisor.run_once()["examined"] == 0
    store.cancel_reaction("owner", reaction["reaction_id"])
    assert supervisor.run_once()["examined"] == 0


def test_two_supervisors_do_not_dispatch_concurrently(tmp_path, monkeypatch):
    import services.background_workflows as background

    store = ReactionStore(str(tmp_path))
    register_workflow("owner", payload(), store)
    entered, release = threading.Event(), threading.Event()

    def held_dispatch(*args, **kwargs):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(background, "dispatch_ready_stages", held_dispatch)
    first = background.BackgroundWorkflowSupervisor(str(tmp_path))
    second = background.BackgroundWorkflowSupervisor(str(tmp_path))
    first._last_remote_pass = second._last_remote_pass = time.monotonic()
    thread = threading.Thread(target=first.run_once)
    thread.start()
    try:
        assert entered.wait(5)
        assert second.run_once()["leader"] is False
    finally:
        release.set()
        thread.join(5)


def test_embedded_local_worker_runs_two_stages_without_companion(tmp_path, monkeypatch):
    from services.application_runtime import ApplicationRuntime
    from services.local_orca_service import save_local_orca_settings

    try:
        from tests.test_local_orca_backend import _make_fake_orca_script
    except ImportError:
        from test_local_orca_backend import _make_fake_orca_script

    state = tmp_path / "state"
    state.mkdir()
    inputs, outputs = tmp_path / "inputs", tmp_path / "outputs"
    inputs.mkdir()
    outputs.mkdir()
    fake_orca = _make_fake_orca_script(tmp_path)
    monkeypatch.setenv("FAKE_ORCA_MODE", "VALID_OPT")
    save_local_orca_settings(
        {"orca_executable": str(fake_orca), "input_directory": str(inputs), "output_directory": str(outputs)},
        str(state),
    )
    store = ReactionStore(str(state))
    reaction = register_workflow("owner", payload("server_local"), store)
    runtime = ApplicationRuntime(str(state), embedded_local_worker=True, interval_seconds=1).start()
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            latest = store.get_reaction("owner", reaction["reaction_id"])
            if latest["state"] == "COMPLETE":
                break
            time.sleep(0.1)
        assert latest["state"] == "COMPLETE", latest
        assert runtime.status()["local_worker_running"]
        assert all(stage.get("local_job_id") for stage in latest["species"][0]["stages"])
    finally:
        runtime.stop()


def test_workflow_http_owner_isolation_and_passcode(tmp_path, monkeypatch):
    import app as webapp

    monkeypatch.setenv("CHEMISTRY_LAB_MULTI_USER", "1")
    monkeypatch.setenv("CHEMISTRY_LAB_ADMIN_USERS", "owner-a")
    monkeypatch.setattr(webapp, "_reaction_store", ReactionStore(str(tmp_path)))
    webapp.app.config["TESTING"] = True
    client = webapp.app.test_client()
    assert client.get("/api/v1/calculation-workflows").status_code == 401
    with client.session_transaction() as sess:
        sess["user"] = {"sub": "owner-a"}
    created = client.post("/api/v1/calculation-workflows", json=payload("server_local"))
    assert created.status_code == 202
    ident = created.json["workflow_id"]
    with client.session_transaction() as sess:
        sess["user"] = {"sub": "owner-b"}
    assert client.get(f"/api/v1/calculation-workflows/{ident}").status_code == 404
    assert client.post(f"/api/v1/calculation-workflows/{ident}/cancel", json={}).status_code == 404
    assert client.get("/api/v1/calculation-workflows").json["jobs"] == []
    assert client.post("/api/v1/calculation-workflows", json=payload("server_local")).status_code == 403
    monkeypatch.setenv("KAGGLE_EXECUTION_PASSCODE", "test-access-code")
    assert client.post("/api/v1/calculation-workflows", json=payload()).status_code == 403


def test_invalid_explicit_key_never_falls_back_to_a_new_key_or_caches_credentials(tmp_path, monkeypatch):
    from orca_orchestrator.credential_vault import CredentialVaultError

    monkeypatch.setenv("KAGGLE_CREDENTIALS_ENCRYPTION_KEY", "invalid")
    monkeypatch.setenv("SECRET_KEY", "otherwise-valid-application-secret")
    manager = EncryptedCredentialVaultManager(
        client=InMemoryCloudflareBackend(), broker=CredentialBroker(), state_dir=str(tmp_path), auto_key=True
    )
    assert not manager.is_configured
    with pytest.raises(CredentialVaultError):
        manager.save_credentials("owner", KaggleCredentials(username="alice", api_token="secret"))
    assert manager.broker.get("alice") is None
    assert not (tmp_path / "connection_vault.key").exists()


def test_failed_verification_preserves_prior_encrypted_connection(tmp_path, monkeypatch):
    import kaggle_runner
    from orca_orchestrator.credential_vault import CredentialVerificationError

    manager = EncryptedCredentialVaultManager(
        client=InMemoryCloudflareBackend(),
        broker=CredentialBroker(),
        master_key=bytes(range(32)),
        state_dir=str(tmp_path),
    )
    manager.save_credentials("owner", KaggleCredentials(username="alice", api_token="old-secret"))
    before = manager.local_storage.get("owner").ciphertext

    def unavailable(*args):
        raise TimeoutError("offline")

    monkeypatch.setattr(kaggle_runner, "verify_kaggle_credentials", unavailable)
    with pytest.raises(CredentialVerificationError):
        manager.save_credentials(
            "owner", KaggleCredentials(username="alice", api_token="new-secret"), verify_with_kaggle=True
        )
    assert manager.local_storage.get("owner").ciphertext == before
    assert manager.load_credentials("owner").api_token == "old-secret"


def test_connection_secrets_never_enter_release_packages(tmp_path):
    from tools.build_deployment_packages import is_global_excluded

    assert is_global_excluded(tmp_path / "connection_vault.key")
    assert is_global_excluded(tmp_path / "local_orca_settings.json")


def test_reaction_start_requires_kaggle_execution_passcode(tmp_path, monkeypatch):
    import app as webapp

    monkeypatch.setenv("KAGGLE_EXECUTION_PASSCODE", "test-access-code")
    store = ReactionStore(str(tmp_path))
    reaction = register_workflow("local_user", payload(), store)
    monkeypatch.setattr(webapp, "_reaction_store", store)
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as client:
        result = client.post(f"/api/v1/reactions/{reaction['reaction_id']}/start-execution", json={})
    assert result.status_code == 403
    assert result.json["error"] == "INVALID_KAGGLE_PASSCODE"
    assert store.get_reaction("local_user", reaction["reaction_id"])["state"] == "READY"
