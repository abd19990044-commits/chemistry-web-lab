# -*- coding: utf-8 -*-
"""
Stall detection and automatic recovery.

The reconciler answers "what should happen to this job?" when someone asks.
The watchdog answers "which jobs is nobody asking about, and are any of them
stuck?" -- which is the question that matters when the browser is closed, the
worker that was driving a job has crashed, or a Kaggle session was killed
without ever reporting why.

What counts as stalled
----------------------
Not "old". A twelve-hour ORCA window is supposed to look idle. A job is
stalled when it has been in a *particular* state longer than that state's own
deadline:

    QUEUED        -> queued_grace       (accepted by Kaggle, never scheduled)
    RUNNING       -> heartbeat_grace    (the process inside the kernel is dead)
    handoff states-> handoff_grace      (a transaction was started, never finished)
    any active    -> stall_escalation   (no epoch advance at all; needs a human)

Per-state deadlines matter because a single global timeout is always wrong in
one direction: short enough to catch a dead handoff is short enough to
interrupt a healthy calculation.

Honest limits
-------------
Under the chosen "no stored credentials" policy the watchdog can only act on
jobs whose owner has supplied credentials recently (`CredentialBroker`, in RAM,
TTL-bounded). Between those moments the chain is carried by the Kaggle kernel,
which self-continues. This is a real constraint, not an implementation gap, and
it is reported in `sweep()`'s result as `skipped_no_credentials` rather than
being quietly ignored -- an operator should be able to see how much of the
fleet the watchdog can currently reach.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .config import CONFIG
from .credentials import BROKER, CredentialBroker
from .errors import OrchestratorError
from .logging_ext import get_logger, log_context, log_event, log_failure, new_correlation_id
from .models import JobManifest, now
from .reconciler import Reconciler
from .states import JobState, Trigger
from .store import JobStore

log = get_logger("orca.watchdog")


@dataclass
class StallVerdict:
    stalled: bool
    reason: str = ""
    age_seconds: float = 0.0
    deadline_seconds: float = 0.0
    escalate: bool = False

    def to_dict(self) -> dict:
        return {"stalled": self.stalled, "reason": self.reason,
                "age_seconds": round(self.age_seconds, 1),
                "deadline_seconds": self.deadline_seconds, "escalate": self.escalate}


def assess(job: JobManifest, *, config=CONFIG, reference_time: float | None = None) -> StallVerdict:
    """Pure predicate. No I/O, so the stall rules are directly testable against
    synthetic manifests rather than only against a live Kaggle account."""
    ts = reference_time if reference_time is not None else now()
    if job.is_terminal:
        return StallVerdict(False, "terminal")

    # Measured from when the job ENTERED this state, never from its last write.
    # `updated_at` moves on every status poll, so using it meant a job anyone was
    # watching could never age into "stalled" -- see JobManifest.state_entered_at.
    reference = job.state_entered_at or job.updated_at or job.created_at
    age = max(0.0, ts - reference)
    wd = config.watchdog

    total_age = max(0.0, ts - job.created_at)
    if total_age > wd.stall_escalation_seconds and job.epoch == 0:
        return StallVerdict(
            True,
            "the job has existed for longer than the escalation threshold without ever "
            "advancing past its first window",
            total_age, wd.stall_escalation_seconds, escalate=True,
        )

    if job.state is JobState.QUEUED:
        if age > wd.queued_grace_seconds:
            return StallVerdict(
                True,
                "the kernel was accepted by Kaggle but has not started within the grace "
                "period; the push may not have produced a scheduled run",
                age, wd.queued_grace_seconds,
            )
        return StallVerdict(False, "queued within grace", age, wd.queued_grace_seconds)

    if job.state is JobState.RUNNING:
        beat = job.last_heartbeat_at
        beat_age = (ts - beat) if beat else age
        if beat_age > wd.heartbeat_grace_seconds:
            return StallVerdict(
                True,
                "no heartbeat from the running window for longer than the grace period, so "
                "the process inside the kernel has died without reporting an outcome",
                beat_age, wd.heartbeat_grace_seconds,
            )
        return StallVerdict(False, "heartbeating", beat_age, wd.heartbeat_grace_seconds)

    if job.state.is_handoff:
        if age > wd.handoff_grace_seconds:
            return StallVerdict(
                True,
                f"the job has been in the handoff state {job.state.value} for longer than "
                f"the grace period; a transaction was started and never completed",
                age, wd.handoff_grace_seconds,
            )
        return StallVerdict(False, "handoff in progress", age, wd.handoff_grace_seconds)

    if age > wd.stall_escalation_seconds:
        return StallVerdict(
            True,
            "no progress of any kind within the escalation window",
            age, wd.stall_escalation_seconds, escalate=True,
        )
    return StallVerdict(False, "active", age, wd.stall_escalation_seconds)


@dataclass
class SweepResult:
    started_at: float = field(default_factory=now)
    examined: int = 0
    stalled: int = 0
    recovered: int = 0
    escalated: int = 0
    failed: int = 0
    skipped_no_credentials: int = 0
    details: list[dict] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return now() - self.started_at

    def to_dict(self) -> dict:
        return {
            "examined": self.examined, "stalled": self.stalled,
            "recovered": self.recovered, "escalated": self.escalated,
            "failed": self.failed,
            "skipped_no_credentials": self.skipped_no_credentials,
            "duration_seconds": round(self.duration_seconds, 2),
            "reachability": (
                "full" if self.skipped_no_credentials == 0 else
                f"partial: {self.skipped_no_credentials} job(s) have no cached credentials, "
                f"so the server cannot act on them until their owner loads the page; those "
                f"chains are still carried forward by the Kaggle kernel itself"
            ),
        }


class Watchdog:
    def __init__(self, store: JobStore, reconciler: Reconciler | None = None,
                 broker: CredentialBroker | None = None, *, config=CONFIG) -> None:
        self.store = store
        self.reconciler = reconciler or Reconciler(store, config=config)
        self.broker = broker or BROKER
        self.config = config
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_result: SweepResult | None = None
        self._quiet_sweeps = 0
        self._quiet_since = now()

    # -- one pass ----------------------------------------------------------
    def sweep(self, *, limit: int = 200) -> SweepResult:
        result = SweepResult()
        correlation_id = new_correlation_id()

        with log_context(correlation_id=correlation_id, component="watchdog"):
            candidates = self.store.list_active_jobs(older_than_seconds=0.0, limit=limit)
            result.examined = len(candidates)

            for job in candidates:
                verdict = assess(job, config=self.config)
                if not verdict.stalled:
                    continue
                result.stalled += 1

                creds = self.broker.get(job.owner)
                if creds is None:
                    result.skipped_no_credentials += 1
                    log_event(
                        log, "watchdog_unreachable",
                        "a stalled job cannot be driven from the server because no "
                        "credentials for its owner are cached; the Kaggle kernel remains "
                        "responsible for continuing the chain",
                        job_id=job.job_id, owner=job.owner, state=job.state.value,
                        **verdict.to_dict(),
                    )
                    result.details.append({"job_id": job.job_id, "action": "skipped",
                                           **verdict.to_dict()})
                    continue

                log_event(log, "watchdog_stall_detected", verdict.reason,
                          job_id=job.job_id, state=job.state.value, epoch=job.epoch,
                          **verdict.to_dict())

                if verdict.escalate:
                    result.escalated += 1
                    self._escalate(job, verdict, correlation_id)
                    result.details.append({"job_id": job.job_id, "action": "escalated",
                                           **verdict.to_dict()})
                    continue

                try:
                    self.reconciler.reconcile(job.job_id, creds, actor="watchdog")
                    result.recovered += 1
                    result.details.append({"job_id": job.job_id, "action": "reconciled",
                                           **verdict.to_dict()})
                except OrchestratorError as exc:
                    result.failed += 1
                    log_failure(
                        log,
                        what="watchdog recovery of a stalled job",
                        why=f"{exc.code}: {exc.message}",
                        recovery="a full reconciliation pass was attempted against the "
                                 "Kaggle-side ledger",
                        next_action=f"retrying on the next sweep in "
                                    f"{self.config.watchdog.sweep_interval_seconds}s",
                        exc=exc, job_id=job.job_id,
                    )
                    result.details.append({"job_id": job.job_id, "action": "failed",
                                           "error": exc.code, **verdict.to_dict()})

        self._last_result = result

        # Log volume is an operational concern, not a cosmetic one.
        #
        # A sweep every 2 minutes from each of 2 workers is ~1,440 lines a day
        # saying "nothing happened". Those lines are not free: they are the
        # noise a person has to scroll past while trying to find the one event
        # that explains why a calculation stopped. An uneventful sweep is
        # therefore DEBUG, and INFO is reserved for sweeps that actually did
        # something -- so an INFO line from this logger always means something
        # worth reading.
        #
        # A periodic summary still goes out at INFO even when idle, because
        # "the watchdog has said nothing" and "the watchdog is dead" must remain
        # distinguishable from the log alone.
        did_something = bool(result.stalled or result.recovered or result.failed
                             or result.escalated or result.skipped_no_credentials)
        quiet_seconds = now() - self._quiet_since
        summary_due = quiet_seconds >= max(900, self.config.watchdog.sweep_interval_seconds * 20)

        if did_something:
            log_event(log, "watchdog_sweep_complete", "watchdog sweep finished",
                      **result.to_dict())
            self._quiet_sweeps = 0
            self._quiet_since = now()
        elif summary_due:
            log_event(log, "watchdog_alive",
                      "watchdog is running; nothing has needed attention",
                      quiet_sweeps=self._quiet_sweeps + 1,
                      quiet_for_seconds=round(quiet_seconds),
                      jobs_examined=result.examined)
            self._quiet_sweeps = 0
            self._quiet_since = now()
        else:
            self._quiet_sweeps += 1
            log.debug("watchdog sweep finished with nothing to do",
                      extra={"event": "watchdog_sweep_quiet", **result.to_dict()})
        return result

    def _escalate(self, job: JobManifest, verdict: StallVerdict, correlation_id: str) -> None:
        """Records an unrecoverable stall against the job.

        Deliberately does not fail the job. An escalation means the *automation*
        has run out of ideas, which is not the same as the calculation being
        doomed -- the Kaggle window may still be making progress the server
        cannot observe. Failing it here would destroy real work to satisfy a
        timeout."""
        try:
            fresh = self.store.require_job(job.job_id)
            fresh.last_note = (
                f"Watchdog escalation: {verdict.reason}. The job has been in state "
                f"{fresh.state.value} for {verdict.age_seconds / 3600.0:.1f} h. Automatic "
                f"recovery has been attempted and has not restored progress; this needs a "
                f"look. The calculation has NOT been cancelled -- if the Kaggle window is "
                f"still running it will continue."
            )
            fresh.last_error = {
                "code": "watchdog_escalation", "reason": verdict.reason,
                "age_seconds": verdict.age_seconds, "at": now(),
            }
            fresh.touch()
            self.store.put_job(fresh, expected_version=fresh._extra.get("_version"))
        except OrchestratorError:
            pass
        log.error("watchdog escalation", extra={
            "event": "watchdog_escalation", "job_id": job.job_id,
            "state": job.state.value, "epoch": job.epoch,
            "correlation_id": correlation_id, **verdict.to_dict(),
        })

    # -- background thread -------------------------------------------------
    def start(self) -> None:
        """Starts the sweeper.

        A daemon thread rather than a process: it must not keep the container
        alive during a Hugging Face shutdown, and there is nothing here worth
        blocking a redeploy for -- every action it performs is idempotent and
        will simply be redone after the restart."""
        if not self.config.watchdog.enabled:
            log_event(log, "watchdog_disabled",
                      "the watchdog is disabled by configuration")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="orca-watchdog", daemon=True)
        self._thread.start()
        log_event(log, "watchdog_started", "background stall sweeper started",
                  interval_seconds=self.config.watchdog.sweep_interval_seconds)

    def _run(self) -> None:
        interval = self.config.watchdog.sweep_interval_seconds
        # Stagger the first sweep. With two gunicorn workers both starting at
        # deploy time, an unstaggered sweep means both hit the same jobs in the
        # same second; the lease makes that correct but it is wasted work.
        self._stop.wait(timeout=interval * (0.5 + 0.5 * (time.time() % 1.0)))
        while not self._stop.is_set():
            try:
                self.sweep()
            except Exception as exc:  # noqa: BLE001 - the sweeper must never die
                log_failure(
                    log,
                    what="watchdog sweep",
                    why=f"unexpected error: {exc}",
                    recovery="the sweep was abandoned; no partial state was written because "
                             "every job is reconciled independently under its own lease",
                    next_action=f"sweeping again in {interval}s",
                    exc=exc,
                )
            self._stop.wait(timeout=interval)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        log_event(log, "watchdog_stopped", "background stall sweeper stopped")

    @property
    def last_result(self) -> dict | None:
        return self._last_result.to_dict() if self._last_result else None


def recover_after_restart(store: JobStore) -> dict:
    """Startup reconciliation, run once per process on boot.

    Answers the question every crash-recovery design has to answer: what do we
    do about work that was in flight when the lights went out?

      * Expired **leases** are cleared. A lease held by a process that no longer
        exists would otherwise block its job until the TTL ran out.
      * In-flight **idempotency claims** are abandoned. A claim made just before
        a crash, with no work behind it, would otherwise wedge that key for a
        full day and make a legitimate resubmission look like a duplicate.
      * Jobs stuck in a **handoff state** are marked for priority reconciliation
        rather than being repaired blindly here. Repair needs credentials and
        needs to observe Kaggle first; guessing at boot is how a system
        double-pushes a window that actually succeeded.

    Nothing is failed and nothing is rolled back at startup. The calculations
    on Kaggle kept running while the server was down, and the correct first
    move is always to look before acting.
    """
    ts = now()
    with store.transaction() as conn:
        expired = conn.execute(
            "DELETE FROM leases WHERE expires_at <= ?", (ts,)
        ).rowcount
        stale_claims = conn.execute(
            "DELETE FROM idempotency WHERE status = 'in_progress' AND created_at < ?",
            (ts - 900,),
        ).rowcount

    in_flight = [j for j in store.list_active_jobs(limit=500) if j.state.is_handoff]
    report = {
        "expired_leases_cleared": expired,
        "stale_idempotency_claims_cleared": stale_claims,
        "jobs_in_handoff_state": len(in_flight),
        "handoff_job_ids": [j.job_id for j in in_flight][:50],
        "policy": "no job is repaired at startup; each is reconciled against the Kaggle "
                  "ledger on its next pass, because the correct recovery depends on what "
                  "Kaggle actually did while this process was down",
    }
    log_event(log, "startup_recovery", "startup reconciliation complete", **report)
    return report
