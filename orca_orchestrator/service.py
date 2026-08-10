# -*- coding: utf-8 -*-
"""
The facade the web layer talks to.

Flask should contain no orchestration logic at all -- a request handler's job
is to parse input, call one method here, and serialise the result. Keeping the
boundary that sharp is what makes the orchestrator testable without a WSGI
stack, and what stops business rules from accumulating in route functions
where they cannot be reused by the watchdog.

Every mutating entry point is idempotent, either naturally or through an
explicit idempotency key. `submit()` in particular takes one, because a
double-clicked button and a browser refresh mid-POST are the two most common
ways a user accidentally launches the same expensive calculation twice.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass

from . import ledger as ledger_mod
from .config import CONFIG, STATE_DIR_DIAGNOSTIC
from .credentials import BROKER, KaggleCredentials, parse as parse_credentials
from .errors import (ConcurrencyError, NotFoundError, OrchestratorError,
                     ValidationError)
from .hashing import sha256_bytes
from .kaggle_api import KaggleClient, is_valid_slug
from .logging_ext import get_logger, log_context, log_event, new_correlation_id
from .models import Event, JobManifest, new_id, now
from .orca_artifacts import detect_job_kind
from .reconciler import Reconciler
from .runner.builder import build_window_directory
from .states import JobState, Trigger
from .store import JobStore, get_store
from .watchdog import Watchdog, assess, recover_after_restart

log = get_logger("orca.service")

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(raw: str) -> str:
    slug = _SLUG_STRIP.sub("-", (raw or "").strip().lower()).strip("-")
    return re.sub(r"-{2,}", "-", slug)


def make_job_id(name: str = "", input_filename: str = "") -> str:
    """`chem-tools-<name>-<random>`.

    The prefix is how a job is recognised as belonging to this site when a
    user's kernel list is read back on sign-in, which is what makes the job list
    survive clearing browser data or switching device. The random suffix makes
    a resubmission of the same molecule a distinct job rather than an
    accidental overwrite of a running one."""
    stem = slugify(name) or slugify(os.path.splitext(os.path.basename(input_filename or ""))[0])
    stem = stem[:24].strip("-")
    suffix = os.urandom(4).hex()
    return f"{CONFIG.job_id_prefix}{stem}-{suffix}" if stem else f"{CONFIG.job_id_prefix}{suffix}"


@dataclass
class SubmitResult:
    job_id: str
    slug: str
    url: str
    title: str
    replayed: bool = False

    def to_dict(self) -> dict:
        return {"job_id": self.job_id, "slug": self.slug, "kaggle_url": self.url,
                "title": self.title, "replayed": self.replayed}


class OrchestratorService:
    def __init__(self, store: JobStore | None = None, *, start_watchdog: bool = True) -> None:
        self.store = store or get_store()
        self.reconciler = Reconciler(self.store)
        self.watchdog = Watchdog(self.store, self.reconciler, BROKER)
        self.startup_report = recover_after_restart(self.store)
        if start_watchdog:
            self.watchdog.start()

    # -- credentials -------------------------------------------------------
    def authenticate(self, username: str, key_or_token: str) -> KaggleCredentials:
        """Validates credentials against Kaggle and caches them in RAM.

        Doubles as the sign-in check: `list_kernels` fails loudly on bad
        credentials, so one call both proves the account and fetches the job
        list, which is what the sign-in screen actually needs."""
        creds = parse_credentials(username, key_or_token)
        KaggleClient(creds).list_kernels()
        BROKER.remember(creds)
        log_event(log, "authenticated", "Kaggle credentials verified",
                  owner=creds.username, fingerprint=creds.fingerprint)
        return creds

    # -- submit ------------------------------------------------------------
    def submit(
        self,
        creds: KaggleCredentials,
        *,
        input_filename: str,
        input_content: str,
        job_name: str = "",
        aux_files: dict[str, bytes] | None = None,
        dataset_sources: list[str] | None = None,
        orca_link: str | None = None,
        idempotency_key: str | None = None,
    ) -> SubmitResult:
        """Creates a job and pushes its first window.

        The manifest is persisted in state CREATED *before* the push. That
        ordering is the whole crash-safety story for submission: if the process
        dies between the write and the push, the next reconciliation sees a
        CREATED job with no kernel and replays the push. If the order were
        reversed, a crash would leave a running Kaggle notebook that nothing
        knows about -- an orphan burning the user's quota.
        """
        if not input_content.strip():
            raise ValidationError("the ORCA input file is empty")

        aux_files = aux_files or {}
        correlation_id = new_correlation_id()
        key = idempotency_key or f"submit:{creds.fingerprint}:{sha256_bytes(input_content.encode())[:32]}"

        request_payload = {
            "owner": creds.username, "filename": input_filename,
            "content_sha": sha256_bytes(input_content.encode("utf-8")),
            "aux": sorted(aux_files), "datasets": sorted(dataset_sources or []),
            "name": job_name,
        }
        replay, stored = self.store.begin_idempotent(key, request_payload)
        if replay:
            if stored is None:
                # An identical submission is in flight in another worker. This
                # is the double-click / refresh case; returning a conflict is
                # far better than launching a second twelve-hour notebook.
                raise ConcurrencyError(
                    "an identical submission is already in progress; it will appear in "
                    "your job list shortly",
                    idempotency_key=key,
                )
            log_event(log, "submit_replayed",
                      "returning the stored response for a repeated submission",
                      owner=creds.username, job_id=stored.get("job_id"))
            return SubmitResult(**{**stored, "replayed": True,
                                   "url": stored.get("kaggle_url", stored.get("url", ""))})

        try:
            job_id = make_job_id(job_name, input_filename)
            with log_context(correlation_id=correlation_id, job_id=job_id):
                job = JobManifest.create(
                    job_id=job_id,
                    owner=creds.username,
                    title=(job_name or os.path.splitext(os.path.basename(input_filename))[0]
                           or job_id),
                    input_filename=input_filename,
                    original_input_sha256=sha256_bytes(input_content.encode("utf-8")),
                    dataset_sources=dataset_sources or [],
                    orca_link_present=bool(orca_link),
                    job_kind=detect_job_kind(input_content),
                )
                self.store.put_job(job)

                job = self.reconciler.transition(
                    job, Trigger.SUBMIT, actor="api", correlation_id=correlation_id,
                    job_kind=job.job_kind, datasets=len(dataset_sources or []),
                )
                self.store.put_job(job, expected_version=job._extra.get("_version"))

                inline = {input_filename: input_content.encode("utf-8")}
                inline.update(aux_files)

                work_dir = tempfile.mkdtemp(prefix="orca-submit-")
                try:
                    build_window_directory(
                        work_dir, job=job, epoch=0, creds=creds,
                        inline_files=inline, orca_link=orca_link,
                    )
                    result = KaggleClient(creds).push_kernel(
                        work_dir, expected_slug=job_id, skip_if_active=False)
                finally:
                    shutil.rmtree(work_dir, ignore_errors=True)

                job.current_slug = result.slug
                job.current_url = result.url
                if result.slug not in job.chain_slugs:
                    job.chain_slugs.append(result.slug)
                job = self.reconciler.transition(
                    job, Trigger.PUSH_ACK, actor="api", correlation_id=correlation_id,
                    slug=result.slug, url=result.url,
                )
                self.store.put_job(job, expected_version=job._extra.get("_version"))

                response = SubmitResult(job_id=job.job_id, slug=result.slug,
                                        url=result.url, title=job.title)
                self.store.complete_idempotent(key, response.to_dict())
                log_event(log, "job_submitted", "job created and its first window pushed",
                          job_id=job.job_id, slug=result.slug, job_kind=job.job_kind)
                return response
        except BaseException:
            # Release the claim so a corrected retry is not blocked for a day
            # by a key that never produced a job.
            self.store.abandon_idempotent(key)
            raise

    # -- status ------------------------------------------------------------
    def status(self, creds: KaggleCredentials, job_id: str, *,
               reconcile: bool = True) -> dict:
        """Returns the job's state, reconciling against Kaggle first.

        Adopts the job automatically when it is missing from the local cache:
        that is the Hugging Face-restart path, the different-browser path, and
        the pre-orchestrator-job path, all of which land here and all of which
        must produce a working answer rather than 'unknown job'."""
        if not is_valid_slug(job_id):
            raise ValidationError("that job id does not look like one of this site's jobs")
        BROKER.remember(creds)

        job = self.store.get_job(job_id)
        if job is None:
            job = ledger_mod.rebuild_from_kaggle(KaggleClient(creds), job_id)
            self.store.put_job(job)
            log_event(log, "job_adopted", "adopted a job that was not in the local cache",
                      job_id=job_id, epoch=job.epoch, state=job.state.value)

        if reconcile and not job.is_terminal:
            job = self.reconciler.reconcile(job_id, creds, actor="api")

        return self.describe(job)

    def describe(self, job: JobManifest) -> dict:
        """Serialises a job for the UI, including *why* it is in its state.

        The old API returned a bare status word, so a job that was quietly
        stuck looked identical to one that was busy. Every field here exists to
        answer a question a user actually asks: how far along is it, what is it
        waiting for, and when did anything last happen."""
        verdict = assess(job)
        checkpoint = (self.store.get_checkpoint(job.verified_checkpoint_id)
                      if job.verified_checkpoint_id else None)
        return {
            "job_id": job.job_id,
            "title": job.title,
            "state": job.state.value,
            "phase": _phase_label(job.state),
            "epoch": job.epoch,
            "window": job.epoch + 1,
            "max_epochs": job.max_epochs,
            "current_slug": job.current_slug,
            "kaggle_url": job.current_url or
                          f"https://www.kaggle.com/code/{job.owner}/{job.current_slug}",
            "chain_slugs": job.chain_slugs,
            "job_kind": job.job_kind,
            "is_terminal": job.is_terminal,
            "cumulative_opt_cycles": job.cumulative_opt_cycles,
            "max_total_opt_cycles": job.max_total_opt_cycles,
            "rollback_count": job.rollback_count,
            "retry_count": job.retry_count,
            "disk_epochs_used": job.disk_epochs_used,
            "verified_checkpoint": {
                "id": checkpoint.checkpoint_id,
                "epoch": checkpoint.epoch,
                "phase": checkpoint.orca_phase,
                "files": len(checkpoint.files),
                "opt_converged": checkpoint.opt_converged,
                "cumulative_opt_cycles": checkpoint.cumulative_opt_cycles,
                "verified_at": checkpoint.verified_at,
            } if checkpoint else None,
            "last_heartbeat_at": job.last_heartbeat_at,
            "heartbeat_detail": job.heartbeat_detail,
            "note": job.last_note,
            "error": job.last_error,
            "disk_report": job.disk_report,
            "stall": verdict.to_dict(),
            "updated_at": job.updated_at,
            "created_at": job.created_at,
            "events": [e.to_dict() for e in job.recent_events[-15:]],
        }

    # -- listing -----------------------------------------------------------
    def list_jobs(self, creds: KaggleCredentials) -> list[dict]:
        """Merges what Kaggle knows with what the local cache knows.

        Kaggle is authoritative about *existence* -- it owns the notebooks. The
        cache is richer about *state*. Neither alone is sufficient: a
        cache-only list loses everything after a redeploy, and a Kaggle-only
        list cannot say anything more specific than 'complete'."""
        BROKER.remember(creds)
        remote = ledger_mod.discover_jobs(KaggleClient(creds))
        local = {j.job_id: j for j in self.store.list_jobs(creds.username)}

        merged = []
        for entry in remote:
            job = local.pop(entry["job_id"], None)
            if job is None:
                merged.append({
                    "job_id": entry["job_id"], "title": entry["title"],
                    "state": "UNKNOWN", "phase": "Not yet examined",
                    "epoch": entry["epoch"], "window": entry["epoch"] + 1,
                    "current_slug": entry["current_slug"],
                    "kaggle_url": entry["kaggle_url"],
                    "chain_slugs": entry["chain_slugs"],
                    "is_terminal": False, "needs_reconcile": True,
                    "last_run": entry["last_run"],
                })
                continue
            described = self.describe(job)
            described["chain_slugs"] = sorted(
                set(described["chain_slugs"]) | set(entry["chain_slugs"]))
            described["last_run"] = entry["last_run"]
            merged.append(described)

        # Jobs the cache knows about that Kaggle no longer lists: the notebooks
        # were deleted on kaggle.com. Report them rather than hiding them, so a
        # user is not left wondering where a job went.
        for job in local.values():
            described = self.describe(job)
            described["deleted_on_kaggle"] = True
            merged.append(described)

        merged.sort(key=lambda j: j.get("updated_at") or 0, reverse=True)
        return merged

    # -- results -----------------------------------------------------------
    def fetch_results(self, creds: KaggleCredentials, job_id: str,
                      *, slug: str | None = None) -> tuple[str | None, str]:
        """Downloads a window's output and returns `(zip_path, cleanup_dir)`.

        Defaults to the *newest* window, since that is where a finished job's
        results are. Earlier windows remain individually addressable, because a
        chemist sometimes wants the trajectory from a specific stage."""
        if not is_valid_slug(job_id):
            raise ValidationError("invalid job id")
        BROKER.remember(creds)
        job = self.store.get_job(job_id)
        target = slug or (job.current_slug if job else job_id)
        if not is_valid_slug(target):
            raise ValidationError("invalid window slug")

        client = KaggleClient(creds)
        out_dir = client.fetch_output(target, timeout=900, page_size=200)

        zip_path = os.path.join(out_dir, "results.zip")
        if os.path.exists(zip_path) and os.path.getsize(zip_path) > 0:
            return zip_path, out_dir

        # No packaged archive: the window may have been killed before packaging.
        # Bundle whatever loose output exists rather than returning a dead end.
        fallback = os.path.join(out_dir, "_partial_results.zip")
        bundled = False
        with zipfile.ZipFile(fallback, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in sorted(os.listdir(out_dir)):
                path = os.path.join(out_dir, name)
                if os.path.isfile(path) and path != fallback:
                    zf.write(path, name)
                    bundled = True
        if bundled:
            return fallback, out_dir
        shutil.rmtree(out_dir, ignore_errors=True)
        return None, out_dir

    # -- lifecycle ---------------------------------------------------------
    def cancel(self, creds: KaggleCredentials, job_id: str) -> dict:
        job = self.store.require_job(job_id)
        if job.is_terminal:
            return self.describe(job)
        job = self.reconciler.transition(job, Trigger.CANCEL, actor="operator",
                                          reason="cancelled by the user")
        self.store.put_job(job, expected_version=job._extra.get("_version"))
        log_event(log, "job_cancelled", "job cancelled by the user", job_id=job_id)
        return self.describe(job)

    def resume(self, creds: KaggleCredentials, job_id: str) -> dict:
        """Manual resume from the last verified checkpoint.

        Never restarts from zero. If there is no verified checkpoint the FSM's
        guard refuses the transition, which is the correct answer -- silently
        starting over would repeat work the user already paid for and would
        almost certainly fail the same way."""
        BROKER.remember(creds)
        job = self.store.require_job(job_id)
        if job.state is not JobState.FAILED:
            raise ValidationError("only a failed job can be resumed",
                                  state=job.state.value)
        job = self.reconciler.transition(job, Trigger.OPERATOR_RESUME, actor="operator",
                                          reason="manual resume requested",
                                          checkpoint_id=job.verified_checkpoint_id)
        self.store.put_job(job, expected_version=job._extra.get("_version"))
        return self.status(creds, job_id)

    def delete(self, creds: KaggleCredentials, job_id: str) -> dict:
        """Deletes every window in the chain, then the local record.

        Deleting only the newest window is what leaves orphan kernels behind:
        the next sign-in rebuilds the job list from Kaggle, finds the older
        windows, and the job the user deleted reappears."""
        if not is_valid_slug(job_id):
            raise ValidationError("invalid job id")
        BROKER.remember(creds)
        client = KaggleClient(creds)

        job = self.store.get_job(job_id)
        slugs = list(job.chain_slugs) if job else []
        if not slugs:
            chains = ledger_mod.group_chains(client.list_kernels())
            slugs = [w["slug"] for w in chains.get(job_id, [])] or [job_id]

        deleted, failed = [], []
        for slug in slugs:
            try:
                client.delete_kernel(slug)
                deleted.append(slug)
            except OrchestratorError as exc:
                failed.append({"slug": slug, "error": exc.code})

        self.store.delete_job(job_id)
        log_event(log, "job_deleted", "job and its whole window chain removed",
                  job_id=job_id, deleted=len(deleted), failed=len(failed))
        return {"job_id": job_id, "deleted": deleted, "failed": failed}

    # -- ops ---------------------------------------------------------------
    def sweep_now(self) -> dict:
        return self.watchdog.sweep().to_dict()

    def health(self) -> dict:
        return {
            "ok": True,
            "store": self.store.stats(),
            "watchdog": {
                "enabled": CONFIG.watchdog.enabled,
                "interval_seconds": CONFIG.watchdog.sweep_interval_seconds,
                "last_sweep": self.watchdog.last_result,
                "credential_owners_cached": len(BROKER.known_owners()),
            },
            "startup_recovery": self.startup_report,
            "config": {
                "time_limit_seconds": CONFIG.runner.time_limit_seconds,
                "handoff_reserve_seconds": CONFIG.runner.handoff_reserve_seconds,
                "max_epochs": CONFIG.runner.max_epochs,
                "working_quota_gb": CONFIG.kaggle.working_quota_bytes / (1 << 30),
                "scratch_quota_gb": CONFIG.kaggle.scratch_quota_bytes / (1 << 30),
                "state_dir": CONFIG.store.state_dir,
                "pid": os.getpid(),
            },
            # Surfaced deliberately. If `shared` is false, every worker has its
            # own database and the leases and idempotency keys coordinate
            # nothing between them -- a condition that is invisible from the
            # outside unless it is reported here.
            "state_dir_diagnostic": dict(STATE_DIR_DIAGNOSTIC),
        }

    def state_machine_diagram(self) -> str:
        from .states import TRANSITIONS
        return TRANSITIONS.as_mermaid()


_PHASE_LABELS = {
    JobState.CREATED: "Preparing",
    JobState.UPLOADING: "Uploading to Kaggle",
    JobState.QUEUED: "Waiting for a Kaggle session",
    JobState.DOWNLOADING: "Retrieving the previous checkpoint",
    JobState.VERIFYING: "Verifying checkpoint integrity",
    JobState.RESTORING: "Restoring restart files",
    JobState.READY: "Starting ORCA",
    JobState.RUNNING: "Running",
    JobState.CHECKPOINTING: "Saving a checkpoint",
    JobState.ROLLING_BACK: "Recovering from the last good checkpoint",
    JobState.RESTARTING: "Starting the next session",
    JobState.FINISHED: "Finished",
    JobState.FAILED: "Failed",
    JobState.CANCELLED: "Cancelled",
}


def _phase_label(state: JobState) -> str:
    return _PHASE_LABELS.get(state, state.value)


_service: OrchestratorService | None = None
_service_lock = __import__("threading").Lock()


def get_service(**kwargs) -> OrchestratorService:
    """Process-wide singleton, constructed lazily and retried on failure.

    Two properties matter here, and the second was learned in production.

    The lock stops a stampede: with four threads per gunicorn worker, an
    uninitialised service would otherwise be constructed four times
    concurrently on the first burst of requests, each opening its own database
    and starting its own watchdog thread.

    Failures are deliberately **not** cached. A construction failure here is far
    more often transient (two workers racing to create the same fresh SQLite
    file at boot) than permanent, and caching it would turn a millisecond-long
    collision into a permanently degraded worker. Because `_service` is only
    assigned on success, the next caller simply tries again.
    """
    global _service
    if _service is not None:
        return _service
    with _service_lock:
        if _service is None:
            _service = OrchestratorService(**kwargs)
        return _service


def service_is_ready() -> bool:
    return _service is not None
