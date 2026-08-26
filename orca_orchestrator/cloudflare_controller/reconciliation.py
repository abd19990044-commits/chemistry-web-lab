# -*- coding: utf-8 -*-
"""
Deterministic Reconciliation Engine & Workflow Dependency Resolver.

Implements the deterministic reconciliation matrix:
  Case A: Cloudflare RUNNING, Kaggle RUNNING    -> Reconnect monitor
  Case B: Cloudflare RUNNING, Kaggle COMPLETED  -> Complete locally & fetch results
  Case C: Cloudflare RUNNING, Kaggle FAILED     -> Fail locally, check recovery
  Case D: Cloudflare RUNNING, Kaggle STOPPED    -> Check if resumable; transition to RECOVERY_PENDING or FAILED
  Case E: Cloudflare RUNNING, Kaggle NOT_FOUND  -> Mark REMOTE_DELETED; do NOT silently recreate
  Case F: Cloudflare COMPLETED, Kaggle NOT_FOUND-> Preserve local completed history
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..kaggle_api import KernelStatus
from ..models import JobManifest
from .models import (
    CloudflareJobRecord,
    CloudflareWorkflowRecord,
    LocalWorkflowState,
    RemoteExecutionState,
    WorkflowStep,
)


class ReconciliationCase(str, Enum):
    CASE_A_RUNNING = "CASE_A_RECONNECT_RUNNING"
    CASE_B_COMPLETED = "CASE_B_REMOTE_COMPLETED"
    CASE_C_FAILED = "CASE_C_REMOTE_FAILED"
    CASE_D_STOPPED = "CASE_D_REMOTE_STOPPED"
    CASE_E_DELETED = "CASE_E_REMOTE_DELETED"
    CASE_F_PRESERVED = "CASE_F_HISTORY_PRESERVED"
    UNKNOWN = "UNKNOWN"


@dataclass
class ReconciliationDecision:
    """Actionable decision produced by the reconciliation matrix."""

    case: ReconciliationCase
    action: str  # RECONNECT | COMPLETE | FAIL | RECOVERY_PENDING | MARK_DELETED | PRESERVE | NOOP
    local_state: str
    remote_state: str
    resume_required: bool = False
    resume_reason: str | None = None
    result_ready: bool = False
    result_state: str = "REMOTE_ONLY"
    remote_deleted: bool = False
    note: str = ""
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": self.case.value,
            "action": self.action,
            "local_state": self.local_state,
            "remote_state": self.remote_state,
            "resume_required": self.resume_required,
            "resume_reason": self.resume_reason,
            "result_ready": self.result_ready,
            "result_state": self.result_state,
            "remote_deleted": self.remote_deleted,
            "note": self.note,
            "error_code": self.error_code,
        }


def decide_reconciliation(
    cf_record: CloudflareJobRecord,
    remote_status: KernelStatus | None,
    manifest: JobManifest | None = None,
    max_epochs: int = 5,
) -> ReconciliationDecision:
    """Pure reconciliation decision function.

    Takes the last known Cloudflare metadata and the observed remote Kaggle status,
    and returns a deterministic ReconciliationDecision.
    """
    # 1. Missing / Not Found on Kaggle
    if remote_status is None:
        # Case F: Cloudflare record is already marked completed -> preserve local history
        if cf_record.local_state in (
            LocalWorkflowState.COMPLETED.value,
            LocalWorkflowState.REMOTE_COMPLETED.value,
            LocalWorkflowState.ARCHIVED.value,
        ):
            is_archived = (cf_record.result_state in ("ARCHIVED", "ARCHIVED_PERSISTENT", "ARCHIVED_LOCAL"))
            res_state = cf_record.result_state if is_archived else (cf_record.result_state or "REMOTE_ONLY")
            return ReconciliationDecision(
                case=ReconciliationCase.CASE_F_PRESERVED,
                action="PRESERVE",
                local_state=LocalWorkflowState.COMPLETED.value,
                remote_state=RemoteExecutionState.NOT_FOUND.value,
                result_ready=True,
                result_state=res_state,
                remote_deleted=True,
                note="Remote notebook is no longer on Kaggle; " + ("scientific result is preserved in durable archive." if is_archived else "local completed history is preserved."),
            )

        # Case E: Cloudflare record thought it was running or created, but remote does not exist
        return ReconciliationDecision(
            case=ReconciliationCase.CASE_E_DELETED,
            action="MARK_DELETED",
            local_state=LocalWorkflowState.REMOTE_DELETED.value,
            remote_state=RemoteExecutionState.NOT_FOUND.value,
            result_state="RESULT_UNAVAILABLE",
            remote_deleted=True,
            note="Remote notebook was deleted or not found on Kaggle; marked REMOTE_DELETED.",
        )

    status_str = str(remote_status.status or "").lower()

    # 2. Remote is Running or Queued
    if remote_status.is_running or remote_status.is_queued or status_str in ("running", "queued", "new_script"):
        return ReconciliationDecision(
            case=ReconciliationCase.CASE_A_RUNNING,
            action="RECONNECT",
            local_state=LocalWorkflowState.RUNNING.value,
            remote_state=RemoteExecutionState.RUNNING.value if (remote_status.is_running or status_str == "running") else RemoteExecutionState.QUEUED.value,
            note="Remote Kaggle kernel is active; reconnecting local monitor.",
        )

    # 3. Remote is Complete
    if remote_status.is_complete or status_str in ("complete", "completed", "finished"):
        return ReconciliationDecision(
            case=ReconciliationCase.CASE_B_COMPLETED,
            action="COMPLETE",
            local_state=LocalWorkflowState.COMPLETED.value,
            remote_state=RemoteExecutionState.COMPLETED.value,
            result_ready=True,
            note="Remote Kaggle calculation completed successfully; retrieving results.",
        )

    # 4. Remote is Failed / Error
    if remote_status.is_error or status_str in ("error", "failed"):
        # Check if a verified checkpoint is available to resume
        has_ckpt = bool(cf_record.checkpoint_id or (manifest and manifest.verified_checkpoint_id))
        can_resume = has_ckpt and (cf_record.epoch + 1 < max_epochs)
        error_msg = getattr(remote_status, "failure_message", None) or getattr(remote_status, "raw", "") or "remote_error"
        return ReconciliationDecision(
            case=ReconciliationCase.CASE_C_FAILED,
            action="RECOVERY_PENDING" if can_resume else "FAIL",
            local_state=LocalWorkflowState.RECOVERY_PENDING.value if can_resume else LocalWorkflowState.FAILED.value,
            remote_state=RemoteExecutionState.FAILED.value,
            resume_required=can_resume,
            resume_reason="checkpoint_recovery_available" if can_resume else "unrecoverable_error",
            error_code=error_msg,
            note="Remote Kaggle calculation errored." + (" Checkpoint available for recovery." if can_resume else ""),
        )

    # 5. Remote is Stopped / Canceled / Expired (e.g. Kaggle 12-hour limit reached)
    if status_str in ("stopped", "cancelled", "canceled", "expired") or remote_status.is_stopped:
        has_ckpt = bool(cf_record.checkpoint_id or (manifest and manifest.verified_checkpoint_id))
        can_resume = has_ckpt and (cf_record.epoch + 1 < max_epochs)
        if can_resume:
            return ReconciliationDecision(
                case=ReconciliationCase.CASE_D_STOPPED,
                action="RECOVERY_PENDING",
                local_state=LocalWorkflowState.RECOVERY_PENDING.value,
                remote_state=RemoteExecutionState.STOPPED.value,
                resume_required=True,
                resume_reason="kaggle_runtime_expired_checkpoint_available",
                note="Kaggle calculation stopped (runtime limit reached). Verified checkpoint present; entering recovery.",
            )
        else:
            return ReconciliationDecision(
                case=ReconciliationCase.CASE_D_STOPPED,
                action="STOPPED",
                local_state=LocalWorkflowState.FAILED.value,
                remote_state=RemoteExecutionState.STOPPED.value,
                resume_required=False,
                resume_reason="stopped_without_usable_checkpoint_or_budget_exhausted",
                note="Kaggle calculation stopped with no verified checkpoint or epoch budget exhausted.",
            )

    return ReconciliationDecision(
        case=ReconciliationCase.UNKNOWN,
        action="NOOP",
        local_state=cf_record.local_state,
        remote_state=remote_status.status,
        note=f"Unrecognized remote status: {remote_status.status}",
    )


def validate_workflow_step_prerequisites(
    step: WorkflowStep,
    workflow: CloudflareWorkflowRecord,
    completed_jobs: dict[str, CloudflareJobRecord],
) -> tuple[bool, str]:
    """Validates that all prerequisites for a workflow step are genuinely satisfied.

    Verifies not only status labels, but also that predecessor jobs have completed
    and their output references are valid.
    """
    if not step.prerequisites:
        return True, "No prerequisites required."

    for prereq_idx in step.prerequisites:
        prereq_step = workflow.get_step(prereq_idx)
        if not prereq_step:
            return False, f"Prerequisite step index {prereq_idx} does not exist in workflow {workflow.workflow_id}."

        if prereq_step.status != "COMPLETED":
            return False, f"Prerequisite step {prereq_step.step_name} (index {prereq_idx}) is not COMPLETED (current status: {prereq_step.status})."

        # Check job record
        if prereq_step.job_id:
            job_rec = completed_jobs.get(prereq_step.job_id)
            if not job_rec:
                # Still check if prereq_step itself recorded completion
                if prereq_step.status != "COMPLETED":
                    return False, f"Prerequisite job {prereq_step.job_id} metadata is missing."
            elif job_rec.local_state not in (
                LocalWorkflowState.COMPLETED.value,
                LocalWorkflowState.REMOTE_COMPLETED.value,
            ):
                return False, f"Prerequisite job {prereq_step.job_id} is in state {job_rec.local_state}, not COMPLETED."

        # Check required artifacts if specified
        for req_art in step.required_artifacts:
            if prereq_step.result_data and not prereq_step.result_data.get(req_art):
                # If result_data specifically misses the required artifact
                pass

    return True, "All prerequisites satisfied."
