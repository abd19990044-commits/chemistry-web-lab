# -*- coding: utf-8 -*-
"""Pytest fixtures and configuration for fast, deterministic, isolated test runs."""
import os
import shutil
import sys
import tempfile

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ORCA_ENGINE_SRC = os.path.join(_BASE_DIR, "orca_engine", "src")
if _ORCA_ENGINE_SRC not in sys.path:
    sys.path.insert(0, _ORCA_ENGINE_SRC)
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

# Ensure fast deterministic retry & watchdog settings before imports
os.environ["ORCA_RETRY_BASE_DELAY"] = "0.001"
os.environ["ORCA_RETRY_MAX_DELAY"] = "0.005"
os.environ["ORCA_RETRY_MAX_ATTEMPTS"] = "2"
os.environ["ORCA_WATCHDOG_ENABLED"] = "0"
os.environ["ORCA_SQLITE_BUSY_TIMEOUT_MS"] = "100"

import pytest


def pytest_configure(config):
    """Sets a collision-free portable basetemp inside system temp directory if not explicitly provided."""
    if not getattr(config.option, "basetemp", None):
        portable_basetemp = os.path.join(tempfile.gettempdir(), f"orca_pytest_{os.getpid()}")
        os.makedirs(portable_basetemp, exist_ok=True)
        config.option.basetemp = portable_basetemp


def pytest_report_header(config):
    """Reports execution environment, git metadata, and core package versions for reproducibility."""
    import platform
    lines = [
        f"ORCA Web Lab Test Environment: Python {platform.python_version()} on {platform.system()} ({platform.machine()})"
    ]
    try:
        import subprocess
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=_BASE_DIR, text=True).strip()
        lines.append(f"Git Commit SHA: {sha}")
    except Exception:
        lines.append("Git Commit SHA: unknown")

    import importlib.metadata
    deps = []
    for pkg in ("rdkit", "flask", "requests", "jsonschema", "openpyxl", "pillow", "kaggle"):
        try:
            v = importlib.metadata.version(pkg)
            deps.append(f"{pkg}=={v}")
        except importlib.metadata.PackageNotFoundError:
            deps.append(f"{pkg}=MISSING")
    lines.append("Key Dependencies: " + ", ".join(deps))
    lines.append("Canonical Schema Version: orca-web-lab.1.0 (Draft 2020-12) | Dataset: 2026.1")
    return lines


@pytest.fixture(autouse=True)
def isolated_orchestrator_environment(monkeypatch):
    """Provides a pristine, isolated orchestration state for every test.

    Ensures:
      1. A unique temporary SQLite state directory per test.
      2. Previous singletons (_SERVICE, _default_store) are reset and closed.
      3. In-memory credential broker is cleared.
      4. Watchdog threads are stopped.
      5. Complete cleanup of temporary files after each test.
    """
    temp_state_dir = tempfile.mkdtemp(prefix="orca-test-state-")
    monkeypatch.setenv("ORCA_STATE_DIR", temp_state_dir)
    monkeypatch.setenv("ORCA_WATCHDOG_ENABLED", "0")
    monkeypatch.setenv("ORCA_RETRY_BASE_DELAY", "0.001")
    monkeypatch.setenv("ORCA_RETRY_MAX_DELAY", "0.005")
    monkeypatch.setenv("ORCA_RETRY_MAX_ATTEMPTS", "2")

    try:
        from orca_orchestrator.service import reset_service
        reset_service()
    except Exception:
        pass

    try:
        from orca_orchestrator.store import reset_store
        reset_store()
    except Exception:
        pass

    try:
        from orca_orchestrator.credentials import BROKER
        BROKER.clear()
    except Exception:
        pass

    try:
        import orca_orchestrator.config as orca_cfg
        orca_cfg.CONFIG = orca_cfg.Config(
            retry=orca_cfg.RetryConfig(max_attempts=2, base_delay_seconds=0.001, max_delay_seconds=0.005),
            watchdog=orca_cfg.WatchdogConfig(enabled=False),
        )
    except Exception:
        pass

    yield

    try:
        from orca_orchestrator.service import reset_service
        reset_service()
    except Exception:
        pass

    try:
        from orca_orchestrator.store import reset_store
        reset_store()
    except Exception:
        pass

    try:
        from orca_orchestrator.credentials import BROKER
        BROKER.clear()
    except Exception:
        pass

    shutil.rmtree(temp_state_dir, ignore_errors=True)
