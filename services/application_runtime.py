"""Lifecycle-owned background services for ASGI and standalone desktop startup."""

from __future__ import annotations

import atexit
import os
import threading

_lock = threading.RLock()
_runtime = None


class ApplicationRuntime:
    def __init__(self, state_dir, *, embedded_local_worker=True, interval_seconds=30):
        from services.background_workflows import BackgroundWorkflowSupervisor

        self.state_dir = state_dir
        self.supervisor = BackgroundWorkflowSupervisor(state_dir, interval_seconds=interval_seconds)
        self.local_worker = None
        self.threads = [
            threading.Thread(target=self.supervisor.run_forever, name="orca-workflow-supervisor", daemon=True)
        ]
        if embedded_local_worker:
            from services.local_orca_worker import LocalOrcaWorker

            self.local_worker = LocalOrcaWorker(state_dir)
            self.threads.append(
                threading.Thread(target=self.local_worker.run_forever, name="orca-embedded-worker", daemon=True)
            )

    def start(self):
        for thread in self.threads:
            thread.start()
        return self

    def stop(self):
        self.supervisor.stop_event.set()
        if self.local_worker:
            self.local_worker.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=3)

    def status(self):
        return {
            "background_workflows": self.threads[0].is_alive(),
            "embedded_local_worker": self.local_worker is not None,
            "local_worker_running": bool(self.local_worker and self.threads[-1].is_alive()),
        }


def start_application_runtime(*, force=False):
    global _runtime
    # Test clients do not launch real background jobs unless explicitly requested.
    if not force and (os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("CHEMISTRY_LAB_TEST_MODE") == "1"):
        return None
    if os.environ.get("CHEMISTRY_LAB_BACKGROUND_ENABLED", "1") == "0":
        return None
    with _lock:
        if _runtime is None:
            from orca_orchestrator.config import CONFIG

            state_dir = (
                os.environ.get("CHEMISTRY_LAB_STATE_DIR") or os.environ.get("ORCA_STATE_DIR") or CONFIG.store.state_dir
            )
            os.environ.setdefault("CHEMISTRY_LAB_STATE_DIR", state_dir)
            external = os.environ.get("CHEMISTRY_LAB_EXTERNAL_LOCAL_WORKER", "0") == "1"
            embedded = os.environ.get("CHEMISTRY_LAB_EMBEDDED_LOCAL_WORKER", "1") != "0" and not external
            _runtime = ApplicationRuntime(
                state_dir,
                embedded_local_worker=embedded,
                interval_seconds=float(os.environ.get("CHEMISTRY_LAB_WORKFLOW_POLL_SECONDS", "30")),
            ).start()
            os.environ["CHEMISTRY_LAB_REQUIRE_BACKGROUND"] = "1"
            atexit.register(stop_application_runtime)
        return _runtime


def stop_application_runtime():
    global _runtime
    with _lock:
        if _runtime is not None:
            _runtime.stop()
            _runtime = None


def runtime_status():
    return _runtime.status() if _runtime is not None else {}
