"""Durable workflow/step ledger for reaction calculations.

The historical UI still reads ``reaction_workflows.json``.  This module is the
transactional ledger underneath it: every save mirrors a workflow and all its
steps in one SQLite transaction, while step attempts/events remain queryable
after a restart instead of being overwritten in a JSON object.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

from services.sqlite_migrations import backup_before_schema_upgrade

TERMINAL_STEPS = frozenset({"COMPLETE", "COMPLETED_WITH_WARNINGS", "FAILED", "CANCELLED"})


def _state_dir(state_dir: str) -> str:
    path = os.path.abspath(state_dir)
    os.makedirs(path, exist_ok=True)
    return path


def database_path(state_dir: str) -> str:
    return os.path.join(_state_dir(state_dir), "workflow_steps.sqlite3")


def _connect(state_dir: str) -> sqlite3.Connection:
    conn = sqlite3.connect(database_path(state_dir), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(value: Optional[str]) -> Any:
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}


def init_db(state_dir: str) -> None:
    path = database_path(state_dir)
    backup_before_schema_upgrade(
        path,
        component="workflow-ledger",
        target_version=1,
        required_schema={
            "workflow_schema": ("version",),
            "workflows": ("workflow_id", "revision", "metadata_json"),
            "workflow_steps": ("workflow_id", "step_id", "attempt_id", "version"),
            "workflow_attempts": ("attempt_id", "workflow_id", "step_id", "attempt_no"),
            "workflow_events": ("event_id", "workflow_id", "event"),
        },
    )
    with contextlib.closing(_connect(state_dir)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS workflow_schema(version INTEGER NOT NULL)")
        if conn.execute("SELECT COUNT(*) FROM workflow_schema").fetchone()[0] == 0:
            conn.execute("INSERT INTO workflow_schema(version) VALUES (1)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workflows (
                workflow_id TEXT PRIMARY KEY,
                owner_id TEXT,
                reaction_id TEXT,
                species_id TEXT,
                status TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_steps (
                workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id) ON DELETE CASCADE,
                step_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                step_type TEXT NOT NULL,
                status TEXT NOT NULL,
                input_artifacts_json TEXT NOT NULL DEFAULT '{}',
                output_artifacts_json TEXT NOT NULL DEFAULT '{}',
                retry_count INTEGER NOT NULL DEFAULT 0,
                failure_reason TEXT,
                started_at REAL,
                finished_at REAL,
                attempt_id TEXT,
                version INTEGER NOT NULL DEFAULT 0,
                warnings_json TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY(workflow_id, step_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_attempts (
                attempt_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                status TEXT NOT NULL,
                input_hash TEXT,
                output_hash TEXT,
                started_at REAL,
                finished_at REAL,
                error_message TEXT,
                result_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(workflow_id, step_id, attempt_no)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id TEXT NOT NULL,
                step_id TEXT,
                attempt_id TEXT,
                at REAL NOT NULL,
                event TEXT NOT NULL,
                old_state TEXT,
                new_state TEXT,
                detail_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_steps_status ON workflow_steps(status, step_index)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_events_workflow ON workflow_events(workflow_id, event_id)")

        # Ensure schema migrations for existing tables to prevent infinite backup loop
        existing_step_cols = {row[1] for row in conn.execute("PRAGMA table_info(workflow_steps)").fetchall()}
        if existing_step_cols:
            if "version" not in existing_step_cols:
                conn.execute("ALTER TABLE workflow_steps ADD COLUMN version INTEGER NOT NULL DEFAULT 0")
            if "attempt_id" not in existing_step_cols:
                conn.execute("ALTER TABLE workflow_steps ADD COLUMN attempt_id TEXT")
            if "warnings_json" not in existing_step_cols:
                conn.execute("ALTER TABLE workflow_steps ADD COLUMN warnings_json TEXT NOT NULL DEFAULT '[]'")

        existing_wf_cols = {row[1] for row in conn.execute("PRAGMA table_info(workflows)").fetchall()}
        if existing_wf_cols:
            if "revision" not in existing_wf_cols:
                conn.execute("ALTER TABLE workflows ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
            if "metadata_json" not in existing_wf_cols:
                conn.execute("ALTER TABLE workflows ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")

        conn.commit()


def _stage_artifacts(stage: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    inputs = {
        "input_hash": stage.get("input_hash"),
        "input_path": stage.get("input_path"),
        "input_text": stage.get("input_text"),
        "charge": stage.get("charge"),
        "multiplicity": stage.get("multiplicity"),
        "method": stage.get("method"),
        "basis_set": stage.get("basis_set"),
    }
    outputs = {
        "output_hash": stage.get("output_hash"),
        "output_path": stage.get("output_path"),
        "geometry_hash": stage.get("geometry_hash"),
        "geometry_xyz": (stage.get("parsed") or {}).get("geometry_xyz"),
        "gbw_path": stage.get("gbw_path"),
        "parsed": stage.get("parsed"),
        "terminated_normally": stage.get("terminated_normally"),
        "scientific_status": stage.get("scientific_status"),
    }
    return inputs, outputs


def sync_reaction(reaction: Dict[str, Any], state_dir: str) -> None:
    """Mirror a reaction into SQLite in one transaction.

    This function is intentionally idempotent.  It never deletes attempts or
    events; only the current workflow/step projection is replaced.  The JSON
    store remains a compatibility read model and can be rebuilt from this
    ledger in a future migration.
    """
    init_db(state_dir)
    now = time.time()
    workflow_rows: List[tuple[Any, ...]] = []
    step_rows: List[tuple[Any, ...]] = []
    for species in reaction.get("species", []):
        workflow_id = species.get("workflow_id")
        if not workflow_id:
            continue
        stages = sorted(species.get("stages", []), key=lambda item: item.get("order", 0))
        workflow_status = species.get("state") or "PENDING"
        workflow_rows.append((
            workflow_id, reaction.get("owner"), reaction.get("reaction_id"), species.get("species_id"),
            workflow_status, now, now,
            _json({
                "workflow_source": species.get("workflow_source"),
                "workflow_hash": species.get("workflow_hash"),
                # Keep a durable compatibility snapshot so the historical
                # JSON read model can be rebuilt after a crash between the
                # SQLite commit and its JSON atomic replace.
                "reaction_snapshot": reaction,
            }),
        ))
        for index, stage in enumerate(stages):
            inputs, outputs = _stage_artifacts(stage)
            attempt_no = int(stage.get("attempt_no") or (int(stage.get("retry_count") or 0) + 1))
            step_rows.append((
                workflow_id, stage.get("stage_id"), index, stage.get("kind") or "CUSTOM_ORCA",
                stage.get("state") or "PENDING", _json(inputs), _json(outputs),
                int(stage.get("retry_count") or 0), stage.get("error"), stage.get("started_at"),
                stage.get("finished_at"), stage.get("attempt_id"), int(stage.get("version") or 0),
                _json(stage.get("warnings") or ([stage["imaginary_warning"]] if stage.get("imaginary_warning") else [])),
                attempt_no,
            ))

    with contextlib.closing(_connect(state_dir)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for workflow_row in workflow_rows:
            conn.execute(
                """
                INSERT INTO workflows(workflow_id, owner_id, reaction_id, species_id, status, created_at, updated_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET owner_id=excluded.owner_id,
                    reaction_id=excluded.reaction_id, species_id=excluded.species_id,
                    status=excluded.status, revision=workflows.revision+1,
                    updated_at=excluded.updated_at, metadata_json=excluded.metadata_json
                """,
                workflow_row,
            )
        for step_row in step_rows:
            previous = conn.execute(
                "SELECT status FROM workflow_steps WHERE workflow_id=? AND step_id=?",
                (step_row[0], step_row[1]),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO workflow_steps(
                    workflow_id, step_id, step_index, step_type, status, input_artifacts_json,
                    output_artifacts_json, retry_count, failure_reason, started_at, finished_at,
                    attempt_id, version, warnings_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id, step_id) DO UPDATE SET step_index=excluded.step_index,
                    step_type=excluded.step_type, status=excluded.status,
                    input_artifacts_json=excluded.input_artifacts_json,
                    output_artifacts_json=excluded.output_artifacts_json,
                    retry_count=excluded.retry_count, failure_reason=excluded.failure_reason,
                    started_at=excluded.started_at, finished_at=excluded.finished_at,
                    attempt_id=excluded.attempt_id, version=workflow_steps.version+1,
                    warnings_json=excluded.warnings_json
                """,
                step_row[:14],
            )
            if step_row[11]:
                existing_att = conn.execute(
                    "SELECT attempt_id FROM workflow_attempts WHERE workflow_id=? AND step_id=? AND attempt_no=?",
                    (step_row[0], step_row[1], int(step_row[14] or 1)),
                ).fetchone()
                if existing_att:
                    conn.execute(
                        "UPDATE workflow_attempts SET status=?, started_at=?, finished_at=?, error_message=?, result_json=? "
                        "WHERE workflow_id=? AND step_id=? AND attempt_no=?",
                        (step_row[4], step_row[9], step_row[10], step_row[8], step_row[6],
                         step_row[0], step_row[1], int(step_row[14] or 1)),
                    )
                else:
                    conn.execute(
                        "INSERT INTO workflow_attempts(attempt_id, workflow_id, step_id, attempt_no, status, started_at, finished_at, error_message, result_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(attempt_id) DO UPDATE SET status=excluded.status, finished_at=excluded.finished_at, error_message=excluded.error_message",
                        (step_row[11], step_row[0], step_row[1], int(step_row[14] or 1),
                         step_row[4], step_row[9], step_row[10], step_row[8], step_row[6]),
                    )
            if previous is None or previous["status"] != step_row[4]:
                conn.execute(
                    "INSERT INTO workflow_events(workflow_id, step_id, attempt_id, at, event, old_state, new_state) VALUES (?, ?, ?, ?, 'MIRROR_STATE', ?, ?)",
                    (step_row[0], step_row[1], step_row[11], now,
                     previous["status"] if previous else None, step_row[4]),
                )
        conn.commit()


def list_reaction_snapshots(state_dir: str) -> List[Dict[str, Any]]:
    """Return the newest durable reaction snapshots stored in the ledger."""
    init_db(state_dir)
    snapshots: Dict[str, Dict[str, Any]] = {}
    with contextlib.closing(_connect(state_dir)) as conn:
        rows = conn.execute(
            "SELECT metadata_json, updated_at FROM workflows ORDER BY updated_at DESC"
        ).fetchall()
    for row in rows:
        metadata = _decode(row["metadata_json"])
        snapshot = metadata.get("reaction_snapshot") if isinstance(metadata, dict) else None
        if not isinstance(snapshot, dict) or not snapshot.get("reaction_id"):
            continue
        reaction_id = str(snapshot["reaction_id"])
        if reaction_id not in snapshots:
            snapshots[reaction_id] = snapshot
    return list(snapshots.values())


def transition_step(*, workflow_id: str, step_id: str, new_state: str,
                    state_dir: str, expected_states: Optional[Iterable[str]] = None,
                    attempt_id: Optional[str] = None, attempt_no: Optional[int] = None,
                    input_artifacts: Optional[Dict[str, Any]] = None,
                    output_artifacts: Optional[Dict[str, Any]] = None,
                    failure_reason: Optional[str] = None,
                    warnings: Optional[List[str]] = None) -> Dict[str, Any]:
    """Atomically change one step and release its successor when complete."""
    init_db(state_dir)
    now = time.time()
    allowed = set(expected_states or ())
    with contextlib.closing(_connect(state_dir)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM workflow_steps WHERE workflow_id=? AND step_id=?", (workflow_id, step_id)).fetchone()
        if row is None:
            conn.rollback()
            return {"ok": False, "error": "STEP_NOT_FOUND"}
        old_state = row["status"]
        if old_state in TERMINAL_STEPS and old_state != new_state:
            conn.rollback()
            return {"ok": False, "error": "TERMINAL_STEP_PROTECTED", "status": old_state}
        if allowed and old_state not in allowed:
            conn.rollback()
            return {"ok": False, "error": "STEP_VERSION_CONFLICT", "status": old_state}
        if attempt_id and attempt_no is not None:
            conn.execute(
                "INSERT INTO workflow_attempts(attempt_id, workflow_id, step_id, attempt_no, status, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(attempt_id) DO UPDATE SET status=excluded.status",
                (attempt_id, workflow_id, step_id, int(attempt_no), new_state, now),
            )
        changed = conn.execute(
            "UPDATE workflow_steps SET status=?, input_artifacts_json=COALESCE(?, input_artifacts_json), "
            "output_artifacts_json=COALESCE(?, output_artifacts_json), failure_reason=?, warnings_json=?, "
            "attempt_id=COALESCE(?, attempt_id), started_at=COALESCE(started_at, ?), "
            "finished_at=?, version=version+1 WHERE workflow_id=? AND step_id=? AND status=?",
            (new_state, _json(input_artifacts) if input_artifacts is not None else None,
             _json(output_artifacts) if output_artifacts is not None else None,
             failure_reason, _json(warnings or []), attempt_id, now if new_state in ("RUNNING", "VALIDATING") else None,
             now if new_state in TERMINAL_STEPS else None, workflow_id, step_id, old_state),
        )
        if changed.rowcount != 1:
            conn.rollback()
            return {"ok": False, "error": "STEP_VERSION_CONFLICT", "status": old_state}
        if attempt_id:
            conn.execute(
                "UPDATE workflow_attempts SET status=?, finished_at=?, error_message=?, result_json=? WHERE attempt_id=?",
                (new_state, now if new_state in TERMINAL_STEPS else None, failure_reason, _json(output_artifacts or {}), attempt_id),
            )
        conn.execute(
            "INSERT INTO workflow_events(workflow_id, step_id, attempt_id, at, event, old_state, new_state, detail_json) "
            "VALUES (?, ?, ?, ?, 'STEP_TRANSITION', ?, ?, ?)",
            (workflow_id, step_id, attempt_id, now, old_state, new_state, _json({"failure_reason": failure_reason, "warnings": warnings or []})),
        )
        if new_state in ("COMPLETE", "COMPLETED_WITH_WARNINGS"):
            next_row = conn.execute(
                "SELECT step_id, status FROM workflow_steps WHERE workflow_id=? AND step_index > ? ORDER BY step_index LIMIT 1",
                (workflow_id, row["step_index"]),
            ).fetchone()
            if next_row and next_row["status"] == "BLOCKED_BY_DEPENDENCY":
                conn.execute(
                    "UPDATE workflow_steps SET status='READY', version=version+1 WHERE workflow_id=? AND step_id=? AND status='BLOCKED_BY_DEPENDENCY'",
                    (workflow_id, next_row["step_id"]),
                )
                conn.execute(
                    "INSERT INTO workflow_events(workflow_id, step_id, at, event, old_state, new_state) VALUES (?, ?, ?, 'DEPENDENCY_RELEASED', 'BLOCKED_BY_DEPENDENCY', 'READY')",
                    (workflow_id, next_row["step_id"], now),
                )
        conn.execute("UPDATE workflows SET revision=revision+1, updated_at=? WHERE workflow_id=?", (now, workflow_id))
        conn.commit()
    return {"ok": True, "old_state": old_state, "new_state": new_state}


def get_workflow(workflow_id: str, state_dir: str) -> Optional[Dict[str, Any]]:
    init_db(state_dir)
    with contextlib.closing(_connect(state_dir)) as conn:
        workflow = conn.execute("SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
        if workflow is None:
            return None
        steps = conn.execute("SELECT * FROM workflow_steps WHERE workflow_id=? ORDER BY step_index", (workflow_id,)).fetchall()
        events = conn.execute("SELECT * FROM workflow_events WHERE workflow_id=? ORDER BY event_id", (workflow_id,)).fetchall()
    result = dict(workflow)
    result["metadata"] = _decode(result.pop("metadata_json", "{}"))
    result["steps"] = []
    for row in steps:
        item = dict(row)
        item["input_artifacts"] = _decode(item.pop("input_artifacts_json", "{}"))
        item["output_artifacts"] = _decode(item.pop("output_artifacts_json", "{}"))
        item["warnings"] = _decode(item.pop("warnings_json", "[]"))
        result["steps"].append(item)
    result["events"] = []
    for row in events:
        item = dict(row)
        item["detail"] = _decode(item.pop("detail_json", "{}"))
        result["events"].append(item)
    return result


def retry_step(*, workflow_id: str, step_id: str, state_dir: str, new_attempt_id: Optional[str] = None) -> Dict[str, Any]:
    """Atomically reset a failed/cancelled step to READY with an incremented attempt_no and new attempt_id (F-015)."""
    init_db(state_dir)
    now = time.time()
    with contextlib.closing(_connect(state_dir)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM workflow_steps WHERE workflow_id=? AND step_id=?", (workflow_id, step_id)).fetchone()
        if row is None:
            conn.rollback()
            return {"ok": False, "error": "STEP_NOT_FOUND"}
        old_state = row["status"]
        if old_state not in ("FAILED", "CANCELLED"):
            conn.rollback()
            return {"ok": False, "error": "INVALID_STATE_FOR_RETRY", "status": old_state}

        max_row = conn.execute("SELECT MAX(attempt_no) as max_att FROM workflow_attempts WHERE workflow_id=? AND step_id=?", (workflow_id, step_id)).fetchone()
        # retry_count=0 still represents the original attempt #1 even for
        # imported legacy steps that had no attempt_id row. The previous
        # fallback allocated attempt #1 again and overwrote that history.
        current_max = (
            max_row["max_att"]
            if max_row and max_row["max_att"] is not None
            else int(row["retry_count"] or 0) + 1
        )
        next_attempt_no = current_max + 1
        attempt_id = new_attempt_id or uuid.uuid4().hex

        conn.execute(
            "INSERT INTO workflow_attempts(attempt_id, workflow_id, step_id, attempt_no, status, started_at) "
            "VALUES (?, ?, ?, ?, 'READY', ?)",
            (attempt_id, workflow_id, step_id, next_attempt_no, now),
        )

        conn.execute(
            "UPDATE workflow_steps SET status='READY', attempt_id=?, retry_count=retry_count+1, "
            "failure_reason=NULL, started_at=NULL, finished_at=NULL, version=version+1 WHERE workflow_id=? AND step_id=?",
            (attempt_id, workflow_id, step_id),
        )

        conn.execute(
            "INSERT INTO workflow_events(workflow_id, step_id, attempt_id, at, event, old_state, new_state, detail_json) "
            "VALUES (?, ?, ?, ?, 'STEP_RETRY', ?, 'READY', ?)",
            (workflow_id, step_id, attempt_id, now, old_state, _json({"attempt_no": next_attempt_no})),
        )

        conn.execute(
            "UPDATE workflows SET status='RUNNING', revision=revision+1, updated_at=? WHERE workflow_id=? AND status IN ('FAILED', 'CANCELLED')",
            (now, workflow_id),
        )
        conn.commit()
    return {"ok": True, "workflow_id": workflow_id, "step_id": step_id, "attempt_id": attempt_id, "attempt_no": next_attempt_no, "new_state": "READY"}
