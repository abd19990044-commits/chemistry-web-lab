# -*- coding: utf-8 -*-
"""
Persistent data model: the job manifest, the checkpoint manifest, and the
event ledger.

The manifest is the answer to "never rely on RAM". It is a complete,
self-describing snapshot of a calculation: given only the newest window's
`STATE.json`, the entire job can be reconstructed -- its state, its position in
the chain, every file it is carrying, every verification result, every retry
budget, and its causal history. No process needs to have been alive for any of
that to be true.

Serialisation rules
-------------------
  * `schema_version` is stamped on everything. A successor kernel pushed by an
    older deploy of the Space must remain readable by a newer one, because
    those two deploys genuinely coexist -- an in-flight chain outlives a
    redeploy by design.
  * Unknown fields are preserved on round-trip (`_extra`), so a newer producer
    never has its data silently dropped by an older consumer.
  * Every timestamp is an epoch float in UTC. Local time in a distributed
    system is a bug generator.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

from .config import CONFIG
from .states import JobState, Trigger


def now() -> float:
    return time.time()


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
@dataclass
class FileRecord:
    """One file inside a checkpoint bundle or a results set.

    `role` is what makes rollback intelligent. A checkpoint missing an optional
    `.hess` is degraded but usable; one missing its required geometry is not,
    and must be rejected rather than half-restored -- which is precisely the
    'restart files are incomplete' symptom."""

    name: str
    sha256: str
    size: int
    role: str = "auxiliary"          # geometry | hessian | trajectory | wavefunction | input | auxiliary
    required: bool = False
    #: How the consumer obtains it: inline in script.py, or pulled from the
    #: predecessor kernel's Kaggle output.
    transport: str = "inline"        # inline | kaggle_output
    structural_check: str | None = None   # name of the validator that passed
    verified_at: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "FileRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------
class CheckpointStatus(str):
    """Deliberately a plain string namespace rather than an Enum: these values
    are written by the in-kernel runner, which must stay dependency-free."""

    STAGED = "staged"        # written, not yet re-read
    VERIFIED = "verified"    # re-read, hashes and structure confirmed
    COMMITTED = "committed"  # a successor carrying it was accepted by Kaggle
    REJECTED = "rejected"    # failed verification; never usable
    SUPERSEDED = "superseded"


@dataclass
class CheckpointManifest:
    """A checkpoint is a *transaction*, and this is its record.

    The three-phase lifecycle (staged -> verified -> committed) is what gives
    the system its 'no checkpoint is valid until verification succeeds'
    property. A checkpoint that is merely `staged` is never selected as a
    rollback target, so a crash between staging and verification cannot poison
    the recovery path -- the previous `verified` checkpoint is still the newest
    thing anyone will fall back to.
    """

    checkpoint_id: str
    job_id: str
    epoch: int
    created_at: float
    status: str = CheckpointStatus.STAGED

    #: Content-addressed digest over the whole file set. Two checkpoints with
    #: the same bundle_digest are interchangeable, which is what makes a
    #: repeated successor push provably idempotent.
    bundle_digest: str = ""
    files: list[FileRecord] = field(default_factory=list)

    #: The exact ORCA input the successor must run. Stored in full because
    #: reconstructing it from the original plus a diff is a guessing game.
    next_input_text: str = ""
    next_input_sha256: str = ""

    #: Scientific position, so progress is measurable rather than assumed.
    orca_phase: str = "unknown"          # opt | opt_freq | freq | scan | neb | md | sp
    completed_opt_cycles: int = 0
    cumulative_opt_cycles: int = 0
    scan_points_done: int = 0
    opt_converged: bool = False
    last_energy: float | None = None

    #: Populated when verification fails, so a rejected checkpoint explains
    #: itself in the ledger instead of just disappearing.
    rejection_reason: str | None = None
    verified_at: float | None = None
    committed_at: float | None = None

    #: Where a consumer can fetch non-inline files.
    source_kernel_slug: str = ""
    parent_checkpoint_id: str | None = None
    schema_version: int = CONFIG.manifest_version
    _extra: dict = field(default_factory=dict)

    # -- helpers ----------------------------------------------------------
    @property
    def is_usable(self) -> bool:
        return self.status in (CheckpointStatus.VERIFIED, CheckpointStatus.COMMITTED)

    @property
    def required_files(self) -> list[FileRecord]:
        return [f for f in self.files if f.required]

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    def file(self, name: str) -> FileRecord | None:
        return next((f for f in self.files if f.name == name), None)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["files"] = [f.to_dict() for f in self.files]
        extra = data.pop("_extra", {}) or {}
        data.update(extra)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "CheckpointManifest":
        data = dict(data or {})
        files = [FileRecord.from_dict(f) for f in data.pop("files", []) or []]
        known = {f for f in cls.__dataclass_fields__ if not f.startswith("_")}
        extra = {k: v for k, v in data.items() if k not in known}
        init = {k: v for k, v in data.items() if k in known}
        init["files"] = files
        obj = cls(**init)
        obj._extra = extra
        return obj


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
@dataclass
class Event:
    """One entry in the append-only ledger.

    Events are the audit trail *and* the recovery input: `RESTARTING` with no
    subsequent `SUCCESSOR_PUSHED` is exactly the signature of a crash during
    the commit phase, and that is recoverable precisely because the attempt was
    recorded before it was made."""

    event_id: str
    job_id: str
    epoch: int
    at: float
    trigger: str
    from_state: str
    to_state: str
    actor: str = "system"            # system | kernel | watchdog | operator | api
    correlation_id: str = "-"
    detail: dict = field(default_factory=dict)
    schema_version: int = CONFIG.manifest_version

    @classmethod
    def create(cls, *, job_id: str, epoch: int, trigger: Trigger | str,
               from_state: JobState | str, to_state: JobState | str,
               actor: str = "system", correlation_id: str = "-", **detail) -> "Event":
        return cls(
            event_id=new_id("ev_"),
            job_id=job_id,
            epoch=epoch,
            at=now(),
            trigger=getattr(trigger, "value", str(trigger)),
            from_state=getattr(from_state, "value", str(from_state)),
            to_state=getattr(to_state, "value", str(to_state)),
            actor=actor,
            correlation_id=correlation_id,
            detail=detail,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Event":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


# ---------------------------------------------------------------------------
# The job manifest
# ---------------------------------------------------------------------------
@dataclass
class JobManifest:
    """Everything about one calculation.

    This object is written to three places, in this order of authority:
      1. `/kaggle/working/STATE.json` in the newest window -- durable, survives
         the loss of the entire web application;
      2. the server's SQLite cache -- fast, disposable, rebuildable;
      3. the browser -- a view, never a source of truth.

    The old design had only (3), which is why clearing browser data lost the
    job list and why nothing could recover a chain nobody was watching.
    """

    job_id: str                       # base slug, stable for the whole chain
    owner: str                        # kaggle username (lowercased)
    title: str                        # the name the person typed
    created_at: float
    updated_at: float

    state: JobState = JobState.CREATED
    epoch: int = 0                    # window index; 0 is the original kernel

    #: When the job ENTERED its current state. Distinct from `updated_at`, and
    #: the distinction is not academic -- conflating the two produced a bug in
    #: which the watchdog could never detect a stalled job that anyone was
    #: watching.
    #:
    #: `updated_at` means "last write of any kind", and a plain status poll is a
    #: write: the reconciler touches the job even on a no-op pass. So a job
    #: stuck in QUEUED for three hours, polled by a browser every 45 seconds,
    #: had `updated_at` refreshed 240 times and never aged past its one-hour
    #: grace period. The watchdog was blind to precisely the jobs a user was
    #: actively waiting on -- the worst possible subset.
    #:
    #: This field is written ONLY by a real state transition, so "how long has
    #: this job been stuck in this state" has an answer that observation cannot
    #: perturb.
    state_entered_at: float = 0.0

    #: Slug of the kernel for the current epoch. Deterministic from job_id and
    #: epoch, which is what makes "push the successor twice" an upsert of one
    #: kernel rather than the creation of two.
    current_slug: str = ""
    current_url: str = ""
    #: Every window ever created for this job, oldest first. Deleting the job
    #: deletes all of them; losing this list is how orphan kernels accumulate.
    chain_slugs: list[str] = field(default_factory=list)

    #: The run that is currently believed to own this job's execution. A
    #: second concurrent run of the same kernel sees a live heartbeat under a
    #: different token and exits without touching anything.
    run_token: str = ""
    last_heartbeat_at: float | None = None
    heartbeat_detail: dict = field(default_factory=dict)

    # -- checkpoint pointers ---------------------------------------------
    #: Newest checkpoint that PASSED verification. The rollback target, and the
    #: only thing a restart is ever allowed to resume from.
    verified_checkpoint_id: str | None = None
    #: The one before it, kept deliberately: if the newest verified checkpoint
    #: turns out to be poison (it verified structurally but ORCA rejects it),
    #: there is still somewhere to fall back to.
    previous_checkpoint_id: str | None = None
    #: In-flight, not yet verified. Never a rollback target.
    pending_checkpoint_id: str | None = None
    rollback_count: int = 0

    # -- inputs -----------------------------------------------------------
    input_filename: str = ""
    original_input_sha256: str = ""
    dataset_sources: list[str] = field(default_factory=list)
    orca_link_present: bool = False   # the link itself is never persisted
    job_kind: str = "unknown"         # opt | opt_freq | freq | scan | neb | md | sp

    # -- budgets ----------------------------------------------------------
    max_epochs: int = CONFIG.runner.max_epochs
    max_disk_epochs: int = CONFIG.runner.max_disk_epochs
    disk_epochs_used: int = 0
    max_total_opt_cycles: int = CONFIG.runner.max_total_opt_cycles
    cumulative_opt_cycles: int = 0
    retry_count: int = 0              # consecutive failures at the current step
    total_retries: int = 0
    total_runtime_seconds: float = 0.0

    # -- observability ----------------------------------------------------
    last_error: dict | None = None
    last_note: str = ""
    #: Bounded tail of the event ledger, carried with the manifest so a window
    #: knows its own history without a database. The full ledger lives in
    #: SQLite and in each window's own STATE.json.
    recent_events: list[Event] = field(default_factory=list)
    max_recent_events: int = 60

    #: Disk telemetry from the last window. Records the quota model, the raw
    #: statvfs number and the effective headroom side by side, so the
    #: discrepancy that hid the original disk bug can never be invisible again.
    disk_report: dict = field(default_factory=dict)

    schema_version: int = CONFIG.manifest_version
    _extra: dict = field(default_factory=dict)

    # -- derived ----------------------------------------------------------
    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    @property
    def epoch_budget_remaining(self) -> int:
        return max(0, self.max_epochs - self.epoch)

    @property
    def has_verified_checkpoint(self) -> bool:
        return bool(self.verified_checkpoint_id)

    def slug_for_epoch(self, epoch: int) -> str:
        """Deterministic window naming. Epoch 0 is the base slug so the address
        the user was originally shown never changes."""
        return self.job_id if epoch <= 0 else f"{self.job_id}-r{epoch}"

    def record_event(self, event: Event) -> None:
        self.recent_events.append(event)
        if len(self.recent_events) > self.max_recent_events:
            # Keep the oldest few: the beginning of a chain explains how it was
            # configured, and dropping it makes long chains unexplainable.
            head = self.recent_events[:5]
            tail = self.recent_events[-(self.max_recent_events - 5):]
            self.recent_events = head + tail

    def touch(self) -> None:
        """Records a write. Deliberately does NOT move `state_entered_at`."""
        self.updated_at = now()

    def enter_state(self, state: JobState) -> None:
        """The only place `state_entered_at` advances.

        A no-op re-entry into the same state does not restart the clock, so a
        job that keeps being re-reported as QUEUED keeps ageing."""
        if state is not self.state:
            self.state_entered_at = now()
        self.state = state
        self.updated_at = now()

    @property
    def seconds_in_state(self) -> float:
        # Manifests written before this field existed fall back to updated_at,
        # which is the old (imperfect) behaviour rather than a crash.
        reference = self.state_entered_at or self.updated_at or self.created_at
        return max(0.0, now() - reference)

    # -- serialisation ----------------------------------------------------
    def to_dict(self) -> dict:
        data = asdict(self)
        data["state"] = self.state.value
        data["recent_events"] = [e.to_dict() for e in self.recent_events]
        extra = data.pop("_extra", {}) or {}
        data.update(extra)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "JobManifest":
        data = dict(data or {})
        events = [Event.from_dict(e) for e in data.pop("recent_events", []) or []]
        raw_state = data.pop("state", JobState.CREATED.value)
        try:
            state = JobState(raw_state)
        except ValueError:
            # A state name written by a newer deploy. Treating it as an unknown
            # active state is safer than crashing: the reconciler will re-derive
            # the real state from the Kaggle-side ledger on its next pass.
            state = JobState.QUEUED
        known = {f for f in cls.__dataclass_fields__ if not f.startswith("_")}
        extra = {k: v for k, v in data.items() if k not in known}
        init = {k: v for k, v in data.items() if k in known}
        obj = cls(**init)
        obj.state = state
        obj.recent_events = events
        obj._extra = extra
        return obj

    @classmethod
    def create(cls, *, job_id: str, owner: str, title: str, input_filename: str,
               original_input_sha256: str, dataset_sources: list[str] | None = None,
               orca_link_present: bool = False, job_kind: str = "unknown",
               **overrides) -> "JobManifest":
        ts = now()
        manifest = cls(
            job_id=job_id,
            owner=owner.lower(),
            title=title,
            created_at=ts,
            updated_at=ts,
            state_entered_at=ts,
            current_slug=job_id,
            chain_slugs=[job_id],
            input_filename=input_filename,
            original_input_sha256=original_input_sha256,
            dataset_sources=list(dataset_sources or []),
            orca_link_present=orca_link_present,
            job_kind=job_kind,
        )
        for key, value in overrides.items():
            if hasattr(manifest, key):
                setattr(manifest, key, value)
        return manifest


# ---------------------------------------------------------------------------
# Lease
# ---------------------------------------------------------------------------
@dataclass
class Lease:
    """Mutual exclusion with a fencing token.

    A TTL alone is not enough. A worker can stall (GC pause, a blocked CLI
    call), have its lease expire, be replaced, and then wake up and complete
    its write against state a new owner has already moved on from. The
    monotonic `fence` makes that write rejectable: the store refuses any
    mutation carrying a fence lower than the one currently recorded for the
    resource. This is Kleppmann's fencing-token argument, and it is the
    difference between a lock that usually works and one that is correct."""

    resource: str
    holder: str
    fence: int
    acquired_at: float
    expires_at: float

    @property
    def is_expired(self) -> bool:
        return now() >= self.expires_at

    @property
    def seconds_remaining(self) -> float:
        return max(0.0, self.expires_at - now())
