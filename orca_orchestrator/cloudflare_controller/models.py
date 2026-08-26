# -*- coding: utf-8 -*-
"""
Data model for Cloudflare persistent control plane.

Cloudflare persists job and workflow metadata across Hugging Face restarts.
It NEVER receives or stores Kaggle credentials, API keys, passwords, or secrets.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from ..models import CheckpointManifest, CheckpointStatus, JobManifest, now
from ..states import JobState


class LocalWorkflowState(str, Enum):
    """Canonical local workflow and recovery states."""

    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    CHECKPOINTED = "CHECKPOINTED"
    WAITING_FOR_REMOTE = "WAITING_FOR_REMOTE"
    REMOTE_COMPLETED = "REMOTE_COMPLETED"
    REMOTE_FAILED = "REMOTE_FAILED"
    REMOTE_STOPPED = "REMOTE_STOPPED"
    REMOTE_DELETED = "REMOTE_DELETED"
    RECOVERY_PENDING = "RECOVERY_PENDING"
    RECOVERING = "RECOVERING"
    RESUMED = "RESUMED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ARCHIVED = "ARCHIVED"

    @property
    def is_terminal(self) -> bool:
        return self in (
            LocalWorkflowState.COMPLETED,
            LocalWorkflowState.FAILED,
            LocalWorkflowState.ARCHIVED,
            LocalWorkflowState.REMOTE_DELETED,
        )

    @property
    def is_active(self) -> bool:
        return not self.is_terminal


class RemoteExecutionState(str, Enum):
    """Canonical representation of observed remote Kaggle execution state."""

    UNKNOWN = "UNKNOWN"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"
    NOT_FOUND = "NOT_FOUND"
    MISSING = "MISSING"


def _clean_owner(owner: str) -> str:
    return (owner or "").strip().lower()


def new_uuid() -> str:
    return str(uuid.uuid4())


@dataclass
class CloudflareJobRecord:
    """Persistent job metadata stored in Cloudflare.

    Contains NO Kaggle secrets or API keys.
    """

    internal_job_id: str
    kaggle_username: str
    kaggle_job_ref: str
    kaggle_url: str = ""
    title: str = ""
    input_filename: str = ""
    job_kind: str = "unknown"
    created_at: float = field(default_factory=now)
    updated_at: float = field(default_factory=now)
    last_reconciliation_at: float = 0.0
    last_seen_remote_state: str = RemoteExecutionState.UNKNOWN.value
    local_state: str = LocalWorkflowState.CREATED.value
    workflow_id: str | None = None
    parent_job_id: str | None = None
    step_index: int = 0
    step_count: int = 1
    step_name: str = "CALC"
    epoch: int = 0
    checkpoint_id: str | None = None
    checkpoint_version: int | None = None
    resume_required: bool = False
    resume_reason: str | None = None
    result_state: str = "REMOTE_ONLY"
    storage_durability: str = "none"
    cf_sync_status: str = "DEGRADED_UNSYNCED"
    result_artifact_id: str | None = None
    result_storage_reference: str | None = None
    result_manifest_id: str | None = None
    result_sha256: str | None = None
    result_size_bytes: int | None = None
    result_archived_at: float | None = None
    result_downloaded_at: float | None = None
    result_provenance: dict[str, Any] = field(default_factory=dict)
    last_error_code: str | None = None
    remote_deleted: bool = False
    chain_slugs: list[str] = field(default_factory=list)
    schema_version: int = 1
    version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.kaggle_username = _clean_owner(self.kaggle_username)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # Ensure no accidental secrets exist in dictionary
        data.pop("kaggle_key", None)
        data.pop("api_token", None)
        data.pop("password", None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CloudflareJobRecord":
        clean = dict(data or {})
        clean.pop("kaggle_key", None)
        clean.pop("api_token", None)
        clean.pop("password", None)
        known = {f for f in cls.__dataclass_fields__}
        init_kwargs = {k: v for k, v in clean.items() if k in known}
        if "internal_job_id" not in init_kwargs or not init_kwargs["internal_job_id"]:
            init_kwargs["internal_job_id"] = new_uuid()
        return cls(**init_kwargs)

    @classmethod
    def from_job_manifest(
        cls,
        manifest: JobManifest,
        *,
        workflow_id: str | None = None,
        parent_job_id: str | None = None,
        step_index: int = 0,
        step_count: int = 1,
        step_name: str = "CALC",
        internal_job_id: str | None = None,
        last_seen_remote_state: str = RemoteExecutionState.UNKNOWN.value,
        cf_sync_status: str = "DEGRADED_UNSYNCED",
    ) -> "CloudflareJobRecord":
        """Builds a Cloudflare metadata record from an existing JobManifest."""
        # Derive local state
        local_state = LocalWorkflowState.RUNNING.value
        if manifest.state == JobState.FINISHED:
            local_state = LocalWorkflowState.COMPLETED.value
        elif manifest.state == JobState.FAILED:
            local_state = LocalWorkflowState.FAILED.value
        elif manifest.state == JobState.CREATED:
            local_state = LocalWorkflowState.CREATED.value
        elif manifest.state == JobState.QUEUED:
            local_state = LocalWorkflowState.WAITING_FOR_REMOTE.value

        return cls(
            internal_job_id=internal_job_id or manifest._extra.get("internal_job_id") or new_uuid(),
            kaggle_username=_clean_owner(manifest.owner),
            kaggle_job_ref=manifest.job_id,
            kaggle_url=manifest.current_url or f"https://www.kaggle.com/code/{manifest.owner}/{manifest.current_slug or manifest.job_id}",
            title=manifest.title,
            input_filename=manifest.input_filename,
            job_kind=manifest.job_kind,
            created_at=manifest.created_at,
            updated_at=manifest.updated_at,
            last_reconciliation_at=now(),
            last_seen_remote_state=last_seen_remote_state,
            local_state=local_state,
            workflow_id=workflow_id or manifest._extra.get("workflow_id"),
            parent_job_id=parent_job_id or manifest._extra.get("parent_job_id"),
            step_index=step_index or manifest._extra.get("step_index", 0),
            step_count=step_count or manifest._extra.get("step_count", 1),
            step_name=step_name or manifest._extra.get("step_name", "CALC"),
            epoch=manifest.epoch,
            checkpoint_id=manifest.verified_checkpoint_id,
            resume_required=bool(manifest.state == JobState.FAILED and manifest.verified_checkpoint_id),
            resume_reason="checkpoint_present" if manifest.verified_checkpoint_id else None,
            result_state=getattr(manifest, "result_state", "REMOTE_ONLY"),
            storage_durability=getattr(manifest, "storage_durability", "none"),
            cf_sync_status=cf_sync_status or getattr(manifest, "cf_sync_status", "DEGRADED_UNSYNCED"),
            result_artifact_id=getattr(manifest, "result_artifact_id", None),
            result_storage_reference=getattr(manifest, "result_storage_reference", None),
            result_manifest_id=getattr(manifest, "result_manifest_id", None),
            result_sha256=getattr(manifest, "result_sha256", None),
            result_size_bytes=getattr(manifest, "result_size_bytes", None),
            result_archived_at=getattr(manifest, "result_archived_at", None),
            result_downloaded_at=getattr(manifest, "result_downloaded_at", None),
            result_provenance=dict(getattr(manifest, "result_provenance", {}) or {}),
            chain_slugs=list(manifest.chain_slugs),
            schema_version=1,
            metadata={"job_id_prefix": manifest.job_id},
        )

    def to_job_manifest(self) -> JobManifest:
        """Converts Cloudflare metadata record back into a local JobManifest."""
        state = JobState.QUEUED
        if self.local_state in (LocalWorkflowState.COMPLETED.value, LocalWorkflowState.REMOTE_COMPLETED.value):
            state = JobState.FINISHED
        elif self.local_state in (
            LocalWorkflowState.FAILED.value,
            LocalWorkflowState.REMOTE_FAILED.value,
            LocalWorkflowState.REMOTE_STOPPED.value,
            LocalWorkflowState.REMOTE_DELETED.value,
            LocalWorkflowState.RECOVERY_PENDING.value,
        ):
            state = JobState.FAILED
        elif self.local_state == LocalWorkflowState.CREATED.value:
            state = JobState.CREATED
        elif self.local_state == LocalWorkflowState.RUNNING.value:
            state = JobState.RUNNING

        manifest = JobManifest(
            job_id=self.kaggle_job_ref,
            owner=self.kaggle_username,
            title=self.title or self.kaggle_job_ref,
            created_at=self.created_at,
            updated_at=self.updated_at,
            state=state,
            epoch=self.epoch,
            current_slug=self.chain_slugs[-1] if self.chain_slugs else self.kaggle_job_ref,
            current_url=self.kaggle_url,
            chain_slugs=list(self.chain_slugs) if self.chain_slugs else [self.kaggle_job_ref],
            input_filename=self.input_filename,
            job_kind=self.job_kind,
            verified_checkpoint_id=self.checkpoint_id,
            result_state=self.result_state or "REMOTE_ONLY",
            storage_durability=self.storage_durability or "none",
            cf_sync_status=self.cf_sync_status or "DEGRADED_UNSYNCED",
            result_artifact_id=self.result_artifact_id,
            result_storage_reference=self.result_storage_reference,
            result_manifest_id=self.result_manifest_id,
            result_sha256=self.result_sha256,
            result_size_bytes=self.result_size_bytes,
            result_archived_at=self.result_archived_at,
            result_downloaded_at=self.result_downloaded_at,
            result_provenance=dict(self.result_provenance or {}),
            _extra={
                "internal_job_id": self.internal_job_id,
                "workflow_id": self.workflow_id,
                "parent_job_id": self.parent_job_id,
                "step_index": self.step_index,
                "step_count": self.step_count,
                "step_name": self.step_name,
                "local_state": self.local_state,
                "last_seen_remote_state": self.last_seen_remote_state,
            },
        )
        return manifest


@dataclass
class WorkflowStep:
    """One discrete step in a multi-step calculation workflow."""

    step_index: int
    step_name: str
    job_id: str | None = None
    input_template: str = ""
    status: str = "PENDING"  # PENDING | READY | RUNNING | COMPLETED | FAILED | SKIPPED
    prerequisites: list[int] = field(default_factory=list)
    required_artifacts: list[str] = field(default_factory=list)
    result_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowStep":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


@dataclass
class CloudflareWorkflowRecord:
    """Multi-step calculation workflow metadata."""

    workflow_id: str
    kaggle_username: str
    title: str
    status: str = "CREATED"  # CREATED | IN_PROGRESS | COMPLETED | FAILED | PAUSED
    steps: list[WorkflowStep] = field(default_factory=list)
    current_step_index: int = 0
    created_at: float = field(default_factory=now)
    updated_at: float = field(default_factory=now)
    schema_version: int = 1
    version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.kaggle_username = _clean_owner(self.kaggle_username)

    def get_step(self, index: int) -> WorkflowStep | None:
        for s in self.steps:
            if s.step_index == index:
                return s
        return None

    def is_step_ready(self, index: int) -> bool:
        step = self.get_step(index)
        if not step or step.status in ("COMPLETED", "RUNNING"):
            return False
        for prereq_idx in step.prerequisites:
            prereq_step = self.get_step(prereq_idx)
            if not prereq_step or prereq_step.status != "COMPLETED":
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["steps"] = [s.to_dict() for s in self.steps]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CloudflareWorkflowRecord":
        clean = dict(data or {})
        raw_steps = clean.pop("steps", []) or []
        steps = [WorkflowStep.from_dict(s) for s in raw_steps]
        known = {f for f in cls.__dataclass_fields__}
        init_kwargs = {k: v for k, v in clean.items() if k in known}
        init_kwargs["steps"] = steps
        if "workflow_id" not in init_kwargs or not init_kwargs["workflow_id"]:
            init_kwargs["workflow_id"] = new_uuid()
        return cls(**init_kwargs)


@dataclass
class CloudflareCheckpointRecord:
    """Checkpoint metadata reference stored in Cloudflare.

    Stores ONLY metadata references, hashes, and status. No large binaries.
    """

    checkpoint_id: str
    job_id: str
    epoch: int
    bundle_digest: str = ""
    status: str = CheckpointStatus.STAGED
    orca_phase: str = "unknown"
    created_at: float = field(default_factory=now)
    verified_at: float | None = None
    source_kernel_slug: str = ""
    file_manifest: list[dict[str, Any]] = field(default_factory=list)
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CloudflareCheckpointRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    @classmethod
    def from_checkpoint_manifest(cls, ckpt: CheckpointManifest) -> "CloudflareCheckpointRecord":
        return cls(
            checkpoint_id=ckpt.checkpoint_id,
            job_id=ckpt.job_id,
            epoch=ckpt.epoch,
            bundle_digest=ckpt.bundle_digest,
            status=ckpt.status,
            orca_phase=ckpt.orca_phase,
            created_at=ckpt.created_at,
            verified_at=ckpt.verified_at,
            source_kernel_slug=ckpt.source_kernel_slug,
            file_manifest=[{"name": f.name, "size": f.size, "sha256": f.sha256, "role": f.role} for f in ckpt.files],
        )


@dataclass
class CloudflareCredentialVaultRecord:
    """Encrypted credential record stored in Cloudflare.

    Contains ONLY ciphertext, random nonces, and authentication tags.
    NEVER contains plaintext passwords, API keys, or master encryption keys.
    """

    owner: str
    kaggle_username: str
    ciphertext: str
    nonce: str
    tag: str
    encryption_version: int = 1
    status: str = "ACTIVE"
    created_at: float = field(default_factory=now)
    updated_at: float = field(default_factory=now)
    last_verified_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CloudflareCredentialVaultRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


@dataclass
class CloudflareCredentialMetadata:
    """Public non-secret credential metadata returned to clients."""

    exists: bool
    owner: str
    kaggle_username: str = ""
    status: str = "ACTIVE"
    encryption_version: int = 1
    created_at: float | None = None
    updated_at: float | None = None
    last_verified_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CloudflareCredentialMetadata":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

