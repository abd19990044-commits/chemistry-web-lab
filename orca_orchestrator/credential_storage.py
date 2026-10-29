"""SQLite mirror of AEAD credential records; plaintext is never accepted here."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing

from .cloudflare_controller.models import CloudflareCredentialVaultRecord


class LocalCredentialStorage:
    def __init__(self, state_dir: str):
        os.makedirs(state_dir, exist_ok=True)
        self.path = os.path.join(state_dir, "credential_vault.sqlite3")
        with closing(self._connect()) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS credential_vault (owner TEXT PRIMARY KEY, record_json TEXT NOT NULL)"
            )
            conn.execute("CREATE TABLE IF NOT EXISTS credential_tombstones (owner TEXT PRIMARY KEY)")
            conn.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA secure_delete=ON")
        return conn

    def get(self, owner: str):
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT record_json FROM credential_vault WHERE owner=?", (owner,)).fetchone()
        return CloudflareCredentialVaultRecord.from_dict(json.loads(row[0])) if row else None

    def put(self, record: CloudflareCredentialVaultRecord):
        if not record.ciphertext or not record.nonce or not record.tag:
            raise ValueError("Only encrypted credential records can be persisted")
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO credential_vault VALUES (?, ?) ON CONFLICT(owner) DO UPDATE SET record_json=excluded.record_json",
                (record.owner, json.dumps(record.to_dict(), allow_nan=False)),
            )
            conn.execute("DELETE FROM credential_tombstones WHERE owner=?", (record.owner,))
            conn.commit()

    def delete(self, owner: str):
        with closing(self._connect()) as conn:
            cursor = conn.execute("DELETE FROM credential_vault WHERE owner=?", (owner,))
            conn.execute("INSERT OR IGNORE INTO credential_tombstones VALUES (?)", (owner,))
            conn.commit()
            return cursor.rowcount > 0

    def is_deleted(self, owner: str):
        with closing(self._connect()) as conn:
            return conn.execute("SELECT 1 FROM credential_tombstones WHERE owner=?", (owner,)).fetchone() is not None

    def owners(self):
        with closing(self._connect()) as conn:
            return [row[0] for row in conn.execute("SELECT owner FROM credential_vault")]
