"""Dependency-free safety helpers for in-place SQLite migrations.

The application has several independent SQLite ledgers.  This module keeps
their migration backup behaviour consistent without coupling the ledgers to
one another.  Backups use SQLite's online backup API so committed WAL pages
are included; copying only the main ``.sqlite3`` file is not safe in WAL mode.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
import uuid
from collections.abc import Iterable, Mapping
from contextlib import closing


class CorruptDatabaseError(RuntimeError):
    """Raised before a migration when SQLite cannot verify the source DB."""


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    # Table names come exclusively from hard-coded migration specifications.
    safe = table.replace('"', '""')
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{safe}")')}


def backup_before_schema_upgrade(
    db_path: str,
    *,
    component: str,
    target_version: int,
    required_schema: Mapping[str, Iterable[str]],
) -> str | None:
    """Create one verified online backup iff an existing DB needs upgrading.

    Fresh/empty databases do not need a backup.  The caller still owns the
    migration transaction and must re-check its schema while holding the write
    lock because another process may have completed the migration meanwhile.
    """
    path = os.path.abspath(db_path)
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return None

    with closing(sqlite3.connect(path, timeout=30.0)) as source:
        source.execute("PRAGMA busy_timeout=30000")
        try:
            integrity = source.execute("PRAGMA quick_check").fetchone()
        except sqlite3.DatabaseError as exc:
            raise CorruptDatabaseError(f"SQLite integrity check failed for {path}") from exc
        if not integrity or str(integrity[0]).lower() != "ok":
            raise CorruptDatabaseError(
                f"SQLite integrity check failed for {path}: {integrity[0] if integrity else 'no result'}"
            )

        existing = _tables(source)
        if not existing:
            return None
        needs_upgrade = any(
            table not in existing or not set(columns).issubset(_columns(source, table))
            for table, columns in required_schema.items()
        )
        if not needs_upgrade:
            return None

        backup_dir = os.path.join(os.path.dirname(path), "migration_backups")
        os.makedirs(backup_dir, exist_ok=True)
        label = re.sub(r"[^A-Za-z0-9_.-]+", "-", component).strip("-.") or "database"
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        final_path = os.path.join(
            backup_dir,
            f"{os.path.basename(path)}.{label}.pre-v{int(target_version)}.{stamp}.{uuid.uuid4().hex[:8]}.bak",
        )
        temp_path = final_path + ".tmp"
        try:
            with closing(sqlite3.connect(temp_path)) as destination:
                source.backup(destination)
                check = destination.execute("PRAGMA quick_check").fetchone()
                if not check or str(check[0]).lower() != "ok":
                    raise CorruptDatabaseError("The pre-migration SQLite backup failed verification")
                destination.commit()
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                pass
            os.replace(temp_path, final_path)
        except Exception:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            raise
        return final_path
