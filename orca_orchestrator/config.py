# -*- coding: utf-8 -*-
"""
Central configuration.

Every tunable lives here with an env-var override, so nothing important is a
magic number buried in a 1300-line script. Values are read once at import and
frozen: a config that can change under a running reconciler is a source of
non-deterministic behaviour.
"""
from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass, field, asdict


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


STATE_DIR_DIAGNOSTIC: dict = {}


def _probe_writable(directory: str) -> bool:
    probe = os.path.join(
        directory, ".write-probe-%d-%s" % (os.getpid(), uuid.uuid4().hex[:8])
    )
    try:
        with open(probe, "w") as fh:
            fh.write("ok")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        return False
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass
    return True


def _default_state_dir() -> str:
    """Chooses a shared writable state directory for all web workers."""
    candidates = [
        ("ORCA_STATE_DIR", os.environ.get("ORCA_STATE_DIR")),
        ("persistent-volume", "/data"),
        ("working-directory", os.path.join(os.getcwd(), ".state")),
    ]
    rejected = []
    for source, candidate in candidates:
        if not candidate:
            continue
        try:
            os.makedirs(candidate, exist_ok=True)
        except OSError as exc:
            rejected.append({"source": source, "path": candidate,
                             "reason": "mkdir failed: %s" % exc})
            continue
        if not _probe_writable(candidate):
            rejected.append({"source": source, "path": candidate,
                             "reason": "directory is not writable"})
            continue
        STATE_DIR_DIAGNOSTIC.update({
            "state_dir": candidate, "source": source, "shared": True,
            "rejected": rejected,
        })
        return candidate

    fallback = tempfile.mkdtemp(prefix="orca-state-")
    STATE_DIR_DIAGNOSTIC.update({
        "state_dir": fallback,
        "source": "process-private temporary directory",
        "shared": False,
        "rejected": rejected,
        "warning": (
            "No shared, writable state directory was found, so this worker fell back to "
            "a PRIVATE temporary directory. Every worker process will get a different "
            "one, which disables cross-worker coordination: leases, fencing tokens and "
            "idempotency keys no longer coordinate anything between workers. Set "
            "ORCA_STATE_DIR to a directory all workers can write."
        ),
    })
    return fallback


GIB = 1 << 30
MIB = 1 << 20


@dataclass(frozen=True)
class KaggleLimits:
    working_quota_bytes: int = _env_int("KAGGLE_WORKING_QUOTA_GB", 20) * GIB
    scratch_quota_bytes: int = _env_int("KAGGLE_SCRATCH_QUOTA_GB", 60) * GIB
    implausible_free_bytes: int = _env_int("KAGGLE_IMPLAUSIBLE_FREE_GB", 200) * GIB
    hard_session_seconds: int = _env_int("KAGGLE_HARD_SESSION_SECONDS", 43200)


@dataclass(frozen=True)
class RunnerConfig:
    time_limit_seconds: int = _env_int("ORCA_TIME_LIMIT_SECONDS", 39600)
    handoff_reserve_seconds: int = _env_int("ORCA_HANDOFF_RESERVE_SECONDS", 1500)
    min_free_bytes: int = int(_env_float("ORCA_MIN_FREE_GB", 6.0) * GIB)
    result_budget_bytes: int = int(_env_float("ORCA_RESULT_BUDGET_GB", 9.0) * GIB)
    #: 48 eleven-hour windows is a practical default for calculations that run
    #: for days or weeks while still providing a finite runaway guard. The
    #: Kaggle account's own quotas remain the hard external limit.
    max_epochs: int = _env_int("ORCA_MAX_EPOCHS", 48)
    #: Separate budget for windows ended specifically by disk pressure.
    max_disk_epochs: int = _env_int("ORCA_MAX_DISK_EPOCHS", 12)
    max_total_opt_cycles: int = _env_int("ORCA_MAX_TOTAL_OPT_CYCLES", 3000)
    per_window_opt_maxiter: int = _env_int("ORCA_PER_WINDOW_MAXITER", 200)
    heartbeat_seconds: int = _env_int("ORCA_HEARTBEAT_SECONDS", 45)
    watchdog_poll_seconds: int = _env_int("ORCA_WATCHDOG_POLL_SECONDS", 10)
    inline_carry_limit_bytes: int = _env_int("ORCA_INLINE_CARRY_BYTES", 350 * 1024)
    max_kernel_source_bytes: int = _env_int("ORCA_MAX_KERNEL_SOURCE_BYTES", 800 * 1024)
    max_checkpoint_file_bytes: int = _env_int("ORCA_MAX_CKPT_FILE_BYTES", 256 * MIB)
    max_checkpoint_bundle_bytes: int = _env_int("ORCA_MAX_CKPT_BUNDLE_BYTES", 512 * MIB)


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = _env_int("ORCA_RETRY_MAX_ATTEMPTS", 6)
    base_delay_seconds: float = _env_float("ORCA_RETRY_BASE_DELAY", 2.0)
    max_delay_seconds: float = _env_float("ORCA_RETRY_MAX_DELAY", 120.0)
    jitter: bool = _env_bool("ORCA_RETRY_JITTER", True)


@dataclass(frozen=True)
class WatchdogConfig:
    queued_grace_seconds: int = _env_int("ORCA_WATCHDOG_QUEUED_GRACE", 3600)
    heartbeat_grace_seconds: int = _env_int("ORCA_WATCHDOG_HEARTBEAT_GRACE", 900)
    handoff_grace_seconds: int = _env_int("ORCA_WATCHDOG_HANDOFF_GRACE", 2700)
    stall_escalation_seconds: int = _env_int("ORCA_WATCHDOG_STALL_ESCALATION", 14 * 3600)
    sweep_interval_seconds: int = _env_int("ORCA_WATCHDOG_SWEEP_INTERVAL", 120)
    enabled: bool = _env_bool("ORCA_WATCHDOG_ENABLED", True)


@dataclass(frozen=True)
class StoreConfig:
    state_dir: str = field(default_factory=_default_state_dir)
    db_filename: str = "orchestrator.sqlite3"
    busy_timeout_ms: int = _env_int("ORCA_SQLITE_BUSY_TIMEOUT_MS", 8000)
    lease_ttl_seconds: int = _env_int("ORCA_LEASE_TTL_SECONDS", 300)
    idempotency_ttl_seconds: int = _env_int("ORCA_IDEMPOTENCY_TTL_SECONDS", 86400)
    credential_ttl_seconds: int = _env_int("ORCA_CREDENTIAL_TTL_SECONDS", 3600)

    @property
    def db_path(self) -> str:
        return os.path.join(self.state_dir, self.db_filename)


@dataclass(frozen=True)
class Config:
    kaggle: KaggleLimits = field(default_factory=KaggleLimits)
    runner: RunnerConfig = field(default_factory=RunnerConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    watchdog: WatchdogConfig = field(default_factory=WatchdogConfig)
    store: StoreConfig = field(default_factory=StoreConfig)
    job_id_prefix: str = "chem-tools-"
    manifest_version: int = 2

    def to_dict(self) -> dict:
        return asdict(self)


CONFIG = Config()
