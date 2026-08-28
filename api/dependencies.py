# -*- coding: utf-8 -*-
"""Shared FastAPI dependencies/helpers for the v1 layer."""
import os


def orchestrator_available() -> bool:
    return os.environ.get("ORCA_ORCHESTRATOR_DISABLED", "") != "1"


def orca_engine_available() -> bool:
    try:
        import orca_engine  # noqa: F401
        return True
    except ImportError:
        return False
