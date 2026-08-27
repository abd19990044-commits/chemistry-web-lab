# -*- coding: utf-8 -*-
"""Regression tests for the production repairs (P1-1, P1-2, P1-3).

P1-1  FAILED -> RESTARTING (operator resume) + fresh failed ledger
      must survive reconciliation: the reconciler pushes the next window
      instead of re-adopting the failed outcome.
P1-2  A submit whose push landed remotely but whose call raised must persist
      the job identity as the idempotent result, so a retry replays it
      instead of creating a second, differently-named kernel.
P1-3  Every externally induced state change the reconciler makes without a
      window ledger is recorded as an auditable event.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orca_orchestrator.config import StoreConfig                       # noqa: E402
from orca_orchestrator.credentials import KaggleCredentials            # noqa: E402
from orca_orchestrator.kaggle_api import KernelStatus                  # noqa: E402
from orca_orchestrator.ledger import LedgerRecord                      # noqa: E402
from orca_orchestrator.models import JobManifest                       # noqa: E402
from orca_orchestrator.reconciler import Observation, Reconciler       # noqa: E402
from orca_orchestrator import reconciler as reconciler_mod             # noqa: E402
from orca_orchestrator.states import JobState, Trigger                 # noqa: E402
from orca_orchestrator.store import JobStore                           # noqa: E402

CREDS = KaggleCredentials(username="tester", key="0" * 32)
INP = "! B3LYP def2-SVP Opt\n* xyz 0 1\nO 0 0 0\nH 0 0 0.96\nH 0 0 -0.96\n*\n"


def _manifest(job_id, epoch, state, *, verified_ckpt=None):
    job = JobManifest.create(job_id=job_id, owner="tester", title="repair",
                             input_filename="mol.inp", original_input_sha256="h",
                             job_kind="opt")
    job.epoch = epoch
    job.state = state
    job.current_slug = job.slug_for_epoch(epoch)
    job.verified_checkpoint_id = verified_ckpt
    return job


def _failed_window_ledger(job_id, epoch):
    window = _manifest(job_id, epoch, JobState.FAILED)
    window.last_note = "ORCA stopped with an error"
    window.last_error = {"message": "orca_fatal"}
    return LedgerRecord(
        slug=window.current_slug, job=window, checkpoint=None,
        heartbeat={"at": 0.0, "epoch": epoch, "run_token": "tok"},
    )


# ---------------------------------------------------------------------------
# P1-1: operator resume survives reconciliation
# ---------------------------------------------------------------------------
def test_operator_resume_survives_fresh_failed_ledger():
    store = JobStore(StoreConfig(state_dir=tempfile.mkdtemp(prefix="orca-rep-")))
    rec = Reconciler(store)
    job_id = "chem-tools-repair-1a2b3c4d"
    job = _manifest(job_id, 1, JobState.RESTARTING, verified_ckpt="ckpt_verified")
    obs = Observation(job_id=job_id,
                      kernel_status=KernelStatus(slug=job.current_slug, status="error",
                                                 raw='status "ERROR"'),
                      record=_failed_window_ledger(job_id, 1))
    decision = reconciler_mod.decide(job, obs)
    assert decision.action == "push_successor", (
        "an operator-resumed job must push the next window, not re-adopt FAILED")


def test_normal_failed_ledger_adoption_is_preserved():
    store = JobStore(StoreConfig(state_dir=tempfile.mkdtemp(prefix="orca-rep-")))
    rec = Reconciler(store)
    job_id = "chem-tools-repair-2b3c4d5e"
    job = _manifest(job_id, 1, JobState.QUEUED)
    obs = Observation(job_id=job_id,
                      kernel_status=KernelStatus(slug=job.current_slug, status="error",
                                                 raw='status "ERROR"'),
                      record=_failed_window_ledger(job_id, 1))
    decision = reconciler_mod.decide(job, obs)
    assert decision.action == "adopt_ledger" and decision.trigger == Trigger.ORCA_FATAL, (
        "non-resumed jobs must keep the normal failed-ledger adoption")


# ---------------------------------------------------------------------------
# P1-3: externally induced state changes are auditable
# ---------------------------------------------------------------------------
def test_no_ledger_adoption_records_an_event():
    store = JobStore(StoreConfig(state_dir=tempfile.mkdtemp(prefix="orca-rep-")))
    rec = Reconciler(store)
    job_id = "chem-tools-repair-3c4d5e6f"
    job = _manifest(job_id, 0, JobState.QUEUED)
    store.put_job(job)
    from orca_orchestrator.reconciler import Decision
    decision = Decision(Trigger.ORCA_COMPLETE, "the window reported a verified completion",
                        {"note": "done"}, action="adopt_ledger")
    obs = Observation(job_id=job_id, record=None)
    out = rec._act(job, decision, obs, client=None, fence=None,
                   correlation_id="t", actor="test")
    assert out.state == JobState.FINISHED
    triggers = [e.trigger for e in store.list_events(job_id)]
    assert "ADOPT_REMOTE_STATUS" in triggers, \
        "a state change made without a window ledger must still be auditable"


# ---------------------------------------------------------------------------
# P1-2: submit retry after a landed-but-raised push replays, never duplicates
# ---------------------------------------------------------------------------
def test_submit_retry_replays_a_landed_push(monkeypatch):
    import orca_orchestrator.service as service_mod

    landed = []

    class FakeLandingClient:
        def __init__(self, creds=None):
            pass

        def push_kernel(self, job_dir, *, expected_slug, skip_if_active=True):
            landed.append(expected_slug)
            # The push was ACCEPTED by Kaggle, then the local call died.
            raise RuntimeError("connection reset while reading the push response")

        def kernel_exists(self, slug):
            if slug in landed:
                return KernelStatus(slug=slug, status="queued", raw="queued")
            return None

    monkeypatch.setattr(service_mod, "KaggleClient", FakeLandingClient)

    store = JobStore(StoreConfig(state_dir=tempfile.mkdtemp(prefix="orca-rep-")))
    svc = service_mod.OrchestratorService(store, start_watchdog=False)

    with pytest.raises(RuntimeError):
        svc.submit(CREDS, input_filename="mol.inp", input_content=INP)
    assert len(landed) == 1

    # The retry (same payload, same idempotency semantics) must REPLAY the
    # first submission instead of pushing a second, differently-named kernel.
    result = svc.submit(CREDS, input_filename="mol.inp", input_content=INP)
    assert len(landed) == 1, "a retry after a landed push must never push again"
    assert result.job_id == store.list_jobs()[0].job_id
    assert result.replayed or result.slug


def test_submit_failure_without_a_landed_kernel_releases_the_key(monkeypatch):
    import orca_orchestrator.service as service_mod

    class FakeDeadClient:
        def __init__(self, creds=None):
            pass

        def push_kernel(self, job_dir, *, expected_slug, skip_if_active=True):
            raise RuntimeError("kaggle rejected the push: bad metadata")

        def kernel_exists(self, slug):
            return None

    monkeypatch.setattr(service_mod, "KaggleClient", FakeDeadClient)

    store = JobStore(StoreConfig(state_dir=tempfile.mkdtemp(prefix="orca-rep-")))
    svc = service_mod.OrchestratorService(store, start_watchdog=False)

    with pytest.raises(RuntimeError):
        svc.submit(CREDS, input_filename="mol.inp", input_content=INP)
    # A corrected retry must be possible: the key was released.
    with pytest.raises(RuntimeError):
        svc.submit(CREDS, input_filename="mol.inp", input_content=INP)
    assert len(store.list_jobs()) <= 2  # no unbounded growth, no duplicate kernel


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
