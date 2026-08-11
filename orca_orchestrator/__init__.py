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

__version__ = "2.0.1"

__all__ = [
    "CONFIG",
    "JobState",
    "Trigger",
    "TRANSITIONS",
    "CheckpointManifest",
    "Event",
    "JobManifest",
    "OrchestratorError",
    "TransientError",
    "PermanentError",
    "IntegrityError",
    "get_service",
    "__version__",
]


def get_service(**kwargs):
    """Lazy accessor.

    Merely importing the package does not open SQLite or start the watchdog.
    """
    from .service import get_service as _get
    return _get(**kwargs)
