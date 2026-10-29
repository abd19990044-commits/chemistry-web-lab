# -*- coding: utf-8 -*-
"""Health/readiness service shared by Flask /health and FastAPI /api/v1."""
import os


def build_health(*, orchestrator_available: bool, orca_engine_available: bool) -> dict:
    """Liveness plus a real readiness picture (moved verbatim from app.py)."""
    # The `kaggle` CLI is what every /api/kaggle/* route shells out to, and it
    # is the path the browser actually uses. Reporting only on the orchestrator
    # meant /health could answer {"ok": true} while the CLI was missing from the
    # image and every job submission was returning 502.
    # The CLI is RUN, not merely located: a failed image build leaves the
    # wrapper script on PATH with the package behind it missing, and a file
    # test calls that healthy while every request raises.
    import kaggle_runner

    cli = kaggle_runner.cli_health()
    payload = {"ok": cli["ok"], "status": "running",
               "runner": "kaggle_runner (legacy routes) - the path the UI submits through",
               "kaggle_cli": cli.get("version") or cli.get("path") or "MISSING",
               "kaggle_cli_ok": cli["ok"],
               "orchestrator": orchestrator_available,
               "orca_engine": orca_engine_available}
    worker_required = os.environ.get("CHEMISTRY_LAB_REQUIRE_LOCAL_WORKER", "0").lower() in ("1", "true", "yes", "on")
    payload["local_worker_required"] = worker_required
    if worker_required:
        try:
            from services.local_orca_worker import worker_health
            worker = worker_health()
            payload["local_worker"] = worker
            payload["local_worker_ok"] = bool(worker.get("ok"))
        except Exception as exc:  # noqa: BLE001
            payload["local_worker"] = {"ok": False, "error": type(exc).__name__}
            payload["local_worker_ok"] = False
    if not cli["ok"]:
        payload["error"] = (
            "the kaggle command-line tool is not usable (%s), so no job can be submitted, "
            "polled or downloaded. Check that requirements.txt installed cleanly - a pin "
            "that does not exist on PyPI fails the whole image build silently."
            % cli.get("detail", "unknown"))
    if orchestrator_available:
        try:
            from orca_orchestrator import get_service

            orch_health = get_service().health()
            payload.update(orch_health)
        except Exception as exc:  # noqa: BLE001
            payload["orchestrator_error"] = str(exc)
    import tempfile
    try:
        # Every worker/probe needs its own file. A shared filename races on
        # concurrent health requests: the first unlink makes others report a
        # false storage failure, and a pre-existing symlink could be followed.
        with tempfile.TemporaryFile(mode="w+", prefix="chemlab-write-probe-") as fh:
            fh.write("ok")
            fh.flush()
        if orchestrator_available and not payload.get("orchestrator_error"):
            state_dir = payload.get("config", {}).get("state_dir")
            if not state_dir:
                from orca_orchestrator.config import CONFIG
                state_dir = CONFIG.store.state_dir
            # /tmp can remain writable while the database/result volume fails.
            with tempfile.TemporaryFile(mode="w+", prefix="chemlab-state-probe-", dir=state_dir) as fh:
                fh.write("ok")
                fh.flush()
                os.fsync(fh.fileno())
        payload["storage_writable"] = True
    except OSError:
        payload["storage_writable"] = False
    background_ok = True
    if os.environ.get("CHEMISTRY_LAB_REQUIRE_BACKGROUND") == "1":
        from services.application_runtime import runtime_status
        runtime = runtime_status()
        payload["background_runtime"] = runtime
        background_ok = bool(runtime.get("background_workflows"))
        if runtime.get("embedded_local_worker"):
            background_ok = background_ok and bool(runtime.get("local_worker_running"))
    payload["background_ok"] = background_ok
    # The orchestrator's own 'ok' must not overwrite dependency failures.
    payload["ok"] = bool(cli["ok"] and orca_engine_available
                         and payload["storage_writable"]
                         and background_ok
                         and not payload.get("orchestrator_error")
                         and (not worker_required or payload.get("local_worker_ok")))
    return payload



def build_ready(*, orchestrator_available: bool, orca_engine_available: bool) -> dict:
    """Readiness: dependencies, storage, orchestrator availability.

    Deliberately does NOT require the Kaggle CLI or network: a Space can be
    ready to serve the UI and API while Kaggle credentials are not yet set.
    """
    health = build_health(orchestrator_available=orchestrator_available,
                          orca_engine_available=orca_engine_available)
    worker_ok = (not health.get("local_worker_required")) or bool(health.get("local_worker_ok"))
    orchestrator_ok = bool(health.get("orchestrator")) and not health.get("orchestrator_error")
    engine_ok = bool(health.get("orca_engine"))
    background_ok = bool(health.get("background_ok", True))
    ready = bool(health.get("storage_writable")) and orchestrator_ok and engine_ok and worker_ok and background_ok
    return {"ok": ready, "ready": ready,
            "checks": {
                "storage": bool(health.get("storage_writable")),
                "orchestrator": orchestrator_ok,
                "orca_engine": engine_ok,
                "kaggle_cli": bool(health.get("kaggle_cli_ok")),
                "local_worker": worker_ok,
                "background_workflows": background_ok,
            },
            "detail": health}
