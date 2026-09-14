# -*- coding: utf-8 -*-
"""
CloudflareController Facade.

Integrates the Cloudflare persistent metadata control plane with the local
JobStore, Reconciler, and Watchdog.
"""
from __future__ import annotations

import logging
from typing import Any

from ..credentials import KaggleCredentials
from ..kaggle_api import KaggleClient, KernelStatus
from ..models import CheckpointManifest, JobManifest, now
from ..states import JobState
from ..store import JobStore, get_store
from .client import CloudflareClientProtocol, get_cloudflare_client
from .config import CLOUDFLARE_CONFIG, CloudflareConfig
from .models import (
    CloudflareCheckpointRecord,
    CloudflareJobRecord,
    CloudflareWorkflowRecord,
    LocalWorkflowState,
    RemoteExecutionState,
    WorkflowStep,
    _clean_owner,
    new_uuid,
)
from .reconciliation import (
    ReconciliationCase,
    ReconciliationDecision,
    decide_reconciliation,
    validate_workflow_step_prerequisites,
)

log = logging.getLogger("orca.cloudflare_controller")


class CloudflareController:
    """The central control facade managing external metadata persistence and recovery."""

    def __init__(
        self,
        client: CloudflareClientProtocol | None = None,
        store: JobStore | None = None,
        config: CloudflareConfig | None = None,
    ) -> None:
        self.config = config or CLOUDFLARE_CONFIG
        self.client = client or get_cloudflare_client(self.config)
        self.store = store or get_store()

    # -- job lifecycle sync ------------------------------------------------

    def register_job(
        self,
        manifest: JobManifest,
        *,
        workflow_id: str | None = None,
        parent_job_id: str | None = None,
        step_index: int = 0,
        step_count: int = 1,
        step_name: str = "CALC",
    ) -> CloudflareJobRecord:
        """Persists a new or existing job's metadata to Cloudflare."""
        record = CloudflareJobRecord.from_job_manifest(
            manifest,
            workflow_id=workflow_id,
            parent_job_id=parent_job_id,
            step_index=step_index,
            step_count=step_count,
            step_name=step_name,
        )
        saved = self.client.put_job(record)
        is_durable = getattr(self.client, "is_durable", False) and getattr(saved, "cf_sync_status", "") == "SYNCED"
        if is_durable:
            log.info(
                "Registered job in Cloudflare control plane",
                extra={
                    "event": "cf_job_registered",
                    "internal_job_id": saved.internal_job_id,
                    "job_id": saved.kaggle_job_ref,
                    "owner": saved.kaggle_username,
                    "durable": True,
                    "backend": "cloudflare",
                },
            )
        else:
            log.warning(
                "Cloudflare unavailable or disabled; job metadata retained in degraded in-memory fallback (non-durable)",
                extra={
                    "event": "cf_degraded_fallback",
                    "internal_job_id": saved.internal_job_id,
                    "job_id": saved.kaggle_job_ref,
                    "owner": saved.kaggle_username,
                    "durable": False,
                    "backend": "memory_fallback",
                },
            )
        return saved

    def sync_job_state(
        self,
        manifest: JobManifest,
        remote_status: KernelStatus | None = None,
    ) -> CloudflareJobRecord:
        """Synchronizes an active or transitioning job's state to Cloudflare."""
        owner = _clean_owner(manifest.owner)
        existing = self.client.get_job(manifest.job_id, owner)
        internal_id = existing.internal_job_id if existing else (manifest._extra.get("internal_job_id") or new_uuid())
        wf_id = existing.workflow_id if existing else manifest._extra.get("workflow_id")
        parent_id = existing.parent_job_id if existing else manifest._extra.get("parent_job_id")
        s_idx = existing.step_index if existing else manifest._extra.get("step_index", 0)
        s_cnt = existing.step_count if existing else manifest._extra.get("step_count", 1)
        s_name = existing.step_name if existing else manifest._extra.get("step_name", "CALC")

        remote_state = RemoteExecutionState.UNKNOWN.value
        if remote_status:
            remote_state = remote_status.status

        record = CloudflareJobRecord.from_job_manifest(
            manifest,
            workflow_id=wf_id,
            parent_job_id=parent_id,
            step_index=s_idx,
            step_count=s_cnt,
            step_name=s_name,
            internal_job_id=internal_id,
            last_seen_remote_state=remote_state,
        )
        saved = self.client.put_job(record)
        manifest.cf_sync_status = getattr(saved, "cf_sync_status", "DEGRADED_UNSYNCED")
        return saved

    def record_checkpoint(self, ckpt: CheckpointManifest, owner: str) -> bool:
        """Persists checkpoint metadata reference to Cloudflare. Returns True ONLY if durably written."""
        rec = CloudflareCheckpointRecord.from_checkpoint_manifest(ckpt)
        return self.client.record_checkpoint(rec, owner)

    # -- workflow management -----------------------------------------------

    def register_workflow(
        self,
        title: str,
        owner: str,
        step_specs: list[dict[str, Any]],
        workflow_id: str | None = None,
    ) -> CloudflareWorkflowRecord:
        """Creates and persists a multi-step workflow structure."""
        owner = _clean_owner(owner)
        w_id = workflow_id or new_uuid()
        steps = []
        for i, spec in enumerate(step_specs):
            step = WorkflowStep(
                step_index=i,
                step_name=spec.get("step_name", f"STEP_{i}"),
                job_id=spec.get("job_id"),
                input_template=spec.get("input_template", ""),
                status=spec.get("status", "PENDING" if i > 0 else "READY"),
                prerequisites=list(spec.get("prerequisites", [i - 1] if i > 0 else [])),
                required_artifacts=list(spec.get("required_artifacts", [])),
                result_data=dict(spec.get("result_data", {})),
            )
            steps.append(step)

        wf = CloudflareWorkflowRecord(
            workflow_id=w_id,
            kaggle_username=owner,
            title=title,
            status="CREATED",
            steps=steps,
            current_step_index=0,
            created_at=now(),
            updated_at=now(),
        )
        return self.client.put_workflow(wf)

    def get_workflow(self, workflow_id: str, owner: str) -> CloudflareWorkflowRecord | None:
        return self.client.get_workflow(workflow_id, owner)

    def list_user_workflows(self, owner: str) -> list[CloudflareWorkflowRecord]:
        return self.client.list_user_workflows(owner)

    # -- startup reconciliation & user reconnect flow ----------------------

    def reconcile_user_session(
        self,
        creds: KaggleCredentials,
        kaggle_client: KaggleClient | None = None,
    ) -> dict[str, Any]:
        """Executes full restart-recovery and state reconciliation upon user authentication.

        Flow:
          1. Authenticated Kaggle identity is established (creds.username).
          2. Retrieve all Cloudflare job and workflow metadata for this user.
          3. Query Kaggle for current remote execution status of each referenced job.
          4. Execute deterministic reconciliation matrix (Case A..F).
          5. Restore local JobStore cache with updated manifests.
          6. Identify interrupted resumable jobs (e.g. stopped by Kaggle runtime expiration)
             and transition them to RECOVERY_PENDING.
          7. Reconcile workflow state graphs and validate prerequisite steps.
          8. Sync updated states back to Cloudflare.
        """
        owner = _clean_owner(creds.username)
        client = kaggle_client or KaggleClient(creds)

        # 1. Fetch Cloudflare metadata for this user
        cf_jobs = self.client.list_user_jobs(owner)
        cf_workflows = self.client.list_user_workflows(owner)

        reconciled_jobs = []
        resumed_jobs = []
        jobs_by_ref: dict[str, CloudflareJobRecord] = {}

        # 2. Reconcile each job
        for cf_job in cf_jobs:
            job_ref = cf_job.kaggle_job_ref
            manifest = self.store.get_job(job_ref)

            # Query Kaggle for live remote status
            remote_status: KernelStatus | None = None
            status_query_failed = False
            try:
                # Query newest window slug or base slug
                slug_to_check = cf_job.chain_slugs[-1] if cf_job.chain_slugs else job_ref
                remote_status = client.kernel_exists(slug_to_check)
            except Exception as exc:
                status_query_failed = True
                log.warning("Could not query Kaggle status for job %s: %s", job_ref, exc)

            # A transport/API failure is UNKNOWN, not NOT_FOUND. Preserve the last durable
            # Cloudflare state and retry later. Only a successful exact lookup returning
            # None is allowed to mean that Kaggle no longer has the kernel.
            if status_query_failed:
                decision = ReconciliationDecision(
                    case=ReconciliationCase.UNKNOWN,
                    action="NOOP",
                    local_state=cf_job.local_state,
                    remote_state=cf_job.last_seen_remote_state or RemoteExecutionState.UNKNOWN.value,
                    resume_required=cf_job.resume_required,
                    resume_reason=cf_job.resume_reason,
                    result_state=cf_job.result_state or "REMOTE_ONLY",
                    remote_deleted=cf_job.remote_deleted,
                    note="Kaggle status lookup failed transiently; preserved last durable state.",
                    error_code=cf_job.last_error_code,
                )
            else:
                decision = decide_reconciliation(
                    cf_record=cf_job,
                    remote_status=remote_status,
                    manifest=manifest,
                )

            # Update Cloudflare record with decision
            cf_job.local_state = decision.local_state
            cf_job.last_seen_remote_state = decision.remote_state
            cf_job.last_reconciliation_at = now()
            cf_job.resume_required = decision.resume_required
            cf_job.resume_reason = decision.resume_reason
            cf_job.remote_deleted = decision.remote_deleted
            if decision.result_state:
                cf_job.result_state = decision.result_state
            cf_job.updated_at = now()
            if decision.error_code:
                cf_job.last_error_code = decision.error_code

            # Restore or update local store
            if manifest is None:
                manifest = cf_job.to_job_manifest()
            else:
                # Sync manifest with reconciled status
                if decision.action == "COMPLETE":
                    manifest.enter_state(JobState.FINISHED)
                elif decision.action == "FAIL":
                    manifest.enter_state(JobState.FAILED)
                elif decision.action == "RECOVERY_PENDING":
                    manifest.enter_state(JobState.FAILED)

                if cf_job.result_state:
                    manifest.result_state = cf_job.result_state
                if cf_job.result_sha256:
                    manifest.result_sha256 = cf_job.result_sha256
                if cf_job.result_size_bytes:
                    manifest.result_size_bytes = cf_job.result_size_bytes
                if cf_job.result_storage_reference:
                    manifest.result_storage_reference = cf_job.result_storage_reference
                if cf_job.result_manifest_id:
                    manifest.result_manifest_id = cf_job.result_manifest_id
                if cf_job.result_archived_at:
                    manifest.result_archived_at = cf_job.result_archived_at

            self.store.put_job(manifest)
            self.client.put_job(cf_job)

            jobs_by_ref[job_ref] = cf_job
            reconciled_jobs.append({
                "job_id": job_ref,
                "internal_job_id": cf_job.internal_job_id,
                "decision": decision.to_dict(),
                "manifest": manifest.to_dict(),
            })

            if decision.resume_required:
                resumed_jobs.append(job_ref)

        # 3. Reconcile workflows
        reconciled_workflows = []
        for wf in cf_workflows:
            all_completed = True
            has_failed = False
            for step in wf.steps:
                if step.job_id and step.job_id in jobs_by_ref:
                    j_rec = jobs_by_ref[step.job_id]
                    if j_rec.local_state in (LocalWorkflowState.COMPLETED.value, LocalWorkflowState.REMOTE_COMPLETED.value):
                        step.status = "COMPLETED"
                    elif j_rec.local_state in (
                        LocalWorkflowState.FAILED.value,
                        LocalWorkflowState.REMOTE_FAILED.value,
                        LocalWorkflowState.REMOTE_DELETED.value,
                        LocalWorkflowState.REMOTE_STOPPED.value,
                    ):
                        step.status = "FAILED"
                        has_failed = True
                    elif j_rec.local_state in (LocalWorkflowState.RUNNING.value, LocalWorkflowState.WAITING_FOR_REMOTE.value):
                        step.status = "RUNNING"
                        all_completed = False
                    elif j_rec.local_state == LocalWorkflowState.RECOVERY_PENDING.value:
                        step.status = "RECOVERY_PENDING"
                        all_completed = False
                elif step.status != "COMPLETED":
                    all_completed = False

            # Check if next pending step can now advance
            for step in wf.steps:
                if step.status == "PENDING":
                    ready, reason = validate_workflow_step_prerequisites(step, wf, jobs_by_ref)
                    if ready:
                        step.status = "READY"

            if all_completed and len(wf.steps) > 0:
                wf.status = "COMPLETED"
            elif has_failed:
                wf.status = "FAILED"
            else:
                wf.status = "IN_PROGRESS"

            wf.updated_at = now()
            self.client.put_workflow(wf)
            reconciled_workflows.append(wf.to_dict())

        return {
            "ok": True,
            "owner": owner,
            "reconciled_jobs_count": len(reconciled_jobs),
            "reconciled_jobs": reconciled_jobs,
            "resumed_jobs": resumed_jobs,
            "workflows": reconciled_workflows,
        }
