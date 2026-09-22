# -*- coding: utf-8 -*-
"""Durable local SQLite store for outbox, job state, and restart recovery."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class LocalJobStore:
    """Maintains local durable job states and unacknowledged outbound messages."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("""
            CREATE TABLE IF NOT EXISTS local_jobs (
                job_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                status TEXT NOT NULL,
                input_path TEXT,
                output_path TEXT,
                work_dir TEXT,
                resources_json TEXT,
                started_at REAL NOT NULL,
                completed_at REAL,
                result_json TEXT,
                exit_code INTEGER
            );
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS outbox_messages (
                message_id TEXT PRIMARY KEY,
                sequence INTEGER NOT NULL,
                type TEXT NOT NULL,
                job_id TEXT,
                attempt_id TEXT,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0
            );
            """)
            conn.commit()

    def record_job_start(
        self,
        job_id: str,
        attempt_id: str,
        stage: str,
        input_path: str,
        output_path: str,
        work_dir: str,
        resources: Dict[str, Any],
    ) -> None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO local_jobs (
                    job_id, attempt_id, stage, status, input_path, output_path,
                    work_dir, resources_json, started_at, completed_at, result_json, exit_code
                ) VALUES (?, ?, ?, 'RUNNING', ?, ?, ?, ?, ?, NULL, NULL, NULL)
                """,
                (
                    job_id,
                    attempt_id,
                    stage,
                    input_path,
                    output_path,
                    work_dir,
                    json.dumps(resources),
                    time.time(),
                ),
            )
            conn.commit()

    def record_job_result(self, job_id: str, result: Dict[str, Any], exit_code: int) -> None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE local_jobs
                SET status = ?, completed_at = ?, result_json = ?, exit_code = ?
                WHERE job_id = ?
                """,
                (
                    "COMPLETED" if result.get("ok") else "FAILED",
                    time.time(),
                    json.dumps(result),
                    exit_code,
                    job_id,
                ),
            )
            conn.commit()

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT job_id, attempt_id, stage, status, result_json, exit_code FROM local_jobs WHERE job_id = ?", (job_id,))
            row = cursor.fetchone()
            if not row:
                return None
            res = {}
            if row[4]:
                try:
                    res = json.loads(row[4])
                except Exception:
                    pass
            return {
                "job_id": row[0],
                "attempt_id": row[1],
                "stage": row[2],
                "status": row[3],
                "result": res,
                "exit_code": row[5],
            }

    def enqueue_outbox(self, msg: Dict[str, Any]) -> None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO outbox_messages (
                    message_id, sequence, type, job_id, attempt_id, payload_json, created_at, acknowledged
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    msg.get("message_id"),
                    msg.get("sequence", 1),
                    msg.get("type", "UNKNOWN"),
                    msg.get("job_id"),
                    msg.get("attempt_id"),
                    json.dumps(msg),
                    time.time(),
                ),
            )
            conn.commit()

    def mark_acknowledged(self, message_id: str) -> None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE outbox_messages SET acknowledged = 1 WHERE message_id = ?", (message_id,))
            conn.commit()

    def get_unacknowledged_messages(self) -> List[Dict[str, Any]]:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT payload_json FROM outbox_messages WHERE acknowledged = 0 ORDER BY sequence ASC")
            msgs = []
            for row in cursor.fetchall():
                try:
                    msgs.append(json.loads(row[0]))
                except Exception:
                    pass
            return msgs
