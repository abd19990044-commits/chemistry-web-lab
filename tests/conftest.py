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

# Imports during collection can initialize SQLite and credential vaults before
# function fixtures run. Always use test-owned storage, even if the shell has
# production state variables configured. Keep it alive for the pytest process.
_COLLECTION_STATE = tempfile.TemporaryDirectory(prefix="orca-pytest-collection-")
os.environ["ORCA_STATE_DIR"] = _COLLECTION_STATE.name
os.environ["CHEMISTRY_LAB_STATE_DIR"] = _COLLECTION_STATE.name

# Ensure fast deterministic retry & watchdog settings before imports
os.environ["ORCA_RETRY_BASE_DELAY"] = "0.001"
os.environ["ORCA_RETRY_MAX_DELAY"] = "0.005"
os.environ["ORCA_RETRY_MAX_ATTEMPTS"] = "2"
os.environ["ORCA_WATCHDOG_ENABLED"] = "0"
os.environ["ORCA_SQLITE_BUSY_TIMEOUT_MS"] = "100"
os.environ["ORCA_LOCAL_MAX_MEMORY_MB"] = "16384"
os.environ["ORCA_LOCAL_MAX_CORES"] = "16"

import pytest


def pytest_configure(config):
    """Sets a collision-free portable basetemp inside system temp directory if not explicitly provided."""
    if not getattr(config.option, "basetemp", None):
        # Separate executors can report the same PID in container namespaces.
        # PID-only names let one pytest process delete another's live files.
        portable_basetemp = tempfile.mkdtemp(prefix="orca_pytest_")
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
    monkeypatch.setenv("CHEMISTRY_LAB_STATE_DIR", temp_state_dir)
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


# ---------------------------------------------------------------------------
# orca_engine test fixtures
# ---------------------------------------------------------------------------
from pathlib import Path  # noqa: E402
from orca_engine.models import JobData  # noqa: E402
from orca_engine.parser import OrcaParser  # noqa: E402

DATA_DIR = Path(_BASE_DIR) / "orca_engine" / "tests" / "data"


def parse_fixture(name: str) -> list[JobData]:
    """Parse one fixture file from ``orca_engine/tests/data``."""
    path = DATA_DIR / name
    with path.open("r", encoding="utf-8") as stream:
        return OrcaParser(stream, source_name=str(path)).parse()


@pytest.fixture
def data_dir() -> Path:
    """Return the fixture data directory."""
    return DATA_DIR


@pytest.fixture
def reaction_dir(tmp_path: Path):
    """Return a directory holding only the balanced reaction fixtures."""
    for name in ("rxn_h2.out", "rxn_o.out", "rxn_h2o.out"):
        (tmp_path / name).write_text(
            (DATA_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    yield tmp_path


@pytest.fixture(autouse=True)
def prevent_live_kaggle_operations(monkeypatch):
    """Unit/contract tests must mock Kaggle, never inherit live cloud authority.

    Mocked subprocess functions still work: tests replace them after autouse
    setup. The underlying Popen boundary rejects an accidental real CLI call
    (except the read-only version probe). HTTP transports to Kaggle are also
    rejected before credentials can leave this process.
    """
    import subprocess
    from urllib.parse import urlparse
    import requests

    original_popen = subprocess.Popen
    original_request = requests.sessions.Session.request

    class GuardedPopen(original_popen):
        # Preserve Popen's type contract for lifecycle code using isinstance.
        def __init__(self, args, *positional, **kwargs):
            tokens = list(args) if isinstance(args, (list, tuple)) else str(args).split()
            is_kaggle = any(os.path.basename(str(token)).lower() in {"kaggle", "kaggle.exe"}
                            for token in tokens[:3])
            is_kaggle = is_kaggle or any("from kaggle.cli import" in str(token) for token in tokens[:3])
            if is_kaggle and "--version" not in tokens:
                raise RuntimeError("Live Kaggle CLI disabled in tests; mock the cloud transport")
            super().__init__(args, *positional, **kwargs)

    def guarded_request(self, method, url, *args, **kwargs):
        host = (urlparse(str(url)).hostname or "").lower()
        if host == "kaggle.com" or host.endswith(".kaggle.com"):
            raise RuntimeError("Live Kaggle HTTP disabled in tests; mock the cloud transport")
        return original_request(self, method, url, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
    monkeypatch.setattr(requests.sessions.Session, "request", guarded_request)
