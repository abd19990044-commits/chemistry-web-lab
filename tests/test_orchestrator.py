# -*- coding: utf-8 -*-
"""
Verification suite for orca_orchestrator.

Runs with plain `python tests/test_orchestrator.py` -- no pytest required, so
it can execute inside the Docker image on Hugging Face without adding a
dependency. Every check that reproduces a real production failure names it.

Coverage targets, in order of importance:

  1. The two observed production bugs, reproduced as regression tests.
  2. The finite-state machine: structure, legal and illegal transitions, guards.
  3. Transactional checkpoints: verification, corruption detection, rollback.
  4. Concurrency: optimistic locking, fencing tokens, idempotency keys.
  5. The generated Kaggle script actually compiles.
"""
import io as _io
import json as _json
import logging as _logging
import multiprocessing as _mp
import os
import re as _re
import shutil
import sqlite3 as _sq3
import sys
import tempfile
import threading as _th
import time
import types
import base64 as _b64

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orca_orchestrator import orca_artifacts as art          # noqa: E402
from orca_orchestrator.checkpoints import (commit_checkpoint, select_rollback_target,  # noqa: E402
                                           stage_checkpoint, verify_checkpoint)
from orca_orchestrator.config import Config, StoreConfig, _probe_writable  # noqa: E402
from orca_orchestrator.credentials import KaggleCredentials, parse  # noqa: E402
from orca_orchestrator.errors import (ConcurrencyError, IllegalTransitionError,  # noqa: E402
                                      IntegrityError, LeaseLostError, NetworkError)
from orca_orchestrator.kaggle_api import KernelStatus      # noqa: E402
from orca_orchestrator.ledger import LedgerRecord          # noqa: E402
from orca_orchestrator.logging_ext import (JsonFormatter, RedactingFilter,  # noqa: E402
                                           log_failure, redact, redact_structure)
from orca_orchestrator.models import CheckpointManifest, CheckpointStatus, Event, JobManifest  # noqa: E402
from orca_orchestrator.reconciler import Observation, decide     # noqa: E402
from orca_orchestrator.runner import builder as _builder        # noqa: E402
from orca_orchestrator.runner.builder import build_header, render_script  # noqa: E402
from orca_orchestrator.states import TRANSITIONS, JobState, Trigger  # noqa: E402
from orca_orchestrator.store import JobStore, _is_lock_error   # noqa: E402
from orca_orchestrator.watchdog import assess                  # noqa: E402


def section(title):
    print("\n" + title)
    print("-" * len(title))


def _boot_worker(state_dir, barrier, queue):
    try:
        from orca_orchestrator.config import StoreConfig as _SC
        from orca_orchestrator.store import JobStore as _JS
        barrier.wait(timeout=30)
        store = _JS(_SC(state_dir=state_dir))
        mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
        store.close()
        queue.put(("OK", mode))
    except Exception as exc:
        queue.put((type(exc).__name__, str(exc)))


def _pick_dir(shared, barrier, queue):
    import os as _os
    _os.environ["ORCA_STATE_DIR"] = shared
    from orca_orchestrator.config import _default_state_dir as _pick
    barrier.wait(timeout=30)
    queue.put(_pick())


def run_all_checks():
    PASS, FAIL = [], []

    def check(name, condition, detail=""):
        if condition:
            PASS.append(name)
            print("  PASS  %s" % name)
        else:
            FAIL.append((name, detail))
            print("  FAIL  %s %s" % (name, ("-- " + detail) if detail else ""))

    # ===========================================================================
    section("1. Regression: ORCA 'TERMINATED NORMALLY' on an unconverged optimisation")
    # ===========================================================================
    MAXITER_OUTPUT = """
                            GEOMETRY OPTIMIZATION CYCLE 199

    FINAL SINGLE POINT ENERGY      -1234.567890123

                            GEOMETRY OPTIMIZATION CYCLE 200

    The optimization did not converge but reached the maximum number of
    optimization cycles. Please check your results very carefully.

                                 ****ORCA TERMINATED NORMALLY****
    TOTAL RUN TIME: 0 days 6 hours 23 minutes 54 seconds
    """

    outcome = art.classify_outcome(MAXITER_OUTPUT, job_kind="opt")
    check("MaxIter exhaustion is NOT classified as complete",
          not outcome.is_complete, "got %s" % outcome.kind)
    check("MaxIter exhaustion is classified as MAXITER_EXHAUSTED",
          outcome.kind == art.OUTCOME_MAXITER, "got %s" % outcome.kind)
    check("MaxIter exhaustion is continuable",
          outcome.is_continuable)
    check("normal termination is still detected",
          outcome.normal_end)
    check("opt_converged is correctly False",
          outcome.opt_converged is False)
    check("the optimisation cycle count is extracted for budgeting",
          outcome.opt_cycles == 200, "got %s" % outcome.opt_cycles)
    check("the final energy is extracted",
          outcome.energy is not None and abs(outcome.energy + 1234.567890123) < 1e-6)
    check("the line-wrapped ORCA warning is matched after whitespace normalisation",
          "maximum number of optimisation cycles was reached" in outcome.reason)

    CONVERGED_OUTPUT = """
                            GEOMETRY OPTIMIZATION CYCLE 42
                       ***********************HURRAY********************
                       ***        THE OPTIMIZATION HAS CONVERGED     ***
                       *************************************************
    FINAL SINGLE POINT ENERGY      -76.4321
                                 ****ORCA TERMINATED NORMALLY****
    """
    converged = art.classify_outcome(CONVERGED_OUTPUT, job_kind="opt")
    check("a genuinely converged optimisation IS complete",
          converged.is_complete, "got %s" % converged.kind)
    check("convergence is detected from the HURRAY banner",
          converged.opt_converged)

    opt_freq_partial = art.classify_outcome(CONVERGED_OUTPUT, job_kind="opt_freq")
    check("Opt Freq without VIBRATIONAL FREQUENCIES is not complete",
          not opt_freq_partial.is_complete, "got %s" % opt_freq_partial.kind)

    opt_freq_done = art.classify_outcome(
        CONVERGED_OUTPUT + "\nVIBRATIONAL FREQUENCIES\n", job_kind="opt_freq")
    check("Opt Freq with frequencies present IS complete",
          opt_freq_done.is_complete, "got %s" % opt_freq_done.kind)

    sp = art.classify_outcome("FINAL SINGLE POINT ENERGY  -1.0\n****ORCA TERMINATED NORMALLY****",
                              job_kind="sp")
    check("a single point IS complete on normal termination", sp.is_complete)

    oom = art.classify_outcome("Error: not enough memory available\naborting the run",
                               job_kind="opt")
    check("an out-of-memory abort is continuable, not fatal",
          oom.is_continuable and not oom.is_fatal, "got %s" % oom.kind)

    bad_input = art.classify_outcome("UNRECOGNIZED OR DUPLICATED KEYWORD: FOO\naborting the run",
                                     job_kind="opt")
    check("a genuine input error IS fatal", bad_input.is_fatal, "got %s" % bad_input.kind)

    killed = art.classify_outcome(CONVERGED_OUTPUT, job_kind="opt", killed_by="time")
    check("a watchdog kill overrides any convergence banner in the output",
          not killed.is_complete and killed.kind == art.OUTCOME_INCOMPLETE)

    disk = art.classify_outcome("No space left on device", job_kind="opt")
    check("a disk failure is classified as DISK_EXHAUSTED",
          disk.kind == art.OUTCOME_DISK)

    # ===========================================================================
    section("2. Regression: disk free space measured against the wrong filesystem")
    # ===========================================================================
    _tmp = tempfile.mkdtemp(prefix="orca-disk-test-")
    working = os.path.join(_tmp, "working")
    scratch = os.path.join(_tmp, "scratch")
    os.makedirs(working); os.makedirs(scratch)

    with open(os.path.join(scratch, "big.bin"), "wb") as fh:
        fh.write(b"\0" * (4 << 20))

    class FakeOverlayAccountant(art.DiskAccountant):
        @staticmethod
        def statvfs_free(path):
            return 1006 * (1 << 30)

    acc = FakeOverlayAccountant(
        working, [scratch],
        working_quota=20 << 30, scratch_quota=60 << 30, implausible_free=200 << 30,
    )
    report = acc.snapshot()
    check("an implausible statvfs figure is detected and distrusted",
          report["statvfs_trusted"] is False)
    check("accounting falls back to the quota model",
          report["accounting_mode"] == "quota", report["accounting_mode"])
    check("effective headroom reflects the quota, not the host overlay",
          report["effective_headroom_bytes"] <= 60 << 30,
          "got %.1f GB" % (report["effective_headroom_bytes"] / (1 << 30)))
    check("the discrepancy is explained in the report",
          "host overlay filesystem" in report.get("statvfs_note", ""))
    check("actual consumption is measured", report["scratch_used_bytes"] >= (4 << 20))

    class SmallDiskAccountant(art.DiskAccountant):
        @staticmethod
        def statvfs_free(path):
            return 3 << 30

    small = SmallDiskAccountant(working, [scratch], working_quota=20 << 30,
                                scratch_quota=60 << 30, implausible_free=200 << 30)
    small_report = small.snapshot()
    check("a plausible statvfs value is trusted and bounds the headroom",
          small_report["statvfs_trusted"] and
          small_report["effective_headroom_bytes"] == 3 << 30)

    probe_report = acc.snapshot(probe=True)
    check("the write probe runs and succeeds on a healthy filesystem",
          probe_report.get("probe_ok") is True)

    shutil.rmtree(_tmp, ignore_errors=True)

    # ===========================================================================
    section("3. Finite-state machine")
    # ===========================================================================
    TRANSITIONS.validate()
    check("the transition table passes its structural invariants", True)

    job = JobManifest.create(job_id="chem-tools-test-aabbccdd", owner="tester",
                             title="test", input_filename="mol.inp",
                             original_input_sha256="x", job_kind="opt")
    check("a new job starts in CREATED", job.state is JobState.CREATED)

    t = TRANSITIONS.apply(JobState.CREATED, Trigger.SUBMIT, job)
    check("CREATED --SUBMIT--> UPLOADING", t.target is JobState.UPLOADING)

    try:
        TRANSITIONS.apply(JobState.CREATED, Trigger.ORCA_COMPLETE, job)
        check("an undefined transition raises", False, "no exception")
    except IllegalTransitionError:
        check("an undefined transition raises IllegalTransitionError", True)

    check("exactly one transition advances the epoch",
          sum(1 for tr in TRANSITIONS._all if tr.advances_epoch) == 1)
    check("the epoch-advancing transition is RESTARTING --SUCCESSOR_PUSHED--> QUEUED",
          TRANSITIONS.get(JobState.RESTARTING, Trigger.SUCCESSOR_PUSHED).advances_epoch)

    job.verified_checkpoint_id = None
    check("ROLLBACK_SELECTED is blocked with no verified checkpoint",
          not TRANSITIONS.can(JobState.ROLLING_BACK, Trigger.ROLLBACK_SELECTED, job))
    job.verified_checkpoint_id = "ckpt_1"
    check("ROLLBACK_SELECTED is allowed once a verified checkpoint exists",
          TRANSITIONS.can(JobState.ROLLING_BACK, Trigger.ROLLBACK_SELECTED, job))

    job.epoch, job.max_epochs = 24, 24
    check("CHECKPOINT_VERIFIED is blocked once the epoch budget is spent",
          not TRANSITIONS.can(JobState.VERIFYING, Trigger.CHECKPOINT_VERIFIED, job))
    job.epoch = 3
    check("CHECKPOINT_VERIFIED is allowed with budget remaining",
          TRANSITIONS.can(JobState.VERIFYING, Trigger.CHECKPOINT_VERIFIED, job))

    check("every non-terminal state can be cancelled directly",
          all(TRANSITIONS.can(s, Trigger.CANCEL)
              for s in JobState if not s.is_terminal and s is not JobState.CREATED
              or s is JobState.CREATED))
    check("VERIFYING routes both inbound and outbound",
          TRANSITIONS.get(JobState.VERIFYING, Trigger.BUNDLE_VERIFIED).target is JobState.RESTORING
          and TRANSITIONS.get(JobState.VERIFYING,
                              Trigger.CHECKPOINT_VERIFIED).target is JobState.RESTARTING)
    check("the mermaid diagram is generated from the live table",
          "stateDiagram-v2" in TRANSITIONS.as_mermaid()
          and "RESTARTING --> QUEUED" in TRANSITIONS.as_mermaid())

    # ===========================================================================
    section("4. Checkpoint transactions and rollback")
    # ===========================================================================
    work = tempfile.mkdtemp(prefix="orca-ckpt-test-")

    XYZ = "3\nwater\nO 0.0 0.0 0.0\nH 0.0 0.0 0.96\nH 0.93 0.0 -0.24\n"
    with open(os.path.join(work, "mol.xyz"), "w") as fh:
        fh.write(XYZ)
    with open(os.path.join(work, "mol_trj.xyz"), "w") as fh:
        fh.write(XYZ + XYZ)

    HESS_OK = ("$hessian\n3\n"
               "        0         1         2\n"
               "0   1.0  0.1  0.0\n"
               "1   0.1  1.0  0.2\n"
               "2   0.0  0.2  1.0\n"
               "$end\n")
    with open(os.path.join(work, "mol.hess"), "w") as fh:
        fh.write(HESS_OK)

    job2 = JobManifest.create(job_id="chem-tools-ckpt-11223344", owner="tester", title="c",
                              input_filename="mol.inp", original_input_sha256="y",
                              job_kind="opt")
    cp = stage_checkpoint(
        job=job2, source_dir=work,
        candidate_files=[os.path.join(work, n) for n in ("mol.xyz", "mol_trj.xyz", "mol.hess")],
        next_input_text="! B3LYP def2-SVP Opt\n* xyzfile 0 1 mol.xyz\n",
        orca_phase="opt", completed_opt_cycles=12,
    )
    check("staging produces a STAGED checkpoint", cp.status == CheckpointStatus.STAGED)
    check("staging collects all three artefacts", len(cp.files) == 3, str(len(cp.files)))
    check("the geometry is marked required for an opt job",
          any(f.required and f.role == "geometry" for f in cp.files))
    check("a bundle digest is computed", len(cp.bundle_digest) == 64)

    verify_checkpoint(cp, work, strict=True)
    check("a good checkpoint verifies", cp.status == CheckpointStatus.VERIFIED)
    check("verification stamps each file", all(f.verified_at for f in cp.files))

    commit_checkpoint(cp)
    check("a verified checkpoint can be committed", cp.status == CheckpointStatus.COMMITTED)

    with open(os.path.join(work, "mol.xyz"), "a") as fh:
        fh.write("H 9.9 9.9 9.9\n")
    cp.status = CheckpointStatus.STAGED
    try:
        verify_checkpoint(cp, work, strict=True)
        check("a hash mismatch on a required file is rejected", False, "no exception")
    except IntegrityError:
        check("a hash mismatch on a required file raises IntegrityError", True)
    check("the rejected checkpoint records why", bool(cp.rejection_reason))
    check("a rejected checkpoint is not usable", not cp.is_usable)

    HESS_TRUNCATED = "$hessian\n3\n        0         1         2\n0   1.0  0.1  0.0\n"
    trunc = os.path.join(work, "trunc.hess")
    with open(trunc, "w") as fh:
        fh.write(HESS_TRUNCATED)
    check("a truncated Hessian fails structural validation",
          not art.validate_hessian(trunc).ok)
    check("a complete Hessian passes structural validation",
          art.validate_hessian(os.path.join(work, "mol.hess")).ok)

    torn = os.path.join(work, "torn_trj.xyz")
    with open(torn, "w") as fh:
        fh.write(XYZ + XYZ + "3\npartial\nO 0.0 0.0 0.0\n")
    frames = art.read_trajectory_frames(torn)
    check("a torn trajectory yields only its complete frames",
          len(frames) == 2, "got %d" % len(frames))

    check("an incomplete .allxyz is rejected",
          not art.validate_allxyz(os.path.join(work, "mol.xyz")).ok)

    selected = {}

    def _load(cid):
        return selected.get(cid)

    def _find(job_id, before):
        candidates = [c for c in selected.values()
                      if c.is_usable and (before is None or c.epoch < before)]
        return max(candidates, key=lambda c: c.epoch) if candidates else None

    good = stage_checkpoint(job=job2, source_dir=work,
                            candidate_files=[os.path.join(work, "mol_trj.xyz")],
                            next_input_text="x", orca_phase="opt")
    good.epoch = 2
    verify_checkpoint(good, work, strict=False)
    selected[good.checkpoint_id] = good

    poison = stage_checkpoint(job=job2, source_dir=work,
                              candidate_files=[os.path.join(work, "mol_trj.xyz")],
                              next_input_text="y", orca_phase="opt")
    poison.epoch = 3
    verify_checkpoint(poison, work, strict=False)
    selected[poison.checkpoint_id] = poison

    job2.epoch = 3
    job2.verified_checkpoint_id = poison.checkpoint_id
    target = select_rollback_target(job=job2, load_checkpoint=_load,
                                    find_latest_verified=_find, failed_epoch=3)
    check("rollback selects a checkpoint strictly older than the failed epoch",
          target is not None and target.epoch == 2,
          "got epoch %s" % (target.epoch if target else None))
    check("rollback never re-selects the checkpoint that just failed",
          target is not None and target.checkpoint_id != poison.checkpoint_id)

    job2.verified_checkpoint_id = None
    job2.previous_checkpoint_id = None
    empty = select_rollback_target(job=job2, load_checkpoint=lambda _c: None,
                                   find_latest_verified=lambda _j, _b: None, failed_epoch=1)
    check("with no verified checkpoint anywhere, rollback returns None rather than restarting from zero", empty is None)

    shutil.rmtree(work, ignore_errors=True)

    # ===========================================================================
    section("5. Store: durability, optimistic concurrency, leases, idempotency")
    # ===========================================================================
    state_dir = tempfile.mkdtemp(prefix="orca-store-test-")
    store = JobStore(StoreConfig(state_dir=state_dir, lease_ttl_seconds=2))

    j = JobManifest.create(job_id="chem-tools-store-99887766", owner="tester", title="s",
                           input_filename="a.inp", original_input_sha256="z", job_kind="opt")
    store.put_job(j)
    loaded = store.get_job(j.job_id)
    check("a job round-trips through SQLite", loaded is not None and loaded.job_id == j.job_id)
    check("the state survives serialisation", loaded.state is JobState.CREATED)

    loaded.epoch = 5
    store.put_job(loaded, expected_version=loaded._extra["_version"])
    check("an optimistic update with the correct version succeeds",
          store.get_job(j.job_id).epoch == 5)

    stale = store.get_job(j.job_id)
    stale._extra["_version"] = 1
    try:
        store.put_job(stale, expected_version=1)
        check("a stale write is rejected", False, "no exception")
    except ConcurrencyError:
        check("a stale write raises ConcurrencyError", True)

    lease_a = store.acquire_lease("job:x", "worker-a")
    lease_b = store.acquire_lease("job:x", "worker-b")
    check("a lease is granted to the first holder", lease_a is not None)
    check("a second holder is refused while the lease is live", lease_b is None)
    check("the fencing token is positive", lease_a.fence >= 1)

    time.sleep(2.1)
    lease_c = store.acquire_lease("job:x", "worker-c")
    check("an expired lease can be taken over", lease_c is not None)
    check("the fencing token increases monotonically on takeover",
          lease_c.fence > lease_a.fence, "%s vs %s" % (lease_c.fence, lease_a.fence))

    try:
        store.renew_lease(lease_a)
        check("a fenced-out holder cannot renew", False, "no exception")
    except LeaseLostError:
        check("a fenced-out holder raises LeaseLostError on renewal", True)

    store.put_job(j)
    try:
        store._assert_fence("job:x", lease_a.fence)
        check("a stale fence is rejected on write", False, "no exception")
    except LeaseLostError:
        check("a stale fence is rejected on write", True)
    store.release_lease(lease_c)

    replay, stored = store.begin_idempotent("k1", {"a": 1})
    check("a fresh idempotency key is not a replay", replay is False and stored is None)

    replay2, stored2 = store.begin_idempotent("k1", {"a": 1})
    check("an identical in-flight request is detected as a replay with no result",
          replay2 is True and stored2 is None)

    store.complete_idempotent("k1", {"job_id": "abc"})
    replay3, stored3 = store.begin_idempotent("k1", {"a": 1})
    check("a completed key replays the stored response",
          replay3 is True and stored3 == {"job_id": "abc"})

    try:
        store.begin_idempotent("k1", {"a": 2})
        check("the same key with a different body is rejected", False, "no exception")
    except ConcurrencyError:
        check("the same key with a different body raises ConcurrencyError", True)

    ev = Event.create(job_id=j.job_id, epoch=0, trigger=Trigger.SUBMIT,
                      from_state=JobState.CREATED, to_state=JobState.UPLOADING)
    store.append_event(ev)
    store.append_event(ev)
    check("the event ledger is append-only and de-duplicated",
          len(store.list_events(j.job_id)) == 1)

    store.close()
    shutil.rmtree(state_dir, ignore_errors=True)

    # ===========================================================================
    section("6. Decision function (pure routing logic)")
    # ===========================================================================
    base = JobManifest.create(job_id="chem-tools-dec-12341234", owner="t", title="d",
                              input_filename="a.inp", original_input_sha256="q",
                              job_kind="opt")
    base.state = JobState.RUNNING
    base.epoch = 2
    base.current_slug = "chem-tools-dec-12341234-r2"

    obs_missing = Observation(job_id=base.job_id, kernel_missing=True)
    check("a deleted notebook is routed to CANCEL, not to a retry loop",
          decide(base, obs_missing).trigger is Trigger.CANCEL)

    cfg = Config()
    stale_beat = LedgerRecord(slug=base.current_slug,
                              heartbeat={"at": time.time() - 100000, "epoch": 2})
    obs_dead = Observation(job_id=base.job_id,
                           kernel_status=KernelStatus(base.current_slug, "running"),
                           record=stale_beat)
    d = decide(base, obs_dead, config=cfg)
    check("a running kernel with a dead heartbeat triggers HEARTBEAT_LOST",
          d.trigger is Trigger.HEARTBEAT_LOST and d.action == "rollback")

    fresh_beat = LedgerRecord(slug=base.current_slug,
                              heartbeat={"at": time.time(), "epoch": 2})
    obs_alive = Observation(job_id=base.job_id,
                            kernel_status=KernelStatus(base.current_slug, "running"),
                            record=fresh_beat)
    check("a healthy running kernel is a no-op",
          decide(base, obs_alive, config=cfg).is_noop)

    obs_err = Observation(job_id=base.job_id, error=NetworkError("blip"))
    check("a network blip does NOT change the job's state",
          decide(base, obs_err).is_noop)

    stale_ledger_job = JobManifest.create(job_id=base.job_id, owner="t", title="d",
                                          input_filename="a.inp", original_input_sha256="q")
    stale_ledger_job.epoch = 1
    stale_ledger_job.state = JobState.FINISHED
    obs_stale = Observation(
        job_id=base.job_id,
        kernel_status=KernelStatus(base.current_slug, "complete"),
        record=LedgerRecord(slug=base.current_slug, job=stale_ledger_job),
    )
    d_stale = decide(base, obs_stale)
    check("a ledger entry from an OLDER epoch is not treated as this window's result",
          d_stale.trigger is not Trigger.ORCA_COMPLETE,
          "got %s" % (d_stale.trigger.value if d_stale.trigger else None))

    mid_handoff = JobManifest.create(job_id=base.job_id, owner="t", title="d",
                                     input_filename="a.inp", original_input_sha256="q")
    mid_handoff.epoch = 2
    mid_handoff.state = JobState.RESTARTING
    cp_ok = CheckpointManifest(checkpoint_id="ckpt_ok", job_id=base.job_id, epoch=2,
                               created_at=time.time(), status=CheckpointStatus.VERIFIED)
    obs_handoff = Observation(
        job_id=base.job_id,
        kernel_status=KernelStatus(base.current_slug, "complete"),
        record=LedgerRecord(slug=base.current_slug, job=mid_handoff, checkpoint=cp_ok),
    )
    d_handoff = decide(base, obs_handoff)
    check("a window that verified a checkpoint but died before pushing is completed, not rolled back",
          d_handoff.action == "push_successor",
          "got %s" % d_handoff.action)

    # ===========================================================================
    section("7. Watchdog stall assessment")
    # ===========================================================================
    w = JobManifest.create(job_id="chem-tools-wd-55667788", owner="t", title="w",
                           input_filename="a.inp", original_input_sha256="r")
    w.state = JobState.QUEUED
    w.state_entered_at = time.time() - 10
    check("a recently queued job is not stalled", not assess(w).stalled)

    w.state_entered_at = time.time() - 7200
    check("a job queued far past the grace period is stalled", assess(w).stalled)

    w.state = JobState.RUNNING
    w.last_heartbeat_at = time.time() - 5
    check("a heartbeating job is not stalled", not assess(w).stalled)

    w.last_heartbeat_at = time.time() - 5000
    check("a running job with no recent heartbeat is stalled", assess(w).stalled)

    w.state = JobState.FINISHED
    check("a terminal job is never stalled", not assess(w).stalled)

    w.state = JobState.CHECKPOINTING
    w.state_entered_at = time.time() - 10000
    check("a job stuck mid-handoff is stalled", assess(w).stalled)

    _stuck = JobManifest.create(job_id="chem-tools-obs-33334444", owner="t", title="o",
                                input_filename="a.inp", original_input_sha256="r")
    _stuck.state = JobState.QUEUED
    _stuck.state_entered_at = time.time() - 7200
    check("a job stuck in QUEUED for 2 h is stalled", assess(_stuck).stalled)

    for _ in range(50):
        _stuck.touch()
    check("50 status polls do NOT reset the stall clock",
          assess(_stuck).stalled,
          "age=%.0fs" % assess(_stuck).age_seconds)
    check("the reported age still reflects real time in state",
          assess(_stuck).age_seconds > 7000, "%.0f" % assess(_stuck).age_seconds)

    _stuck.enter_state(JobState.RUNNING)
    check("a real state transition DOES reset the clock",
          assess(_stuck).age_seconds < 5, "%.0f" % assess(_stuck).age_seconds)

    _before = _stuck.state_entered_at
    _stuck.enter_state(JobState.RUNNING)
    check("re-entering the same state does not restart the clock",
          _stuck.state_entered_at == _before)

    _sd = tempfile.mkdtemp(prefix="orca-sea-")
    try:
        _s = JobStore(StoreConfig(state_dir=_sd))
        _j = JobManifest.create(job_id="chem-tools-rt-55556666", owner="t", title="rt",
                                input_filename="a.inp", original_input_sha256="r")
        _j.state = JobState.QUEUED
        _j.state_entered_at = time.time() - 9000
        _s.put_job(_j)
        _r = _s.get_job(_j.job_id)
        check("state_entered_at survives a write/read cycle through SQLite",
              abs(_r.state_entered_at - _j.state_entered_at) < 1.0)
        check("a job stuck 2.5 h is still detected as stalled after reloading",
              assess(_r).stalled)
        _s.close()
    finally:
        shutil.rmtree(_sd, ignore_errors=True)

    # ===========================================================================
    section("8. Generated Kaggle script")
    # ===========================================================================
    hjob = JobManifest.create(job_id="chem-tools-gen-abcdabcd", owner="tester", title="g",
                              input_filename="mol.inp", original_input_sha256="s",
                              job_kind="opt")
    creds = KaggleCredentials(username="tester", key="0" * 32)
    header = build_header(job=hjob, epoch=0, creds=creds,
                          inline_files={"mol.inp": b"! B3LYP Opt\n* xyz 0 1\nO 0 0 0\n*\n"})
    script = render_script(header)

    try:
        compile(script, "script.py", "exec")
        check("the generated Kaggle script compiles as valid Python", True)
    except SyntaxError as exc:
        check("the generated Kaggle script compiles as valid Python", False, str(exc))

    _FUTURE = _re.compile(r"^\s*from\s+__future__\s+import\s+", _re.MULTILINE)
    check("no executable __future__ import survives into the generated script",
          _FUTURE.search(script) is None)

    check("the builder's stripper removes a future import wherever it appears",
          _FUTURE.search(
              _builder._FUTURE_IMPORT.sub("# stripped",
                                          "import os\nfrom __future__ import annotations\n")
          ) is None)
    check("the embedded artefacts module is registered before the runner body runs",
          script.index("sys.modules['orca_artifacts']".replace("sys", "_sys"))
          < script.index("import orca_artifacts as art"))
    check("the header carries the quota constants the disk fix depends on",
          header["budgets"]["scratch_quota_bytes"] > 0
          and header["budgets"]["implausible_free_bytes"] > 0)
    check("the runner source is carried for self-continuation",
          len(header["runner_body_b64"]) > 1000 and len(header["artifacts_source_b64"]) > 1000)

    _mod = types.ModuleType("orca_artifacts_probe")
    exec(compile(_b64.b64decode(header["artifacts_source_b64"]).decode("utf-8"),
                 "orca_artifacts.py", "exec"), _mod.__dict__)
    check("the embedded artefacts module executes standalone",
          _mod.classify_outcome(MAXITER_OUTPUT, job_kind="opt").kind == "MAXITER_EXHAUSTED")

    # ===========================================================================
    section("9. Credentials and input rewriting")
    # ===========================================================================
    c1 = parse("TesterName", "0123456789abcdef0123456789abcdef")
    check("a legacy 32-hex key is recognised", c1.key is not None and c1.api_token is None)
    check("the username is lower-cased so it keys consistently", c1.username == "testername")

    c2 = parse("tester", "KGAT_abcdefghijklmnop")
    check("a new-style API token is recognised", c2.api_token is not None and c2.key is None)

    c3 = parse("", '{"username":"someone","key":"0123456789abcdef0123456789abcdef"}')
    check("a pasted kaggle.json is unpacked", c3.username == "someone" and c3.key is not None)
    check("credentials never render their secret",
          "0123456789abcdef" not in repr(c1) and "fingerprint" in repr(c1))

    inp = "! B3LYP def2-SVP Opt\n%pal nprocs 4 end\n%maxcore 6000\n* xyz 0 1\nO 0 0 0\n*\n"
    check("job kind detection identifies an optimisation", art.detect_job_kind(inp) == "opt")
    check("job kind detection identifies Opt Freq",
          art.detect_job_kind("! B3LYP Opt Freq\n") == "opt_freq")
    check("job kind detection identifies a single point",
          art.detect_job_kind("! B3LYP def2-SVP\n") == "sp")
    check("job kind detection identifies NEB",
          art.detect_job_kind("! NEB-TS\n%neb end\n") == "neb")

    with_maxiter = art.set_geom_maxiter(inp, 300)
    check("MaxIter is injected so a continued optimisation gets a fresh budget",
          "MaxIter 300" in with_maxiter)
    check("injecting MaxIter twice is idempotent",
          art.set_geom_maxiter(with_maxiter, 300).count("MaxIter") == 1)

    inp_large = "! B3LYP def2-SVP Opt\n%pal nprocs 4 end\n%maxcore 8500\n* xyz 0 1\nO 0 0 0\n*\n"
    clamped, info = art.clamp_maxcore(inp_large, 4, 32100)
    check("%maxcore is clamped when exceeding 30GB", info is not None and info["granted_mb"] < 8500)
    check("the clamp reports both the request and the grant",
          info["requested_mb"] == 8500 and info["machine_total_mb"] == 32100)

    check("NoAutoStart is forced so a corrupt .gbw is never auto-read",
          "NoAutoStart" in art.ensure_simple_keyword(inp, "NoAutoStart"))
    check("MOREAD is stripped from a continuation input",
          "MOREAD" not in art.strip_moread("! MOREAD\n%moinp \"old.gbw\"\n").upper())
    check("nprocs rewriting works", "nprocs 1" in art.set_nprocs(inp, 1))

    # ===========================================================================
    section("10. Log redaction (credentials must never reach stdout)")
    # ===========================================================================
    LEGACY_KEY = "0123456789abcdef0123456789abcdef"
    NEW_TOKEN = "KGAT_abcdefghijklmnopqrstuvwxyz012345"

    check("a legacy 32-hex key is redacted from free text",
          LEGACY_KEY not in redact("key=" + LEGACY_KEY))
    check("a new-style token is redacted from free text",
          NEW_TOKEN not in redact("token " + NEW_TOKEN))
    check("nested structures are redacted",
          LEGACY_KEY not in _json.dumps(
              redact_structure({"a": {"b": [{"kaggle_key": LEGACY_KEY}]}})))

    _stream = _io.StringIO()
    _handler = _logging.StreamHandler(_stream)
    _handler.setFormatter(JsonFormatter())
    _handler.addFilter(RedactingFilter())
    _probe = _logging.getLogger("redaction-probe")
    _probe.handlers[:] = [_handler]
    _probe.setLevel(_logging.INFO)
    _probe.propagate = False

    log_failure(_probe, what="pushing", why="network blip",
                recovery="retried", next_action="retrying in 7s",
                kaggle_key=LEGACY_KEY, api_token=NEW_TOKEN,
                nested={"creds": {"key": LEGACY_KEY}})
    _written = _stream.getvalue()
    check("a credential passed via extra= is NOT written to the log",
          LEGACY_KEY not in _written, _written[:200])
    check("a new-style token passed via extra= is NOT written to the log",
          NEW_TOKEN not in _written)
    check("a credential nested inside an extra= structure is NOT written",
          _written.count("<redacted") >= 3, "%d redactions" % _written.count("<redacted"))
    check("the failure record still carries all four mandatory fields",
          all(k in _json.loads(_written) for k in
              ("failure_what", "failure_why", "recovery_attempted", "next_action")))
    check("no bogus 'NoneType: None' traceback is emitted",
          "NoneType: None" not in _written)

    # ===========================================================================
    section("11. Regression: concurrent worker boot on a fresh database")
    # ===========================================================================
    check("a lock error is recognised",
          _is_lock_error(_sq3.OperationalError("database is locked")))
    check("a non-lock error is NOT treated as retryable",
          not _is_lock_error(_sq3.OperationalError("no such column: foo")))

    # ===========================================================================
    section("12. Regression: all workers must choose the SAME state directory")
    # ===========================================================================
    _pdir = tempfile.mkdtemp(prefix="orca-probe-")
    try:
        check("the write probe succeeds on a writable directory", _probe_writable(_pdir))
        check("the probe leaves nothing behind", os.listdir(_pdir) == [],
              str(os.listdir(_pdir)))
        check("repeated probes all succeed",
              all(_probe_writable(_pdir) for _ in range(20)))

        _stop = _th.Event()

        def _sweeper():
            while not _stop.is_set():
                for _f in os.listdir(_pdir):
                    try:
                        os.remove(os.path.join(_pdir, _f))
                    except OSError:
                        pass

        _t = _th.Thread(target=_sweeper, daemon=True)
        _t.start()
        _verdicts = [_probe_writable(_pdir) for _ in range(50)]
        _stop.set()
        _t.join(timeout=2)
        check("a concurrent deleter cannot make a writable directory look unwritable",
              all(_verdicts), "%d/%d failed" % (_verdicts.count(False), len(_verdicts)))
    finally:
        shutil.rmtree(_pdir, ignore_errors=True)

    check("an unwritable directory is correctly rejected",
          not _probe_writable("/proc/nonexistent-orca-probe-dir"))

    print("\n" + "=" * 70)
    print("RESULT: %d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("\nFailures:")
        for name, detail in FAIL:
            print("  - %s %s" % (name, ("(" + detail + ")") if detail else ""))
    print("=" * 70)
    return PASS, FAIL


def test_suite():
    PASS, FAIL = run_all_checks()
    assert len(FAIL) == 0, f"{len(FAIL)} checks failed: {FAIL}"


if __name__ == "__main__":
    _p, _f = run_all_checks()
    sys.exit(1 if _f else 0)
