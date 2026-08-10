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


#: Diagnostic filled in by `_default_state_dir`, surfaced in the structured log
#: and in /api/orca/health. It exists because the fallback below used to be
#: completely silent, and a silent fallback here is far more damaging than the
#: condition it was hiding.
STATE_DIR_DIAGNOSTIC: dict = {}


def _probe_writable(directory: str) -> bool:
    """Tests whether we can actually create a file in `directory`.

    Two details matter, and getting either wrong produced a production bug in
    which two gunicorn workers silently chose *different* state directories.

    **The probe filename must be unique per process.** It used to be a fixed
    `.write-probe`. Two workers booting together both created it, the first
    removed it, and the second's `os.remove()` raised `FileNotFoundError` --
    a subclass of `OSError`, so it was caught by the same handler that means
    "this directory is not writable". A cleanup collision was therefore read as
    a permissions failure.

    **Cleanup must not influence the verdict.** Removing the probe is
    housekeeping; failing to remove it says nothing about whether we can write.
    Hence the `finally` with a swallowed error, rather than the removal sitting
    inside the same `try` as the write.
    """
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
    """Chooses where the local cache lives.

    All workers in a container **must** agree on this path. The database is not
    the source of truth -- the Kaggle-side ledger is -- but it *is* where the
    leases, the fencing tokens and the idempotency keys live, and those only
    coordinate anything if every worker opens the same file. Two workers on two
    databases means two workers that cannot see each other's leases: duplicate
    reconciliation, duplicate pushes, and a submit that is idempotent only
    against half the fleet.

    Hugging Face gives a persistent `/data` volume on paid tiers only; on the
    free tier the filesystem is wiped on every restart. Losing this directory is
    survivable by design, so a temp-dir fallback is acceptable as a last resort
    -- but it is now recorded and reported loudly, because a per-process temp
    dir silently disables cross-worker coordination.
    """
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
            "idempotency keys no longer coordinate anything between workers, so the same "
            "job can be reconciled twice and a duplicate submission can slip through. "
            "Job state itself is still safe -- the Kaggle-side ledger remains the source "
            "of truth -- but set ORCA_STATE_DIR to a directory all workers can write."
        ),
    })
    return fallback


GIB = 1 << 30
MIB = 1 << 20


@dataclass(frozen=True)
class KaggleLimits:
    """Kaggle's *enforced quotas*, which are NOT what statvfs reports.

    This is the root cause of the 'free=1006.8 GB' line in the production log:
    `shutil.disk_usage('/tmp').free` measures the host overlay filesystem the
    container is layered on, not the per-notebook quota Kaggle enforces on top
    of it. The watchdog compared 1006.8 GB against a 5 GB floor, concluded
    there was a terabyte of headroom, and therefore could never fire -- while
    the real budget is roughly 20 GB of output plus ~60 GiB of scratch.

    Sources:
      https://www.kaggle.com/docs/notebooks (technical specifications)
      https://www.kaggle.com/product-feedback/195163
    """

    #: /kaggle/working -- auto-saved as the notebook's output. ~20 GB.
    working_quota_bytes: int = _env_int("KAGGLE_WORKING_QUOTA_GB", 20) * GIB
    #: /kaggle/temp, /kaggle/tmp -- scratch, not persisted. ~60 GiB.
    scratch_quota_bytes: int = _env_int("KAGGLE_SCRATCH_QUOTA_GB", 60) * GIB
    #: Anything statvfs reports above this is the host overlay, not our quota.
    #: Crossing it switches the runner from free-space accounting to
    #: consumption-against-budget accounting.
    implausible_free_bytes: int = _env_int("KAGGLE_IMPLAUSIBLE_FREE_GB", 200) * GIB
    #: Kaggle's documented hard session cutoff for CPU/GPU notebooks: 12 h.
    hard_session_seconds: int = _env_int("KAGGLE_HARD_SESSION_SECONDS", 43200)


@dataclass(frozen=True)
class RunnerConfig:
    """Budgets handed to the in-kernel script."""

    #: When *we* choose to wrap up, comfortably before Kaggle's 12 h cutoff.
    time_limit_seconds: int = _env_int("ORCA_TIME_LIMIT_SECONDS", 39600)  # 11h00m
    #: Reserved at the end of a window for checkpoint + verify + successor push.
    handoff_reserve_seconds: int = _env_int("ORCA_HANDOFF_RESERVE_SECONDS", 1500)  # 25 min
    #: Free-headroom floor, measured against the *quota*, not against statvfs.
    min_free_bytes: int = int(_env_float("ORCA_MIN_FREE_GB", 6.0) * GIB)
    #: Cap on what results.zip may consume inside the 20 GB output quota.
    result_budget_bytes: int = int(_env_float("ORCA_RESULT_BUDGET_GB", 9.0) * GIB)
    #: Session windows a single job may consume.
    max_epochs: int = _env_int("ORCA_MAX_EPOCHS", 24)
    #: Separate budget for windows ended specifically by disk pressure.
    max_disk_epochs: int = _env_int("ORCA_MAX_DISK_EPOCHS", 6)
    #: Cumulative geometry-optimisation cycles across every window. This is the
    #: brake that stops a genuinely non-converging system from consuming
    #: max_epochs * 11 h of somebody's Kaggle quota for nothing.
    max_total_opt_cycles: int = _env_int("ORCA_MAX_TOTAL_OPT_CYCLES", 1500)
    #: %geom MaxIter written into each window's input. ORCA's own default is
    #: 200; a window that ends at MaxIter is continued from its last geometry
    #: with a fresh budget, which is the standard manual recovery.
    per_window_opt_maxiter: int = _env_int("ORCA_PER_WINDOW_MAXITER", 200)
    #: Heartbeat cadence. The watchdog calls a window dead at 6x this.
    heartbeat_seconds: int = _env_int("ORCA_HEARTBEAT_SECONDS", 45)
    #: Disk/time watchdog poll cadence inside the kernel.
    watchdog_poll_seconds: int = _env_int("ORCA_WATCHDOG_POLL_SECONDS", 10)
    #: Max compressed bytes of checkpoint payload carried *inline* in script.py.
    #: Anything larger is fetched by the successor from the predecessor's Kaggle
    #: output instead of being silently dropped (which is what the old
    #: 'restart payload trimmed' path did).
    inline_carry_limit_bytes: int = _env_int("ORCA_INLINE_CARRY_BYTES", 350 * 1024)
    #: Kaggle rejects an oversized kernel source; keep the whole script under this.
    max_kernel_source_bytes: int = _env_int("ORCA_MAX_KERNEL_SOURCE_BYTES", 800 * 1024)
    #: Single-file ceiling for anything placed in a checkpoint bundle.
    max_checkpoint_file_bytes: int = _env_int("ORCA_MAX_CKPT_FILE_BYTES", 256 * MIB)
    #: Whole-bundle ceiling.
    max_checkpoint_bundle_bytes: int = _env_int("ORCA_MAX_CKPT_BUNDLE_BYTES", 512 * MIB)


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = _env_int("ORCA_RETRY_MAX_ATTEMPTS", 6)
    base_delay_seconds: float = _env_float("ORCA_RETRY_BASE_DELAY", 2.0)
    max_delay_seconds: float = _env_float("ORCA_RETRY_MAX_DELAY", 120.0)
    #: Full jitter (AWS architecture blog). Deterministic backoff makes every
    #: worker in a fleet retry in lockstep, turning one Kaggle blip into a
    #: synchronised thundering herd against the same endpoint.
    jitter: bool = _env_bool("ORCA_RETRY_JITTER", True)


@dataclass(frozen=True)
class WatchdogConfig:
    #: A kernel accepted by Kaggle but never started within this is re-driven.
    queued_grace_seconds: int = _env_int("ORCA_WATCHDOG_QUEUED_GRACE", 3600)
    #: RUNNING with no heartbeat for this long == the window died silently.
    heartbeat_grace_seconds: int = _env_int("ORCA_WATCHDOG_HEARTBEAT_GRACE", 900)
    #: Ceiling on any single handoff phase (CHECKPOINTING/VERIFYING/RESTARTING).
    handoff_grace_seconds: int = _env_int("ORCA_WATCHDOG_HANDOFF_GRACE", 2700)
    #: No epoch advance at all for this long escalates to operator attention.
    stall_escalation_seconds: int = _env_int("ORCA_WATCHDOG_STALL_ESCALATION", 14 * 3600)
    #: How often the background sweeper wakes up.
    sweep_interval_seconds: int = _env_int("ORCA_WATCHDOG_SWEEP_INTERVAL", 120)
    enabled: bool = _env_bool("ORCA_WATCHDOG_ENABLED", True)


@dataclass(frozen=True)
class StoreConfig:
    state_dir: str = field(default_factory=_default_state_dir)
    db_filename: str = "orchestrator.sqlite3"
    #: SQLite busy_timeout. Gunicorn runs 2 workers x 4 threads against one file.
    busy_timeout_ms: int = _env_int("ORCA_SQLITE_BUSY_TIMEOUT_MS", 8000)
    #: How long a lease is held before it is considered abandoned. Must exceed
    #: the longest single action (a `kaggle kernels push` with retries).
    lease_ttl_seconds: int = _env_int("ORCA_LEASE_TTL_SECONDS", 300)
    #: Idempotency records older than this are pruned.
    idempotency_ttl_seconds: int = _env_int("ORCA_IDEMPOTENCY_TTL_SECONDS", 86400)
    #: In-RAM only, never written to disk. See credentials.CredentialBroker and
    #: the security note in ARCHITECTURE.md.
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

    #: Slug prefix that identifies this site's kernels on a Kaggle account.
    #: Changing it orphans every existing job, so it is deliberately not an
    #: env var that could differ between two deploys of the same Space.
    job_id_prefix: str = "chem-tools-"
    #: Schema version stamped into every manifest. A successor kernel pushed by
    #: an older deploy must be readable by a newer one and vice versa.
    manifest_version: int = 2

    def to_dict(self) -> dict:
        return asdict(self)


CONFIG = Config()
