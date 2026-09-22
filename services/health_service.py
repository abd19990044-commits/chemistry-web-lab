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
        probe_dir = tempfile.gettempdir()
        probe_path = os.path.join(probe_dir, ".chemlab_write_probe")
        with open(probe_path, "w") as fh:
            fh.write("ok")
        os.remove(probe_path)
        payload["storage_writable"] = True
    except OSError:
        payload["storage_writable"] = False
    return payload



def build_ready(*, orchestrator_available: bool, orca_engine_available: bool) -> dict:
    """Readiness: dependencies, storage, orchestrator availability.

    Deliberately does NOT require the Kaggle CLI or network: a Space can be
    ready to serve the UI and API while Kaggle credentials are not yet set.
    """
    health = build_health(orchestrator_available=orchestrator_available,
                          orca_engine_available=orca_engine_available)
    worker_ok = (not health.get("local_worker_required")) or bool(health.get("local_worker_ok"))
    ready = bool(health.get("storage_writable")) and orchestrator_available and worker_ok
    return {"ok": ready, "ready": ready,
            "checks": {
                "storage": bool(health.get("storage_writable")),
                "orchestrator": bool(health.get("orchestrator")),
                "orca_engine": bool(health.get("orca_engine")),
                "kaggle_cli": bool(health.get("kaggle_cli_ok")),
                "local_worker": worker_ok,
            },
            "detail": health}
