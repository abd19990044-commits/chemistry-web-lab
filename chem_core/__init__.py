# -*- coding: utf-8 -*-
"""Compatibility package for the legacy ``chem_core.py`` module.

Python resolves this package before the historical sibling ``chem_core.py``.
The legacy module remains untouched as the reference implementation for all
chemistry/business logic; this package re-exports it and replaces only the 2D
drawing entry points with the enhanced renderer in ``chem_core.drawing``.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_LEGACY_PATH = Path(__file__).resolve().parent.parent / "chem_core.py"
_SPEC = importlib.util.spec_from_file_location("_chem_core_legacy", _LEGACY_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Could not load legacy chemistry core: {_LEGACY_PATH}")
_LEGACY = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_LEGACY)

# Preserve the complete public API used by app.py, tests and downstream code.
for _name, _value in vars(_LEGACY).items():
    if not _name.startswith("__"):
        globals()[_name] = _value

from .drawing import (  # noqa: E402
    MOL_IMAGE_SIZE,
    apply_draw_options,
    prepare_molecule,
    render_molecule_png,
    render_molecule_svg,
    trim_white,
)

__all__ = [name for name in globals() if not name.startswith("_")]
