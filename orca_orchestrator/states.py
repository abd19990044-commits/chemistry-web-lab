# -*- coding: utf-8 -*-
"""
The calculation lifecycle finite-state machine.

Every transition is explicit. There is no code path anywhere in the
orchestrator that assigns to a job's state directly; the only way to change
state is `TRANSITIONS.apply(...)`, which rejects any (state, trigger) pair the
model does not define. An undefined transition is a bug in the caller's model
of the world, and the correct response to that is a loud `IllegalTransitionError`,
not a silently mutated field.

Why a state machine at all
--------------------------
The previous design had no state; it had *inferences*. `check_job_status()`
asked Kaggle for a kernel status word and then guessed what that implied,
freshly, on every poll. Two observers polling at the same moment could reach
different conclusions, nothing recorded which conclusion had been acted on,
and a conclusion drawn from a stale artefact (a `NEXT_JOB_ID.txt` left over
from a *previous run of the same kernel*) was indistinguishable from a fresh
one. Duplicated work, lost checkpoints and stuck jobs all follow from that.

The two-direction VERIFYING state
---------------------------------
VERIFYING is entered from two places and leaves to two different places:

    CHECKPOINTING -> VERIFYING -> RESTARTING     (outbound: sealing a checkpoint)
    DOWNLOADING   -> VERIFYING -> RESTORING      (inbound: accepting a checkpoint)

That is intentional and is what makes the transaction symmetric: a checkpoint
is verified by the producer *before* it is committed, and again by the
consumer *before* it is trusted. A single verification on either side alone
would miss corruption introduced by the transfer itself. The `direction` field
on the trigger disambiguates, so the transition remains deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable

from .errors import IllegalTransitionError


class JobState(str, Enum):
    """`str` mixin so a state serialises to its own name in JSON with no
    custom encoder, and so a manifest written by an older deploy still
    round-trips through a newer one."""

    CREATED = "CREATED"
    QUEUED = "QUEUED"
    UPLOADING = "UPLOADING"
    READY = "READY"
    RUNNING = "RUNNING"
    CHECKPOINTING = "CHECKPOINTING"
    DOWNLOADING = "DOWNLOADING"
    VERIFYING = "VERIFYING"
    RESTORING = "RESTORING"
    ROLLING_BACK = "ROLLING_BACK"
    RESTARTING = "RESTARTING"
    FINISHED = "FINISHED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def is_active(self) -> bool:
        """A state in which the watchdog is expected to see forward progress."""
        return self in _ACTIVE

    @property
    def is_handoff(self) -> bool:
        """Mid-transaction. A crash here is the dangerous case: something was
        started that has not yet been committed, so recovery must decide
        whether to roll forward or back."""
        return self in _HANDOFF


_TERMINAL = frozenset({JobState.FINISHED, JobState.FAILED, JobState.CANCELLED})
_HANDOFF = frozenset({
    JobState.CHECKPOINTING, JobState.VERIFYING, JobState.DOWNLOADING,
    JobState.RESTORING, JobState.ROLLING_BACK, JobState.RESTARTING,
    JobState.UPLOADING,
})
_ACTIVE = frozenset(set(JobState) - _TERMINAL)


class Trigger(str, Enum):
    """Named causes. A trigger is *why* a state changed, and it is stored on
    the event alongside the states, so the ledger reads as a causal history
    rather than a list of assignments."""

    # Submission
    SUBMIT = "SUBMIT"
    PUSH_ACK = "PUSH_ACK"
    PUSH_RETRY = "PUSH_RETRY"
    PUSH_EXHAUSTED = "PUSH_EXHAUSTED"

    # Window boot
    KERNEL_BOOT_FRESH = "KERNEL_BOOT_FRESH"            # epoch 0: nothing to restore
    KERNEL_BOOT_RESUME = "KERNEL_BOOT_RESUME"          # epoch > 0: fetch checkpoint
    KERNEL_NEVER_STARTED = "KERNEL_NEVER_STARTED"

    # Inbound checkpoint transfer
    BUNDLE_FETCHED = "BUNDLE_FETCHED"
    FETCH_RETRY = "FETCH_RETRY"
    FETCH_EXHAUSTED = "FETCH_EXHAUSTED"
    BUNDLE_VERIFIED = "BUNDLE_VERIFIED"
    RESTORE_COMPLETE = "RESTORE_COMPLETE"
    RESTORE_FAILED = "RESTORE_FAILED"

    # Execution
    ORCA_STARTED = "ORCA_STARTED"
    ORCA_UNAVAILABLE = "ORCA_UNAVAILABLE"
    ORCA_COMPLETE = "ORCA_COMPLETE"
    ORCA_FATAL = "ORCA_FATAL"

    # Reasons a window must end without the calculation being finished
    WINDOW_EXPIRING = "WINDOW_EXPIRING"
    DISK_PRESSURE = "DISK_PRESSURE"
    MAXITER_EXHAUSTED = "MAXITER_EXHAUSTED"
    ORCA_EXIT_INCOMPLETE = "ORCA_EXIT_INCOMPLETE"
    HEARTBEAT_LOST = "HEARTBEAT_LOST"

    # Outbound checkpoint transaction
    CHECKPOINT_STAGED = "CHECKPOINT_STAGED"
    CHECKPOINT_VERIFIED = "CHECKPOINT_VERIFIED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    STAGING_FAILED = "STAGING_FAILED"

    # Rollback
    ROLLBACK_SELECTED = "ROLLBACK_SELECTED"
    NO_VALID_CHECKPOINT = "NO_VALID_CHECKPOINT"

    # Successor
    SUCCESSOR_PUSHED = "SUCCESSOR_PUSHED"
    SUCCESSOR_RETRY = "SUCCESSOR_RETRY"
    SUCCESSOR_EXHAUSTED = "SUCCESSOR_EXHAUSTED"

    # Budgets and operator actions
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    CANCEL = "CANCEL"
    OPERATOR_RESUME = "OPERATOR_RESUME"
    OPERATOR_FAIL = "OPERATOR_FAIL"


@dataclass(frozen=True)
class Transition:
    source: JobState
    trigger: Trigger
    target: JobState
    #: Advances the window counter. Exactly one transition does this
    #: (RESTARTING -> QUEUED), which is what makes the epoch a reliable
    #: monotonic clock for the whole chain.
    advances_epoch: bool = False
    #: Human-readable rationale, surfaced in the API and in ARCHITECTURE.md.
    note: str = ""
    #: Optional predicate over the job manifest. A guard that returns False
    #: makes the transition illegal *for that job right now* -- e.g. you may
    #: not push a successor once the epoch budget is spent.
    guard: Callable[["object"], bool] | None = None


def _has_epoch_budget(job) -> bool:
    return int(getattr(job, "epoch", 0)) + 1 <= int(getattr(job, "max_epochs", 0))


def _has_verified_checkpoint(job) -> bool:
    return bool(getattr(job, "verified_checkpoint_id", None))


_ALL: tuple[Transition, ...] = (
    # ---- submission -------------------------------------------------------
    Transition(JobState.CREATED, Trigger.SUBMIT, JobState.UPLOADING,
               note="manifest persisted before any network call, so a crash mid-push is recoverable"),
    Transition(JobState.CREATED, Trigger.CANCEL, JobState.CANCELLED),
    Transition(JobState.UPLOADING, Trigger.PUSH_ACK, JobState.QUEUED,
               note="Kaggle acknowledged the push and reported the authoritative slug"),
    Transition(JobState.UPLOADING, Trigger.PUSH_RETRY, JobState.UPLOADING,
               note="self-loop: the push is idempotent on a deterministic slug"),
    Transition(JobState.UPLOADING, Trigger.PUSH_EXHAUSTED, JobState.FAILED),
    Transition(JobState.UPLOADING, Trigger.CANCEL, JobState.CANCELLED),

    # ---- window boot ------------------------------------------------------
    Transition(JobState.QUEUED, Trigger.KERNEL_BOOT_FRESH, JobState.READY,
               note="epoch 0 carries its inputs inline; there is nothing to restore"),
    Transition(JobState.QUEUED, Trigger.KERNEL_BOOT_RESUME, JobState.DOWNLOADING,
               note="epoch > 0 must retrieve and verify its predecessor's checkpoint first"),
    Transition(JobState.QUEUED, Trigger.KERNEL_NEVER_STARTED, JobState.RESTARTING,
               guard=_has_epoch_budget,
               note="watchdog: accepted by Kaggle but never scheduled; re-drive the same epoch"),
    Transition(JobState.QUEUED, Trigger.BUDGET_EXHAUSTED, JobState.FAILED),
    Transition(JobState.QUEUED, Trigger.CANCEL, JobState.CANCELLED),

    # ---- inbound transfer -------------------------------------------------
    Transition(JobState.DOWNLOADING, Trigger.BUNDLE_FETCHED, JobState.VERIFYING),
    Transition(JobState.DOWNLOADING, Trigger.FETCH_RETRY, JobState.DOWNLOADING,
               note="partial download: restart the transfer, never resume into the same file"),
    Transition(JobState.DOWNLOADING, Trigger.FETCH_EXHAUSTED, JobState.ROLLING_BACK),
    Transition(JobState.DOWNLOADING, Trigger.CANCEL, JobState.CANCELLED),

    Transition(JobState.VERIFYING, Trigger.BUNDLE_VERIFIED, JobState.RESTORING,
               note="inbound: hashes and ORCA structure both check out"),
    Transition(JobState.VERIFYING, Trigger.CHECKPOINT_VERIFIED, JobState.RESTARTING,
               guard=_has_epoch_budget,
               note="outbound: the checkpoint is sealed and may now be committed"),
    Transition(JobState.VERIFYING, Trigger.VERIFICATION_FAILED, JobState.ROLLING_BACK),
    Transition(JobState.VERIFYING, Trigger.BUDGET_EXHAUSTED, JobState.FAILED),
    Transition(JobState.VERIFYING, Trigger.CANCEL, JobState.CANCELLED),

    Transition(JobState.RESTORING, Trigger.RESTORE_COMPLETE, JobState.READY),
    Transition(JobState.RESTORING, Trigger.RESTORE_FAILED, JobState.ROLLING_BACK),
    Transition(JobState.RESTORING, Trigger.CANCEL, JobState.CANCELLED),

    # ---- execution --------------------------------------------------------
    Transition(JobState.READY, Trigger.ORCA_STARTED, JobState.RUNNING),
    Transition(JobState.READY, Trigger.ORCA_UNAVAILABLE, JobState.FAILED,
               note="no ORCA binary in the dataset or behind the link; retrying cannot help"),
    Transition(JobState.READY, Trigger.CANCEL, JobState.CANCELLED),

    Transition(JobState.RUNNING, Trigger.ORCA_COMPLETE, JobState.FINISHED,
               note="requires the job-type convergence marker, NOT merely "
                    "'ORCA TERMINATED NORMALLY' -- see checkpoints.classify_orca_outcome"),
    Transition(JobState.RUNNING, Trigger.ORCA_FATAL, JobState.FAILED,
               note="input error / unrecognised keyword: identical resubmission fails identically"),
    Transition(JobState.RUNNING, Trigger.WINDOW_EXPIRING, JobState.CHECKPOINTING),
    Transition(JobState.RUNNING, Trigger.DISK_PRESSURE, JobState.CHECKPOINTING),
    Transition(JobState.RUNNING, Trigger.MAXITER_EXHAUSTED, JobState.CHECKPOINTING,
               note="ORCA exited normally at %geom MaxIter without converging; this is "
                    "unfinished work, not a finished job"),
    Transition(JobState.RUNNING, Trigger.ORCA_EXIT_INCOMPLETE, JobState.CHECKPOINTING),
    Transition(JobState.RUNNING, Trigger.HEARTBEAT_LOST, JobState.CHECKPOINTING,
               note="watchdog: the window died without writing a checkpoint; recover from "
                    "the last verified one"),
    Transition(JobState.RUNNING, Trigger.CANCEL, JobState.CANCELLED),

    # ---- outbound transaction --------------------------------------------
    Transition(JobState.CHECKPOINTING, Trigger.CHECKPOINT_STAGED, JobState.VERIFYING),
    Transition(JobState.CHECKPOINTING, Trigger.STAGING_FAILED, JobState.ROLLING_BACK),
    Transition(JobState.CHECKPOINTING, Trigger.CANCEL, JobState.CANCELLED),

    # ---- rollback ---------------------------------------------------------
    Transition(JobState.ROLLING_BACK, Trigger.ROLLBACK_SELECTED, JobState.RESTARTING,
               guard=_has_verified_checkpoint,
               note="fall back to the last checkpoint that passed verification"),
    Transition(JobState.ROLLING_BACK, Trigger.NO_VALID_CHECKPOINT, JobState.FAILED),
    Transition(JobState.ROLLING_BACK, Trigger.CANCEL, JobState.CANCELLED),

    # ---- successor --------------------------------------------------------
    Transition(JobState.RESTARTING, Trigger.SUCCESSOR_PUSHED, JobState.QUEUED,
               advances_epoch=True,
               note="the ONLY transition that advances the epoch, which is what makes the "
                    "epoch a trustworthy monotonic clock across the whole chain"),
    Transition(JobState.RESTARTING, Trigger.SUCCESSOR_RETRY, JobState.RESTARTING),
    Transition(JobState.RESTARTING, Trigger.SUCCESSOR_EXHAUSTED, JobState.FAILED),
    Transition(JobState.RESTARTING, Trigger.BUDGET_EXHAUSTED, JobState.FAILED),
    Transition(JobState.RESTARTING, Trigger.CANCEL, JobState.CANCELLED),

    # ---- operator ---------------------------------------------------------
    Transition(JobState.FAILED, Trigger.OPERATOR_RESUME, JobState.RESTARTING,
               guard=_has_verified_checkpoint,
               note="manual resume replays from the last verified checkpoint, never from zero"),
)


class TransitionTable:
    def __init__(self, transitions: Iterable[Transition]) -> None:
        self._by_key: dict[tuple[JobState, Trigger], Transition] = {}
        for t in transitions:
            key = (t.source, t.trigger)
            if key in self._by_key:
                raise ValueError(f"duplicate transition for {key}")
            self._by_key[key] = t
        self._all = tuple(transitions)

    def get(self, state: JobState, trigger: Trigger) -> Transition | None:
        return self._by_key.get((state, trigger))

    def allowed_triggers(self, state: JobState) -> list[Trigger]:
        return sorted((t for (s, t) in self._by_key if s == state), key=lambda x: x.value)

    def reachable_states(self, state: JobState) -> list[JobState]:
        return sorted({t.target for t in self._all if t.source == state}, key=lambda s: s.value)

    def apply(self, state: JobState, trigger: Trigger, job=None) -> Transition:
        """Returns the Transition, or raises. Never mutates anything: the
        caller records the result, so the decision and the write stay separable
        and the decision itself is trivially unit-testable."""
        transition = self.get(state, trigger)
        if transition is None:
            raise IllegalTransitionError(
                f"no transition defined from {state.value} on {trigger.value}",
                state=state.value,
                trigger=trigger.value,
                allowed=[t.value for t in self.allowed_triggers(state)],
            )
        if transition.guard is not None and job is not None and not transition.guard(job):
            raise IllegalTransitionError(
                f"transition {state.value} --{trigger.value}--> {transition.target.value} "
                f"is blocked by its guard",
                state=state.value, trigger=trigger.value,
                target=transition.target.value,
                reason="guard predicate returned False (budget spent, or no verified "
                       "checkpoint to fall back to)",
            )
        return transition

    def can(self, state: JobState, trigger: Trigger, job=None) -> bool:
        try:
            self.apply(state, trigger, job)
            return True
        except IllegalTransitionError:
            return False

    def as_mermaid(self) -> str:
        """Renders the machine as a Mermaid state diagram. The documentation and
        the implementation cannot drift, because the diagram is generated from
        the same table the runtime dispatches on."""
        lines = ["stateDiagram-v2"]
        for t in self._all:
            label = t.trigger.value + (" / epoch+1" if t.advances_epoch else "")
            lines.append(f"    {t.source.value} --> {t.target.value}: {label}")
        return "\n".join(lines)

    def validate(self) -> None:
        """Structural invariants, asserted at import time.

        Catches the class of modelling mistake that is invisible until a job is
        already stuck: a state with no way out, or a non-terminal state with no
        way in."""
        sources = {t.source for t in self._all}
        targets = {t.target for t in self._all}
        for state in JobState:
            if state.is_terminal:
                continue
            if state not in sources:
                raise ValueError(f"state {state.value} has no outgoing transition (dead end)")
            if state is not JobState.CREATED and state not in targets:
                raise ValueError(f"state {state.value} is unreachable")
        for state in JobState:
            if state.is_terminal or state is JobState.CREATED:
                continue
            if not any(t.source == state and t.target in _TERMINAL for t in self._all):
                # Every active state must be able to reach a terminal state in
                # one hop, so a job can always be stopped without waiting for a
                # multi-step dance to complete.
                raise ValueError(f"state {state.value} cannot terminate directly")


TRANSITIONS = TransitionTable(_ALL)
TRANSITIONS.validate()


#: Triggers that mean "this window is over but the calculation is not".
#: Grouped here so the runner and the watchdog cannot disagree about which
#: outcomes deserve a continuation.
CONTINUABLE_TRIGGERS = frozenset({
    Trigger.WINDOW_EXPIRING,
    Trigger.DISK_PRESSURE,
    Trigger.MAXITER_EXHAUSTED,
    Trigger.ORCA_EXIT_INCOMPLETE,
    Trigger.HEARTBEAT_LOST,
})
