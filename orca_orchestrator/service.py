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
import hashlib
import json
import secrets
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Any

from . import ledger as ledger_mod
from .cloudflare_controller import CloudflareController
from .config import CONFIG, STATE_DIR_DIAGNOSTIC
from .credentials import BROKER, KaggleCredentials, parse as parse_credentials
from .errors import (ConcurrencyError, NotFoundError, OrchestratorError,
                     ValidationError)
from .hashing import content_id, sha256_bytes
from .kaggle_api import KaggleClient, is_valid_slug
from .logging_ext import get_logger, log_context, log_event, new_correlation_id
from .models import Event, JobManifest, new_id, now
from .orca_artifacts import (detect_job_kind, extract_charge_mult,
                             read_trajectory_frames, set_geometry)
from .reconciler import Reconciler
from .result_store import ResultArtifactStore, ResultDurabilityState, ResultManifest
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
        self.cf_controller = CloudflareController(store=self.store)
        self.result_store = ResultArtifactStore()
        self.watchdog = Watchdog(
            self.store, self.reconciler, BROKER,
            workflow_driver=self._reconcile_and_drive_workflows,
        )
        self.startup_report = recover_after_restart(self.store)
        if start_watchdog:
            self.watchdog.start()

    def shutdown(self) -> None:
        """Stop background threads and close database connections cleanly."""
        if hasattr(self, "watchdog") and self.watchdog is not None:
            try:
                self.watchdog.stop(timeout=1.0)
            except Exception:
                pass
        if hasattr(self, "store") and self.store is not None:
            try:
                self.store.close()
            except Exception:
                pass

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

    def ensure_authenticated(self, creds: KaggleCredentials) -> KaggleCredentials:
        """Verifies credentials against Kaggle or RAM cache, returning authenticated creds.

        Never trusts unverified usernames or keys without authentication."""
        if not creds.username or (not creds.key and not creds.api_token):
            raise ValidationError("Kaggle username and API key are required.")
        cached = BROKER.get(creds.username)
        if cached is None or cached.fingerprint != creds.fingerprint:
            return self.authenticate(creds.username, creds.key or creds.api_token)
        return cached

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
        workflow_id: str | None = None,
        parent_job_id: str | None = None,
        step_index: int = 0,
        step_count: int = 1,
        step_name: str = "CALC",
        callback_base_url: str | None = None,
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

        request_payload = {
            "owner": creds.username, "filename": input_filename,
            "content_sha": sha256_bytes(input_content.encode("utf-8")),
            "aux": sorted(aux_files), "datasets": sorted(dataset_sources or []),
            "name": job_name,
            "workflow_id": workflow_id,
            "step_index": step_index,
        }
        payload_hash = content_id(request_payload)
        key = idempotency_key or f"submit:{creds.fingerprint}:{payload_hash}"
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
            return SubmitResult(
                job_id=stored.get("job_id", ""),
                slug=stored.get("slug", ""),
                url=stored.get("kaggle_url") or stored.get("url") or "",
                title=stored.get("title", ""),
                replayed=True,
            )

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
                    _extra={
                        "workflow_id": workflow_id,
                        "parent_job_id": parent_job_id,
                        "step_index": step_index,
                        "step_count": step_count,
                        "step_name": step_name,
                        "callback_base_url": (callback_base_url or "").rstrip("/"),
                        "callback_token": secrets.token_urlsafe(32),
                    },
                )
                self.store.put_job(job)

                job = self.reconciler.transition(
                    job, Trigger.SUBMIT, actor="api", correlation_id=correlation_id,
                    job_kind=job.job_kind, datasets=len(dataset_sources or []),
                )
                self.store.put_job(job, expected_version=job._extra.get("_version"))

                # Kaggle output is the durable job ledger; no Cloudflare registration.
                inline = {input_filename: input_content.encode("utf-8")}
                inline.update(aux_files)

                work_dir = tempfile.mkdtemp(prefix="orca-submit-")
                try:
                    build_window_directory(
                        work_dir, job=job, epoch=0, creds=creds,
                        inline_files=inline, orca_link=orca_link,
                        callback_base_url=job._extra.get("callback_base_url"),
                        callback_token=job._extra.get("callback_token"),
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

                # No external control plane: Kaggle retains the authoritative ledger.
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
        auth_creds = self.ensure_authenticated(creds)

        job = self.store.get_job(job_id)
        if job and job.owner != auth_creds.username.lower():
            raise ValidationError(f"Access denied: Job {job_id} does not belong to user {auth_creds.username}")

        if job is None:
            job = ledger_mod.rebuild_from_kaggle(KaggleClient(auth_creds), job_id)
            self.store.put_job(job)
            log_event(log, "job_adopted", "adopted a job that was not in the local cache",
                      job_id=job_id, epoch=job.epoch, state=job.state.value)

        if reconcile:
            job = self.reconciler.reconcile(job_id, auth_creds, actor="api")

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
        extra = getattr(job, "_extra", {}) or {}
        res_state = getattr(job, "result_state", ResultDurabilityState.REMOTE_ONLY.value)
        is_archived = res_state in (
            ResultDurabilityState.ARCHIVED_PERSISTENT.value,
            ResultDurabilityState.ARCHIVED_LOCAL.value,
            ResultDurabilityState.ARCHIVED.value,
        )
        return {
            "job_id": job.job_id,
            "internal_job_id": extra.get("internal_job_id"),
            "title": job.title,
            "state": job.state.value,
            "local_state": extra.get("local_state", job.state.value),
            "remote_state": extra.get("last_seen_remote_state", "UNKNOWN"),
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
            "workflow_id": extra.get("workflow_id"),
            "parent_job_id": extra.get("parent_job_id"),
            "step_index": extra.get("step_index", 0),
            "step_count": extra.get("step_count", 1),
            "step_name": extra.get("step_name", "CALC"),
            "resume_required": extra.get("resume_required", False) or bool(job.state == JobState.FAILED and job.verified_checkpoint_id),
            "resume_reason": extra.get("resume_reason") or ("checkpoint_present" if job.verified_checkpoint_id else None),
            "result_state": res_state,
            "storage_durability": getattr(job, "storage_durability", "persistent_volume" if self.result_store.is_persistent else "ephemeral_local"),
            "is_durable": bool(res_state == ResultDurabilityState.ARCHIVED_PERSISTENT.value),
            "cf_sync_status": "KAGGLE_LEDGER",
            "result_available": bool(
                is_archived
                or self.result_store.exists(job.job_id, job.owner)
                or job.state == JobState.FINISHED
            ),
            "result_sha256": getattr(job, "result_sha256", None),
            "result_size_bytes": getattr(job, "result_size_bytes", None),
            "result_archived_at": getattr(job, "result_archived_at", None),
            "result_storage_reference": getattr(job, "result_storage_reference", None),
            "result_manifest_id": getattr(job, "result_manifest_id", None),
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
        """List jobs with Kaggle as the only durable source of truth.

        Kaggle's kernel list discovers chains; each chain is rebuilt/reconciled from
        STATE.json in saved output. The local SQLite store is only a warm cache and is
        never required for recovery after a Space restart.
        """
        self._last_listing_creds = creds
        auth_creds = self.ensure_authenticated(creds)
        client = KaggleClient(auth_creds)
        try:
            remote = ledger_mod.discover_jobs(client)
        except Exception as exc:
            log.warning("Kaggle bulk listing unavailable; serving warm local cache: %s", exc)
            remote = []

        merged, seen = [], set()
        for entry in remote:
            job_id = entry["job_id"]
            seen.add(job_id)
            try:
                # Always rebuild if the cache is absent; otherwise reconcile against the
                # newest saved Kaggle output. No Cloudflare metadata is consulted.
                job = self.store.get_job(job_id)
                if job is None:
                    job = ledger_mod.rebuild_from_kaggle(client, job_id)
                    self.store.put_job(job)
                if not job.is_terminal:
                    job = self.reconciler.reconcile(job_id, auth_creds, actor="list")
                described = self.describe(job)
                described["chain_slugs"] = sorted(
                    set(described.get("chain_slugs", [])) | set(entry.get("chain_slugs", []))
                )
                described["last_run"] = entry.get("last_run")
                described["state_source"] = "kaggle"
                merged.append(described)
            except Exception as exc:
                merged.append({
                    "job_id": job_id, "title": entry.get("title", job_id),
                    "state": "VERIFYING", "phase": "Verifying Kaggle state",
                    "epoch": entry.get("epoch", 0), "window": entry.get("epoch", 0) + 1,
                    "current_slug": entry.get("current_slug"),
                    "kaggle_url": entry.get("kaggle_url"),
                    "chain_slugs": entry.get("chain_slugs", []),
                    "is_terminal": False, "needs_reconcile": True,
                    "last_run": entry.get("last_run"), "state_source": "kaggle",
                    "note": f"Kaggle status check will retry: {type(exc).__name__}",
                })

        # A just-submitted kernel can take a short time to appear in Kaggle's bulk list.
        # Keep it visible from the warm cache, but label the source honestly.
        for job in self.store.list_jobs(auth_creds.username):
            if job.job_id not in seen:
                described = self.describe(job)
                described["not_returned_by_kaggle_listing"] = True
                described["deleted_on_kaggle"] = False
                described["state_source"] = "local_cache_pending_kaggle"
                merged.append(described)

        def stamp(item):
            for key in ("updated_at", "last_run", "created_at"):
                value = item.get(key)
                if isinstance(value, (int, float)):
                    return float(value)
            return 0.0
        merged.sort(key=stamp, reverse=True)
        return merged

    def ingest_kernel_update(self, payload: dict[str, Any], callback_token: str) -> dict[str, Any]:
        """Warm the local cache from a signed-by-possession Kaggle callback.

        This endpoint is intentionally not authoritative: user-facing status is still
        reconciled against Kaggle. The per-job callback token is random, lives inside the
        private Kaggle kernel header, and is checked whenever the local job record exists.
        """
        state = payload.get("state") or payload.get("document") or {}
        job_data = state.get("job") if isinstance(state, dict) else None
        if not isinstance(job_data, dict):
            raise ValidationError("kernel update is missing state.job")
        job_id = str(job_data.get("job_id") or "").strip()
        if not is_valid_slug(job_id) or not job_id.startswith(CONFIG.job_id_prefix):
            raise ValidationError("kernel update has an invalid job id")
        if not callback_token or len(callback_token) < 24:
            raise ValidationError("kernel callback token is missing or invalid")

        # Verify STATE.json's own digest before accepting it into the warm cache.
        digest = state.get("_digest")
        if digest:
            body = {k: v for k, v in state.items() if k != "_digest"}
            actual = hashlib.sha256(
                json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                           default=str).encode("utf-8")
            ).hexdigest()
            if actual != digest:
                raise IntegrityError("kernel STATE.json digest mismatch")

        current = self.store.get_job(job_id)
        if current is not None:
            expected = str((current._extra or {}).get("callback_token") or "")
            if expected and not secrets.compare_digest(expected, callback_token):
                raise ValidationError("kernel callback token does not match this job")
            manifest = current
            incoming = JobManifest.from_dict(job_data)
            # Preserve local-only callback/workflow metadata while copying durable fields.
            keep_extra = dict(current._extra or {})
            incoming._extra.update({k: v for k, v in keep_extra.items() if k not in incoming._extra})
            incoming._extra["callback_token"] = callback_token
            manifest = incoming
        else:
            manifest = JobManifest.from_dict(job_data)
            manifest._extra["callback_token"] = callback_token
        self.store.put_job(manifest)
        return {"job_id": job_id, "state": manifest.state.value,
                "epoch": manifest.epoch, "source": "kaggle_callback"}

    # -- workflow execution -------------------------------------------------
    def _workflow_result_payload(self, creds: KaggleCredentials, parent_job_id: str,
                                 step) -> tuple[str, dict[str, bytes]]:
        """Build the next step from the predecessor's actual calculated geometry."""
        parent = self.store.get_job(parent_job_id)
        zip_path, cleanup_dir = self.fetch_results(creds, parent_job_id)
        if not zip_path or not os.path.exists(zip_path):
            raise ValidationError(
                "the predecessor finished but its Kaggle output is not available yet; "
                "the workflow will retry this READY step"
            )

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                file_names = [n for n in zf.namelist() if not n.endswith("/")]
                xyz_names = [n for n in file_names if n.lower().endswith(".xyz")]
                if not xyz_names:
                    raise ValidationError(
                        "the predecessor result contains no XYZ geometry, so the next "
                        "calculation cannot be started safely"
                    )

                wanted = ""
                if parent and parent.input_filename:
                    wanted = os.path.splitext(os.path.basename(parent.input_filename))[0].lower() + ".xyz"

                def xyz_rank(name: str) -> tuple[int, str]:
                    base = os.path.basename(name).lower()
                    if wanted and base == wanted:
                        return (0, base)
                    if base == "last_geometry.xyz":
                        return (1, base)
                    if not base.endswith("_trj.xyz"):
                        return (2, base)
                    return (3, base)

                geometry_text = None
                for name in sorted(xyz_names, key=xyz_rank):
                    try:
                        raw = zf.read(name).decode("utf-8", errors="replace")
                        frames = read_trajectory_frames(raw, is_text=True)
                    except Exception:
                        frames = []
                    if frames:
                        geometry_text = frames[-1].rstrip() + "\n"
                        break
                if not geometry_text:
                    raise ValidationError(
                        "XYZ files were present in the predecessor output, but none contained "
                        "a complete geometry frame"
                    )

                geom_name = f"workflow_step_{step.step_index}_geometry.xyz"
                aux = {geom_name: geometry_text.encode("utf-8")}

                geometry_aliases = {"geometry", "xyz", "optimized_geometry", "optimised_geometry"}
                by_base = {os.path.basename(n).lower(): n for n in file_names}
                for requested in step.required_artifacts or []:
                    req = str(requested).strip()
                    if not req or req.lower() in geometry_aliases:
                        continue
                    match = by_base.get(os.path.basename(req).lower())
                    if not match:
                        matches = [n for n in file_names if n.lower().endswith(req.lower())]
                        match = matches[0] if matches else None
                    if not match:
                        raise ValidationError(
                            f"required workflow artefact '{req}' is absent from predecessor "
                            f"job {parent_job_id}"
                        )
                    data = zf.read(match)
                    if len(data) > CONFIG.runner.inline_carry_limit_bytes // 2:
                        raise ValidationError(
                            f"required workflow artefact '{req}' is too large for safe inline "
                            "handoff; put it in a Kaggle Dataset or use a restartable single job"
                        )
                    aux[os.path.basename(match)] = data

            template = step.input_template or ""
            if not template.strip():
                raise ValidationError(f"workflow step {step.step_index} has no ORCA input template")
            charge, mult = extract_charge_mult(template)
            rewritten = set_geometry(template, geom_name, charge, mult)
            if rewritten == template and not re.search(r"(?i)\*\s*xyzfile\b", template):
                raise ValidationError(
                    "the next workflow step has no replaceable ORCA geometry block; include "
                    "a '* xyz charge multiplicity ... *' block in its input template"
                )
            return rewritten, aux
        finally:
            if cleanup_dir:
                shutil.rmtree(cleanup_dir, ignore_errors=True)

    def _drive_ready_workflows(self, creds: KaggleCredentials,
                               workflow_id: str | None = None) -> list[dict[str, Any]]:
        """Launch READY workflow steps exactly once using durable Cloudflare fencing."""
        owner = creds.username.lower()
        workflows = ([self.cf_controller.get_workflow(workflow_id, owner)] if workflow_id
                     else self.cf_controller.list_user_workflows(owner))
        workflows = [wf for wf in workflows if wf is not None]
        try:
            jobs = self.cf_controller.client.list_user_jobs(owner)
        except Exception:
            jobs = []
        existing = {(j.workflow_id, int(j.step_index)): j for j in jobs if j.workflow_id}
        launched = []

        for wf in workflows:
            if wf.status in ("COMPLETED", "FAILED", "PAUSED"):
                continue
            for step in sorted(wf.steps, key=lambda s: s.step_index):
                if step.status != "READY":
                    continue

                prior = existing.get((wf.workflow_id, int(step.step_index)))
                if prior:
                    step.job_id = prior.kaggle_job_ref
                    if prior.local_state in ("COMPLETED", "REMOTE_COMPLETED"):
                        step.status = "COMPLETED"
                    elif prior.local_state in ("FAILED", "REMOTE_FAILED", "REMOTE_DELETED"):
                        step.status = "FAILED"
                    else:
                        step.status = "RUNNING"
                    wf.current_step_index = max(wf.current_step_index, step.step_index)
                    wf.updated_at = now()
                    self.cf_controller.client.put_workflow(wf)
                    break

                parent_step = None
                if step.prerequisites:
                    completed = [wf.get_step(i) for i in step.prerequisites]
                    completed = [s for s in completed if s and s.status == "COMPLETED" and s.job_id]
                    if completed:
                        parent_step = max(completed, key=lambda s: s.step_index)
                elif step.step_index > 0:
                    candidate = wf.get_step(step.step_index - 1)
                    if candidate and candidate.status == "COMPLETED" and candidate.job_id:
                        parent_step = candidate

                try:
                    if step.step_index > 0:
                        if parent_step is None:
                            raise ValidationError(
                                f"workflow step {step.step_index} is READY but has no completed "
                                "predecessor with a job id"
                            )
                        input_content, step_aux = self._workflow_result_payload(
                            creds, parent_step.job_id, step
                        )
                        parent_job_id = parent_step.job_id
                    else:
                        input_content = step.input_template
                        step_aux = {}
                        parent_job_id = None

                    launch_cfg = dict(wf.metadata.get("launch_config") or {})
                    step_datasets = list(launch_cfg.get("dataset_sources") or [])
                    if not step_datasets and parent_job_id:
                        parent_manifest = self.store.get_job(parent_job_id)
                        if parent_manifest:
                            step_datasets = list(parent_manifest.dataset_sources or [])
                    sub = self.submit(
                        creds,
                        input_filename=f"{slugify(step.step_name) or 'step'}-{step.step_index}.inp",
                        input_content=input_content,
                        job_name=f"{wf.title}-{step.step_name}",
                        aux_files=step_aux,
                        dataset_sources=step_datasets,
                        orca_link=launch_cfg.get("orca_link") or None,
                        idempotency_key=f"workflow:{wf.workflow_id}:step:{step.step_index}",
                        workflow_id=wf.workflow_id,
                        parent_job_id=parent_job_id,
                        step_index=step.step_index,
                        step_count=len(wf.steps),
                        step_name=step.step_name,
                    )
                except Exception as exc:
                    step.result_data["last_launch_error"] = f"{type(exc).__name__}: {exc}"
                    step.result_data["last_launch_attempt_at"] = now()
                    wf.updated_at = now()
                    self.cf_controller.client.put_workflow(wf)
                    log.warning("Could not launch READY workflow %s step %s: %s",
                                wf.workflow_id, step.step_index, exc)
                    break

                step.job_id = sub.job_id
                step.status = "RUNNING"
                step.result_data.pop("last_launch_error", None)
                wf.status = "IN_PROGRESS"
                wf.current_step_index = step.step_index
                wf.updated_at = now()
                self.cf_controller.client.put_workflow(wf)
                launched.append({"workflow_id": wf.workflow_id,
                                 "step_index": step.step_index,
                                 "job_id": sub.job_id})
                break
        return launched

    def _reconcile_and_drive_workflows(self, creds: KaggleCredentials) -> None:
        """Watchdog hook: advance workflows even when the browser is closed."""
        try:
            self.cf_controller.reconcile_user_session(creds, KaggleClient(creds))
            self._drive_ready_workflows(creds)
        except Exception as exc:
            log.warning("Background workflow reconciliation degraded: %s", exc)

    # -- workflows ---------------------------------------------------------
    def submit_workflow(
        self,
        creds: KaggleCredentials,
        title: str,
        steps: list[dict[str, Any]],
        *,
        aux_files: dict[str, bytes] | None = None,
        dataset_sources: list[str] | None = None,
        orca_link: str | None = None,
    ) -> dict[str, Any]:
        """Creates a multi-step workflow and launches step 0."""
        if not steps:
            raise ValidationError("A workflow must define at least one step.")

        BROKER.remember(creds)
        wf = self.cf_controller.register_workflow(title=title, owner=creds.username, step_specs=steps)
        wf.metadata["launch_config"] = {
            "dataset_sources": list(dataset_sources or []),
            "orca_link": orca_link,
        }
        self.cf_controller.client.put_workflow(wf)

        # Launch Step 0
        step0 = wf.steps[0]
        step0_input = step0.input_template
        if not step0_input.strip():
            raise ValidationError(f"Step 0 ({step0.step_name}) has no input content.")

        sub_res = self.submit(
            creds,
            input_filename=f"{step0.step_name.lower()}.inp",
            input_content=step0_input,
            job_name=f"{title}-{step0.step_name}",
            aux_files=aux_files,
            dataset_sources=dataset_sources,
            orca_link=orca_link,
            workflow_id=wf.workflow_id,
            step_index=0,
            step_count=len(wf.steps),
            step_name=step0.step_name,
        )
        step0.job_id = sub_res.job_id
        step0.status = "RUNNING"
        wf.status = "IN_PROGRESS"
        self.cf_controller.client.put_workflow(wf)
        return {
            "ok": True,
            "workflow_id": wf.workflow_id,
            "workflow": wf.to_dict(),
            "step_0_job": sub_res.to_dict(),
        }

    def get_workflow(self, creds: KaggleCredentials, workflow_id: str) -> dict[str, Any] | None:
        BROKER.remember(creds)
        try:
            self.cf_controller.reconcile_user_session(creds, KaggleClient(creds))
            self._drive_ready_workflows(creds, workflow_id=workflow_id)
        except Exception as exc:
            log.warning("Workflow reconciliation degraded for %s: %s", workflow_id, exc)
        wf = self.cf_controller.get_workflow(workflow_id, creds.username)
        return wf.to_dict() if wf else None

    def list_workflows(self, creds: KaggleCredentials) -> list[dict[str, Any]]:
        BROKER.remember(creds)
        try:
            self.cf_controller.reconcile_user_session(creds, KaggleClient(creds))
            self._drive_ready_workflows(creds)
        except Exception as exc:
            log.warning("Workflow listing reconciliation degraded: %s", exc)
        wfs = self.cf_controller.list_user_workflows(creds.username)
        return [w.to_dict() for w in wfs]


    # -- results -----------------------------------------------------------
    def fetch_results(self, creds: KaggleCredentials, job_id: str,
                      *, slug: str | None = None) -> tuple[str | None, str]:
        """Downloads a window's output and returns `(zip_path, cleanup_dir)`.

        Defaults to the *newest* window, since that is where a finished job's
        results are. Earlier windows remain individually addressable, because a
        chemist sometimes wants the trajectory from a specific stage."""
        if not is_valid_slug(job_id):
            raise ValidationError("invalid job id")
        auth_creds = self.ensure_authenticated(creds)

        job = self.store.get_job(job_id)
        if job and job.owner != auth_creds.username.lower():
            raise ValidationError(f"Access denied: Job {job_id} does not belong to user {auth_creds.username}")

        target = slug or (job.current_slug if job else job_id)
        if not is_valid_slug(target):
            raise ValidationError("invalid window slug")

        # 1. Check if already preserved in ResultArtifactStore (strictly scoped to authenticated user)
        archived_zip, manifest = self.result_store.retrieve(job_id, auth_creds.username)
        if archived_zip and os.path.exists(archived_zip):
            return archived_zip, ""

        # 2. Otherwise download from Kaggle
        client = KaggleClient(auth_creds)
        out_dir = client.fetch_output(target, timeout=900, page_size=200)

        zip_path = os.path.join(out_dir, "results.zip")
        if os.path.exists(zip_path) and os.path.getsize(zip_path) > 0 and zipfile.is_zipfile(zip_path):
            return zip_path, out_dir

        # No packaged archive or corrupt results.zip: bundle loose output files.
        fallback = os.path.join(out_dir, "_partial_results.zip")
        bundled = False
        with zipfile.ZipFile(fallback, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in sorted(os.listdir(out_dir)):
                path = os.path.join(out_dir, name)
                if os.path.isfile(path) and path not in (fallback, zip_path):
                    zf.write(path, name)
                    bundled = True
        if bundled and zipfile.is_zipfile(fallback):
            return fallback, out_dir
        shutil.rmtree(out_dir, ignore_errors=True)
        return None, out_dir

    def archive_job_results(
        self,
        creds: KaggleCredentials,
        job_id: str,
        *,
        slug: str | None = None,
    ) -> ResultManifest | None:
        """Downloads, validates, checksums, and archives result artifacts durably."""
        if not is_valid_slug(job_id):
            raise ValidationError("invalid job id")
        auth_creds = self.ensure_authenticated(creds)
        job = self.store.get_job(job_id)
        if job is None:
            raise NotFoundError(f"Job {job_id} not found")
        if job.owner != auth_creds.username.lower():
            raise ValidationError(f"Access denied: Job {job_id} does not belong to user {auth_creds.username}")

        # 1. Transition to DOWNLOADING
        job.result_state = ResultDurabilityState.DOWNLOADING.value
        self.store.put_job(job)
        try:
            self.cf_controller.sync_job_state(job)
        except Exception:
            pass

        # 2. Retrieve output from Kaggle (or local store)
        zip_path, cleanup_dir = self.fetch_results(auth_creds, job_id, slug=slug)
        if not zip_path or not os.path.exists(zip_path):
            job.result_state = ResultDurabilityState.DOWNLOAD_FAILED.value
            self.store.put_job(job)
            try:
                self.cf_controller.sync_job_state(job)
            except Exception:
                pass
            return None

        job.result_downloaded_at = now()
        job.result_state = ResultDurabilityState.VALIDATING.value
        self.store.put_job(job)

        try:
            # 3. Transition to ARCHIVING & store into ResultArtifactStore
            job.result_state = ResultDurabilityState.ARCHIVING.value
            self.store.put_job(job)

            manifest = self.result_store.store(
                job_id=job.job_id,
                owner=auth_creds.username,
                raw_zip_or_dir_path=zip_path,
                provenance={
                    "title": job.title,
                    "job_kind": job.job_kind,
                    "epoch": job.epoch,
                    "input_filename": job.input_filename,
                    "workflow_id": getattr(job, "_extra", {}).get("workflow_id"),
                    "step_name": getattr(job, "_extra", {}).get("step_name"),
                },
            )

            # 4. Mark ARCHIVED and record hashes/references
            job.result_state = manifest.status
            job.storage_durability = self.result_store.storage_durability
            job.result_sha256 = manifest.bundle_sha256
            job.result_size_bytes = manifest.total_size_bytes
            job.result_storage_reference = f"{auth_creds.username}/{job.job_id}/results.zip"
            job.result_manifest_id = manifest.manifest_id
            job.result_archived_at = manifest.archived_at
            self.store.put_job(job)

            try:
                self.cf_controller.sync_job_state(job)
            except Exception as exc:
                log.warning("Could not sync archived result state to Cloudflare: %s", exc)

            log_event(
                log,
                "result_archived",
                "Job result successfully verified and archived durably",
                job_id=job.job_id,
                sha256=manifest.bundle_sha256,
                size_bytes=manifest.total_size_bytes,
                result_state=job.result_state,
            )
            return manifest
        except Exception as exc:
            log.warning("Archiving job result failed for %s: %s", job_id, exc)
            job.result_state = ResultDurabilityState.ARCHIVE_FAILED.value
            self.store.put_job(job)
            try:
                self.cf_controller.sync_job_state(job)
            except Exception:
                pass
            return None
        finally:
            if cleanup_dir and cleanup_dir != self.result_store._job_dir(auth_creds.username, job_id):
                shutil.rmtree(cleanup_dir, ignore_errors=True)

    # -- lifecycle ---------------------------------------------------------
    def cancel(self, creds: KaggleCredentials, job_id: str) -> dict:
        auth_creds = self.ensure_authenticated(creds)
        job = self.store.require_job(job_id)
        if job.owner != auth_creds.username.lower():
            raise ValidationError(f"Access denied: Job {job_id} does not belong to user {auth_creds.username}")
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
        auth_creds = self.ensure_authenticated(creds)
        job = self.store.require_job(job_id)
        if job.owner != auth_creds.username.lower():
            raise ValidationError(f"Access denied: Job {job_id} does not belong to user {auth_creds.username}")
        if job.state is not JobState.FAILED:
            raise ValidationError("only a failed job can be resumed",
                                  state=job.state.value)
        job = self.reconciler.transition(job, Trigger.OPERATOR_RESUME, actor="operator",
                                          reason="manual resume requested",
                                          checkpoint_id=job.verified_checkpoint_id)
        self.store.put_job(job, expected_version=job._extra.get("_version"))
        return self.status(auth_creds, job_id)

    def delete(self, creds: KaggleCredentials, job_id: str) -> dict:
        """Deletes every window in the chain, then the local record.

        Deleting only the newest window is what leaves orphan kernels behind:
        the next sign-in rebuilds the job list from Kaggle, finds the older
        windows, and the job the user deleted reappears."""
        if not is_valid_slug(job_id):
            raise ValidationError("invalid job id")
        auth_creds = self.ensure_authenticated(creds)
        client = KaggleClient(auth_creds)

        job = self.store.get_job(job_id)
        if job and job.owner != auth_creds.username.lower():
            raise ValidationError(f"Access denied: Job {job_id} does not belong to user {auth_creds.username}")

        slugs = list(job.chain_slugs) if job else []
        if not slugs:
            chains = ledger_mod.group_chains(client.list_kernels())
            slugs = [w["slug"] for w in chains.get(job_id, [])] or [job_id]

        deleted = []
        for slug in slugs:
            client.delete_kernel(slug)
            deleted.append(slug)

        self.store.delete_job(job_id)
        self.result_store.delete(job_id, auth_creds.username)
        try:
            self.cf_controller.client.delete_job(job_id, auth_creds.username)
        except Exception:
            pass
        log_event(log, "job_deleted", "job and its whole window chain removed",
                  job_id=job_id, deleted=len(deleted), failed=0)
        return {"job_id": job_id, "deleted": deleted, "failed": []}

    # -- ops ---------------------------------------------------------------
    def sweep_now(self, owner: str | None = None) -> dict:
        return self.watchdog.sweep(owner=owner).to_dict()

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


def reset_service() -> None:
    """Explicitly shutdown and discard the process singleton (for tests and clean restarts)."""
    global _service
    with _service_lock:
        if _service is not None:
            try:
                _service.shutdown()
            except Exception:
                pass
            _service = None

