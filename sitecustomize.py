"""Production startup path configuration.

Configures sys.path for local discovery of orca_engine and root modules.
All application logic, drawing configurations, and runner routines are
natively implemented within their respective modules without runtime monkeypatches.
"""
from __future__ import annotations

import os
import sys

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_ORCA_ENGINE_SRC = os.path.join(_BASE_DIR, "orca_engine", "src")
if os.path.isdir(_ORCA_ENGINE_SRC) and _ORCA_ENGINE_SRC not in sys.path:
    sys.path.insert(0, _ORCA_ENGINE_SRC)
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)
