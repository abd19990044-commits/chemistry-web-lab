# -*- coding: utf-8 -*-
"""
Persistent state: SQLite in WAL mode.

Positioning
-----------
This store is a **cache with transactional guarantees**, not the source of
truth. The source of truth is the Kaggle-side ledger (`ledger.py`), because
that is the only storage in the system that outlives both the web application
and any individual kernel. Losing this database must therefore be a
performance event, never a correctness event -- `ledger.rebuild_from_kaggle()`
reconstructs every field in it.

That positioning is forced by the deployment: a free Hugging Face Space has an
ephemeral filesystem, wiped on every restart and every rebuild. A design that
made SQLite authoritative would lose every in-flight job on each redeploy, and
the jobs would keep running on Kaggle with nothing able to adopt them.

Concurrency
-----------
Gunicorn runs 2 workers x 4 threads against one file, so eight threads across
two processes contend here. Three mechanisms handle that:

  * **WAL** so readers never block writers.
  * **`BEGIN IMMEDIATE`** for every read-modify-write. SQLite's default
    deferred transaction upgrades to a write lock only at the first write, and
    two upgraders deadlock into `SQLITE_BUSY`; taking the write lock at the
    start makes the transaction serialise cleanly instead.
  * **Fencing tokens** on leases, so a stalled worker whose lease expired
    cannot land a late write on top of its successor's work.

Every connection is thread-local: a `sqlite3.Connection` is not safe to share
across threads, and `check_same_thread=False` merely turns a loud error into a
silent race.
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .config import CONFIG, StoreConfig
from .errors import ConcurrencyError, LeaseLostError, NotFoundError
from .hashing import content_id, stable_json
from .logging_ext import get_logger, log_event
from .models import CheckpointManifest, Event, JobManifest, Lease, now
from .states import JobState

log = get_logger("orca.store")

#: No PRAGMAs here on purpose. Connection pragmas belong in `_connect`, and
#: `journal_mode` in particular must go through `_enable_wal` -- issuing it from
#: `executescript` gives it no retry path and reintroduces the boot race this
#: module now guards against.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    owner         TEXT NOT NULL,
    state         TEXT NOT NULL,
    epoch         INTEGER NOT NULL DEFAULT 0,
    current_slug  TEXT NOT NULL DEFAULT '',
    updated_at    REAL NOT NULL,
    created_at    REAL NOT NULL,
    is_terminal   INTEGER NOT NULL DEFAULT 0,
    manifest_json TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    -- Optimistic-concurrency counter. Every write asserts the version it read.
    version       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_jobs_owner_state ON jobs(owner, state);
CREATE INDEX IF NOT EXISTS idx_jobs_active ON jobs(is_terminal, updated_at);

CREATE TABLE IF NOT EXISTS events (
    event_id       TEXT PRIMARY KEY,
    job_id         TEXT NOT NULL,
    epoch          INTEGER NOT NULL,
    at             REAL NOT NULL,
    trigger        TEXT NOT NULL,
    from_state     TEXT NOT NULL,
    to_state       TEXT NOT NULL,
    actor          TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    detail_json    TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, at);

CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id  TEXT PRIMARY KEY,
    job_id         TEXT NOT NULL,
    epoch          INTEGER NOT NULL,
    status         TEXT NOT NULL,
    bundle_digest  TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL,
    manifest_json  TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ckpt_job ON checkpoints(job_id, epoch, status);
-- One verified checkpoint per (job, epoch): a second one for the same window
-- means two producers raced, and the unique index turns that into a loud
-- constraint violation instead of a silent fork of the chain.
CREATE UNIQUE INDEX IF NOT EXISTS idx_ckpt_unique_digest
    ON checkpoints(job_id, epoch, bundle_digest);

CREATE TABLE IF NOT EXISTS leases (
    resource    TEXT PRIMARY KEY,
    holder      TEXT NOT NULL,
    fence       INTEGER NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS fence_counter (
    resource TEXT PRIMARY KEY,
    value    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency (
    key           TEXT PRIMARY KEY,
    request_hash  TEXT NOT NULL,
    status        TEXT NOT NULL,          -- in_progress | done | failed
    response_json TEXT,
    created_at    REAL NOT NULL,
    completed_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_idem_created ON idempotency(created_at);
"""


def _is_lock_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


class JobStore:
    def __init__(self, config: StoreConfig | None = None) -> None:
        self.config = config or CONFIG.store
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialised = False
        self._journal_mode = "unknown"
        self._ensure_schema()

    # -- connection management -------------------------------------------
    def _enable_wal(self, conn: sqlite3.Connection) -> str:
        """Switches the database into WAL mode, tolerating a concurrent switch.

        This is the fix for a real production failure. Two gunicorn workers boot
        in the same second against a brand-new database file, both issue
        `PRAGMA journal_mode=WAL`, and one dies with `database is locked` --
        which previously took the whole orchestrator down in that worker.

        The cause is specific and not obvious: changing the journal mode needs a
        brief EXCLUSIVE lock, and for that operation SQLite returns SQLITE_BUSY
        **immediately without consulting the busy-timeout handler**. Setting
        `busy_timeout` therefore does nothing here, which is exactly why the
        8-second timeout already configured on the connection did not help.

        Two properties make the retry safe. WAL is a *persistent property of the
        file*, not of the connection, so if the other worker wins the race we
        simply read back the mode it set and carry on. And if the switch never
        succeeds, the database still works correctly in rollback-journal mode --
        readers no longer run concurrently with writers, so throughput drops,
        but nothing is lost or corrupted. Degrading is always better than
        refusing to start.
        """
        for attempt in range(1, 9):
            try:
                row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
                mode = (row[0] if row else "").lower()
                if mode == "wal":
                    return mode
            except sqlite3.OperationalError as exc:
                if not _is_lock_error(exc):
                    raise
            # Someone else may have completed the switch in the meantime.
            try:
                row = conn.execute("PRAGMA journal_mode").fetchone()
                if row and str(row[0]).lower() == "wal":
                    return "wal"
            except sqlite3.OperationalError:
                pass
            # Jittered backoff: without jitter, workers that collided once
            # collide again on every retry, in lockstep.
            time.sleep(0.05 * attempt + random.uniform(0, 0.05))

        try:
            row = conn.execute("PRAGMA journal_mode").fetchone()
            mode = str(row[0]).lower() if row else "unknown"
        except sqlite3.OperationalError:
            mode = "unknown"
        if mode != "wal":
            log.warning(
                "could not switch the database to WAL; continuing in %s journal mode. "
                "Correctness is unaffected -- reads and writes will serialise instead of "
                "running concurrently.", mode,
                extra={"event": "wal_unavailable", "journal_mode": mode},
            )
        return mode

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.config.db_path,
            timeout=self.config.busy_timeout_ms / 1000.0,
            isolation_level=None,          # explicit transactions only
        )
        conn.row_factory = sqlite3.Row
        # busy_timeout first: every statement after this one gets to wait rather
        # than failing instantly on contention.
        conn.execute(f"PRAGMA busy_timeout={int(self.config.busy_timeout_ms)}")
        self._journal_mode = self._enable_wal(conn)
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def _ensure_schema(self) -> None:
        """Creates the schema, tolerating concurrent creation by another worker.

        `CREATE TABLE IF NOT EXISTS` is idempotent by construction, so several
        workers running this simultaneously converge on the same result. What
        they can still collide on is the write lock, and a lost collision at
        boot previously disabled the orchestrator in that worker for the entire
        life of the process. Retrying costs milliseconds; not retrying cost half
        the fleet."""
        with self._init_lock:
            if self._initialised:
                return
            os.makedirs(self.config.state_dir, exist_ok=True)

            last: Exception | None = None
            for attempt in range(1, 7):
                try:
                    self.conn.executescript(_SCHEMA)
                    self.conn.execute(
                        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', ?)",
                        (str(CONFIG.manifest_version),),
                    )
                    last = None
                    break
                except sqlite3.OperationalError as exc:
                    if not _is_lock_error(exc):
                        raise
                    last = exc
                    time.sleep(0.1 * attempt + random.uniform(0, 0.1))
            if last is not None:
                raise last

            self._initialised = True
            from .config import STATE_DIR_DIAGNOSTIC

            log_event(log, "store_ready", "state store initialised",
                      db_path=self.config.db_path, journal_mode=self._journal_mode,
                      pid=os.getpid(),
                      state_dir_shared=STATE_DIR_DIAGNOSTIC.get("shared", True),
                      state_dir_source=STATE_DIR_DIAGNOSTIC.get("source"))

            # A per-process state directory silently disables every cross-worker
            # guarantee this store provides. It must never pass unnoticed again.
            if STATE_DIR_DIAGNOSTIC.get("shared") is False:
                log.error(
                    "state directory is NOT shared between workers",
                    extra={"event": "state_dir_not_shared",
                           "pid": os.getpid(),
                           **{k: v for k, v in STATE_DIR_DIAGNOSTIC.items()
                              if k != "shared"}},
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """`BEGIN IMMEDIATE` -- takes the write lock up front.

        With the default deferred transaction, two threads that both read then
        write deadlock: each holds a read lock and each needs the other to drop
        it before it can upgrade. SQLite resolves that by failing one with
        SQLITE_BUSY, and under contention that failure rate is not small. Taking
        the write lock at BEGIN turns the deadlock into a queue."""
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- jobs -------------------------------------------------------------
    def _row_to_manifest(self, row: sqlite3.Row) -> JobManifest:
        manifest = JobManifest.from_dict(json.loads(row["manifest_json"]))
        manifest._extra["_version"] = row["version"]
        return manifest

    def get_job(self, job_id: str) -> JobManifest | None:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return self._row_to_manifest(row) if row else None

    def require_job(self, job_id: str) -> JobManifest:
        job = self.get_job(job_id)
        if job is None:
            raise NotFoundError("no such job in the local cache", job_id=job_id)
        return job

    def put_job(self, manifest: JobManifest, *, expected_version: int | None = None,
                fence: int | None = None) -> JobManifest:
        """Upsert with optimistic concurrency control.

        `expected_version` is the version the caller read. If another writer
        has since committed, the update matches zero rows and this raises --
        the caller must re-read and re-decide rather than clobbering a decision
        it never saw. That is the mechanism that stops two workers from
        silently overwriting each other's state transitions.
        """
        manifest.touch()
        if fence is not None:
            self._assert_fence(f"job:{manifest.job_id}", fence)

        payload = manifest.to_dict()
        payload.pop("_version", None)
        manifest_json = stable_json(payload)
        manifest_hash = content_id(payload)

        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT version FROM jobs WHERE job_id = ?", (manifest.job_id,)
            ).fetchone()

            if existing is None:
                conn.execute(
                    "INSERT INTO jobs(job_id, owner, state, epoch, current_slug, updated_at,"
                    " created_at, is_terminal, manifest_json, manifest_hash, version)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,1)",
                    (manifest.job_id, manifest.owner, manifest.state.value, manifest.epoch,
                     manifest.current_slug, manifest.updated_at, manifest.created_at,
                     1 if manifest.is_terminal else 0, manifest_json, manifest_hash),
                )
                new_version = 1
            else:
                current_version = int(existing["version"])
                if expected_version is not None and current_version != expected_version:
                    raise ConcurrencyError(
                        "the job was modified by another writer since it was read",
                        job_id=manifest.job_id,
                        expected_version=expected_version,
                        actual_version=current_version,
                    )
                new_version = current_version + 1
                cur = conn.execute(
                    "UPDATE jobs SET owner=?, state=?, epoch=?, current_slug=?, updated_at=?,"
                    " is_terminal=?, manifest_json=?, manifest_hash=?, version=?"
                    " WHERE job_id=? AND version=?",
                    (manifest.owner, manifest.state.value, manifest.epoch,
                     manifest.current_slug, manifest.updated_at,
                     1 if manifest.is_terminal else 0, manifest_json, manifest_hash,
                     new_version, manifest.job_id, current_version),
                )
                if cur.rowcount != 1:
                    raise ConcurrencyError(
                        "lost an optimistic-concurrency race while writing the job",
                        job_id=manifest.job_id, expected_version=current_version,
                    )

        manifest._extra["_version"] = new_version
        return manifest

    def list_jobs(self, owner: str | None = None, *, include_terminal: bool = True,
                  limit: int = 200) -> list[JobManifest]:
        sql = "SELECT * FROM jobs"
        clauses, params = [], []
        if owner:
            clauses.append("owner = ?")
            params.append(owner.lower())
        if not include_terminal:
            clauses.append("is_terminal = 0")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(int(limit))
        return [self._row_to_manifest(r) for r in self.conn.execute(sql, params)]

    def list_active_jobs(self, *, older_than_seconds: float = 0.0,
                         limit: int = 500) -> list[JobManifest]:
        """Watchdog feed: active jobs whose last update is older than the given
        age. Filtering in SQL keeps a sweep O(stalled jobs) rather than
        O(all jobs ever submitted)."""
        cutoff = now() - max(0.0, older_than_seconds)
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE is_terminal = 0 AND updated_at <= ?"
            " ORDER BY updated_at ASC LIMIT ?",
            (cutoff, int(limit)),
        )
        return [self._row_to_manifest(r) for r in rows]

    def delete_job(self, job_id: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))

    # -- events -----------------------------------------------------------
    def append_event(self, event: Event) -> None:
        """Append-only. Events are never updated or deleted except by the
        cascade when a job is removed, so the ledger cannot be rewritten to
        make a failure look like something it was not."""
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO events(event_id, job_id, epoch, at, trigger,"
                " from_state, to_state, actor, correlation_id, detail_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (event.event_id, event.job_id, event.epoch, event.at, event.trigger,
                 event.from_state, event.to_state, event.actor, event.correlation_id,
                 stable_json(event.detail)),
            )

    def list_events(self, job_id: str, limit: int = 500) -> list[Event]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE job_id = ? ORDER BY at ASC LIMIT ?",
            (job_id, int(limit)),
        )
        return [
            Event(
                event_id=r["event_id"], job_id=r["job_id"], epoch=r["epoch"], at=r["at"],
                trigger=r["trigger"], from_state=r["from_state"], to_state=r["to_state"],
                actor=r["actor"], correlation_id=r["correlation_id"],
                detail=json.loads(r["detail_json"] or "{}"),
            )
            for r in rows
        ]

    # -- checkpoints ------------------------------------------------------
    def put_checkpoint(self, checkpoint: CheckpointManifest) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO checkpoints(checkpoint_id, job_id, epoch, status,"
                " bundle_digest, created_at, manifest_json) VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(checkpoint_id) DO UPDATE SET"
                " status=excluded.status, bundle_digest=excluded.bundle_digest,"
                " manifest_json=excluded.manifest_json",
                (checkpoint.checkpoint_id, checkpoint.job_id, checkpoint.epoch,
                 checkpoint.status, checkpoint.bundle_digest, checkpoint.created_at,
                 stable_json(checkpoint.to_dict())),
            )

    def get_checkpoint(self, checkpoint_id: str) -> CheckpointManifest | None:
        row = self.conn.execute(
            "SELECT manifest_json FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
        ).fetchone()
        return CheckpointManifest.from_dict(json.loads(row["manifest_json"])) if row else None

    def list_checkpoints(self, job_id: str, *, status: str | None = None,
                         limit: int = 50) -> list[CheckpointManifest]:
        sql = "SELECT manifest_json FROM checkpoints WHERE job_id = ?"
        params: list[Any] = [job_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY epoch DESC, created_at DESC LIMIT ?"
        params.append(int(limit))
        return [CheckpointManifest.from_dict(json.loads(r["manifest_json"]))
                for r in self.conn.execute(sql, params)]

    def latest_verified_checkpoint(self, job_id: str,
                                   before_epoch: int | None = None) -> CheckpointManifest | None:
        """The rollback target.

        `before_epoch` is what makes rollback actually go *back*: when epoch N's
        checkpoint is rejected, the search must exclude epoch N, or the system
        would keep selecting the same poison and loop."""
        sql = ("SELECT manifest_json FROM checkpoints WHERE job_id = ?"
               " AND status IN ('verified','committed')")
        params: list[Any] = [job_id]
        if before_epoch is not None:
            sql += " AND epoch < ?"
            params.append(int(before_epoch))
        sql += " ORDER BY epoch DESC, created_at DESC LIMIT 1"
        row = self.conn.execute(sql, params).fetchone()
        return CheckpointManifest.from_dict(json.loads(row["manifest_json"])) if row else None

    # -- leases -----------------------------------------------------------
    def _next_fence(self, conn: sqlite3.Connection, resource: str) -> int:
        conn.execute(
            "INSERT INTO fence_counter(resource, value) VALUES(?, 0)"
            " ON CONFLICT(resource) DO NOTHING", (resource,)
        )
        conn.execute(
            "UPDATE fence_counter SET value = value + 1 WHERE resource = ?", (resource,)
        )
        row = conn.execute(
            "SELECT value FROM fence_counter WHERE resource = ?", (resource,)
        ).fetchone()
        return int(row["value"])

    def acquire_lease(self, resource: str, holder: str,
                      ttl_seconds: float | None = None) -> Lease | None:
        """Returns a Lease, or None if someone else holds a live one.

        Never blocks. A caller that cannot get the lease should simply do
        nothing: the holder is already performing the work, and because every
        action here is idempotent there is no partial state to clean up."""
        ttl = ttl_seconds if ttl_seconds is not None else self.config.lease_ttl_seconds
        ts = now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM leases WHERE resource = ?", (resource,)
            ).fetchone()
            if row is not None and float(row["expires_at"]) > ts:
                if row["holder"] == holder:
                    # Re-entrant renewal by the same holder. Idempotent.
                    expires = ts + ttl
                    conn.execute("UPDATE leases SET expires_at = ? WHERE resource = ?",
                                 (expires, resource))
                    return Lease(resource, holder, int(row["fence"]),
                                 float(row["acquired_at"]), expires)
                return None

            fence = self._next_fence(conn, resource)
            expires = ts + ttl
            conn.execute(
                "INSERT INTO leases(resource, holder, fence, acquired_at, expires_at)"
                " VALUES(?,?,?,?,?) ON CONFLICT(resource) DO UPDATE SET"
                " holder=excluded.holder, fence=excluded.fence,"
                " acquired_at=excluded.acquired_at, expires_at=excluded.expires_at",
                (resource, holder, fence, ts, expires),
            )
            return Lease(resource, holder, fence, ts, expires)

    def renew_lease(self, lease: Lease, ttl_seconds: float | None = None) -> Lease:
        ttl = ttl_seconds if ttl_seconds is not None else self.config.lease_ttl_seconds
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE leases SET expires_at = ? WHERE resource = ? AND fence = ?",
                (now() + ttl, lease.resource, lease.fence),
            )
            if cur.rowcount != 1:
                raise LeaseLostError(
                    "the lease was taken over by another holder before renewal",
                    resource=lease.resource, fence=lease.fence,
                )
        lease.expires_at = now() + ttl
        return lease

    def release_lease(self, lease: Lease) -> None:
        """Fence-checked release, so a worker that already lost the lease
        cannot free the *new* holder's lease on its way out."""
        with self.transaction() as conn:
            conn.execute("DELETE FROM leases WHERE resource = ? AND fence = ?",
                         (lease.resource, lease.fence))

    def _assert_fence(self, resource: str, fence: int) -> None:
        row = self.conn.execute(
            "SELECT fence, expires_at FROM leases WHERE resource = ?", (resource,)
        ).fetchone()
        if row is None:
            return
        if int(row["fence"]) > int(fence):
            raise LeaseLostError(
                "a newer lease holder has taken over this resource; the write is rejected",
                resource=resource, our_fence=fence, current_fence=int(row["fence"]),
            )

    @contextmanager
    def lease(self, resource: str, holder: str,
              ttl_seconds: float | None = None) -> Iterator[Lease | None]:
        acquired = self.acquire_lease(resource, holder, ttl_seconds)
        try:
            yield acquired
        finally:
            if acquired is not None:
                try:
                    self.release_lease(acquired)
                except Exception:  # pragma: no cover - release must never mask the body
                    log.warning("failed to release lease", extra={"resource": resource})

    # -- idempotency ------------------------------------------------------
    def begin_idempotent(self, key: str, request_payload: Any) -> tuple[bool, Any]:
        """Claims an idempotency key.

        Returns `(is_replay, stored_response)`:
          * `(False, None)` -- first time; the caller performs the work and
            then calls `complete_idempotent`.
          * `(True, response)` -- already completed; return the stored response
            verbatim without redoing anything.
          * `(True, None)` -- an identical request is *in flight* in another
            worker. The caller must not start a second one; this is what stops
            a double-clicked submit or a browser refresh mid-POST from
            launching two notebooks.

        A different payload under the same key is a client bug and is rejected
        loudly, matching the semantics of Stripe's Idempotency-Key.
        """
        request_hash = content_id(request_payload)
        ts = now()
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM idempotency WHERE created_at < ?",
                (ts - self.config.idempotency_ttl_seconds,),
            )
            row = conn.execute(
                "SELECT * FROM idempotency WHERE key = ?", (key,)
            ).fetchone()
            if row is not None:
                if row["request_hash"] != request_hash:
                    raise ConcurrencyError(
                        "this idempotency key was already used with a different request body",
                        key=key,
                    )
                if row["status"] == "done":
                    return True, json.loads(row["response_json"] or "null")
                return True, None
            conn.execute(
                "INSERT INTO idempotency(key, request_hash, status, created_at)"
                " VALUES(?,?,'in_progress',?)",
                (key, request_hash, ts),
            )
        return False, None

    def complete_idempotent(self, key: str, response: Any, *, failed: bool = False) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE idempotency SET status = ?, response_json = ?, completed_at = ?"
                " WHERE key = ?",
                ("failed" if failed else "done", stable_json(response), now(), key),
            )

    def abandon_idempotent(self, key: str) -> None:
        """Releases an in-flight claim whose work never started, so a crash
        between claim and execution does not wedge the key for its whole TTL."""
        with self.transaction() as conn:
            conn.execute("DELETE FROM idempotency WHERE key = ? AND status = 'in_progress'",
                         (key,))

    # -- maintenance ------------------------------------------------------
    def vacuum(self) -> None:
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.execute("VACUUM")

    def stats(self) -> dict:
        def _count(table: str) -> int:
            return int(self.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"])

        return {
            "db_path": self.config.db_path,
            "jobs": _count("jobs"),
            "active_jobs": int(self.conn.execute(
                "SELECT COUNT(*) c FROM jobs WHERE is_terminal = 0").fetchone()["c"]),
            "events": _count("events"),
            "checkpoints": _count("checkpoints"),
            "live_leases": int(self.conn.execute(
                "SELECT COUNT(*) c FROM leases WHERE expires_at > ?", (now(),)).fetchone()["c"]),
        }


_default_store: JobStore | None = None
_default_lock = threading.Lock()


def get_store() -> JobStore:
    """Process-wide singleton. Each gunicorn worker gets its own instance and
    its own thread-local connections, all pointing at the same WAL file."""
    global _default_store
    if _default_store is None:
        with _default_lock:
            if _default_store is None:
                _default_store = JobStore()
    return _default_store
