"""Compatibility loader for the legacy Kaggle runner.

The historical runner lives at the repository root as ``kaggle_runner.py``.
Python prefers a package directory over a same-named module, so this small
loader gives us a safe place to apply compatibility fixes without duplicating
the ~3,500-line runner.

The legacy runner uses ``sys.platform`` in its Windows CLI handling but does
not import ``sys``.  That bug is fatal on the first CLI invocation.  We inject
the standard-library ``sys`` module into the legacy module before executing it,
then re-export its public names unchanged.

This is intentionally a compatibility shim, not a second implementation.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

_LEGACY_PATH = Path(__file__).resolve().parent.parent / "kaggle_runner.py"
_SPEC = importlib.util.spec_from_file_location(
    "_chemistry_lab_legacy_kaggle_runner", _LEGACY_PATH
)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Could not load legacy Kaggle runner: {_LEGACY_PATH}")

_LEGACY = importlib.util.module_from_spec(_SPEC)
# The legacy source references sys.platform but omitted `import sys`.
_LEGACY.sys = sys
_SPEC.loader.exec_module(_LEGACY)

for _name, _value in vars(_LEGACY).items():
    if _name not in {"__name__", "__package__", "__loader__", "__spec__"}:
        globals()[_name] = _value

__all__ = [name for name in globals() if not name.startswith("_")]
