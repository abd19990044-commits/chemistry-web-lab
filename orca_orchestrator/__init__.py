# -*- coding: utf-8 -*-
"""
orca_orchestrator
=================

A fault-tolerant orchestration layer for long ORCA calculations that must
survive many time-limited Kaggle sessions.
"""
from .config import CONFIG
from .errors import (IntegrityError, OrchestratorError, PermanentError, TransientError)
from .models import CheckpointManifest, Event, JobManifest
from .states import TRANSITIONS, JobState, Trigger

__version__ = "2.0.3"

__all__ = [
    "CONFIG",
    "JobState",
    "Trigger",
    "TRANSITIONS",
    "CheckpointManifest",
    "JobManifest",
    "OrchestratorError",
    "TransientError",
    "PermanentError",
    "IntegrityError",
    "get_service",
]

# The Flask compatibility adapter is installed explicitly by app.py, because
# it needs the actual Flask application instance. Importing this package must
# remain side-effect-safe for CI, workers, CLI tools, and tests.

# The Flask application imports chem_core immediately before importing this
# package. Install the scientific policy gate at this boundary so the existing
# low-level renderer remains reusable while the public wizard cannot emit a
# method combination that ORCA 6.1 does not support or that changes the meaning
# of the UI selection (notably "No RI" -> explicit NORI).
from orca_policy import install_policy as _install_orca_policy
_install_orca_policy()
del _install_orca_policy


def get_service(**kwargs):
    """Lazy accessor.

    Merely importing the package does not open SQLite or start the watchdog.
    """
    from .service import get_service as _get
    return _get(**kwargs)
