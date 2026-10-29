"""Browser-independent workflow reconciliation with one cross-process scheduler."""

from __future__ import annotations

import logging
import os
import threading
import time

from services.local_orca_service import CrossProcessFileLock
from services.reaction_workflow_service import ReactionStore, dispatch_ready_stages, reconcile_kaggle_stages

log = logging.getLogger("chemlab.background_workflows")
TERMINAL = {"COMPLETE", "COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "CANCELLED"}


def finish_cancellation(reaction, store):
    from orca_orchestrator.credential_vault import get_vault_manager
    from orca_orchestrator.service import get_service
    from services.local_orca_worker import request_cancel_local_job

    pending = False
    for species in reaction.get("species", []):
        for stage in species.get("stages", []):
            if stage.get("state") in TERMINAL:
                continue
            try:
                if stage.get("kaggle_job_id"):
                    creds = get_vault_manager().load_credentials(reaction["owner"])
                    if not creds:
                        pending = True
                        stage["waiting_reason"] = "KAGGLE_CREDENTIALS_REQUIRED"
                        continue
                    get_service().cancel(creds, stage["kaggle_job_id"])
                elif stage.get("local_job_id"):
                    result = request_cancel_local_job(
                        stage["local_job_id"], owner_id=reaction["owner"], state_dir=store.state_dir
                    )
                    if not result.get("ok") or result.get("status") not in TERMINAL:
                        pending = True
                        stage["state"] = "CANCEL_REQUESTED"
                        continue
                elif stage.get("agent_job_id"):
                    from services.local_agent_service import request_agent_job_cancel

                    result = request_agent_job_cancel(
                        stage["agent_job_id"], reaction["owner"], state_dir=store.state_dir
                    )
                    if not result.get("ok") or result.get("status") not in TERMINAL:
                        pending = True
                        stage["state"] = "CANCEL_REQUESTED"
                        continue
                stage["state"] = "CANCELLED"
            except Exception as exc:
                pending = True
                stage["waiting_reason"] = "CANCELLATION_RETRY_REQUIRED"
                log.warning("Workflow cancellation deferred: %s", type(exc).__name__)
    reaction["state"] = "CANCEL_REQUESTED" if pending else "CANCELLED"
    if not pending:
        from services.reaction_workflow_service import utcnow_iso

        reaction["finished_at"] = utcnow_iso(store.now_provider)
    store.save_reaction(reaction)


class BackgroundWorkflowSupervisor:
    def __init__(self, state_dir, *, interval_seconds=30):
        self.state_dir = state_dir
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.stop_event = threading.Event()
        self._last_remote_pass = 0.0

    def run_once(self):
        lock = CrossProcessFileLock(os.path.join(self.state_dir, ".workflow-supervisor.lock"))
        if not lock.acquire(blocking=False):
            return {"leader": False, "examined": 0}
        try:
            store = ReactionStore(self.state_dir)
            examined = 0
            for snapshot in store.list_reactions(None):
                if self.stop_event.is_set():
                    break
                state = snapshot.get("state")
                stages = [s for sp in snapshot.get("species", []) for s in sp.get("stages", [])]
                enabled = snapshot.get("background_enabled") or any(
                    s.get("kaggle_job_id") or s.get("local_job_id") for s in stages
                )
                if not enabled or state in TERMINAL or state == "PAUSED":
                    continue
                examined += 1
                try:
                    if state == "CANCEL_REQUESTED":
                        finish_cancellation(snapshot, store)
                        continue
                    if any(s.get("kaggle_job_id") and s.get("state") not in TERMINAL for s in stages):
                        reconcile_kaggle_stages(
                            snapshot, owner_id=snapshot["owner"], state_dir=self.state_dir, store=store
                        )
                    # Reload after every observation/projection so a cancelled or
                    # paused chain cannot be submitted using a stale snapshot.
                    current = store.get_reaction(snapshot["owner"], snapshot["reaction_id"])
                    if (
                        current
                        and current.get("kind") == "calculation"
                        and current.get("state") not in {"PAUSED", "CANCEL_REQUESTED", "COMPLETE"}
                    ):
                        current_stages = [s for sp in current.get("species", []) for s in sp.get("stages", [])]
                        stopped = next((s for s in current_stages if s.get("state") in {"FAILED", "CANCELLED"}), None)
                        if stopped:
                            current["state"] = "FAILED" if stopped["state"] == "FAILED" else "CANCELLED"
                            for stage in current_stages:
                                if stage.get("state") in {"READY", "BLOCKED_BY_DEPENDENCY", "PAUSED"}:
                                    stage["state"] = "CANCELLED"
                                    stage["error"] = "Predecessor calculation failed or was cancelled"
                            store.save_reaction(current)
                    if current and current.get("state") not in TERMINAL | {"PAUSED", "CANCEL_REQUESTED"}:
                        dispatch_ready_stages(current, owner_id=current["owner"], state_dir=self.state_dir, store=store)
                except Exception as exc:
                    log.warning("Workflow pass deferred for %s: %s", snapshot.get("reaction_id"), type(exc).__name__)
            # Also advance ordinary ORCA windows and collect results with no UI polls.
            if time.monotonic() - self._last_remote_pass >= self.interval_seconds:
                from orca_orchestrator.service import get_service

                service = get_service()
                if service.store.list_active_jobs(older_than_seconds=0, limit=1):
                    service.watchdog.reconcile_all_active()
                service.watchdog.collect_finished_results()
                self._last_remote_pass = time.monotonic()
            return {"leader": True, "examined": examined}
        finally:
            lock.release()

    def run_forever(self):
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:
                log.warning("Workflow supervisor pass failed: %s", type(exc).__name__)
            self.stop_event.wait(self.interval_seconds)
