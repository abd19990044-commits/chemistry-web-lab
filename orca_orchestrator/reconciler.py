# -*- coding: utf-8 -*-
"""
The reconciliation loop: observe, decide, act.

This replaces `check_job_status()`, which mixed all three into one function and
re-derived its conclusions from scratch on every poll. Separating them buys
three things that matter more than the tidiness:

  * **Determinism.** `decide()` is a pure function of (manifest, observation).
    It performs no I/O and mutates nothing, so every routing decision this
    system makes is reproducible from a recorded observation and unit-testable
    without a Kaggle account.
  * **Crash safety.** `act()` writes its *intent* before performing an effect.
    A crash between intent and effect leaves the job in a handoff state whose
    recovery is defined, rather than in a state that claims an effect happened
    when it did not.
  * **Idempotency.** Because the decision is derived from observed reality
    rather than from local assumptions, running the loop twice converges on the
    same result. Two workers racing produce the same answer; the lease merely
    stops them from both paying for it.

The model is a level-triggered controller, like a Kubernetes controller: it
does not react to *events*, it repeatedly drives observed state toward desired
state. Level-triggered logic is what survives missed events, and every failure
in the brief -- a crashed worker, a browser that was closed, a Space that
restarted mid-transition -- is a missed event.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Any

from . import checkpoints as ckpt
from .config import CONFIG
from .credentials import KaggleCredentials
from .errors import (ConcurrencyError, IllegalTransitionError, LeaseLostError,
                     NotFoundError, OrchestratorError, PermanentError, QuotaExhaustedError)
from .kaggle_api import KaggleClient, KernelStatus
from .ledger import LedgerRecord, newest_window_slug, read_window
from .logging_ext import get_logger, log_context, log_event, log_failure, new_correlation_id
from .models import CheckpointStatus, Event, JobManifest, now
from .states import TRANSITIONS, JobState, Trigger
from .store import JobStore

log = get_logger("orca.reconciler")


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------
@dataclass
class Observation:
    """Everything learned about a job from the outside world in one pass.

    Snapshotted deliberately: `decide()` must see a single coherent view. If it
    could call out mid-decision, two facts read a second apart could describe
    different epochs and the routing would be based on a world that never
    existed."""

    job_id: str
    observed_at: float = field(default_factory=now)
    kernel_status: KernelStatus | None = None
    kernel_missing: bool = False
    record: LedgerRecord | None = None
    error: OrchestratorError | None = None

    @property
    def kaggle_state(self) -> str:
        if self.kernel_missing:
            return "missing"
        return self.kernel_status.status if self.kernel_status else "unknown"

    @property
    def heartbeat_age(self) -> float | None:
        return self.record.heartbeat_age_seconds if self.record else None

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "observed_at": self.observed_at,
            "kaggle_state": self.kaggle_state,
            "heartbeat_age_seconds": self.heartbeat_age,
            "ledger_epoch": self.record.epoch if self.record else None,
            "ledger_state": (self.record.job.state.value
                             if self.record and self.record.job else None),
            "error": self.error.to_dict() if self.error else None,
        }


def observe(client: KaggleClient, job: JobManifest) -> Observation:
    """Reads the world. Never mutates anything, never raises for an expected
    condition -- a missing kernel is data, not an exception, because 'the
    notebook was deleted on kaggle.com' is a normal thing for a user to do."""
    obs = Observation(job_id=job.job_id)
    slug = job.current_slug or job.slug_for_epoch(job.epoch)

    try:
        status = client.kernel_exists(slug)
        if status is None:
            obs.kernel_missing = True
            return obs
        obs.kernel_status = status
    except OrchestratorError as exc:
        obs.error = exc
        return obs

    # The ledger is only worth fetching once the window has produced something.
    # A queued kernel has no output yet, and asking for it costs a round trip
    # that returns nothing on every single poll of a long queue.
    if status.is_stopped or status.status == "running":
        try:
            obs.record = read_window(client, slug)
        except NotFoundError:
            obs.record = None
        except OrchestratorError as exc:
            obs.error = exc
    return obs


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------
@dataclass
class Decision:
    trigger: Trigger | None
    reason: str
    detail: dict = field(default_factory=dict)
    #: Set when the decision requires an effect beyond the state write.
    action: str | None = None       # push_successor | adopt_ledger | rollback | none

    @property
    def is_noop(self) -> bool:
        return self.trigger is None and self.action is None


def _noop(reason: str, **detail) -> Decision:
    return Decision(None, reason, detail)


def decide(job: JobManifest, obs: Observation, *, config=CONFIG) -> Decision:
    """Pure. No I/O, no mutation. Given what we believe and what we observed,
    what should happen next?

    Kept free of side effects on purpose: this is the function whose behaviour
    determines whether jobs get stuck, and a pure function can be exhaustively
    tested against synthetic observations covering every failure in the brief."""
    if job.is_terminal:
        return _noop("job is in a terminal state", state=job.state.value)

    if obs.error is not None:
        # A transport failure tells us nothing about the job. Reporting it as a
        # job failure is what previously flipped healthy jobs to 'error' on a
        # network blip.
        return _noop("observation failed; the job's state is unchanged because a "
                     "failure to *look* is not a failure of the job",
                     error=obs.error.code)

    # ---- the kernel is gone -------------------------------------------
    if obs.kernel_missing:
        if job.state in (JobState.UPLOADING, JobState.RESTARTING):
            return Decision(Trigger.SUCCESSOR_RETRY,
                            "the kernel we believed we pushed does not exist on Kaggle; "
                            "the push did not take effect and must be replayed",
                            {"slug": job.current_slug}, action="push_successor")
        return Decision(Trigger.CANCEL,
                        "the notebook no longer exists on Kaggle -- it was deleted there, "
                        "so there is nothing left to drive",
                        {"slug": job.current_slug})

    ledger = obs.record
    ledger_job = ledger.job if ledger else None

    # Computed ONCE and applied everywhere the ledger is consulted.
    #
    # `/kaggle/working` persists across runs of the same kernel, so a window
    # can read a STATE.json written by a *previous* run of itself. Trusting
    # that leftover is how a job gets declared finished on the strength of an
    # epoch it has already moved past, or follows a hand-off pointer that leads
    # nowhere. An earlier revision of this function applied the staleness guard
    # only to the fresh-ledger fast path and left the stopped-window branch
    # below unguarded; the regression test
    # "a ledger entry from an OLDER epoch is not treated as this window's
    # result" exists because of that omission.
    ledger_fresh = ledger_job is not None and not ledger.is_stale_relative_to(job.epoch)

    # ---- the ledger is authoritative when it is fresh -----------------
    # A window that wrote STATE.json knows more about itself than we do.
    if ledger_fresh:
        if ledger_job.epoch > job.epoch:
            return Decision(Trigger.SUCCESSOR_PUSHED,
                            "the window already pushed its successor and recorded it in the "
                            "ledger; adopting the ledger's epoch",
                            {"ledger_epoch": ledger_job.epoch, "local_epoch": job.epoch},
                            action="adopt_ledger")
        if ledger_job.state is JobState.FINISHED and job.state is not JobState.FINISHED:
            return Decision(Trigger.ORCA_COMPLETE,
                            "the window reported a verified completion",
                            {"note": ledger_job.last_note[:400]}, action="adopt_ledger")
        if ledger_job.state is JobState.FAILED and job.state is not JobState.FAILED:
            return Decision(Trigger.ORCA_FATAL,
                            "the window reported an unrecoverable failure",
                            {"note": ledger_job.last_note[:400],
                             "error": ledger_job.last_error}, action="adopt_ledger")

    # ---- legacy chain hand-off ----------------------------------------
    if ledger is not None and ledger.legacy_next_slug and ledger_job is None:
        return Decision(Trigger.SUCCESSOR_PUSHED,
                        "a pre-orchestrator window handed off via NEXT_JOB_ID.txt; "
                        "following it so an in-flight legacy chain is not stranded",
                        {"next_slug": ledger.legacy_next_slug}, action="adopt_ledger")

    kaggle_state = obs.kaggle_state

    # ---- still working ------------------------------------------------
    if kaggle_state == "queued":
        return _noop("the kernel is queued on Kaggle", kaggle_state=kaggle_state)

    if kaggle_state == "running":
        if job.state is JobState.QUEUED:
            trigger = (Trigger.KERNEL_BOOT_RESUME if job.epoch > 0
                       else Trigger.KERNEL_BOOT_FRESH)
            return Decision(trigger, "the kernel has started running")
        age = obs.heartbeat_age
        if (job.state is JobState.RUNNING and age is not None
                and age > config.watchdog.heartbeat_grace_seconds):
            return Decision(Trigger.HEARTBEAT_LOST,
                            "Kaggle still reports the kernel as running, but it has not "
                            "written a heartbeat for longer than the grace period, so the "
                            "process inside it is dead",
                            {"heartbeat_age_seconds": round(age, 1),
                             "grace_seconds": config.watchdog.heartbeat_grace_seconds},
                            action="rollback")
        return _noop("the kernel is running and heartbeating",
                     heartbeat_age_seconds=age)

    # ---- the window stopped -------------------------------------------
    if kaggle_state in ("complete", "error", "cancelled"):
        if job.state in (JobState.QUEUED, JobState.UPLOADING):
            # It stopped without ever telling us it started. Whatever happened,
            # there is no checkpoint from it, so recover from the last verified
            # one rather than assuming progress.
            return Decision(Trigger.KERNEL_NEVER_STARTED,
                            "the window reached a stopped state without ever reporting that "
                            "it started; no progress was recorded for this epoch",
                            {"kaggle_state": kaggle_state}, action="push_successor")

        if not ledger_fresh:
            # Either no ledger at all, or one describing an epoch we have
            # already passed. Both mean the same thing: this window's outcome
            # is unknown. Assuming progress here would either declare an
            # unfinished job complete or resume from a superseded checkpoint,
            # so the conservative path -- recover from the last checkpoint that
            # actually verified -- is the only safe one.
            return Decision(Trigger.HEARTBEAT_LOST,
                            "the window stopped without leaving a ledger entry for its own "
                            "epoch, so its outcome is unknown and cannot be assumed to be "
                            "progress",
                            {"kaggle_state": kaggle_state,
                             "ledger_epoch": ledger.epoch if ledger else None,
                             "job_epoch": job.epoch},
                            action="rollback")

        if ledger_job.state.is_terminal:
            trigger = (Trigger.ORCA_COMPLETE if ledger_job.state is JobState.FINISHED
                       else Trigger.ORCA_FATAL)
            return Decision(trigger, "the window recorded a terminal outcome",
                            {"ledger_state": ledger_job.state.value,
                             "note": ledger_job.last_note[:400]}, action="adopt_ledger")

        # The window stopped mid-handoff: it staged or verified a checkpoint but
        # never got the successor pushed. This is precisely the crash-during-
        # commit case, and it is recoverable because the intent was recorded.
        if ledger_job.state in (JobState.CHECKPOINTING, JobState.VERIFYING,
                                JobState.RESTARTING, JobState.ROLLING_BACK):
            if ledger.checkpoint and ledger.checkpoint.is_usable:
                return Decision(Trigger.SUCCESSOR_RETRY,
                                "the window verified a checkpoint but stopped before its "
                                "successor was accepted; completing the commit from here",
                                {"checkpoint_id": ledger.checkpoint.checkpoint_id,
                                 "ledger_state": ledger_job.state.value},
                                action="push_successor")
            return Decision(Trigger.VERIFICATION_FAILED,
                            "the window stopped mid-handoff with no verified checkpoint to "
                            "commit",
                            {"ledger_state": ledger_job.state.value}, action="rollback")

        return Decision(Trigger.HEARTBEAT_LOST,
                        "the window stopped while still reporting an in-progress state",
                        {"ledger_state": ledger_job.state.value}, action="rollback")

    return _noop("Kaggle reported an unrecognised status; treating the job as unchanged "
                 "rather than guessing", kaggle_state=kaggle_state)


# ---------------------------------------------------------------------------
# Action
# ---------------------------------------------------------------------------
class Reconciler:
    def __init__(self, store: JobStore, *, config=CONFIG) -> None:
        self.store = store
        self.config = config

    # -- transition helper ------------------------------------------------
    def transition(self, job: JobManifest, trigger: Trigger, *, actor: str = "system",
                   correlation_id: str = "-", **detail) -> JobManifest:
        """The ONE place a job's state changes.

        Records the event before returning, so the ledger contains the decision
        even if the caller's subsequent effect fails. That ordering is what
        makes a crash mid-effect diagnosable instead of invisible."""
        source = job.state
        transition = TRANSITIONS.apply(source, trigger, job)

        # enter_state, not `job.state = ...`: it is what advances
        # `state_entered_at`, which is the clock the watchdog measures against.
        # Assigning the field directly would leave that clock frozen and make
        # the job undetectable as stalled.
        job.enter_state(transition.target)
        if transition.advances_epoch:
            job.epoch += 1
            job.current_slug = job.slug_for_epoch(job.epoch)
            if job.current_slug not in job.chain_slugs:
                job.chain_slugs.append(job.current_slug)
            job.retry_count = 0
        if transition.target in (JobState.RUNNING, JobState.READY):
            job.retry_count = 0
        job.touch()

        event = Event.create(
            job_id=job.job_id, epoch=job.epoch, trigger=trigger,
            from_state=source, to_state=transition.target, actor=actor,
            correlation_id=correlation_id, note=transition.note, **detail,
        )
        job.record_event(event)
        self.store.append_event(event)
        log_event(log, "state_transition",
                  f"{source.value} --{trigger.value}--> {transition.target.value}",
                  job_id=job.job_id, epoch=job.epoch, from_state=source.value,
                  to_state=transition.target.value, trigger=trigger.value,
                  actor=actor, **detail)
        return job

    # -- routing -----------------------------------------------------------
    #: Triggers excluded when the reconciler needs to *walk* a job to a state it
    #: has already reached in reality. These represent real failures and
    #: operator actions; using one as a routing step would write a false cause
    #: into the ledger, which is worse than not routing at all.
    _ROUTING_EXCLUDED = frozenset({
        Trigger.CANCEL, Trigger.OPERATOR_RESUME, Trigger.OPERATOR_FAIL,
        Trigger.ORCA_FATAL, Trigger.ORCA_UNAVAILABLE, Trigger.BUDGET_EXHAUSTED,
        Trigger.PUSH_EXHAUSTED, Trigger.FETCH_EXHAUSTED, Trigger.SUCCESSOR_EXHAUSTED,
        Trigger.NO_VALID_CHECKPOINT, Trigger.STAGING_FAILED,
        Trigger.VERIFICATION_FAILED, Trigger.RESTORE_FAILED,
        Trigger.KERNEL_NEVER_STARTED,
    })

    def _find_path(self, source: JobState, target: JobState, job: JobManifest,
                   *, allow_failure_triggers: bool = False) -> list[Trigger] | None:
        """Breadth-first search for a legal trigger sequence from `source` to
        `target`.

        This exists because of a real modelling gap. When a window verifies a
        checkpoint and then dies before its successor is accepted, the server
        still believes the job is RUNNING -- it never saw CHECKPOINTING or
        VERIFYING happen. Completing the commit from RUNNING requires replaying
        the transitions the window actually performed but did not get to report.

        Searching the transition table is strictly better than hardcoding that
        sequence: the path stays correct when the machine changes, and a path
        that does not exist produces a clear failure instead of an invented
        transition. Nothing is ever reached by assignment.

        Epoch-advancing transitions are excluded, because routing must never
        have the side effect of consuming a session window.
        """
        if source is target:
            return []
        queue: list[tuple[JobState, list[Trigger]]] = [(source, [])]
        seen = {source}
        while queue:
            state, path = queue.pop(0)
            for trigger in TRANSITIONS.allowed_triggers(state):
                if trigger in self._ROUTING_EXCLUDED and not allow_failure_triggers:
                    continue
                transition = TRANSITIONS.get(state, trigger)
                if transition is None or transition.advances_epoch:
                    continue
                if transition.target.is_terminal and transition.target is not target:
                    continue
                if transition.guard is not None and not transition.guard(job):
                    continue
                if transition.target is target:
                    return path + [trigger]
                if transition.target not in seen:
                    seen.add(transition.target)
                    queue.append((transition.target, path + [trigger]))
        return None

    def _route_to(self, job: JobManifest, target: JobState, *, actor: str,
                  correlation_id: str, reason: str,
                  allow_failure_triggers: bool = False) -> JobManifest:
        """Walks a job to `target` through legal transitions only."""
        if job.state is target:
            return job
        path = self._find_path(job.state, target, job,
                               allow_failure_triggers=allow_failure_triggers)
        if path is None:
            path = self._find_path(job.state, target, job, allow_failure_triggers=True)
        if path is None:
            raise IllegalTransitionError(
                f"no legal path from {job.state.value} to {target.value}",
                state=job.state.value, target=target.value,
            )
        for trigger in path:
            job = self.transition(job, trigger, actor=actor,
                                  correlation_id=correlation_id,
                                  routing=True, routing_reason=reason)
        return job

    # -- the loop ----------------------------------------------------------
    def reconcile(self, job_id: str, creds: KaggleCredentials, *,
                  actor: str = "system") -> JobManifest:
        """One full pass for one job. Safe to call concurrently and repeatedly.

        A caller that cannot take the lease gets the current cached manifest
        back rather than an error: from the user's point of view "someone else
        is already working on this" and "I just did the work" produce the same
        answer, and surfacing a lock-contention error to a browser poll would
        be noise."""
        correlation_id = new_correlation_id()
        holder = f"{os.getpid()}:{id(self):x}"

        with log_context(correlation_id=correlation_id, job_id=job_id):
            with self.store.lease(f"job:{job_id}", holder) as lease:
                if lease is None:
                    log_event(log, "reconcile_skipped_locked",
                              "another worker holds this job's lease; skipping to avoid "
                              "duplicate work",
                              job_id=job_id)
                    return self.store.require_job(job_id)

                job = self.store.require_job(job_id)
                version = int(job._extra.get("_version", 0)) or None
                client = KaggleClient(creds)

                obs = observe(client, job)
                decision = decide(job, obs, config=self.config)

                # decision.detail is nested rather than splatted: it is
                # attacker-adjacent free-form data (it can carry keys named
                # `state`, `epoch`, `error`) and splatting it collides with the
                # record's own fields. A logging call must never be able to
                # raise inside the reconciler.
                log_event(log, "reconcile_decision", decision.reason,
                          job_id=job_id, epoch=job.epoch, state=job.state.value,
                          trigger=decision.trigger.value if decision.trigger else None,
                          action=decision.action, observation=obs.to_dict(),
                          detail=decision.detail)

                if decision.is_noop:
                    job.touch()
                    return self._save(job, version, lease.fence)

                try:
                    job = self._act(job, decision, obs, client, lease.fence,
                                    correlation_id, actor)
                except LeaseLostError:
                    log_event(log, "reconcile_fenced",
                              "our lease was taken over mid-action; abandoning without "
                              "writing, the new holder will redo the work idempotently",
                              job_id=job_id)
                    return self.store.require_job(job_id)
                except ConcurrencyError:
                    log_event(log, "reconcile_raced",
                              "another writer committed first; re-reading on the next pass",
                              job_id=job_id)
                    return self.store.require_job(job_id)

                return job

    def _save(self, job: JobManifest, version: int | None, fence: int) -> JobManifest:
        return self.store.put_job(job, expected_version=version, fence=fence)

    def _act(self, job: JobManifest, decision: Decision, obs: Observation,
             client: KaggleClient, fence: int, correlation_id: str,
             actor: str) -> JobManifest:
        version = int(job._extra.get("_version", 0)) or None

        # ---- adopt the ledger's own view --------------------------------
        if decision.action == "adopt_ledger" and obs.record is not None:
            job = self._adopt(job, obs.record, decision, correlation_id, actor)
            return self._save(job, version, fence)

        # ---- roll back --------------------------------------------------
        if decision.action == "rollback":
            if decision.trigger is not None and TRANSITIONS.can(job.state, decision.trigger, job):
                job = self.transition(job, decision.trigger, actor=actor,
                                      correlation_id=correlation_id, **decision.detail)
            job = self._route_to(job, JobState.ROLLING_BACK, actor=actor,
                                 correlation_id=correlation_id,
                                 reason=decision.reason, allow_failure_triggers=True)
            job = self._rollback(job, client, fence, correlation_id, actor)
            return self._save(job, version, fence)

        # ---- push (or re-push) a window ---------------------------------
        if decision.action == "push_successor":
            # Take the window's checkpoint FIRST.
            #
            # This branch is reached when a window verified a checkpoint and
            # then died before its successor was accepted. That checkpoint
            # exists only in the window's ledger; it is not yet in the local
            # store. Pushing without adopting it would build the successor with
            # `checkpoint=None` -- silently discarding every optimisation cycle
            # the window paid for and restarting from the original geometry.
            # The lifecycle simulation exists in part to catch exactly this.
            if obs.record is not None:
                job = self._adopt_checkpoint(job, obs.record)
            if decision.trigger is not None and TRANSITIONS.can(job.state, decision.trigger, job):
                job = self.transition(job, decision.trigger, actor=actor,
                                      correlation_id=correlation_id, **decision.detail)
            job = self._push_successor(job, client, fence, correlation_id, actor)
            return self._save(job, version, fence)

        # ---- plain transition -------------------------------------------
        if decision.trigger is not None:
            try:
                job = self.transition(job, decision.trigger, actor=actor,
                                      correlation_id=correlation_id, **decision.detail)
            except IllegalTransitionError as exc:
                # The FSM refused. That means our belief about the job's state
                # is wrong, not that the observation is wrong -- so re-derive
                # from the ledger rather than forcing the transition.
                log_failure(
                    log,
                    what="applying a state transition",
                    why=exc.message,
                    recovery="the transition was refused by the state machine, so no state "
                             "was written",
                    next_action="re-deriving the job's state from the Kaggle ledger on the "
                                "next pass",
                    exc=exc, job_id=job.job_id,
                )
                if obs.record is not None and obs.record.job is not None:
                    job = self._adopt(job, obs.record, decision, correlation_id, actor)
        return self._save(job, version, fence)

    # -- effects ----------------------------------------------------------
    def _adopt(self, job: JobManifest, record: LedgerRecord, decision: Decision,
               correlation_id: str, actor: str) -> JobManifest:
        """Takes the window's own account of itself as authoritative.

        The window ran the calculation; we did not. Where the ledger and the
        local cache disagree, the ledger wins -- that is the whole point of
        making Kaggle the source of truth. Local-only fields (chain membership
        derived from the kernel listing) are preserved because the ledger
        cannot know about windows created after it was written."""
        ledger_job = record.job
        if ledger_job is not None:
            chain = list(dict.fromkeys(job.chain_slugs + ledger_job.chain_slugs))
            previous_state = job.state

            ledger_job.chain_slugs = chain
            ledger_job.current_slug = newest_window_slug(job.job_id, chain)
            ledger_job.owner = job.owner or ledger_job.owner
            ledger_job.title = job.title or ledger_job.title
            # Carry the stall clock across adoption. A window's own STATE.json
            # reports its state but not when *we* first saw the job in it, and
            # taking the window's timestamp would reset the clock on every
            # adoption -- reintroducing the blindness this field exists to fix.
            if ledger_job.state is previous_state:
                ledger_job.state_entered_at = (job.state_entered_at
                                               or ledger_job.state_entered_at)
            else:
                ledger_job.state_entered_at = now()
            if record.heartbeat:
                ledger_job.last_heartbeat_at = record.heartbeat.get("at")
                ledger_job.heartbeat_detail = record.heartbeat.get("detail", {})
            ledger_job._extra["_version"] = job._extra.get("_version")
            job = ledger_job

            event = Event.create(
                job_id=job.job_id, epoch=job.epoch, trigger="ADOPT_LEDGER",
                from_state=previous_state, to_state=job.state, actor=actor,
                correlation_id=correlation_id, reason=decision.reason, **decision.detail,
            )
            job.record_event(event)
            self.store.append_event(event)

        job = self._adopt_checkpoint(job, record)

        if record.legacy_next_slug and record.legacy_next_slug not in job.chain_slugs:
            job.chain_slugs.append(record.legacy_next_slug)
            job.current_slug = record.legacy_next_slug
            job.epoch = max(job.epoch + 1, job.epoch)
            job.state = JobState.QUEUED

        if record.note:
            job.last_note = record.note[:2000]
        job.touch()
        return job

    def _adopt_checkpoint(self, job: JobManifest, record: LedgerRecord) -> JobManifest:
        """Persists a checkpoint reported by a window and advances the anchor.

        The previous anchor is kept as `previous_checkpoint_id` rather than
        discarded. That second pointer is what makes rollback survive a
        checkpoint that verifies structurally but that ORCA nonetheless refuses
        -- without it, the newest verified checkpoint would be the only fallback
        and a poisoned one would leave nowhere to go.
        """
        checkpoint = record.checkpoint
        if checkpoint is None:
            return job

        self.store.put_checkpoint(checkpoint)
        if not checkpoint.is_usable:
            log_event(log, "checkpoint_ignored",
                      "the window reported a checkpoint that is not in a usable state; "
                      "it is stored for the audit trail but will never be restarted from",
                      job_id=job.job_id, checkpoint_id=checkpoint.checkpoint_id,
                      status=checkpoint.status,
                      rejection_reason=checkpoint.rejection_reason)
            return job

        if (job.verified_checkpoint_id
                and job.verified_checkpoint_id != checkpoint.checkpoint_id):
            job.previous_checkpoint_id = job.verified_checkpoint_id
        job.verified_checkpoint_id = checkpoint.checkpoint_id
        job.cumulative_opt_cycles = max(job.cumulative_opt_cycles,
                                        checkpoint.cumulative_opt_cycles)
        log_event(log, "checkpoint_adopted",
                  "adopted a verified checkpoint reported by the window",
                  job_id=job.job_id, checkpoint_id=checkpoint.checkpoint_id,
                  epoch=checkpoint.epoch, phase=checkpoint.orca_phase,
                  files=len(checkpoint.files),
                  cumulative_opt_cycles=checkpoint.cumulative_opt_cycles,
                  previous=job.previous_checkpoint_id)
        return job

    def _rollback(self, job: JobManifest, client: KaggleClient, fence: int,
                  correlation_id: str, actor: str) -> JobManifest:
        """Selects a rollback target and re-drives from it."""
        target = ckpt.select_rollback_target(
            job=job,
            load_checkpoint=self.store.get_checkpoint,
            find_latest_verified=self.store.latest_verified_checkpoint,
            failed_epoch=job.epoch,
        )
        if target is None:
            return self.transition(
                job, Trigger.NO_VALID_CHECKPOINT, actor=actor,
                correlation_id=correlation_id,
                reason="no verified checkpoint exists before the failed window, so there is "
                       "nothing to resume from; restarting from zero would silently repeat "
                       "work and would very likely fail the same way",
            )

        job.rollback_count += 1
        job.previous_checkpoint_id = job.verified_checkpoint_id
        job.verified_checkpoint_id = target.checkpoint_id
        job = self.transition(
            job, Trigger.ROLLBACK_SELECTED, actor=actor, correlation_id=correlation_id,
            checkpoint_id=target.checkpoint_id, target_epoch=target.epoch,
            rollback_count=job.rollback_count,
        )
        return self._push_successor(job, client, fence, correlation_id, actor)

    def _push_successor(self, job: JobManifest, client: KaggleClient, fence: int,
                        correlation_id: str, actor: str) -> JobManifest:
        """Builds and pushes the next window.

        Ordering is deliberate and is the crash-safety property: the *intent*
        (state RESTARTING, event recorded) is already persisted before the push
        is attempted. If this process dies mid-push, the next reconciliation
        observes RESTARTING with no newer kernel and replays the push -- which
        is safe because the successor slug is deterministic, so a replay
        updates one kernel rather than creating a second."""
        from .runner.builder import build_window_directory

        # The job must be in RESTARTING before a successor can be committed.
        # It often is not: when a window verifies a checkpoint and then dies
        # before its push is accepted, the server still believes the job is
        # RUNNING, because it never observed CHECKPOINTING or VERIFYING. Rather
        # than assign the state, walk the transitions the window really
        # performed, so the ledger records an honest causal history.
        if job.state is not JobState.RESTARTING and not job.is_terminal:
            job = self._route_to(
                job, JobState.RESTARTING, actor=actor, correlation_id=correlation_id,
                reason="completing a handoff the window began but did not finish "
                       "reporting before it stopped",
            )

        if job.epoch + 1 > job.max_epochs:
            return self.transition(
                job, Trigger.BUDGET_EXHAUSTED, actor=actor, correlation_id=correlation_id,
                reason=f"the job has used its budget of {job.max_epochs} session windows "
                       f"without converging",
                max_epochs=job.max_epochs,
            )
        if job.cumulative_opt_cycles >= job.max_total_opt_cycles:
            return self.transition(
                job, Trigger.BUDGET_EXHAUSTED, actor=actor, correlation_id=correlation_id,
                reason=f"the optimisation has run {job.cumulative_opt_cycles} cumulative "
                       f"cycles across all windows without converging, which exceeds the "
                       f"budget of {job.max_total_opt_cycles}; the geometry or the method "
                       f"needs a human decision rather than more compute",
                cumulative_opt_cycles=job.cumulative_opt_cycles,
            )

        checkpoint = (self.store.get_checkpoint(job.verified_checkpoint_id)
                      if job.verified_checkpoint_id else None)
        if checkpoint is not None and not checkpoint.is_usable:
            checkpoint = None

        next_epoch = job.epoch + 1
        target_slug = job.slug_for_epoch(next_epoch)

        # Duplicate-launch guard, before anything is built. If the successor is
        # already alive, this pass has nothing to do -- and doing it anyway
        # would schedule a second concurrent run writing to the same output.
        existing = client.kernel_exists(target_slug)
        if existing is not None and existing.is_active:
            log_event(log, "successor_already_active",
                      "the successor window is already running on Kaggle; adopting it "
                      "instead of pushing a duplicate",
                      job_id=job.job_id, slug=target_slug, kaggle_status=existing.status)
            return self.transition(job, Trigger.SUCCESSOR_PUSHED, actor=actor,
                                   correlation_id=correlation_id,
                                   slug=target_slug, adopted_existing=True)

        work_dir = tempfile.mkdtemp(prefix="orca-window-")
        try:
            build_window_directory(
                work_dir, job=job, epoch=next_epoch, checkpoint=checkpoint,
                creds=client.creds,
            )
            result = client.push_kernel(work_dir, expected_slug=target_slug,
                                        skip_if_active=True)
        except PermanentError as exc:
            job.last_error = exc.to_dict()
            job.retry_count += 1
            job.total_retries += 1
            log_failure(
                log,
                what="pushing the successor window",
                why=f"{exc.code}: {exc.message}",
                recovery=f"the checkpoint remains verified and committed; nothing was lost",
                next_action="failing the job -- a permanent push error cannot be resolved "
                            "by replaying the identical request",
                exc=exc, job_id=job.job_id, epoch=next_epoch,
            )
            return self.transition(job, Trigger.SUCCESSOR_EXHAUSTED, actor=actor,
                                   correlation_id=correlation_id, error=exc.to_dict())
        except OrchestratorError as exc:
            job.last_error = exc.to_dict()
            job.retry_count += 1
            job.total_retries += 1
            if job.retry_count >= self.config.retry.max_attempts:
                return self.transition(job, Trigger.SUCCESSOR_EXHAUSTED, actor=actor,
                                       correlation_id=correlation_id, error=exc.to_dict(),
                                       retry_count=job.retry_count)
            log_failure(
                log,
                what="pushing the successor window",
                why=f"{exc.code}: {exc.message}",
                recovery=f"retry {job.retry_count}/{self.config.retry.max_attempts}; the "
                         f"successor slug is deterministic so a replay is safe",
                next_action="the next reconciliation pass will replay the push",
                exc=exc, job_id=job.job_id, epoch=next_epoch,
            )
            return self.transition(job, Trigger.SUCCESSOR_RETRY, actor=actor,
                                   correlation_id=correlation_id,
                                   retry_count=job.retry_count)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        if checkpoint is not None and checkpoint.status == CheckpointStatus.VERIFIED:
            ckpt.commit_checkpoint(checkpoint)
            self.store.put_checkpoint(checkpoint)

        job.current_url = result.url
        job = self.transition(job, Trigger.SUCCESSOR_PUSHED, actor=actor,
                              correlation_id=correlation_id,
                              slug=result.slug, url=result.url,
                              checkpoint_id=checkpoint.checkpoint_id if checkpoint else None,
                              slug_diverged=not result.slug_matches_request)
        if result.slug != target_slug:
            # Kaggle placed the notebook somewhere other than requested. Follow
            # it, or every subsequent poll addresses a kernel that is not there.
            job.current_slug = result.slug
            if result.slug not in job.chain_slugs:
                job.chain_slugs.append(result.slug)
        return job
