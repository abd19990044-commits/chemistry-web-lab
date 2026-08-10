# -*- coding: utf-8 -*-
"""
orca_orchestrator
=================

A fault-tolerant orchestration layer for long ORCA calculations that must
survive many time-limited Kaggle sessions.

Design summary (the full rationale is in ARCHITECTURE.md):

  * **The Kaggle-side ledger is the source of truth.** Each window writes
    `STATE.json` into its saved output. That storage outlives both the web
    application and any individual kernel, so a job is fully reconstructible
    after the Space is wiped, redeployed, or replaced.
  * **SQLite is a cache, not an authority.** Losing it costs API calls, never
    correctness.
  * **Every state change goes through an explicit finite-state machine.** There
    is no assignment to `job.state` outside `Reconciler.transition`, and an
    undefined transition raises rather than being tolerated.
  * **A checkpoint is a transaction:** staged, then verified by re-reading every
    byte, and only then committed by pushing a successor. Nothing unverified is
    ever a restart source or a rollback target.
  * **Every operation is idempotent.** Deterministic window slugs, idempotency
    keys on submission, content-addressed checkpoints, and fenced leases mean
    replaying any action is safe -- which is what makes crash recovery a matter
    of running the loop again.

Two production bugs this package exists to fix are documented in detail in
`orca_artifacts.py`: ORCA reporting "TERMINATED NORMALLY" on an unconverged
optimisation, and `shutil.disk_usage` reporting the host overlay (1006.8 GB)
instead of Kaggle's enforced quota.
"""
from .config import CONFIG
from .errors import (IntegrityError, OrchestratorError, PermanentError, TransientError)
from .models import CheckpointManifest, Event, JobManifest
from .states import TRANSITIONS, JobState, Trigger

__version__ = "2.0.0"

__all__ = [
    "CONFIG",
    "JobState",
    "Trigger",
    "TRANSITIONS",
    "JobManifest",
    "CheckpointManifest",
    "Event",
    "OrchestratorError",
    "TransientError",
    "PermanentError",
    "IntegrityError",
    "get_service",
    "__version__",
]


def get_service(**kwargs):
    """Lazy accessor.

    Imported lazily so that merely importing this package does not open a
    SQLite connection or start the watchdog thread -- which matters for tests,
    for `python -m compileall`, and for any tooling that imports the package
    without intending to run it."""
    from .service import get_service as _get

    return _get(**kwargs)
