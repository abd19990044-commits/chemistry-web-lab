"""Durable server-local ORCA worker.

The web process only enqueues work in this module.  A separate worker process
claims one attempt at a time from SQLite, launches ORCA, renews a lease, and
persists enough process identity to adopt an ORCA process after a worker or web
restart.  The old synchronous ``execute_local_orca_job`` API remains available
for compatibility, but production routes must use this queue.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import platform
import shutil
import signal
import socket
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from services import local_orca_service
from services.sqlite_migrations import backup_before_schema_upgrade

log = logging.getLogger("chemlab.local_orca_worker")

SCHEMA_VERSION = 1
DEFAULT_POLL_SECONDS = float(os.environ.get("ORCA_LOCAL_WORKER_POLL_SECONDS", "1.0"))
DEFAULT_LEASE_SECONDS = float(os.environ.get("ORCA_LOCAL_WORKER_LEASE_SECONDS", "30"))
HEARTBEAT_SECONDS = float(os.environ.get("ORCA_LOCAL_WORKER_HEARTBEAT_SECONDS", "2"))
MIN_FREE_DISK_GB = float(os.environ.get("ORCA_LOCAL_MIN_FREE_DISK_GB", "1"))
LOG_READ_CHUNK_BYTES = int(os.environ.get("ORCA_LOCAL_LOG_READ_CHUNK_BYTES", str(256 * 1024)))
RECONCILE_INTERVAL_SECONDS = float(os.environ.get(
    "ORCA_LOCAL_RECONCILE_INTERVAL_SECONDS", str(max(2.0, DEFAULT_LEASE_SECONDS / 2.0))
))

TERMINAL_STATES = frozenset({"COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "CANCELLED"})
ACTIVE_STATES = frozenset({"STARTING", "RUNNING", "VERIFYING", "CANCEL_REQUESTED", "RECOVERY_REQUIRED"})


class WorkerFenced(RuntimeError):
    """The worker lost its lease and must not mutate the Job any further."""


class LocalJobNotFound(KeyError):
    pass


def resolve_state_dir(state_dir: Optional[str] = None) -> str:
    base = (state_dir or os.environ.get("CHEMISTRY_LAB_STATE_DIR")
            or os.environ.get("ORCA_STATE_DIR") or os.path.join(os.getcwd(), "data"))
    base = os.path.abspath(base)
    os.makedirs(base, exist_ok=True)
    return base


def db_path(state_dir: Optional[str] = None) -> str:
    return os.path.join(resolve_state_dir(state_dir), "local_orca_worker.sqlite3")


def worker_health(state_dir: Optional[str] = None, *, max_age_seconds: float = 10.0) -> Dict[str, Any]:
    """Return whether a live worker heartbeat exists for readiness probes."""
    path = db_path(state_dir)
    try:
        with contextlib.closing(_connect(path)) as conn:
            rows = conn.execute(
                "SELECT worker_id, pid, hostname, heartbeat_at, status "
                "FROM local_workers WHERE status='RUNNING' ORDER BY heartbeat_at DESC"
            ).fetchall()
        now = _now()
        live = [dict(row) for row in rows if now - float(row["heartbeat_at"] or 0) <= max_age_seconds]
        return {"ok": bool(live), "workers": live, "stale_workers": len(rows) - len(live)}
    except (OSError, sqlite3.Error) as exc:
        return {"ok": False, "workers": [], "error": type(exc).__name__}


def _now() -> float:
    return time.time()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(value: Optional[str], default: Any = None) -> Any:
    if not value:
        return {} if default is None else default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {} if default is None else default


def _creation_value(value: Any) -> Any:
    """Keep OS process creation timestamps comparable across SQLite text storage."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _safe_row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    result = dict(row)
    for key in ("metadata_json", "resources_json", "result_json"):
        if key in result:
            result[key[:-5] if key.endswith("_json") else key] = _decode(result[key])
    return result


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db(state_dir: Optional[str] = None) -> str:
    path = db_path(state_dir)
    backup_path = backup_before_schema_upgrade(
        path,
        component="local-orca-worker",
        target_version=SCHEMA_VERSION,
        required_schema={
            "schema_meta": ("key", "value"),
            "local_jobs": (
                "job_id", "cancel_requested_at", "log_offset", "log_inode",
                "stdout_tail", "revision", "next_attempt_at", "canonical_output_path",
            ),
            "local_job_attempts": ("attempt_id", "canonical_output_path"),
            "local_job_idempotency": ("idempotency_key", "job_id"),
            "local_job_events": ("event_id", "job_id"),
            "local_workers": ("worker_id", "heartbeat_at"),
        },
    )
    if backup_path:
        log.warning(
            "created verified SQLite backup before local-worker schema migration",
            extra={"event": "sqlite_pre_migration_backup", "backup_path": backup_path},
        )
    with contextlib.closing(_connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS local_jobs (
                job_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                input_text TEXT NOT NULL,
                stage_kind TEXT,
                workflow_id TEXT,
                step_id TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                resources_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL,
                attempt_no INTEGER NOT NULL DEFAULT 1,
                attempt_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                queued_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                worker_id TEXT,
                lease_fence INTEGER NOT NULL DEFAULT 0,
                lease_expires_at REAL,
                heartbeat_at REAL,
                pid INTEGER,
                process_start_time TEXT,
                command_json TEXT,
                command_fingerprint TEXT,
                hostname TEXT,
                workspace TEXT,
                input_path TEXT,
                output_path TEXT,
                canonical_output_path TEXT,
                return_code INTEGER,
                result_json TEXT,
                error_code TEXT,
                error_message TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                cancel_requested_at REAL,
                log_offset INTEGER NOT NULL DEFAULT 0,
                log_inode TEXT,
                stdout_tail TEXT NOT NULL DEFAULT '',
                revision INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL,
                projected_at REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS local_job_attempts (
                attempt_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES local_jobs(job_id) ON DELETE CASCADE,
                attempt_no INTEGER NOT NULL,
                status TEXT NOT NULL,
                worker_id TEXT,
                lease_fence INTEGER NOT NULL DEFAULT 0,
                started_at REAL,
                finished_at REAL,
                pid INTEGER,
                process_start_time TEXT,
                command_fingerprint TEXT,
                workspace TEXT,
                input_path TEXT,
                output_path TEXT,
                canonical_output_path TEXT,
                return_code INTEGER,
                result_json TEXT,
                error_code TEXT,
                error_message TEXT,
                UNIQUE(job_id, attempt_no)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS local_job_idempotency (
                idempotency_key TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                job_id TEXT NOT NULL REFERENCES local_jobs(job_id) ON DELETE CASCADE,
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS local_job_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES local_jobs(job_id) ON DELETE CASCADE,
                attempt_id TEXT,
                at REAL NOT NULL,
                worker_id TEXT,
                event TEXT NOT NULL,
                old_state TEXT,
                new_state TEXT,
                detail_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS local_workers (
                worker_id TEXT PRIMARY KEY,
                hostname TEXT NOT NULL,
                pid INTEGER NOT NULL,
                started_at REAL NOT NULL,
                heartbeat_at REAL NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_local_jobs_status ON local_jobs(status, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_local_jobs_owner ON local_jobs(owner_id, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_local_jobs_workflow ON local_jobs(workflow_id, step_id)")
        existing = _columns(conn, "local_jobs")
        migrations = {
            "cancel_requested_at": "REAL",
            "log_offset": "INTEGER NOT NULL DEFAULT 0",
            "log_inode": "TEXT",
            "stdout_tail": "TEXT NOT NULL DEFAULT ''",
            "revision": "INTEGER NOT NULL DEFAULT 0",
            "next_attempt_at": "REAL",
            "canonical_output_path": "TEXT",
        }
        for name, type_sql in migrations.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE local_jobs ADD COLUMN {name} {type_sql}")
        attempt_existing = _columns(conn, "local_job_attempts")
        if "canonical_output_path" not in attempt_existing:
            conn.execute("ALTER TABLE local_job_attempts ADD COLUMN canonical_output_path TEXT")
        cols = {row[1] for row in conn.execute("PRAGMA table_info(local_jobs)").fetchall()}
        if cols and "projected_at" not in cols:
            conn.execute("ALTER TABLE local_jobs ADD COLUMN projected_at REAL")
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('local_orca_worker', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    return path


def _request_hash(*, owner_id: str, input_text: str, job_name: str, stage_kind: Optional[str],
                  workflow_id: Optional[str], step_id: Optional[str], metadata: Dict[str, Any],
                  resources: Dict[str, Any]) -> str:
    payload = {
        "owner_id": owner_id,
        "input_text": input_text,
        "job_name": job_name,
        "stage_kind": stage_kind,
        "workflow_id": workflow_id,
        "step_id": step_id,
        "metadata": metadata,
        "resources": resources,
    }
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _event(conn: sqlite3.Connection, job_id: str, event: str, *, attempt_id: Optional[str] = None,
           worker_id: Optional[str] = None, old_state: Optional[str] = None,
           new_state: Optional[str] = None, detail: Optional[Dict[str, Any]] = None) -> None:
    conn.execute(
        "INSERT INTO local_job_events(job_id, attempt_id, at, worker_id, event, old_state, new_state, detail_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, attempt_id, _now(), worker_id, event, old_state, new_state, _json(detail or {})),
    )


def enqueue_local_job(*, owner_id: str, input_text: str, job_name: str = "calculation",
                      job_id: Optional[str] = None, attempt_id: Optional[str] = None,
                      stage_kind: Optional[str] = None, workflow_id: Optional[str] = None,
                      step_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                      resources: Optional[Dict[str, Any]] = None, idempotency_key: Optional[str] = None,
                      state_dir: Optional[str] = None) -> Dict[str, Any]:
    if not owner_id:
        raise ValueError("owner_id is required")
    if not input_text or not input_text.strip():
        raise ValueError("input_text is required")
    init_db(state_dir)
    path = db_path(state_dir)
    metadata = dict(metadata or {})
    resources = dict(resources or {})
    request_hash = _request_hash(
        owner_id=owner_id, input_text=input_text, job_name=job_name,
        stage_kind=stage_kind, workflow_id=workflow_id, step_id=step_id,
        metadata=metadata, resources=resources,
    )
    # A retry caused by browser refresh or a lost HTTP response must replay
    # the same durable job even when the client omitted the optional header.
    # Callers that intentionally want a new execution provide a new explicit
    # idempotency key/attempt identity.
    idempotency_key = idempotency_key or f"local:{owner_id}:{request_hash}"
    now = _now()
    job_id = job_id or f"local_{uuid.uuid4().hex[:16]}"
    attempt_id = attempt_id or uuid.uuid4().hex

    with contextlib.closing(_connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if idempotency_key:
            prior = conn.execute(
                "SELECT owner_id, request_hash, job_id FROM local_job_idempotency WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if prior:
                if prior["owner_id"] != owner_id or prior["request_hash"] != request_hash:
                    conn.rollback()
                    return {"ok": False, "error": "IDEMPOTENCY_KEY_REUSE"}
                existing = conn.execute("SELECT * FROM local_jobs WHERE job_id = ?", (prior["job_id"],)).fetchone()
                conn.commit()
                return {"ok": True, "replayed": True, "job": _safe_row(existing)}

        conn.execute(
            """
            INSERT INTO local_jobs(
                job_id, owner_id, input_text, stage_kind, workflow_id, step_id,
                metadata_json, resources_json, status, attempt_no, attempt_id,
                created_at, updated_at, queued_at, heartbeat_at, revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'QUEUED', 1, ?, ?, ?, ?, ?, 1)
            """,
            (job_id, owner_id, input_text, stage_kind, workflow_id, step_id,
             _json({**metadata, "job_name": job_name}), _json(resources), attempt_id,
             now, now, now, now),
        )
        conn.execute(
            "INSERT INTO local_job_attempts(attempt_id, job_id, attempt_no, status) VALUES (?, ?, 1, 'QUEUED')",
            (attempt_id, job_id),
        )
        if idempotency_key:
            conn.execute(
                "INSERT INTO local_job_idempotency(idempotency_key, owner_id, request_hash, job_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (idempotency_key, owner_id, request_hash, job_id, now),
            )
        _event(conn, job_id, "ENQUEUED", attempt_id=attempt_id, new_state="QUEUED", detail={"job_name": job_name})
        row = conn.execute("SELECT * FROM local_jobs WHERE job_id = ?", (job_id,)).fetchone()
        conn.commit()
    return {"ok": True, "replayed": False, "job": _safe_row(row)}


def get_local_job(job_id: str, *, owner_id: Optional[str] = None,
                  state_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    init_db(state_dir)
    with contextlib.closing(_connect(db_path(state_dir))) as conn:
        row = conn.execute("SELECT * FROM local_jobs WHERE job_id = ?", (job_id,)).fetchone()
    item = _safe_row(row)
    if item and owner_id is not None and item.get("owner_id") != owner_id:
        return None
    return item


def list_local_jobs(*, owner_id: Optional[str] = None, state_dir: Optional[str] = None,
                    limit: int = 200) -> List[Dict[str, Any]]:
    init_db(state_dir)
    limit = max(1, min(int(limit), 1000))
    with contextlib.closing(_connect(db_path(state_dir))) as conn:
        if owner_id is None:
            rows = conn.execute("SELECT * FROM local_jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM local_jobs WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?",
                (owner_id, limit),
            ).fetchall()
    decoded = [_safe_row(row) for row in rows if row is not None]
    return [item for item in decoded if item is not None]


def get_local_job_events(job_id: str, *, owner_id: Optional[str] = None,
                         state_dir: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    job = get_local_job(job_id, owner_id=owner_id, state_dir=state_dir)
    if not job:
        return []
    init_db(state_dir)
    with contextlib.closing(_connect(db_path(state_dir))) as conn:
        rows = conn.execute(
            "SELECT * FROM local_job_events WHERE job_id = ? ORDER BY event_id DESC LIMIT ?",
            (job_id, max(1, min(int(limit), 1000))),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["detail"] = _decode(item.pop("detail_json", "{}"))
        result.append(item)
    return result


def request_cancel_local_job(job_id: str, *, owner_id: Optional[str] = None,
                             state_dir: Optional[str] = None) -> Dict[str, Any]:
    init_db(state_dir)
    path = db_path(state_dir)
    now = _now()
    with contextlib.closing(_connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM local_jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None or (owner_id is not None and row["owner_id"] != owner_id):
            conn.rollback()
            return {"ok": False, "error": "JOB_NOT_FOUND"}
        old = row["status"]
        if old in TERMINAL_STATES:
            conn.commit()
            return {"ok": True, "cancelled": old == "CANCELLED", "status": old, "already_terminal": True}
        if old in ("QUEUED", "QUEUED_WAITING_RESOURCES", "RECOVERY_REQUIRED"):
            new = "CANCELLED"
            conn.execute(
                "UPDATE local_jobs SET status=?, cancel_requested=1, cancel_requested_at=?, updated_at=?, "
                "finished_at=?, revision=revision+1 WHERE job_id=? AND status=?",
                (new, now, now, now, job_id, old),
            )
            if row["workflow_id"] and row["step_id"]:
                try:
                    from services.reaction_workflow_service import apply_local_worker_result
                    apply_local_worker_result(dict(row), {"ok": False, "error_code": "CANCELLED", "error": "Cancelled while queued", "exit_code": -15}, state_dir)
                except Exception:
                    pass
        else:
            new = "CANCEL_REQUESTED"
            conn.execute(
                "UPDATE local_jobs SET status=?, cancel_requested=1, cancel_requested_at=?, updated_at=?, "
                "revision=revision+1 WHERE job_id=? AND status NOT IN ('COMPLETED','COMPLETED_WITH_WARNINGS','FAILED','CANCELLED')",
                (new, now, now, job_id),
            )
        _event(conn, job_id, "CANCEL_REQUESTED", attempt_id=row["attempt_id"], old_state=old, new_state=new)
        conn.commit()
    if old in ACTIVE_STATES:
        # The worker will also observe cancel_requested.  This direct signal is
        # what makes cancellation responsive when the worker is busy in ORCA.
        local_orca_service.cancel_local_orca_job(job_id, row["attempt_id"], state_dir=state_dir)
    return {"ok": True, "cancelled": new == "CANCELLED", "status": new}


def _resource_estimate(job: Dict[str, Any]) -> tuple[int, int]:
    resources = dict(job.get("resources") or {})
    metadata = dict(job.get("metadata") or {})
    nprocs = int(resources.get("nprocs") or resources.get("cpu_cores") or metadata.get("nprocs") or 1)
    maxcore = int(resources.get("maxcore_mb") or resources.get("maxcore") or metadata.get("maxcore_mb") or 1000)
    input_text = str(job.get("input_text") or "")
    for match in __import__("re").finditer(r"(?im)^\s*%pal\b.*?nprocs\s+(\d+)", input_text):
        nprocs = max(nprocs, int(match.group(1)))
    for match in __import__("re").finditer(r"(?im)^\s*%maxcore\s+(\d+)", input_text):
        maxcore = max(maxcore, int(match.group(1)))
    return max(1, nprocs), max(128, maxcore)


def _resources_available(job: Dict[str, Any], state_dir: str) -> tuple[bool, str]:
    nprocs, maxcore = _resource_estimate(job)
    try:
        import psutil
        cpu_limit = int(os.environ.get("ORCA_LOCAL_MAX_CORES", psutil.cpu_count(logical=True) or 1))
        mem_limit_mb = int(os.environ.get("ORCA_LOCAL_MAX_MEMORY_MB", (psutil.virtual_memory().available // (1024 * 1024))))
    except Exception:
        cpu_limit = int(os.environ.get("ORCA_LOCAL_MAX_CORES", "1"))
        mem_limit_mb = int(os.environ.get("ORCA_LOCAL_MAX_MEMORY_MB", "4096"))
    if nprocs > cpu_limit:
        return False, f"requested {nprocs} cores, limit is {cpu_limit}"
    if nprocs * maxcore > mem_limit_mb:
        return False, f"requested {nprocs * maxcore} MB, available scheduler limit is {mem_limit_mb} MB"
    cfg = local_orca_service.get_local_orca_settings(state_dir)
    output_dir = cfg.get("output_directory") or state_dir
    try:
        free_gb = shutil.disk_usage(output_dir).free / (1024 ** 3)
        if free_gb < MIN_FREE_DISK_GB:
            return False, f"free disk is {free_gb:.2f} GB, minimum is {MIN_FREE_DISK_GB:.2f} GB"
    except OSError:
        return False, "cannot determine free disk space"
    return True, ""


def _worker_row(conn: sqlite3.Connection, job_id: str) -> Optional[Dict[str, Any]]:
    return _safe_row(conn.execute("SELECT * FROM local_jobs WHERE job_id = ?", (job_id,)).fetchone())


@dataclass
class ClaimedJob:
    job: Dict[str, Any]
    fence: int
    worker_id: str


class LocalOrcaWorker:
    def __init__(self, state_dir: Optional[str] = None, *, worker_id: Optional[str] = None,
                 poll_seconds: float = DEFAULT_POLL_SECONDS):
        self.state_dir = resolve_state_dir(state_dir)
        init_db(self.state_dir)
        self.worker_id = worker_id or f"local-worker:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.poll_seconds = max(0.1, float(poll_seconds))
        self.stop_event = threading.Event()
        self._active: Optional[ClaimedJob] = None
        self._log_lock = threading.Lock()
        self._disk_warned: set[str] = set()
        self._last_reconcile_at = 0.0
        self._register_worker()

    def _register_worker(self) -> None:
        with contextlib.closing(_connect(db_path(self.state_dir))) as conn:
            now = _now()
            conn.execute(
                "INSERT OR REPLACE INTO local_workers(worker_id, hostname, pid, started_at, heartbeat_at, status) "
                "VALUES (?, ?, ?, ?, ?, 'RUNNING')",
                (self.worker_id, socket.gethostname(), os.getpid(), now, now),
            )
            conn.commit()

    def _touch_worker(self, status: str = "RUNNING") -> None:
        with contextlib.closing(_connect(db_path(self.state_dir))) as conn:
            conn.execute("UPDATE local_workers SET heartbeat_at=?, status=? WHERE worker_id=?", (_now(), status, self.worker_id))
            conn.commit()

    def close(self) -> None:
        self.stop_event.set()
        self._touch_worker("STOPPED")

    def _claim_next(self) -> Optional[ClaimedJob]:
        path = db_path(self.state_dir)
        now = _now()
        with contextlib.closing(_connect(path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            active_count = conn.execute(
                "SELECT COUNT(*) AS c FROM local_jobs WHERE status IN "
                "('STARTING','RUNNING','VERIFYING','CANCEL_REQUESTED','RECOVERY_REQUIRED') "
                "AND (status='RECOVERY_REQUIRED' OR lease_expires_at > ?)",
                (now,),
            ).fetchone()["c"]
            # The primary server-local backend is deliberately serial.  This
            # database check is in the same write transaction as the claim, so
            # two worker processes cannot both pass the resource gate and then
            # race at Popen; the old file lock remains a second defence.
            if int(active_count or 0) >= 1:
                conn.commit()
                return None
            row = conn.execute(
                "SELECT * FROM local_jobs WHERE status IN ('QUEUED','QUEUED_WAITING_RESOURCES') "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY created_at ASC LIMIT 1",
                (_now(),),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            job = _safe_row(row)
            if job is None:
                conn.rollback()
                return None
            available, reason = _resources_available(job, self.state_dir)
            if not available:
                conn.execute(
                    "UPDATE local_jobs SET status='QUEUED_WAITING_RESOURCES', error_code='WAITING_FOR_RESOURCES', "
                    "error_message=?, next_attempt_at=?, updated_at=?, heartbeat_at=?, revision=revision+1 WHERE job_id=? AND status IN ('QUEUED','QUEUED_WAITING_RESOURCES')",
                    (reason, _now() + 10.0, _now(), _now(), job["job_id"]),
                )
                _event(conn, job["job_id"], "WAITING_FOR_RESOURCES", attempt_id=job["attempt_id"],
                       old_state=job["status"], new_state="QUEUED_WAITING_RESOURCES", detail={"reason": reason})
                conn.commit()
                return None
            now = _now()
            new_fence = int(job.get("lease_fence") or 0) + 1
            changed = conn.execute(
                "UPDATE local_jobs SET status='STARTING', worker_id=?, lease_fence=?, lease_expires_at=?, "
                "heartbeat_at=?, updated_at=?, started_at=COALESCE(started_at, ?), revision=revision+1 "
                "WHERE job_id=? AND status IN ('QUEUED','QUEUED_WAITING_RESOURCES')",
                (self.worker_id, new_fence, now + DEFAULT_LEASE_SECONDS, now, now, now, job["job_id"]),
            )
            if changed.rowcount != 1:
                conn.rollback()
                return None
            conn.execute(
                "UPDATE local_job_attempts SET status='STARTING', worker_id=?, lease_fence=?, started_at=COALESCE(started_at, ?) WHERE attempt_id=?",
                (self.worker_id, new_fence, now, job["attempt_id"]),
            )
            _event(conn, job["job_id"], "CLAIMED", attempt_id=job["attempt_id"], worker_id=self.worker_id,
                   old_state=job["status"], new_state="STARTING", detail={"fence": new_fence})
            claimed = _worker_row(conn, job["job_id"])
            conn.commit()
        return ClaimedJob(claimed or job, new_fence, self.worker_id)

    def _sync_log(self, job_id: str, fence: int, output_path: Optional[str]) -> None:
        if not output_path or not os.path.isfile(output_path):
            return
        path = db_path(self.state_dir)
        with contextlib.closing(_connect(path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM local_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                conn.rollback()
                return
            offset = int(row["log_offset"] or 0)
            try:
                inode = str(os.stat(output_path).st_ino)
            except OSError:
                inode = ""
            if row["log_inode"] and row["log_inode"] != inode:
                offset = 0
            size = os.path.getsize(output_path)
            if size < offset:
                offset = 0
            with open(output_path, "rb") as fh:
                fh.seek(offset)
                chunk = fh.read(max(1, LOG_READ_CHUNK_BYTES))
            if chunk:
                text = chunk.decode("utf-8", errors="replace")
                tail = ((row["stdout_tail"] or "") + text)[-50000:]
                offset += len(chunk)
            else:
                tail = row["stdout_tail"] or ""
            conn.execute(
                "UPDATE local_jobs SET log_offset=?, log_inode=?, stdout_tail=?, updated_at=?, heartbeat_at=?, "
                "lease_expires_at=? WHERE job_id=? AND worker_id=? AND lease_fence=? AND status IN ('STARTING','RUNNING','VERIFYING','CANCEL_REQUESTED')",
                (offset, inode, tail, _now(), _now(), _now() + DEFAULT_LEASE_SECONDS, job_id, self.worker_id, fence),
            )
            conn.commit()

    def _heartbeat(self, claim: ClaimedJob, process_info: Optional[Dict[str, Any]] = None) -> None:
        job = claim.job
        info = dict(process_info or {})
        now = _now()
        path = db_path(self.state_dir)
        with contextlib.closing(_connect(path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status, cancel_requested FROM local_jobs WHERE job_id=?", (job["job_id"],)).fetchone()
            if not row:
                conn.rollback()
                raise WorkerFenced("job disappeared")
            status = "CANCEL_REQUESTED" if row["cancel_requested"] else "RUNNING"
            changed = conn.execute(
                "UPDATE local_jobs SET status=?, worker_id=?, lease_expires_at=?, heartbeat_at=?, updated_at=?, "
                "pid=COALESCE(?,pid), process_start_time=COALESCE(?,process_start_time), command_json=COALESCE(?,command_json), "
                "command_fingerprint=COALESCE(?,command_fingerprint), hostname=COALESCE(?,hostname), workspace=COALESCE(?,workspace), "
                "input_path=COALESCE(?,input_path), output_path=COALESCE(?,output_path), "
                "canonical_output_path=COALESCE(?,canonical_output_path), revision=revision+1 "
                "WHERE job_id=? AND worker_id=? AND lease_fence=? AND status IN ('STARTING','RUNNING','VERIFYING','CANCEL_REQUESTED')",
                (status, self.worker_id, now + DEFAULT_LEASE_SECONDS, now, now,
                 info.get("pid"), str(info.get("creation_time")) if info.get("creation_time") is not None else None,
                 _json(info.get("command")) if info.get("command") else None, info.get("command_fingerprint"),
                 info.get("hostname"), info.get("working_directory"), info.get("input_path"), info.get("output_path"),
                 info.get("canonical_output_path"),
                 job["job_id"], self.worker_id, claim.fence),
            )
            if changed.rowcount != 1:
                conn.rollback()
                raise WorkerFenced("local job lease/fence no longer belongs to this worker")
            if status == "RUNNING":
                conn.execute("UPDATE local_job_attempts SET status='RUNNING', worker_id=?, pid=COALESCE(?,pid), process_start_time=COALESCE(?,process_start_time), command_fingerprint=COALESCE(?,command_fingerprint), workspace=COALESCE(?,workspace), input_path=COALESCE(?,input_path), output_path=COALESCE(?,output_path), canonical_output_path=COALESCE(?,canonical_output_path) WHERE attempt_id=? AND lease_fence=?",
                             (self.worker_id, info.get("pid"), str(info.get("creation_time")) if info.get("creation_time") is not None else None, info.get("command_fingerprint"), info.get("working_directory"), info.get("input_path"), info.get("output_path"), info.get("canonical_output_path"), job["attempt_id"], claim.fence))
            conn.execute(
                "UPDATE local_workers SET heartbeat_at=?, status='RUNNING' WHERE worker_id=?",
                (now, self.worker_id),
            )
            conn.commit()
        self._sync_log(job["job_id"], claim.fence, info.get("output_path") or job.get("output_path"))
        output_path = info.get("output_path") or job.get("output_path")
        if output_path and job["job_id"] not in self._disk_warned:
            try:
                free_gb = shutil.disk_usage(os.path.dirname(output_path)).free / (1024 ** 3)
            except OSError:
                free_gb = None
            if free_gb is not None and free_gb < MIN_FREE_DISK_GB:
                with contextlib.closing(_connect(path)) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    owned = conn.execute(
                        "SELECT 1 FROM local_jobs WHERE job_id=? AND worker_id=? AND lease_fence=?",
                        (job["job_id"], self.worker_id, claim.fence),
                    ).fetchone()
                    if owned:
                        _event(
                            conn, job["job_id"], "DISK_PRESSURE_WARNING",
                            attempt_id=job["attempt_id"], worker_id=self.worker_id,
                            old_state=status, new_state=status,
                            detail={"free_gb": round(free_gb, 3), "minimum_gb": MIN_FREE_DISK_GB},
                        )
                    conn.commit()
                self._disk_warned.add(job["job_id"])
                log.warning(
                    "Local ORCA job %s is running under the free-disk threshold: %.3f GB",
                    job["job_id"], free_gb,
                )

    def _cancel_requested(self, job_id: str, fence: int) -> bool:
        with contextlib.closing(_connect(db_path(self.state_dir))) as conn:
            row = conn.execute("SELECT cancel_requested, worker_id, lease_fence FROM local_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row or row["worker_id"] != self.worker_id or int(row["lease_fence"] or 0) != fence:
            return False
        return bool(row["cancel_requested"])

    def _finalize(self, claim: ClaimedJob, result: Dict[str, Any], *, adopted: bool = False) -> bool:
        now = _now()
        status = "COMPLETED" if result.get("ok") else "FAILED"
        if result.get("error_code") == "RECOVERY_REQUIRED":
            # An ambiguous process identity is neither a chemistry failure nor
            # permission to rerun the calculation.  Keep it explicitly
            # recoverable until an operator or a later reconciliation can
            # establish what happened.
            status = "RECOVERY_REQUIRED"
        if result.get("ok") and result.get("return_code_unknown"):
            status = "COMPLETED_WITH_WARNINGS"
        path = db_path(self.state_dir)
        with contextlib.closing(_connect(path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM local_jobs WHERE job_id=?", (claim.job["job_id"],)).fetchone()
            if not row or row["worker_id"] != self.worker_id or int(row["lease_fence"] or 0) != claim.fence:
                conn.rollback()
                return False
            cancel_at = row["cancel_requested_at"]
            finished_at = result.get("process_finished_at") or now
            if cancel_at is not None and float(cancel_at) <= float(finished_at):
                status = "CANCELLED"
                result = dict(result)
                result["error_code"] = "CANCELLED"
                result["error"] = "Cancelled by user before process completion."
            old = row["status"]
            changed = conn.execute(
                "UPDATE local_jobs SET status=?, finished_at=?, updated_at=?, lease_expires_at=NULL, "
                "return_code=?, result_json=?, error_code=?, error_message=?, revision=revision+1 WHERE job_id=? "
                "AND worker_id=? AND lease_fence=? AND status NOT IN ('COMPLETED','COMPLETED_WITH_WARNINGS','FAILED','CANCELLED')",
                (status, now, now, result.get("exit_code"), _json(result), result.get("error_code"), result.get("error"),
                 claim.job["job_id"], self.worker_id, claim.fence),
            )
            if changed.rowcount != 1:
                conn.rollback()
                return False
            conn.execute(
                "UPDATE local_job_attempts SET status=?, finished_at=?, return_code=?, result_json=?, error_code=?, error_message=? "
                "WHERE attempt_id=? AND lease_fence=?",
                (status, now, result.get("exit_code"), _json(result), result.get("error_code"), result.get("error"), claim.job["attempt_id"], claim.fence),
            )
            _event(conn, claim.job["job_id"], "FINALIZED", attempt_id=claim.job["attempt_id"], worker_id=self.worker_id,
                   old_state=old, new_state=status, detail={"adopted": adopted, "return_code": result.get("exit_code")})
            conn.commit()
        return True

    def _monitor_adopted(self, claim: ClaimedJob) -> Dict[str, Any]:
        job = claim.job
        info = {
            "pid": job.get("pid"),
            "creation_time": _creation_value(job.get("process_start_time")),
            "executable": (job.get("metadata") or {}).get("orca_executable"),
            "working_directory": job.get("workspace"),
            "input_path": job.get("input_path"),
            "output_path": job.get("output_path"),
            "canonical_output_path": job.get("canonical_output_path"),
        }
        while not self.stop_event.is_set():
            identity = local_orca_service.verify_process_identity({
                "pid": job.get("pid"),
                "creation_time": _creation_value(job.get("process_start_time")),
                "executable": (job.get("metadata") or {}).get("orca_executable"),
                "state": "RUNNING",
                "start_time": job.get("started_at") or _now(),
            })
            self._heartbeat(claim, info)
            if identity == "MATCH":
                if self._cancel_requested(job["job_id"], claim.fence):
                    local_orca_service.cancel_local_orca_job(job["job_id"], job["attempt_id"], state_dir=self.state_dir)
                self.stop_event.wait(HEARTBEAT_SECONDS)
                continue
            if identity == "UNKNOWN":
                return {"ok": False, "error_code": "RECOVERY_REQUIRED", "error": "Process identity could not be verified after worker restart.", "exit_code": None, "return_code_unknown": True}
            # NOT_RUNNING: the old worker is gone, so validate its durable output.
            recovered_output, artifacts = self._recover_artifacts(job)
            try:
                recovered_finished_at = os.path.getmtime(recovered_output)
            except OSError:
                recovered_finished_at = _now()
            return local_orca_service.validate_local_orca_output(
                recovered_output, stage_kind=job.get("stage_kind"), return_code=None,
                input_file=job.get("input_path") or "", duration_seconds=(_now() - float(job.get("started_at") or _now())),
                artifacts=artifacts, process_finished_at=recovered_finished_at,
            )
        return {"detached": True, "error_code": "WORKER_STOPPED",
                "error": "Worker stopped while the verified ORCA process remains independently active.",
                "exit_code": None}

    def _recover_artifacts(self, job: Dict[str, Any]) -> tuple[str, List[str]]:
        """Atomically copy a finished scratch workspace into its canonical job directory."""
        actual_output = str(job.get("output_path") or "")
        canonical_output = str(job.get("canonical_output_path") or actual_output)
        workspace = str(job.get("workspace") or (os.path.dirname(actual_output) if actual_output else ""))
        if not actual_output or not os.path.isfile(actual_output):
            return canonical_output, []
        canonical_dir = os.path.dirname(canonical_output)
        os.makedirs(canonical_dir, exist_ok=True)
        artifacts: List[str] = []
        candidates: List[str] = []
        if workspace and os.path.isdir(workspace):
            candidates = [os.path.join(workspace, name) for name in os.listdir(workspace)]
        if actual_output not in candidates:
            candidates.append(actual_output)
        for source in candidates:
            if not os.path.isfile(source) or os.path.islink(source):
                continue
            name = os.path.basename(source)
            destination = canonical_output if os.path.abspath(source) == os.path.abspath(actual_output) else os.path.join(canonical_dir, name)
            if os.path.abspath(source) != os.path.abspath(destination):
                tmp = destination + f".recovery-{self.worker_id.rsplit(':', 1)[-1]}.tmp"
                try:
                    shutil.copy2(source, tmp)
                    os.replace(tmp, destination)
                finally:
                    try:
                        if os.path.exists(tmp):
                            os.unlink(tmp)
                    except OSError:
                        pass
            artifacts.append(os.path.basename(destination))
        return canonical_output, sorted(set(artifacts))

    def _run_claimed(self, claim: ClaimedJob) -> None:
        job = claim.job
        metadata = dict(job.get("metadata") or {})
        metadata.update({"worker_id": self.worker_id, "worker_fence": claim.fence, "workflow_id": job.get("workflow_id"), "step_id": job.get("step_id")})
        lost_fence = False
        finalized = False

        def heartbeat(info: Dict[str, Any]) -> None:
            nonlocal lost_fence
            if lost_fence:
                return
            try:
                self._heartbeat(claim, info)
            except WorkerFenced:
                # A stale worker must stop writing, but it must not kill or
                # clear the shared process registry.  The current worker (or
                # the reconciler) owns finalization now.
                lost_fence = True

        try:
            if job.get("pid") and job.get("output_path"):
                result = self._monitor_adopted(claim)
            else:
                result = local_orca_service.execute_local_orca_job(
                    job_id=job["job_id"], input_text=job["input_text"], attempt_id=job["attempt_id"],
                    stage_kind=job.get("stage_kind"), state_dir=self.state_dir, metadata=metadata,
                    heartbeat_callback=heartbeat,
                    cancel_requested_callback=lambda: self._cancel_requested(job["job_id"], claim.fence),
                    detach_requested_callback=lambda: bool(self.stop_event.is_set() or lost_fence),
                )
            if result.get("detached"):
                log.info("Worker %s detached from still-running local job %s", self.worker_id, job["job_id"])
                return
            if lost_fence:
                return
            if result.get("error_code") == "LOCAL_CONCURRENCY_LIMIT_EXCEEDED":
                log.info("Job %s hit local concurrency limit; returning to QUEUED with backoff", job["job_id"])
                with contextlib.closing(_connect(db_path(self.state_dir))) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "UPDATE local_jobs SET status='QUEUED', worker_id=NULL, lease_fence=0, lease_expires_at=NULL, "
                        "next_attempt_at=?, error_code=NULL, error_message=NULL, updated_at=?, revision=revision+1 WHERE job_id=? AND lease_fence=?",
                        (_now() + 2.0, _now(), job["job_id"], claim.fence),
                    )
                    _event(conn, job["job_id"], "REQUEUED_CONCURRENCY_LIMIT", attempt_id=job["attempt_id"],
                           old_state="RUNNING", new_state="QUEUED")
                    conn.commit()
                return
            finalized = self._finalize(claim, result, adopted=bool(job.get("pid")))
            if (finalized and result.get("error_code") != "RECOVERY_REQUIRED"
                    and job.get("workflow_id") and job.get("step_id")):
                    self._project_workflow_result(job, result)
        except WorkerFenced:
            log.warning("Worker %s lost fence for local job %s", self.worker_id, job["job_id"])
        except Exception as exc:
            log.exception("Local worker failed while handling %s", job["job_id"])
            if not lost_fence:
                self._finalize(claim, {"ok": False, "error_code": "WORKER_ERROR", "error": str(exc), "exit_code": None})

    def _project_workflow_result(self, job: Dict[str, Any], result: Dict[str, Any]) -> None:
        """Update the durable reaction read model after a fenced finalization.

        This is intentionally a separate projection step because SQLite's
        local-job ledger and the legacy reaction JSON are different stores.
        A failed projection is recorded and retried by reconciliation; it must
        never cause the already-finished ORCA attempt to run again.
        """
        try:
            from services.reaction_workflow_service import apply_local_worker_result
            projection = apply_local_worker_result(job, result, self.state_dir)
            if not projection.get("ok"):
                raise RuntimeError(projection.get("error") or "workflow projection failed")
            with contextlib.closing(_connect(db_path(self.state_dir))) as conn:
                conn.execute("UPDATE local_jobs SET projected_at=? WHERE job_id=?", (_now(), job.get("job_id")))
                conn.commit()
        except Exception as exc:
            log.exception("Local job %s finished but workflow projection failed", job.get("job_id"))
            with contextlib.closing(_connect(db_path(self.state_dir))) as conn:
                conn.execute("BEGIN IMMEDIATE")
                _event(conn, job["job_id"], "WORKFLOW_PROJECTION_FAILED", attempt_id=job.get("attempt_id"),
                       worker_id=self.worker_id, detail={"error": str(exc)[:400]})
                conn.commit()

    def reconcile_jobs(self) -> Dict[str, int]:
        """Adopt verified live processes and classify ambiguous processes safely."""
        init_db(self.state_dir)
        adopted = dead = required = 0
        path = db_path(self.state_dir)
        with contextlib.closing(_connect(path)) as conn:
            rows = conn.execute(
                "SELECT * FROM local_jobs WHERE status IN "
                "('STARTING','RUNNING','VERIFYING','CANCEL_REQUESTED','RECOVERY_REQUIRED')"
            ).fetchall()
        for raw in rows:
            job = _safe_row(raw)
            if not job:
                continue
            # An unexpired lease is authoritative.  A second worker waits for
            # expiry instead of stealing a live calculation/finalization.
            lease_expires = float(job.get("lease_expires_at") or 0.0)
            if job.get("worker_id") != self.worker_id and lease_expires > _now():
                continue
            identity = local_orca_service.verify_process_identity({
                "pid": job.get("pid"), "creation_time": _creation_value(job.get("process_start_time")),
                "executable": (job.get("metadata") or {}).get("orca_executable"), "state": job.get("status"),
                "start_time": job.get("started_at") or _now(),
            })
            if identity == "MATCH":
                if self._adopt(job):
                    adopted += 1
            elif identity == "NOT_RUNNING":
                if job.get("pid") and job.get("output_path"):
                    self._mark_for_verification(job)
                    dead += 1
                else:
                    self._mark_failed(job, "STARTUP_FAILED", "Process failed to start or worker crashed during startup.")
                    dead += 1
            else:
                self._mark_recovery_required(job, identity)
                required += 1
        # A worker may crash after the local attempt was durably finalized but
        # before the compatibility reaction view was projected. Replaying the
        # projection is idempotent and never launches ORCA again.
        with contextlib.closing(_connect(path)) as conn:
            terminal_rows = conn.execute(
                "SELECT * FROM local_jobs WHERE status IN ('COMPLETED','COMPLETED_WITH_WARNINGS','FAILED','CANCELLED') "
                "AND workflow_id IS NOT NULL AND step_id IS NOT NULL AND projected_at IS NULL"
            ).fetchall()
        for raw in terminal_rows:
            job = _safe_row(raw)
            if job:
                self._project_workflow_result(job, job.get("result") or {})
        return {"adopted": adopted, "dead": dead, "recovery_required": required}

    def _adopt(self, job: Dict[str, Any]) -> bool:
        now = _now()
        with contextlib.closing(_connect(db_path(self.state_dir))) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, lease_fence, attempt_id, worker_id, lease_expires_at "
                "FROM local_jobs WHERE job_id=?", (job["job_id"],)
            ).fetchone()
            if not row or row["status"] in TERMINAL_STATES:
                conn.rollback()
                return False
            if row["worker_id"] != self.worker_id and float(row["lease_expires_at"] or 0.0) > now:
                conn.rollback()
                return False
            if self._active is not None and self._active.job.get("job_id") != job.get("job_id"):
                conn.rollback()
                return False
            fence = int(row["lease_fence"] or 0) + 1
            conn.execute("UPDATE local_jobs SET worker_id=?, lease_fence=?, lease_expires_at=?, heartbeat_at=?, updated_at=?, status='RUNNING', revision=revision+1 WHERE job_id=? AND status IN ('STARTING','RUNNING','VERIFYING','CANCEL_REQUESTED')",
                         (self.worker_id, fence, now + DEFAULT_LEASE_SECONDS, now, now, job["job_id"]))
            conn.execute("UPDATE local_job_attempts SET worker_id=?, lease_fence=?, status='RUNNING' WHERE attempt_id=?",
                         (self.worker_id, fence, row["attempt_id"]))
            _event(conn, job["job_id"], "ADOPTED_LIVE_PROCESS", attempt_id=row["attempt_id"], worker_id=self.worker_id,
                   old_state=row["status"], new_state="RUNNING", detail={"fence": fence})
            conn.commit()
        job["worker_id"] = self.worker_id
        job["lease_fence"] = fence
        job["status"] = "RUNNING"
        self._active = ClaimedJob(job, fence, self.worker_id)
        return True

    def _mark_for_verification(self, job: Dict[str, Any]) -> None:
        now = _now()
        with contextlib.closing(_connect(db_path(self.state_dir))) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status, lease_fence, attempt_id FROM local_jobs WHERE job_id=?", (job["job_id"],)).fetchone()
            if not row or row["status"] in TERMINAL_STATES:
                conn.rollback()
                return
            fence = int(row["lease_fence"] or 0) + 1
            conn.execute("UPDATE local_jobs SET status='VERIFYING', worker_id=?, lease_fence=?, lease_expires_at=?, heartbeat_at=?, updated_at=?, revision=revision+1 WHERE job_id=? AND status IN ('STARTING','RUNNING','VERIFYING','CANCEL_REQUESTED')",
                         (self.worker_id, fence, now + DEFAULT_LEASE_SECONDS, now, now, job["job_id"]))
            conn.execute("UPDATE local_job_attempts SET worker_id=?, lease_fence=?, status='VERIFYING' WHERE attempt_id=?",
                         (self.worker_id, fence, row["attempt_id"]))
            _event(conn, job["job_id"], "PROCESS_EXITED_RECONCILE", attempt_id=row["attempt_id"], worker_id=self.worker_id,
                   old_state=row["status"], new_state="VERIFYING")
            conn.commit()
        job["worker_id"] = self.worker_id
        job["lease_fence"] = fence
        job["status"] = "VERIFYING"
        self._active = ClaimedJob(job, fence, self.worker_id)

    def _mark_failed(self, job: Dict[str, Any], error_code: str, error_message: str) -> None:
        now = _now()
        with contextlib.closing(_connect(db_path(self.state_dir))) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status, attempt_id FROM local_jobs WHERE job_id=?", (job["job_id"],)).fetchone()
            if row and row["status"] not in TERMINAL_STATES:
                conn.execute(
                    "UPDATE local_jobs SET status='FAILED', finished_at=?, updated_at=?, error_code=?, error_message=?, revision=revision+1 "
                    "WHERE job_id=? AND status NOT IN ('COMPLETED','COMPLETED_WITH_WARNINGS','FAILED','CANCELLED')",
                    (now, now, error_code, error_message, job["job_id"]),
                )
                conn.execute(
                    "UPDATE local_job_attempts SET status='FAILED', finished_at=?, error_code=?, error_message=? "
                    "WHERE attempt_id=?",
                    (now, error_code, error_message, row["attempt_id"]),
                )
                _event(conn, job["job_id"], "FAILED", attempt_id=row["attempt_id"], old_state=row["status"], new_state="FAILED",
                       detail={"error_code": error_code, "error": error_message})
            conn.commit()

    def _mark_recovery_required(self, job: Dict[str, Any], identity: str) -> None:
        with contextlib.closing(_connect(db_path(self.state_dir))) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status, attempt_id FROM local_jobs WHERE job_id=?", (job["job_id"],)).fetchone()
            if row and row["status"] not in TERMINAL_STATES:
                err_code = f"PROCESS_IDENTITY_{identity}"
                conn.execute("UPDATE local_jobs SET status='RECOVERY_REQUIRED', error_code=?, error_message=?, updated_at=?, revision=revision+1 WHERE job_id=? AND status NOT IN ('COMPLETED','COMPLETED_WITH_WARNINGS','FAILED','CANCELLED')",
                             (err_code, f"Persisted local process identity is {identity}; automatic restart was refused.", _now(), job["job_id"]))
                _event(conn, job["job_id"], "RECOVERY_REQUIRED", attempt_id=row["attempt_id"], old_state=row["status"], new_state="RECOVERY_REQUIRED", detail={"identity": identity})
            conn.commit()

    def run_once(self) -> bool:
        self._touch_worker()
        if _now() - self._last_reconcile_at >= RECONCILE_INTERVAL_SECONDS:
            self.reconcile_jobs()
            self._last_reconcile_at = _now()
        if self._active:
            claim = self._active
            self._active = None
            self._run_claimed(claim)
            return True
        next_claim = self._claim_next()
        if next_claim is None:
            return False
        self._run_claimed(next_claim)
        return True

    def run_forever(self) -> None:
        self.reconcile_jobs()
        self._last_reconcile_at = _now()
        while not self.stop_event.is_set():
            try:
                did_work = self.run_once()
                if not did_work:
                    self.stop_event.wait(self.poll_seconds)
            except KeyboardInterrupt:
                break
            except Exception:
                log.exception("Local ORCA worker loop error")
                self.stop_event.wait(self.poll_seconds)
        self.close()


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Durable SQLite-backed server-local ORCA worker")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    worker = LocalOrcaWorker(args.state_dir, poll_seconds=args.poll_seconds)
    if args.once:
        worker.reconcile_jobs()
        worker.run_once()
        worker.close()
    else:
        worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
