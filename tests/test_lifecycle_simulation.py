# -*- coding: utf-8 -*-
"""
End-to-end lifecycle simulation against a fake Kaggle.

The unit suite proves each component in isolation. This one proves they compose:
a job is driven through multiple session windows, a crash, a corrupted
checkpoint, a duplicate push attempt and a rollback, using the real reconciler,
the real state machine, the real store and the real kernel builder — with only
the Kaggle CLI replaced.

That boundary is deliberate. Everything below `KaggleClient` is exercised for
real, including `build_window_directory`, so a broken generated script or an
illegal transition fails here rather than in production twelve hours later.

Run: `python tests/test_lifecycle_simulation.py`
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orca_orchestrator import reconciler as reconciler_mod       # noqa: E402
from orca_orchestrator.config import StoreConfig                 # noqa: E402
from orca_orchestrator.credentials import KaggleCredentials      # noqa: E402
from orca_orchestrator.kaggle_api import KernelStatus, PushResult  # noqa: E402
from orca_orchestrator.ledger import LedgerRecord                # noqa: E402
from orca_orchestrator.models import (CheckpointManifest, CheckpointStatus,  # noqa: E402
                                      FileRecord, JobManifest)
from orca_orchestrator.reconciler import Reconciler              # noqa: E402
from orca_orchestrator.states import JobState, Trigger           # noqa: E402
from orca_orchestrator.store import JobStore                     # noqa: E402

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print("  %s  %s%s" % ("PASS" if condition else "FAIL", name,
                          ("" if condition else "  -- " + str(detail))))


def section(title):
    print("\n" + title)
    print("-" * len(title))


CREDS = KaggleCredentials(username="tester", key="0" * 32)


# ---------------------------------------------------------------------------
# Fake Kaggle
# ---------------------------------------------------------------------------
class FakeKaggle:
    """A scriptable stand-in for a Kaggle account.

    Records every push so duplicate-launch behaviour is directly observable,
    which is the only way to prove the guard actually guards."""

    def __init__(self):
        self.kernels = {}          # slug -> status word
        self.ledgers = {}          # slug -> LedgerRecord
        self.push_log = []
        self.fail_next_push = None

    def set_window(self, slug, status, record=None):
        self.kernels[slug] = status
        if record is not None:
            self.ledgers[slug] = record


FAKE = FakeKaggle()


class FakeClient:
    creds = CREDS

    def __init__(self, creds=None):
        self.creds = creds or CREDS

    def kernel_exists(self, slug):
        status = FAKE.kernels.get(slug)
        return None if status is None else KernelStatus(slug, status)

    def status(self, slug):
        return KernelStatus(slug, FAKE.kernels.get(slug, "unknown"))

    def push_kernel(self, job_dir, *, expected_slug, skip_if_active=True):
        # The real script must exist and be valid Python; the builder really ran.
        script = os.path.join(job_dir, "script.py")
        assert os.path.isfile(script), "builder produced no script.py"
        with open(script) as fh:
            compile(fh.read(), "script.py", "exec")

        if skip_if_active and FAKE.kernels.get(expected_slug) in ("running", "queued"):
            FAKE.push_log.append((expected_slug, "skipped"))
            return PushResult(slug=expected_slug, owner="tester",
                              url="https://kaggle/%s" % expected_slug,
                              requested_slug=expected_slug)
        if FAKE.fail_next_push is not None:
            exc, FAKE.fail_next_push = FAKE.fail_next_push, None
            raise exc
        FAKE.push_log.append((expected_slug, "pushed"))
        FAKE.kernels[expected_slug] = "queued"
        return PushResult(slug=expected_slug, owner="tester",
                          url="https://kaggle/%s" % expected_slug,
                          requested_slug=expected_slug)

    def list_kernels(self, prefix=None, page_size=100):
        return [{"slug": s, "owner": "tester", "ref": "tester/" + s,
                 "url": "https://kaggle/" + s, "title": s, "last_run": "2026-07-30"}
                for s in FAKE.kernels]

    def delete_kernel(self, slug):
        FAKE.kernels.pop(slug, None)
        return True


def fake_read_window(client, slug):
    return FAKE.ledgers.get(slug) or LedgerRecord(slug=slug)


reconciler_mod.KaggleClient = FakeClient
reconciler_mod.read_window = fake_read_window


def ledger_for(job_id, epoch, state, *, checkpoint=None, heartbeat_age=1.0, note=""):
    """Builds the STATE.json a window would have written."""
    job = JobManifest.create(job_id=job_id, owner="tester", title="sim",
                             input_filename="mol.inp", original_input_sha256="h",
                             job_kind="opt")
    job.epoch = epoch
    job.state = state
    job.last_note = note
    job.current_slug = job.slug_for_epoch(epoch)
    return LedgerRecord(
        slug=job.current_slug, job=job, checkpoint=checkpoint,
        heartbeat={"at": time.time() - heartbeat_age, "epoch": epoch,
                   "run_token": "tok%d" % epoch},
    )


def verified_checkpoint(job_id, epoch):
    cp = CheckpointManifest(
        checkpoint_id="ckpt_e%d" % epoch, job_id=job_id, epoch=epoch,
        created_at=time.time(), status=CheckpointStatus.VERIFIED,
        next_input_text="! B3LYP Opt\n* xyzfile 0 1 last_geometry.xyz\n",
        orca_phase="opt", completed_opt_cycles=180,
        cumulative_opt_cycles=180 * (epoch + 1),
        source_kernel_slug=job_id if epoch == 0 else "%s-r%d" % (job_id, epoch),
    )
    cp.files = [FileRecord(name="last_geometry.xyz", sha256="a" * 64, size=120,
                           role="geometry", required=True, transport="kaggle_output")]
    cp.bundle_digest = "d" * 64
    return cp


# ===========================================================================
section("Setup")
# ===========================================================================
state_dir = tempfile.mkdtemp(prefix="orca-sim-")
store = JobStore(StoreConfig(state_dir=state_dir, lease_ttl_seconds=60))
rec = Reconciler(store)

JOB_ID = "chem-tools-sim-deadbeef"
job = JobManifest.create(job_id=JOB_ID, owner="tester", title="simulation",
                         input_filename="mol.inp", original_input_sha256="h",
                         job_kind="opt")
store.put_job(job)
job = rec.transition(job, Trigger.SUBMIT, actor="test")
job = rec.transition(job, Trigger.PUSH_ACK, actor="test", slug=JOB_ID)
store.put_job(job, expected_version=job._extra.get("_version"))
FAKE.set_window(JOB_ID, "queued")
check("job reaches QUEUED after submission", job.state is JobState.QUEUED)


# ===========================================================================
section("1. Window 0 boots and runs")
# ===========================================================================
FAKE.set_window(JOB_ID, "running", ledger_for(JOB_ID, 0, JobState.RUNNING))
job = rec.reconcile(JOB_ID, CREDS)
check("QUEUED + running kernel -> READY (epoch 0 has nothing to restore)",
      job.state is JobState.READY, job.state.value)

job = rec.transition(job, Trigger.ORCA_STARTED, actor="test")
store.put_job(job, expected_version=job._extra.get("_version"))
job = rec.reconcile(JOB_ID, CREDS)
check("a healthy heartbeating window stays RUNNING",
      job.state is JobState.RUNNING, job.state.value)
check("no push happened while the window was healthy", len(FAKE.push_log) == 0)


# ===========================================================================
section("2. Window 0 hits MaxIter, checkpoints, and hands off")
# ===========================================================================
cp0 = verified_checkpoint(JOB_ID, 0)
store.put_checkpoint(cp0)
FAKE.set_window(JOB_ID, "complete",
                ledger_for(JOB_ID, 0, JobState.RESTARTING, checkpoint=cp0,
                           note="MaxIter reached; checkpoint verified"))

job = rec.reconcile(JOB_ID, CREDS)
check("a window that verified a checkpoint but died before pushing gets its "
      "successor pushed", job.epoch == 1, "epoch=%d state=%s" % (job.epoch, job.state.value))
check("the successor slug is deterministic",
      job.current_slug == JOB_ID + "-r1", job.current_slug)
check("the successor was actually pushed",
      (JOB_ID + "-r1", "pushed") in FAKE.push_log, str(FAKE.push_log))
check("the committed checkpoint is recorded as the rollback anchor",
      job.verified_checkpoint_id == cp0.checkpoint_id)
check("the checkpoint moved to COMMITTED",
      store.get_checkpoint(cp0.checkpoint_id).status == CheckpointStatus.COMMITTED)


# ===========================================================================
section("3. Idempotency: reconciling the same stopped window twice")
# ===========================================================================
pushes_before = len(FAKE.push_log)
job = rec.reconcile(JOB_ID, CREDS)
job = rec.reconcile(JOB_ID, CREDS)
check("re-reconciling does not push a duplicate successor",
      len(FAKE.push_log) == pushes_before, str(FAKE.push_log[pushes_before:]))
check("the epoch did not advance spuriously", job.epoch == 1, str(job.epoch))


# ===========================================================================
section("4. Duplicate-launch guard: successor already running")
# ===========================================================================
FAKE.set_window(JOB_ID + "-r1", "running",
                ledger_for(JOB_ID, 1, JobState.RUNNING))
job = store.require_job(JOB_ID)
job.state = JobState.RESTARTING
job.epoch = 0
store.put_job(job, expected_version=job._extra.get("_version"))

pushes_before = len(FAKE.push_log)
job = rec._push_successor(store.require_job(JOB_ID), FakeClient(), fence=1,
                          correlation_id="t", actor="test")
store.put_job(job, expected_version=job._extra.get("_version"))
check("an already-running successor is adopted, not re-pushed",
      len(FAKE.push_log) == pushes_before, str(FAKE.push_log[pushes_before:]))
check("adopting the existing successor still advances the epoch", job.epoch == 1)


# ===========================================================================
section("5. Crash recovery: the window dies with no ledger entry")
# ===========================================================================
job = store.require_job(JOB_ID)
job.state = JobState.RUNNING
job.epoch = 1
job.current_slug = JOB_ID + "-r1"
job.verified_checkpoint_id = cp0.checkpoint_id
store.put_job(job, expected_version=job._extra.get("_version"))

FAKE.kernels[JOB_ID + "-r1"] = "error"
FAKE.ledgers.pop(JOB_ID + "-r1", None)          # died before writing STATE.json

job = rec.reconcile(JOB_ID, CREDS)
check("a window that died silently rolls back rather than assuming progress",
      job.rollback_count >= 1, "rollback_count=%d" % job.rollback_count)
check("rollback re-drove the chain from the last verified checkpoint",
      job.epoch == 2, "epoch=%d state=%s" % (job.epoch, job.state.value))
check("the rollback target was the verified checkpoint, not nothing",
      job.verified_checkpoint_id == cp0.checkpoint_id)


# ===========================================================================
section("6. Stale ledger: a leftover STATE.json from an older epoch")
# ===========================================================================
job = store.require_job(JOB_ID)
job.state = JobState.RUNNING
store.put_job(job, expected_version=job._extra.get("_version"))

# /kaggle/working persists across runs, so the newest window can read a
# STATE.json written by an *older* run claiming the job already finished.
FAKE.set_window(job.current_slug, "complete",
                ledger_for(JOB_ID, 0, JobState.FINISHED, note="stale leftover"))
job = rec.reconcile(JOB_ID, CREDS)
check("a stale FINISHED ledger from an older epoch does NOT finish the job",
      job.state is not JobState.FINISHED, job.state.value)


# ===========================================================================
section("7. Genuine completion")
# ===========================================================================
job = store.require_job(JOB_ID)
job.state = JobState.RUNNING
store.put_job(job, expected_version=job._extra.get("_version"))

FAKE.set_window(job.current_slug, "complete",
                ledger_for(JOB_ID, job.epoch, JobState.FINISHED,
                           note="THE OPTIMIZATION HAS CONVERGED"))
job = rec.reconcile(JOB_ID, CREDS)
check("a fresh FINISHED ledger for the current epoch finishes the job",
      job.state is JobState.FINISHED, job.state.value)
check("a terminal job is a no-op on further reconciliation",
      rec.reconcile(JOB_ID, CREDS).state is JobState.FINISHED)


# ===========================================================================
section("8. Budget exhaustion stops the chain")
# ===========================================================================
BUDGET_JOB = "chem-tools-budget-cafebabe"
b = JobManifest.create(job_id=BUDGET_JOB, owner="tester", title="budget",
                       input_filename="m.inp", original_input_sha256="h",
                       job_kind="opt")
b.epoch = 3
b.max_epochs = 3
b.state = JobState.RESTARTING
b.verified_checkpoint_id = "ckpt_x"
store.put_job(b)
b = rec._push_successor(store.require_job(BUDGET_JOB), FakeClient(), fence=1,
                        correlation_id="t", actor="test")
check("the epoch budget terminates the chain instead of looping",
      b.state is JobState.FAILED, b.state.value)
check("the failure explains the budget",
      "session windows" in (b.recent_events[-1].detail.get("reason") or ""),
      str(b.recent_events[-1].detail))

CYCLE_JOB = "chem-tools-cycles-12345678"
c = JobManifest.create(job_id=CYCLE_JOB, owner="tester", title="cycles",
                       input_filename="m.inp", original_input_sha256="h",
                       job_kind="opt")
c.epoch = 2
c.cumulative_opt_cycles = 99999
c.state = JobState.RESTARTING
c.verified_checkpoint_id = "ckpt_y"
store.put_job(c)
c = rec._push_successor(store.require_job(CYCLE_JOB), FakeClient(), fence=1,
                        correlation_id="t", actor="test")
check("the cumulative optimisation-cycle budget terminates a runaway",
      c.state is JobState.FAILED, c.state.value)
check("the failure names the cycle budget",
      "cumulative" in (c.recent_events[-1].detail.get("reason") or ""))


# ===========================================================================
section("9. No verified checkpoint anywhere -> fail loudly, never from zero")
# ===========================================================================
ORPHAN = "chem-tools-orphan-87654321"
o = JobManifest.create(job_id=ORPHAN, owner="tester", title="orphan",
                       input_filename="m.inp", original_input_sha256="h",
                       job_kind="opt")
o.state = JobState.ROLLING_BACK
o.epoch = 1
store.put_job(o)
o = rec._rollback(store.require_job(ORPHAN), FakeClient(), fence=1,
                  correlation_id="t", actor="test")
check("with nothing verified to fall back to, the job fails rather than "
      "silently restarting from zero", o.state is JobState.FAILED, o.state.value)


# ===========================================================================
section("10. Event ledger is a complete causal history")
# ===========================================================================
events = store.list_events(JOB_ID)
check("every transition was recorded", len(events) >= 8, "got %d" % len(events))
check("events carry their trigger and both states",
      all(e.trigger and e.from_state and e.to_state for e in events))
transitions = [(e.from_state, e.trigger, e.to_state) for e in events]
check("the ledger shows the successor push that advanced the epoch",
      any(t[1] == "SUCCESSOR_PUSHED" for t in transitions))
check("the ledger shows the rollback",
      any(t[1] == "ROLLBACK_SELECTED" for t in transitions))
check("the ledger shows the recovery from a silent death",
      any(t[1] == "HEARTBEAT_LOST" for t in transitions))

store.close()
shutil.rmtree(state_dir, ignore_errors=True)

print("\n" + "=" * 70)
print("LIFECYCLE SIMULATION: %d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL:
    for name in FAIL:
        print("  - " + name)
print("=" * 70)
sys.exit(1 if FAIL else 0)
