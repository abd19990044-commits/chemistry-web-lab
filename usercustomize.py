"""Last-mile production patches loaded automatically by Python's :mod:`site`.

This module deliberately contains small compatibility and production fixes that
are applied before ``app.py`` imports the job runner.
"""
from __future__ import annotations


def _install_kaggle_heartbeat_patch() -> None:
    """Make every generated Kaggle kernel emit a real progress heartbeat.

    ORCA can legitimately stay silent for many hours during a large Hessian,
    SCF cycle, optimisation step, or frequency calculation.  Kaggle's output
    then looks frozen even though the process is alive.  Replace the blocking
    ``proc.wait()`` in the generated kernel with a 30-second supervision loop
    that prints elapsed time, PID, output-file size and free scratch space every
    five minutes.  This changes monitoring only; it never kills or restarts a
    healthy ORCA process.
    """
    import kaggle_runner as kr

    body = getattr(kr, "KAGGLE_RUNNER_BODY", None)
    if not isinstance(body, str):
        return
    if "[heartbeat] ORCA still running" in body:
        return

    old = """    thread.start()\n    proc.wait()\n    run_finished.set()"""
    new = """    thread.start()\n    heartbeat_every = 300.0\n    next_heartbeat = time.monotonic() + heartbeat_every\n    while proc.poll() is None:\n        try:\n            proc.wait(timeout=min(30.0, max(1.0, next_heartbeat - time.monotonic())))\n        except subprocess.TimeoutExpired:\n            pass\n        if proc.poll() is None and time.monotonic() >= next_heartbeat:\n            elapsed = time.time() - START_TIME\n            free = _free_bytes(WORKDIR)\n            try:\n                out_size = os.path.getsize(out_path) if os.path.exists(out_path) else 0\n            except OSError:\n                out_size = 0\n            log(\"[heartbeat] ORCA still running | elapsed=%s | pid=%s | output=%s | free=%s\"\n                % (time.strftime(\"%Hh%Mm%Ss\", time.gmtime(max(0, int(elapsed)))),\n                   proc.pid, _gb(out_size), _gb(free)))\n            next_heartbeat = time.monotonic() + heartbeat_every\n    proc.wait()\n    run_finished.set()"""
    if old not in body:
        return
    kr.KAGGLE_RUNNER_BODY = body.replace(old, new, 1)


def _install_legacy_status_patch() -> None:
    import kaggle_runner as kr

    original = getattr(kr, "_ORIGINAL_CHECK_JOB_STATUS", kr.check_job_status)

    def patched(username: str, key: str, job_id: str) -> dict:
        if not kr.is_valid_job_id(job_id):
            raise RuntimeError(f"'{job_id}' is not a valid job id.")
        auth = kr.resolve_kaggle_auth(username, key)
        with kr._temp_kaggle_env(username, key) as env:
            proc = kr._run_kaggle_cli(
                ["kaggle", "kernels", "status", f"{auth['username']}/{job_id}"],
                env=env, timeout=60,
            )
            text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            status = kr._classify_kernel_status(text)
            if proc.returncode != 0:
                kr._raise_if_cli_broken(text)
                kr._raise_if_unreachable(text)
                return {"status": "unknown", "next_job_id": None,
                        "next_kaggle_url": None,
                        "note": text or "Kaggle returned an unrecognised response; status will be checked again."}

            if status not in ("running", "queued"):
                next_id, next_url = kr._probe_successor(env, auth["username"], job_id)
                if next_id:
                    return {"status": "restarting", "next_job_id": next_id,
                            "next_kaggle_url": next_url,
                            "note": "Following the newest continuation notebook on Kaggle."}

            if status in ("complete", "running", "queued", "cancelled", "error"):
                return {"status": status, "next_job_id": None,
                        "next_kaggle_url": None, "note": text}

        result = original(username, key, job_id)
        if result.get("status") == "unknown":
            result = dict(result)
            result["note"] = (result.get("note") or "Kaggle status is being verified.")
        return result

    kr.check_job_status = patched


def _install_orchestrator_listing_patch() -> None:
    from orca_orchestrator.service import OrchestratorService
    from orca_orchestrator import ledger as ledger_mod
    from orca_orchestrator.kaggle_api import KaggleClient
    from orca_orchestrator.logging_ext import log_event

    def list_jobs(self, creds):
        """Reconcile discovered jobs immediately instead of exposing UNKNOWN."""
        self._last_listing_creds = creds
        remote = ledger_mod.discover_jobs(KaggleClient(creds))
        merged = []
        seen = set()
        for entry in remote:
            job_id = entry["job_id"]
            seen.add(job_id)
            try:
                job = self.store.get_job(job_id)
                if job is None:
                    job = ledger_mod.rebuild_from_kaggle(KaggleClient(creds), job_id)
                    self.store.put_job(job)
                    log_event(__import__("orca_orchestrator.service", fromlist=["log"]).log,
                              "job_adopted", "adopted discovered Kaggle job during listing", job_id=job_id)
                if not job.is_terminal:
                    job = self.reconciler.reconcile(job_id, creds, actor="list")
                described = self.describe(job)
                described["chain_slugs"] = sorted(set(described.get("chain_slugs", [])) | set(entry.get("chain_slugs", [])))
                described["last_run"] = entry.get("last_run")
                merged.append(described)
            except Exception as exc:
                merged.append({"job_id": job_id, "title": entry.get("title", job_id),
                               "state": "VERIFYING", "phase": "Verifying Kaggle status",
                               "epoch": entry.get("epoch", 0), "window": entry.get("epoch", 0) + 1,
                               "current_slug": entry.get("current_slug"),
                               "kaggle_url": entry.get("kaggle_url"),
                               "chain_slugs": entry.get("chain_slugs", []),
                               "is_terminal": False, "needs_reconcile": True,
                               "note": f"Status check will retry: {type(exc).__name__}"})
        for job in self.store.list_jobs(creds.username):
            if job.job_id not in seen:
                described = self.describe(job)
                described["deleted_on_kaggle"] = True
                merged.append(described)
        merged.sort(key=lambda item: item.get("updated_at") or item.get("last_run") or 0, reverse=True)
        return merged

    OrchestratorService.list_jobs = list_jobs


try:
    _install_kaggle_heartbeat_patch()
except Exception:
    pass

try:
    _install_legacy_status_patch()
except Exception:
    pass

try:
    _install_orchestrator_listing_patch()
except Exception:
    pass
