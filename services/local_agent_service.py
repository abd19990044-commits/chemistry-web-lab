# -*- coding: utf-8 -*-
"""Local Companion Agent Ephemeral Runtime Session & Device Registry Service.

CRITICAL INVARIANTS:
1. Every Agent process launch generates a NEW Connection API (CLA_...).
2. Connection APIs are strictly ephemeral and tied to agent_session_id.
3. Installation proof is persistent private state; pairing/runtime credentials are ephemeral.
4. Server stores SHA-256 verifiers, never plaintext Connection APIs or installation proofs.
5. In Single-Owner mode, claiming one token invalidates any remaining unclaimed tokens for that session.
6. The browser NEVER receives runtime_session_secret; it is delivered ONLY to the Agent via finalize/bootstrap channel.
7. nprocs * maxcore_mb <= usable_ram_mb is strictly enforced.
"""
from __future__ import annotations

import contextlib
import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from services.sqlite_migrations import backup_before_schema_upgrade

TOKEN_PREFIX = "CLA_"
DEFAULT_LOCAL_ORCA_CONCURRENCY = 1
DEFAULT_MAXDISK_MB = 20000
HEARTBEAT_TIMEOUT_SECONDS = 90  # Session marked OFFLINE if no heartbeat within 90s

_REGISTRY_LOCK = threading.Lock()
_WS_CONNECTIONS: Dict[str, Any] = {}  # agent_session_id -> WebSocket wrapper
_WS_OWNERS: Dict[str, str] = {}       # agent_session_id -> owner_id


def _pairing_cipher(state_dir: Optional[str] = None):
    """Return a process-independent cipher for one-time pairing delivery."""
    from cryptography.fernet import Fernet

    configured = (os.environ.get("SECRET_KEY") or os.environ.get("FLASK_SECRET_KEY") or "").encode("utf-8")
    if configured:
        material = hashlib.sha256(b"local-agent-pairing-v1\0" + configured).digest()
    else:
        base = state_dir or os.environ.get("CHEMISTRY_LAB_STATE_DIR") or os.path.join(os.getcwd(), "data")
        os.makedirs(base, exist_ok=True)
        key_path = os.path.join(base, ".local_agent_pairing.key")
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            fd = None
        if fd is not None:
            with os.fdopen(fd, "wb") as key_file:
                key_file.write(secrets.token_bytes(32))
        with open(key_path, "rb") as key_file:
            material = key_file.read(32)
        if len(material) != 32:
            raise RuntimeError("Invalid local-agent pairing encryption key")
    return Fernet(base64.urlsafe_b64encode(material))


def _get_db_path(state_dir: Optional[str] = None) -> str:
    base = state_dir or os.environ.get("CHEMISTRY_LAB_STATE_DIR") or os.path.join(os.getcwd(), "data")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "local_agent_registry.db")


@contextlib.contextmanager
def _db_connection(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    safe = table.replace('"', '""')
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{safe}")')}


def _init_db(db_path: str) -> None:
    backup_before_schema_upgrade(
        db_path,
        component="local-agent-registry",
        target_version=2,
        required_schema={
            "agent_schema": ("version",),
            "agent_installations": ("installation_id", "installation_secret_hash"),
            "agent_runtime_sessions": (
                "agent_session_id", "runtime_secret_hash", "pending_secret_ciphertext",
            ),
            "agent_runtime_tokens": ("token_id", "token_verifier"),
            "agent_jobs": (
                "job_id", "output_text", "workflow_id", "step_id", "attempt_id",
                "projection_status", "process_pid", "process_start_time",
                "process_command_fingerprint", "process_workspace", "process_last_heartbeat",
            ),
            "agent_job_idempotency": ("idempotency_key", "job_id"),
        },
    )
    with _db_connection(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS agent_schema(version INTEGER NOT NULL)")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_installations (
            installation_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            platform TEXT NOT NULL,
            backend_kind TEXT NOT NULL DEFAULT 'local',
            scheduler_type TEXT,
            created_at REAL NOT NULL,
            last_seen REAL NOT NULL,
            installation_secret_hash TEXT
        );
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_runtime_sessions (
            agent_session_id TEXT PRIMARY KEY,
            installation_id TEXT NOT NULL,
            owner_id TEXT,
            runtime_secret_hash TEXT,
            started_at REAL NOT NULL,
            paired_at REAL,
            last_seen REAL NOT NULL,
            ended_at REAL,
            connection_state TEXT NOT NULL DEFAULT 'UNPAIRED',
            agent_version TEXT NOT NULL DEFAULT '1.0.3',
            protocol_version INTEGER NOT NULL DEFAULT 1,
            capabilities_json TEXT NOT NULL DEFAULT '{}'
        );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runtime_owner ON agent_runtime_sessions(owner_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runtime_inst ON agent_runtime_sessions(installation_id);")

        if "installation_secret_hash" not in _table_columns(conn, "agent_installations"):
            conn.execute(
                "ALTER TABLE agent_installations ADD COLUMN installation_secret_hash TEXT"
            )

        conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_runtime_tokens (
            token_id TEXT PRIMARY KEY,
            agent_session_id TEXT NOT NULL,
            token_verifier TEXT NOT NULL,
            created_at REAL NOT NULL,
            claimed_at REAL,
            invalidated_at REAL
        );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_token_verifier ON agent_runtime_tokens(token_verifier);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_token_session ON agent_runtime_tokens(agent_session_id);")

        conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_jobs (
            job_id TEXT PRIMARY KEY,
            agent_session_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            job_name TEXT NOT NULL,
            input_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'QUEUED',
            created_at REAL NOT NULL,
            started_at REAL,
            completed_at REAL,
            exit_code INTEGER,
            stdout_tail TEXT,
            parsed_results_json TEXT,
            error_message TEXT
        );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_jobs_session ON agent_jobs(agent_session_id, status);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_jobs_owner ON agent_jobs(owner_id);")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_job_idempotency (
            idempotency_key TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            job_id TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        """)
        agent_job_columns = _table_columns(conn, "agent_jobs")
        for col, col_type in [
            ("output_text", "TEXT"),
            ("xyz_structure", "TEXT"),
            ("artifacts_zip_path", "TEXT"),
            ("cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
            ("workflow_id", "TEXT"),
            ("step_id", "TEXT"),
            ("attempt_id", "TEXT"),
            ("reaction_id", "TEXT"),
            ("projection_status", "TEXT NOT NULL DEFAULT 'NOT_REQUIRED'"),
            ("process_pid", "INTEGER"),
            ("process_start_time", "REAL"),
            ("process_command_fingerprint", "TEXT"),
            ("process_workspace", "TEXT"),
            ("process_last_heartbeat", "REAL"),
        ]:
            if col not in agent_job_columns:
                conn.execute(f"ALTER TABLE agent_jobs ADD COLUMN {col} {col_type};")
        if "pending_secret_ciphertext" not in _table_columns(conn, "agent_runtime_sessions"):
            conn.execute("ALTER TABLE agent_runtime_sessions ADD COLUMN pending_secret_ciphertext TEXT")
        conn.execute("DELETE FROM agent_schema")
        conn.execute("INSERT INTO agent_schema(version) VALUES (2)")
        conn.commit()


def hash_token_verifier(token: str) -> str:
    clean = token.strip()
    return hashlib.sha256(f"chemlab_verifier_salt_{clean}".encode("utf-8")).hexdigest()


def hash_runtime_secret(secret: str) -> str:
    clean = secret.strip()
    return hashlib.sha256(f"chemlab_runtime_salt_{clean}".encode("utf-8")).hexdigest()


def redact_token_for_log(token: str) -> str:
    clean = (token or "").strip()
    if len(clean) <= 10:
        return "CLA_***"
    return f"{clean[:7]}...{clean[-4:]}"


def init_runtime_session(
    installation_id: str,
    agent_session_id: str,
    token_verifiers: List[str],
    device_name: str = "Local Computer",
    platform: str = "windows",
    backend_kind: str = "local",
    scheduler_type: Optional[str] = None,
    agent_version: str = "1.0.3",
    protocol_version: int = 1,
    capabilities: Optional[Dict[str, Any]] = None,
    state_dir: Optional[str] = None,
    installation_secret: Optional[str] = None,
) -> Dict[str, Any]:
    db_path = _get_db_path(state_dir)
    _init_db(db_path)
    now_t = time.time()

    with _REGISTRY_LOCK, _db_connection(db_path) as conn:
        cursor = conn.cursor()

        # F-018: Installation authentication and spoofing protection
        cursor.execute("SELECT installation_secret_hash FROM agent_installations WHERE installation_id = ?", (installation_id,))
        inst_row = cursor.fetchone()

        given_hash = hashlib.sha256(installation_secret.encode("utf-8")).hexdigest() if installation_secret else None

        if inst_row is not None:
            cursor.execute(
                "SELECT agent_session_id, connection_state FROM agent_runtime_sessions "
                "WHERE installation_id = ? AND connection_state IN ('ONLINE', 'RECONNECTING')",
                (installation_id,),
            )
            active_s = cursor.fetchone()
            stored_hash = inst_row[0]
            valid_proof = bool(
                stored_hash and given_hash
                and hmac.compare_digest(given_hash, stored_hash)
            )
            # Legacy installations without a stored verifier may enrol a
            # secret only while inactive. Otherwise an attacker who knows the
            # public installation id could become the first writer and evict
            # the live legitimate session.
            if active_s and not valid_proof:
                return {
                    "ok": False,
                    "error": "Cannot supersede active installation session without valid installation secret proof.",
                    "error_code": "INSTALLATION_AUTH_REQUIRED",
                }

        secret_hash_to_store = given_hash or (inst_row[0] if inst_row else None)
        cursor.execute(
            """
            INSERT OR REPLACE INTO agent_installations (
                installation_id, display_name, platform, backend_kind, scheduler_type, created_at, last_seen, installation_secret_hash
            ) VALUES (
                ?, ?, ?, ?, ?,
                COALESCE((SELECT created_at FROM agent_installations WHERE installation_id = ?), ?),
                ?, ?
            )
            """,
            (installation_id, device_name, platform.lower(), backend_kind.lower(), scheduler_type, installation_id, now_t, now_t, secret_hash_to_store),
        )

        cursor.execute(
            """
            UPDATE agent_runtime_sessions
            SET connection_state = 'SUPERSEDED', ended_at = ?
            WHERE installation_id = ? AND connection_state IN ('UNPAIRED', 'ONLINE', 'RECONNECTING', 'OFFLINE')
            """,
            (now_t, installation_id),
        )

        # Never automatically replay a RUNNING agent job. The old ORCA/MPI
        # process may have survived the companion agent crash, and assigning
        # that logical job to the new session would launch a duplicate. Keep
        # the original session/process association as recovery evidence and
        # require an explicit operator retry (a new attempt) if identity cannot
        # be proved.
        cursor.execute(
            """
            UPDATE agent_jobs
            SET status = 'RECOVERY_REQUIRED',
                error_message = 'Companion agent restarted while this job was running; automatic replay was refused'
            WHERE status = 'RUNNING'
              AND agent_session_id IN (
                  SELECT agent_session_id FROM agent_runtime_sessions
                  WHERE installation_id = ? AND agent_session_id <> ?
              )
            """,
            (installation_id, agent_session_id),
        )

        cursor.execute(
            """
            INSERT INTO agent_runtime_sessions (
                agent_session_id, installation_id, owner_id, runtime_secret_hash,
                started_at, paired_at, last_seen, ended_at, connection_state,
                agent_version, protocol_version, capabilities_json
            ) VALUES (?, ?, NULL, NULL, ?, NULL, ?, NULL, 'UNPAIRED', ?, ?, ?)
            """,
            (
                agent_session_id,
                installation_id,
                now_t,
                now_t,
                agent_version,
                protocol_version,
                json.dumps(capabilities or {}),
            ),
        )

        for verifier in token_verifiers:
            token_id = str(uuid.uuid4())
            cursor.execute(
                """
                INSERT INTO agent_runtime_tokens (
                    token_id, agent_session_id, token_verifier, created_at, claimed_at, invalidated_at
                ) VALUES (?, ?, ?, ?, NULL, NULL)
                """,
                (token_id, agent_session_id, verifier, now_t),
            )

        conn.commit()

    projection_report = reconcile_agent_workflow_projections(state_dir=state_dir)
    return {
        "ok": True,
        "agent_session_id": agent_session_id,
        "installation_id": installation_id,
        "status": "UNPAIRED",
        "workflow_reconciliation": projection_report,
    }


def claim_runtime_token(
    connection_api: str,
    owner_id: str,
    custom_device_name: Optional[str] = None,
    single_owner_mode: bool = True,
    state_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Browser claim: Binds owner_id to agent_session_id without returning runtime_session_secret to browser."""
    clean_token = connection_api.strip()
    if not clean_token.startswith(TOKEN_PREFIX):
        return {
            "ok": False,
            "error": "Invalid Connection API format (must start with CLA_)",
            "error_code": "INVALID_TOKEN_FORMAT",
        }

    token_verifier = hash_token_verifier(clean_token)
    db_path = _get_db_path(state_dir)
    _init_db(db_path)
    now_t = time.time()

    with _REGISTRY_LOCK, _db_connection(db_path) as conn:
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT t.token_id, t.agent_session_id, t.claimed_at, t.invalidated_at,
                   s.installation_id, s.owner_id, s.connection_state, s.started_at,
                   i.display_name, i.platform, i.backend_kind, i.scheduler_type
            FROM agent_runtime_tokens t
            JOIN agent_runtime_sessions s ON t.agent_session_id = s.agent_session_id
            JOIN agent_installations i ON s.installation_id = i.installation_id
            WHERE t.token_verifier = ?
            """,
            (token_verifier,),
        )
        row = cursor.fetchone()
        if not row:
            return {"ok": False, "error": "Connection API not found or agent process restarted.", "error_code": "TOKEN_NOT_FOUND"}

        (
            token_id, session_id, claimed_at, invalidated_at,
            inst_id, existing_owner, conn_state, started_at,
            disp_name, platform, backend_kind, scheduler_type
        ) = row

        if invalidated_at is not None:
            return {"ok": False, "error": "Connection API has been invalidated by a restart or previous claim.", "error_code": "TOKEN_INVALIDATED"}

        if claimed_at is not None:
            return {"ok": False, "error": "Connection API has already been claimed for this process.", "error_code": "TOKEN_ALREADY_CLAIMED"}

        if conn_state in ("STOPPED", "SUPERSEDED", "REVOKED"):
            return {"ok": False, "error": f"Agent process is no longer active ({conn_state}). Restart Agent and enter new API.", "error_code": "SESSION_INACTIVE"}

        if existing_owner and existing_owner != owner_id:
            return {"ok": False, "error": "Agent process is already claimed by another user account.", "error_code": "CROSS_USER_CLAIM_BLOCKED"}

        runtime_session_secret = f"CRS_{secrets.token_urlsafe(32)}"
        secret_hash = hash_runtime_secret(runtime_session_secret)

        pending_ciphertext = _pairing_cipher(state_dir).encrypt(
            runtime_session_secret.encode("utf-8")
        ).decode("ascii")

        final_display_name = (custom_device_name or disp_name).strip()

        if custom_device_name:
            cursor.execute("UPDATE agent_installations SET display_name = ? WHERE installation_id = ?", (final_display_name, inst_id))

        cursor.execute(
            """
            UPDATE agent_runtime_sessions
            SET owner_id = ?, runtime_secret_hash = ?, pending_secret_ciphertext = ?,
                paired_at = ?, last_seen = ?, connection_state = 'ONLINE'
            WHERE agent_session_id = ?
            """,
            (owner_id, secret_hash, pending_ciphertext, now_t, now_t, session_id),
        )

        cursor.execute("UPDATE agent_runtime_tokens SET claimed_at = ? WHERE token_id = ?", (now_t, token_id))

        if single_owner_mode:
            cursor.execute(
                "UPDATE agent_runtime_tokens SET invalidated_at = ? WHERE agent_session_id = ? AND claimed_at IS NULL",
                (now_t, session_id),
            )

        conn.commit()

    # Browser response: ZERO runtime_session_secret!
    return {
        "ok": True,
        "agent_session_id": session_id,
        "installation_id": inst_id,
        "owner_id": owner_id,
        "display_name": final_display_name,
        "platform": platform,
        "backend_kind": backend_kind,
        "scheduler_type": scheduler_type,
        "status": "ONLINE",
        "paired_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_t)),
    }


def finalize_agent_runtime(
    agent_session_id: str,
    connection_api: str,
    state_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Called exclusively by the Agent process to retrieve its in-memory runtime session credential."""
    clean_token = connection_api.strip()
    verifier = hash_token_verifier(clean_token)
    db_path = _get_db_path(state_dir)
    _init_db(db_path)

    with _REGISTRY_LOCK, _db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT s.agent_session_id, s.owner_id, s.connection_state,
                   s.runtime_secret_hash, s.pending_secret_ciphertext
            FROM agent_runtime_sessions s
            JOIN agent_runtime_tokens t ON s.agent_session_id = t.agent_session_id
            WHERE s.agent_session_id = ? AND t.token_verifier = ?
              AND t.claimed_at IS NOT NULL AND t.invalidated_at IS NULL
            """,
            (agent_session_id, verifier),
        )
        row = cursor.fetchone()
        if not row:
            return {"ok": False, "error": "Invalid session or connection API proof.", "error_code": "INVALID_AGENT_PROOF"}

        sess_id, owner_id, conn_state, stored_hash, pending_ciphertext = row
        if not owner_id or conn_state not in ("ONLINE", "RECONNECTING"):
            return {"ok": False, "error": "Session has not yet been claimed by website user.", "error_code": "AWAITING_CLAIM"}

        if not pending_ciphertext:
            return {"ok": False, "error": "Pairing credential was already finalized.",
                    "error_code": "PAIRING_ALREADY_FINALIZED"}
        try:
            secret = _pairing_cipher(state_dir).decrypt(
                pending_ciphertext.encode("ascii")
            ).decode("utf-8")
        except Exception:
            return {"ok": False,
                    "error": "Pairing credential cannot be decrypted by this deployment.",
                    "error_code": "PAIRING_KEY_MISMATCH"}

        # F-029: One-time delivery. Clear pending ciphertext and invalidate token.
        now_t = time.time()
        cursor.execute(
            """
            UPDATE agent_runtime_sessions
            SET pending_secret_ciphertext = NULL
            WHERE agent_session_id = ?
            """,
            (sess_id,),
        )
        cursor.execute(
            """
            UPDATE agent_runtime_tokens
            SET invalidated_at = ?
            WHERE agent_session_id = ? AND token_verifier = ?
            """,
            (now_t, sess_id, verifier),
        )
        conn.commit()

        return {
            "ok": True,
            "agent_session_id": sess_id,
            "owner_id": owner_id,
            "runtime_session_secret": secret,
            "status": "ONLINE",
        }


def authenticate_runtime_session(
    agent_session_id: str,
    runtime_session_secret: str,
    state_dir: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    db_path = _get_db_path(state_dir)
    _init_db(db_path)
    s_hash = hash_runtime_secret(runtime_session_secret.strip())

    with _REGISTRY_LOCK, _db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT s.agent_session_id, s.installation_id, s.owner_id, s.runtime_secret_hash,
                   s.connection_state, s.capabilities_json, i.display_name, i.platform,
                   i.backend_kind, i.scheduler_type
            FROM agent_runtime_sessions s
            JOIN agent_installations i ON s.installation_id = i.installation_id
            WHERE s.agent_session_id = ?
            """,
            (agent_session_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None

        (
            sess_id, inst_id, owner_id, stored_hash,
            conn_state, caps_json, disp_name, platform,
            backend_kind, scheduler_type
        ) = row

        if conn_state in ("STOPPED", "SUPERSEDED", "REVOKED"):
            return None

        if not stored_hash or not hmac.compare_digest(stored_hash, s_hash):
            return None

        now_t = time.time()
        # Successful runtime authentication proves the Agent received the
        # one-time credential, so the recoverable ciphertext can be erased.
        cursor.execute(
            "UPDATE agent_runtime_sessions SET last_seen = ?, connection_state = 'ONLINE', "
            "pending_secret_ciphertext = NULL WHERE agent_session_id = ?",
            (now_t, sess_id),
        )
        cursor.execute("UPDATE agent_installations SET last_seen = ? WHERE installation_id = ?", (now_t, inst_id))
        conn.commit()

        caps = {}
        try:
            caps = json.loads(caps_json)
        except Exception:
            pass

        return {
            "agent_session_id": sess_id,
            "installation_id": inst_id,
            "owner_id": owner_id,
            "display_name": disp_name,
            "platform": platform,
            "backend_kind": backend_kind,
            "scheduler_type": scheduler_type,
            "capabilities": caps,
        }



def get_user_runtime_device(agent_session_id: str, owner_id: str, state_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieves a runtime device strictly owned by owner_id."""
    db_path = _get_db_path(state_dir)
    _init_db(db_path)
    now_t = time.time()

    with _REGISTRY_LOCK, _db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT s.agent_session_id, s.installation_id, s.owner_id, s.connection_state,
                   s.started_at, s.paired_at, s.last_seen, s.capabilities_json,
                   i.display_name, i.platform, i.backend_kind, i.scheduler_type
            FROM agent_runtime_sessions s
            JOIN agent_installations i ON s.installation_id = i.installation_id
            WHERE s.agent_session_id = ? AND s.owner_id = ? AND s.connection_state NOT IN ('STOPPED', 'SUPERSEDED')
            """,
            (agent_session_id, owner_id),
        )
        row = cursor.fetchone()
        if not row:
            return None

        (
            sess_id, inst_id, o_id, conn_state,
            started_t, paired_t, seen_t, caps_str,
            disp_name, platform, backend_kind, scheduler_type
        ) = row

        caps = {}
        try:
            caps = json.loads(caps_str)
        except Exception:
            pass

        effective_status = conn_state
        if sess_id not in _WS_CONNECTIONS and (now_t - seen_t > HEARTBEAT_TIMEOUT_SECONDS):
            effective_status = "OFFLINE"

        return {
            "agent_session_id": sess_id,
            "installation_id": inst_id,
            "owner_id": o_id,
            "display_name": disp_name,
            "platform": platform,
            "backend_kind": backend_kind,
            "scheduler_type": scheduler_type,
            "status": effective_status,
            "process_started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_t)),
            "paired_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(paired_t)) if paired_t else None,
            "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seen_t)) if seen_t else None,
            "capabilities": caps,
        }


def disconnect_user_device(agent_session_id: str, owner_id: str, state_dir: Optional[str] = None) -> bool:
    """Disconnects an agent device strictly matching agent_session_id AND owner_id at the DB boundary."""
    db_path = _get_db_path(state_dir)
    _init_db(db_path)
    now_t = time.time()

    with _REGISTRY_LOCK, _db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE agent_runtime_sessions
            SET connection_state = 'STOPPED', ended_at = ?
            WHERE agent_session_id = ? AND owner_id = ?
            """,
            (now_t, agent_session_id, owner_id),
        )
        affected = cursor.rowcount
        if affected > 0:
            cursor.execute("UPDATE agent_runtime_tokens SET invalidated_at = ? WHERE agent_session_id = ?", (now_t, agent_session_id))
            cursor.execute(
                """
                UPDATE agent_jobs
                SET status = 'FAILED', error_message = 'Agent disconnected', completed_at = ?,
                    projection_status = CASE WHEN workflow_id IS NOT NULL AND step_id IS NOT NULL THEN 'PENDING' ELSE projection_status END
                WHERE agent_session_id = ? AND status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')
                """,
                (now_t, agent_session_id),
            )
        conn.commit()
    if affected > 0:
        try:
            reconcile_agent_workflow_projections(state_dir=state_dir)
        except Exception:
            pass

    if affected > 0 and agent_session_id in _WS_CONNECTIONS:
        try:
            ws = _WS_CONNECTIONS.pop(agent_session_id, None)
            _WS_OWNERS.pop(agent_session_id, None)
            if ws and hasattr(ws, "close"):
                ws.close()
        except Exception:
            pass

    return affected > 0


def end_agent_runtime_session(
    agent_session_id: str,
    runtime_secret: Optional[str] = None,
    state_dir: Optional[str] = None,
) -> bool:
    """Terminates an agent session when verified by its runtime secret."""
    if not runtime_secret:
        return False
    auth = authenticate_runtime_session(agent_session_id, runtime_secret, state_dir=state_dir)
    if not auth:
        return False
    return end_runtime_session(agent_session_id, state_dir=state_dir)

def end_runtime_session(agent_session_id: str, state_dir: Optional[str] = None) -> bool:
    db_path = _get_db_path(state_dir)
    _init_db(db_path)
    now_t = time.time()

    with _REGISTRY_LOCK, _db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE agent_runtime_sessions
            SET connection_state = 'STOPPED', ended_at = ?
            WHERE agent_session_id = ?
            """,
            (now_t, agent_session_id),
        )
        cursor.execute("UPDATE agent_runtime_tokens SET invalidated_at = ? WHERE agent_session_id = ?", (now_t, agent_session_id))
        affected = cursor.rowcount
        if affected > 0:
            cursor.execute(
                """
                UPDATE agent_jobs
                SET status = 'FAILED', error_message = 'Agent runtime session ended', completed_at = ?,
                    projection_status = CASE WHEN workflow_id IS NOT NULL AND step_id IS NOT NULL THEN 'PENDING' ELSE projection_status END
                WHERE agent_session_id = ? AND status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')
                """,
                (now_t, agent_session_id),
            )
        conn.commit()
    if affected > 0:
        try:
            reconcile_agent_workflow_projections(state_dir=state_dir)
        except Exception:
            pass

    if affected > 0 and agent_session_id in _WS_CONNECTIONS:
        try:
            ws = _WS_CONNECTIONS.pop(agent_session_id, None)
            _WS_OWNERS.pop(agent_session_id, None)
            if ws and hasattr(ws, "close"):
                ws.close()
        except Exception:
            pass

    return affected > 0


def list_user_runtime_devices(owner_id: str, state_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    db_path = _get_db_path(state_dir)
    _init_db(db_path)
    now_t = time.time()

    devices: List[Dict[str, Any]] = []
    with _REGISTRY_LOCK, _db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT s.agent_session_id, s.installation_id, s.owner_id, s.connection_state,
                   s.started_at, s.paired_at, s.last_seen, s.capabilities_json,
                   i.display_name, i.platform, i.backend_kind, i.scheduler_type
            FROM agent_runtime_sessions s
            JOIN agent_installations i ON s.installation_id = i.installation_id
            WHERE s.owner_id = ? AND s.connection_state NOT IN ('STOPPED', 'SUPERSEDED')
            ORDER BY s.started_at DESC
            """,
            (owner_id,),
        )
        for row in cursor.fetchall():
            (
                sess_id, inst_id, o_id, conn_state,
                started_t, paired_t, seen_t, caps_str,
                disp_name, platform, backend_kind, scheduler_type
            ) = row

            caps = {}
            try:
                caps = json.loads(caps_str)
            except Exception:
                pass

            effective_status = conn_state
            if sess_id not in _WS_CONNECTIONS and (now_t - seen_t > HEARTBEAT_TIMEOUT_SECONDS):
                effective_status = "OFFLINE"

            devices.append({
                "agent_session_id": sess_id,
                "installation_id": inst_id,
                "owner_id": o_id,
                "display_name": disp_name,
                "platform": platform,
                "backend_kind": backend_kind,
                "scheduler_type": scheduler_type,
                "status": effective_status,
                "process_started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_t)),
                "paired_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(paired_t)) if paired_t else None,
                "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seen_t)) if seen_t else None,
                "capabilities": caps,
            })

    return devices


def calculate_orca_resource_directives(
    cpu_cores: int = 1,
    ram_gb: float = 4.0,
    disk_gb: float = 20.0,
) -> Dict[str, Any]:
    """Calculates ORCA resource directives with strict mathematical bounds.
    
    Hard Invariant: nprocs * maxcore_mb <= usable_ram_mb (where usable = 80% of total)
    """
    nprocs = max(1, int(cpu_cores))
    requested_total_ram_mb = max(1.0, float(ram_gb) * 1024.0)
    usable_ram_mb = requested_total_ram_mb * 0.80
    calculated_maxcore_mb = math.floor(usable_ram_mb / nprocs)

    MIN_MAXCORE_MB = 256
    if calculated_maxcore_mb < MIN_MAXCORE_MB:
        min_required_ram_gb = math.ceil((MIN_MAXCORE_MB * nprocs / 0.80) / 1024.0 * 10.0) / 10.0
        return {
            "ok": False,
            "error": f"Requested RAM ({ram_gb:.1f} GB) is insufficient for {nprocs} CPU cores (requires at least {min_required_ram_gb:.1f} GB usable RAM with 80% safety reserve).",
            "error_code": "INSUFFICIENT_RAM_FOR_CORE_COUNT",
            "min_required_ram_gb": min_required_ram_gb,
        }

    maxcore_mb = int(calculated_maxcore_mb)

    requested_disk_mb = int(float(disk_gb) * 1024.0)
    MIN_DISK_MB = 1000
    if requested_disk_mb < MIN_DISK_MB:
        return {
            "ok": False,
            "error": f"Requested disk ({disk_gb:.2f} GB / {requested_disk_mb} MB) is below the minimum required limit of 1.0 GB ({MIN_DISK_MB} MB).",
            "error_code": "INSUFFICIENT_DISK_ALLOCATION",
        }
    maxdisk_mb = requested_disk_mb

    return {
        "ok": True,
        "nprocs": nprocs,
        "maxcore_mb": maxcore_mb,
        "maxdisk_mb": maxdisk_mb,
        "pal_block": f"%pal\n  nprocs {nprocs}\nend" if nprocs > 1 else "",
        "maxcore_line": f"%maxcore {maxcore_mb}",
        "maxdisk_block": f"%scf\n  MaxDisk {maxdisk_mb}\nend",
        "maxdisk_line": f"MaxDisk {maxdisk_mb}",
    }


def _strip_orca_line_comments(line: str) -> str:
    return line.split("#")[0].strip()


def inject_orca_resources(input_text: str, resources: Dict[str, Any]) -> str:
    res = calculate_orca_resource_directives(
        cpu_cores=resources.get("cpu_cores", 1),
        ram_gb=resources.get("ram_gb", 4.0),
        disk_gb=resources.get("disk_gb", 20.0),
    )
    if not res.get("ok"):
        raise ValueError(res.get("error", "Invalid resource allocation"))

    lines = input_text.splitlines()
    cleaned_lines: List[str] = []
    in_pal = False

    for line in lines:
        stripped = _strip_orca_line_comments(line)
        s_line = stripped.lower()

        # F-023: Structure- and comment-aware block handling
        if s_line.startswith("%pal"):
            tokens = s_line.split()
            if len(tokens) >= 2 and tokens[-1] == "end":
                # Single-line %pal ... end; do not enter multi-line in_pal state
                continue
            in_pal = True
            continue

        if in_pal:
            tokens = s_line.split()
            if tokens and tokens[0] == "end":
                in_pal = False
            continue

        tokens = s_line.split()
        if tokens and tokens[0] in ("%maxcore", "%maxdisk", "maxdisk"):
            continue

        cleaned_lines.append(line)

    injections: List[str] = []
    if res.get("maxcore_line"):
        injections.append(res["maxcore_line"])
    if res.get("maxdisk_block"):
        injections.append(res["maxdisk_block"])
    if res.get("pal_block"):
        injections.append(res["pal_block"])

    final_lines: List[str] = []
    injected = False
    for line in cleaned_lines:
        final_lines.append(line)
        if not injected and line.strip().startswith("!"):
            final_lines.extend(injections)
            injected = True

    if not injected:
        final_lines = injections + final_lines

    return "\n".join(final_lines) + "\n"


# ---------------------------------------------------------------------------
# Bidirectional Agent Job Queue & Execution Support
# ---------------------------------------------------------------------------

def enqueue_agent_job(
    agent_session_id: str,
    owner_id: str,
    input_text: str,
    job_name: str = "calculation",
    state_dir: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    workflow_id: Optional[str] = None,
    step_id: Optional[str] = None,
    attempt_id: Optional[str] = None,
    reaction_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Enqueues an ORCA calculation job for a specific connected companion agent."""
    db_path = _get_db_path(state_dir)
    _init_db(db_path)
    now_t = time.time()
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    request_hash = hashlib.sha256(json.dumps({
        "agent_session_id": agent_session_id,
        "owner_id": owner_id,
        "job_name": job_name,
        "input_text": input_text,
        "workflow_id": workflow_id,
        "step_id": step_id,
        "attempt_id": attempt_id,
        "reaction_id": reaction_id,
    }, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    with _REGISTRY_LOCK, _db_connection(db_path) as conn:
        cursor = conn.cursor()
        target = cursor.execute(
            "SELECT owner_id, connection_state FROM agent_runtime_sessions "
            "WHERE agent_session_id = ?",
            (agent_session_id,),
        ).fetchone()
        if not target:
            return {"ok": False, "error": "AGENT_SESSION_NOT_FOUND"}
        if target[0] != owner_id:
            return {"ok": False, "error": "AGENT_SESSION_NOT_OWNED"}
        if target[1] in ("STOPPED", "SUPERSEDED", "REVOKED"):
            return {"ok": False, "error": "AGENT_SESSION_INACTIVE"}
        if idempotency_key:
            cursor.execute(
                "SELECT owner_id, request_hash, job_id FROM agent_job_idempotency "
                "WHERE idempotency_key = ?", (idempotency_key,))
            prior = cursor.fetchone()
            if prior:
                if prior[0] != owner_id or prior[1] != request_hash:
                    return {"ok": False, "error": "IDEMPOTENCY_KEY_REUSE"}
                cursor.execute("SELECT status, created_at FROM agent_jobs WHERE job_id = ?",
                               (prior[2],))
                job_row = cursor.fetchone()
                return {
                    "ok": True, "job_id": prior[2],
                    "agent_session_id": agent_session_id,
                    "status": job_row[0] if job_row else "UNKNOWN",
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                  time.gmtime(job_row[1] if job_row else now_t)),
                    "replayed": True,
                }
        cursor.execute(
            """
            INSERT INTO agent_jobs (
                job_id, agent_session_id, owner_id, job_name, input_text,
                status, created_at, started_at, completed_at, exit_code,
                stdout_tail, parsed_results_json, error_message, workflow_id,
                step_id, attempt_id, reaction_id, projection_status
            ) VALUES (?, ?, ?, ?, ?, 'QUEUED', ?, NULL, NULL, NULL, '', '{}', NULL,
                      ?, ?, ?, ?, ?)
            """,
            (job_id, agent_session_id, owner_id, job_name, input_text, now_t,
             workflow_id, step_id, attempt_id, reaction_id,
             "PENDING" if workflow_id and step_id else "NOT_REQUIRED"),
        )
        if idempotency_key:
            cursor.execute(
                "INSERT INTO agent_job_idempotency "
                "(idempotency_key, owner_id, request_hash, job_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (idempotency_key, owner_id, request_hash, job_id, now_t),
            )
        conn.commit()

    return {
        "ok": True,
        "job_id": job_id,
        "agent_session_id": agent_session_id,
        "status": "QUEUED",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_t)),
    }


def poll_next_agent_job(
    agent_session_id: str,
    runtime_session_secret: str,
    state_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Polls the queue for the next available job destined for this agent session."""
    db_path = _get_db_path(state_dir)
    _init_db(db_path)
    auth = authenticate_runtime_session(agent_session_id, runtime_session_secret, state_dir=state_dir)
    if not auth:
        return {"ok": False, "error": "UNAUTHORIZED_AGENT_SESSION", "job": None}

    now_t = time.time()
    with _REGISTRY_LOCK, _db_connection(db_path) as conn:
        cursor = conn.cursor()
        # SELECT-then-UPDATE must be one write transaction.  The process-local
        # lock does not protect a second web worker/process sharing SQLite.
        conn.execute("BEGIN IMMEDIATE")
        # Refresh last_seen timestamp
        cursor.execute(
            "UPDATE agent_runtime_sessions SET last_seen = ? WHERE agent_session_id = ?",
            (now_t, agent_session_id),
        )

        cursor.execute(
            """
            SELECT job_id, job_name, input_text, created_at
            FROM agent_jobs
            WHERE agent_session_id = ? AND status = 'QUEUED'
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (agent_session_id,),
        )
        row = cursor.fetchone()
        if not row:
            conn.commit()
            return {"ok": True, "job": None}

        job_id, job_name, input_text, created_at = row
        cursor.execute(
            "UPDATE agent_jobs SET status = 'RUNNING', started_at = ? "
            "WHERE job_id = ? AND agent_session_id = ? AND status = 'QUEUED'",
            (now_t, job_id, agent_session_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return {"ok": True, "job": None}
        conn.commit()

    return {
        "ok": True,
        "job": {
            "job_id": job_id,
            "job_name": job_name,
            "input_text": input_text,
            "created_at": created_at,
        },
    }


def update_agent_job_progress(
    job_id: str,
    agent_session_id: str,
    runtime_session_secret: str,
    stdout_chunk: str,
    state_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Updates log progress / stdout tail for a running agent job."""
    db_path = _get_db_path(state_dir)
    _init_db(db_path)
    auth = authenticate_runtime_session(agent_session_id, runtime_session_secret, state_dir=state_dir)
    if not auth:
        return {"ok": False, "error": "UNAUTHORIZED_AGENT_SESSION"}

    now_t = time.time()
    with _REGISTRY_LOCK, _db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT stdout_tail, cancel_requested, status FROM agent_jobs "
            "WHERE job_id = ? AND agent_session_id = ?",
            (job_id, agent_session_id),
        )
        row = cursor.fetchone()
        if not row:
            return {"ok": False, "error": "JOB_NOT_FOUND"}

        existing_tail = row[0] or ""
        # Keep the latest 50 KB of log output to prevent unbounded database bloat
        combined = (existing_tail + "\n" + stdout_chunk)[-50000:]
        cursor.execute(
            "UPDATE agent_jobs SET stdout_tail = ? "
            "WHERE job_id = ? AND agent_session_id = ? "
            "AND status IN ('RUNNING', 'CANCEL_REQUESTED')",
            (combined, job_id, agent_session_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return {"ok": False, "error": "JOB_NOT_RUNNING"}
        cursor.execute("UPDATE agent_runtime_sessions SET last_seen = ? WHERE agent_session_id = ?", (now_t, agent_session_id))
        conn.commit()

    return {"ok": True, "cancel_requested": bool(row[1]), "status": row[2]}


def register_agent_job_process(
    job_id: str,
    agent_session_id: str,
    runtime_session_secret: str,
    *,
    pid: int,
    process_start_time: float,
    command_fingerprint: str,
    workspace: str,
    recovering: bool = False,
    state_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist or recover a Local Agent process after strong installation checks.

    A restarted companion may rebind only a RECOVERY_REQUIRED job that belongs
    to the same installation and owner.  The companion is responsible for
    checking PID create-time and command/workspace before requesting recovery;
    the server records that proof for diagnostics and prevents cross-device
    adoption.
    """
    db_path = _get_db_path(state_dir)
    _init_db(db_path)
    auth = authenticate_runtime_session(agent_session_id, runtime_session_secret, state_dir=state_dir)
    if not auth:
        return {"ok": False, "error": "UNAUTHORIZED_AGENT_SESSION"}
    if int(pid) <= 0 or float(process_start_time) <= 0:
        return {"ok": False, "error": "INVALID_PROCESS_IDENTITY"}
    fingerprint = str(command_fingerprint or "").strip()
    workspace_text = str(workspace or "").strip()
    if not fingerprint or not workspace_text:
        return {"ok": False, "error": "INCOMPLETE_PROCESS_IDENTITY"}

    now_t = time.time()
    with _REGISTRY_LOCK, _db_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT j.status, j.agent_session_id, j.owner_id,
                   old.installation_id
            FROM agent_jobs j
            JOIN agent_runtime_sessions old ON old.agent_session_id = j.agent_session_id
            WHERE j.job_id = ?
            """,
            (job_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            return {"ok": False, "error": "JOB_NOT_FOUND"}
        status, old_session_id, owner_id, old_installation_id = row
        if owner_id != auth.get("owner_id") or old_installation_id != auth.get("installation_id"):
            conn.rollback()
            return {"ok": False, "error": "PROCESS_RECOVERY_NOT_AUTHORIZED"}

        if recovering:
            if status != "RECOVERY_REQUIRED":
                conn.rollback()
                return {"ok": False, "error": "JOB_NOT_RECOVERABLE", "status": status}
            changed = conn.execute(
                """
                UPDATE agent_jobs
                SET agent_session_id = ?, status = 'RUNNING', error_message = NULL,
                    process_pid = ?, process_start_time = ?,
                    process_command_fingerprint = ?, process_workspace = ?,
                    process_last_heartbeat = ?
                WHERE job_id = ? AND agent_session_id = ? AND status = 'RECOVERY_REQUIRED'
                """,
                (agent_session_id, int(pid), float(process_start_time), fingerprint,
                 workspace_text, now_t, job_id, old_session_id),
            )
        else:
            if old_session_id != agent_session_id or status != "RUNNING":
                conn.rollback()
                return {"ok": False, "error": "JOB_NOT_RUNNING", "status": status}
            changed = conn.execute(
                """
                UPDATE agent_jobs
                SET process_pid = ?, process_start_time = ?,
                    process_command_fingerprint = ?, process_workspace = ?,
                    process_last_heartbeat = ?
                WHERE job_id = ? AND agent_session_id = ? AND status = 'RUNNING'
                """,
                (int(pid), float(process_start_time), fingerprint, workspace_text,
                 now_t, job_id, agent_session_id),
            )
        if changed.rowcount != 1:
            conn.rollback()
            return {"ok": False, "error": "STATE_CONFLICT"}
        conn.commit()
    return {"ok": True, "status": "RUNNING", "recovered": bool(recovering)}


def request_agent_job_cancel(
    job_id: str,
    owner_id: str,
    state_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist cancellation; a running Agent sees it on its next heartbeat."""
    db_path = _get_db_path(state_dir)
    _init_db(db_path)
    now_t = time.time()
    with _REGISTRY_LOCK, _db_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM agent_jobs WHERE job_id=? AND owner_id=?",
            (job_id, owner_id),
        ).fetchone()
        if not row:
            conn.rollback()
            return {"ok": False, "error": "JOB_NOT_FOUND"}
        if row[0] in ("COMPLETED", "FAILED", "CANCELLED"):
            conn.rollback()
            return {"ok": True, "status": row[0], "terminal": True}
        new_state = "CANCELLED" if row[0] == "QUEUED" else "CANCEL_REQUESTED"
        changed = conn.execute(
            "UPDATE agent_jobs SET status=?, cancel_requested=1, "
            "completed_at=CASE WHEN ?='CANCELLED' THEN ? ELSE completed_at END, "
            "error_message='Cancellation requested by user' "
            "WHERE job_id=? AND owner_id=? AND status=?",
            (new_state, new_state, now_t, job_id, owner_id, row[0]),
        )
        if changed.rowcount != 1:
            conn.rollback()
            return {"ok": False, "error": "STATE_CONFLICT"}
        conn.commit()
    return {"ok": True, "status": new_state}


def list_agent_jobs(
    owner_id: str,
    state_dir: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Return the durable Local Agent queue for one authenticated owner.

    This read model deliberately comes from SQLite rather than the websocket
    registry.  A disconnected browser or Agent therefore cannot make queued
    work disappear from ``My Jobs``.
    """
    if not owner_id:
        return []
    db_path = _get_db_path(state_dir)
    _init_db(db_path)
    safe_limit = max(1, min(int(limit), 1000))
    now_t = time.time()
    with _REGISTRY_LOCK, _db_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT j.job_id, j.agent_session_id, j.job_name, j.status,
                   j.created_at, j.started_at, j.completed_at, j.exit_code,
                   j.error_message, j.workflow_id, j.step_id, j.attempt_id,
                   j.process_last_heartbeat, j.cancel_requested,
                   s.connection_state, s.last_seen AS agent_last_seen,
                   i.display_name AS device_name
            FROM agent_jobs j
            LEFT JOIN agent_runtime_sessions s
              ON s.agent_session_id = j.agent_session_id
            LEFT JOIN agent_installations i
              ON i.installation_id = s.installation_id
            WHERE j.owner_id = ?
            ORDER BY j.created_at DESC
            LIMIT ?
            """,
            (owner_id, safe_limit),
        ).fetchall()

    jobs: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if item["status"] == "QUEUED":
            last_seen = float(item.get("agent_last_seen") or 0)
            connected = item.get("connection_state") not in {
                None, "STOPPED", "SUPERSEDED", "REVOKED", "OFFLINE"
            }
            if not connected or now_t - last_seen > HEARTBEAT_TIMEOUT_SECONDS:
                item["waiting_reason"] = "Waiting for the Local Agent to reconnect."
            else:
                item["waiting_reason"] = "Waiting for the Local Agent to claim this job."
        elif item["status"] == "CANCEL_REQUESTED":
            item["waiting_reason"] = "Waiting for the Local Agent to confirm cancellation."
        else:
            item["waiting_reason"] = None
        jobs.append(item)
    return jobs


def complete_agent_job(
    job_id: str,
    agent_session_id: str,
    runtime_session_secret: str,
    exit_code: int = 0,
    stdout_tail: str = "",
    parsed_results: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
    output_text: Optional[str] = None,
    xyz_structure: Optional[str] = None,
    state_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Marks an agent job as completed or failed with full scientific results and output logs."""
    db_path = _get_db_path(state_dir)
    _init_db(db_path)
    auth = authenticate_runtime_session(agent_session_id, runtime_session_secret, state_dir=state_dir)
    if not auth:
        return {"ok": False, "error": "UNAUTHORIZED_AGENT_SESSION"}

    now_t = time.time()
    combined_output = (output_text or "") + "\n" + (stdout_tail or "")
    normal_end = "ORCA TERMINATED NORMALLY" in combined_output.upper()
    status = "COMPLETED" if exit_code == 0 and normal_end else "FAILED"
    if status == "FAILED" and exit_code == 0 and not error_message:
        error_message = "ORCA output does not contain the normal termination marker."
    results_json = json.dumps(parsed_results or {})

    with _REGISTRY_LOCK, _db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status, cancel_requested, owner_id, workflow_id, step_id, attempt_id, reaction_id "
            "FROM agent_jobs WHERE job_id = ? AND agent_session_id = ?",
            (job_id, agent_session_id),
        )
        existing = cursor.fetchone()
        if not existing:
            return {"ok": False, "error": "JOB_NOT_FOUND"}
        if existing[0] in ("COMPLETED", "FAILED", "CANCELLED"):
            # Completion delivery is idempotent; a retry after a successful
            # response must not turn into a false error or rewrite history.
            return {"ok": True, "status": existing[0]}
        if bool(existing[1]):
            status = "CANCELLED"
            error_message = error_message or "Cancellation requested by user"
        cursor.execute(
            """
            UPDATE agent_jobs
            SET status = ?, completed_at = ?, exit_code = ?,
                stdout_tail = ?, parsed_results_json = ?, error_message = ?,
                output_text = COALESCE(?, output_text),
                xyz_structure = COALESCE(?, xyz_structure)
            -- A companion may finish and deliver its result before a
            -- separate progress/claim heartbeat reaches the server (for
            -- short jobs or a transient poll response). The authenticated
            -- runtime session and exact job/session identity still fence the
            -- update; accepting QUEUED here prevents a valid result from
            -- being discarded solely because the claim acknowledgement was
            -- lost.
            WHERE job_id = ? AND agent_session_id = ?
              AND status IN ('RUNNING', 'QUEUED', 'CANCEL_REQUESTED')
            """,
            (status, now_t, exit_code, stdout_tail[-50000:], results_json, error_message, output_text, xyz_structure, job_id, agent_session_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return {"ok": False, "error": "JOB_NOT_RUNNING"}
        cursor.execute("UPDATE agent_runtime_sessions SET last_seen = ? WHERE agent_session_id = ?", (now_t, agent_session_id))
        conn.commit()

    projection = None
    if existing[3] and existing[4]:
        try:
            from services.reaction_workflow_service import apply_local_worker_result

            projection = apply_local_worker_result(
                {
                    "owner_id": existing[2],
                    "workflow_id": existing[3],
                    "step_id": existing[4],
                    "attempt_id": existing[5],
                    "reaction_id": existing[6],
                },
                {
                    "ok": status == "COMPLETED",
                    "output_text": combined_output,
                    "parsed": parsed_results or {},
                    "exit_code": exit_code,
                    "error_code": "CANCELLED" if status == "CANCELLED" else (
                        None if status == "COMPLETED" else "EXECUTION_FAILED"
                    ),
                    "error": error_message,
                },
                state_dir or os.path.dirname(db_path),
            )
            projection_status = "APPLIED" if projection.get("ok") else "PENDING"
        except Exception as exc:  # noqa: BLE001
            projection = {"ok": False, "error": type(exc).__name__}
            projection_status = "PENDING"
        with _REGISTRY_LOCK, _db_connection(db_path) as conn:
            conn.execute(
                "UPDATE agent_jobs SET projection_status=? WHERE job_id=? AND projection_status='PENDING'",
                (projection_status, job_id),
            )
            conn.commit()

    return {"ok": True, "status": status, "workflow_projection": projection}


def reconcile_agent_workflow_projections(state_dir: Optional[str] = None) -> Dict[str, int]:
    """Replay durable terminal Agent results into workflow state after restart."""
    db_path = _get_db_path(state_dir)
    _init_db(db_path)
    with _REGISTRY_LOCK, _db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT job_id, owner_id, workflow_id, step_id, attempt_id, reaction_id, "
            "status, exit_code, output_text, stdout_tail, parsed_results_json, error_message "
            "FROM agent_jobs WHERE projection_status='PENDING' "
            "AND status IN ('COMPLETED','FAILED','CANCELLED')"
        ).fetchall()
    applied = failed = 0
    for row in rows:
        try:
            from services.reaction_workflow_service import apply_local_worker_result

            parsed = json.loads(row[10] or "{}")
            result = apply_local_worker_result(
                {
                    "owner_id": row[1], "workflow_id": row[2], "step_id": row[3],
                    "attempt_id": row[4], "reaction_id": row[5],
                },
                {
                    "ok": row[6] == "COMPLETED",
                    "output_text": (row[8] or "") + "\n" + (row[9] or ""),
                    "parsed": parsed, "exit_code": row[7],
                    "error_code": "CANCELLED" if row[6] == "CANCELLED" else (
                        None if row[6] == "COMPLETED" else "EXECUTION_FAILED"
                    ),
                    "error": row[11],
                },
                state_dir or os.path.dirname(db_path),
            )
            if not result.get("ok"):
                failed += 1
                continue
            with _REGISTRY_LOCK, _db_connection(db_path) as conn:
                conn.execute(
                    "UPDATE agent_jobs SET projection_status='APPLIED' "
                    "WHERE job_id=? AND projection_status='PENDING'",
                    (row[0],),
                )
                conn.commit()
            applied += 1
        except Exception:
            failed += 1
    return {"examined": len(rows), "applied": applied, "failed": failed}


def get_agent_job_status(
    job_id: str,
    owner_id: Optional[str] = None,
    state_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Retrieves current job status, execution logs, and scientific results."""
    db_path = _get_db_path(state_dir)
    _init_db(db_path)

    with _REGISTRY_LOCK, _db_connection(db_path) as conn:
        cursor = conn.cursor()
        if owner_id:
            cursor.execute(
                """
                SELECT job_id, agent_session_id, owner_id, job_name, status,
                       created_at, started_at, completed_at, exit_code,
                       stdout_tail, parsed_results_json, error_message
                FROM agent_jobs
                WHERE job_id = ? AND owner_id = ?
                """,
                (job_id, owner_id),
            )
        else:
            cursor.execute(
                """
                SELECT job_id, agent_session_id, owner_id, job_name, status,
                       created_at, started_at, completed_at, exit_code,
                       stdout_tail, parsed_results_json, error_message
                FROM agent_jobs
                WHERE job_id = ?
                """,
                (job_id,),
            )
        row = cursor.fetchone()
        if not row:
            return {"ok": False, "error": "JOB_NOT_FOUND"}

        (
            j_id, a_sess_id, o_id, j_name, status,
            c_at, s_at, comp_at, exit_code,
            tail, results_json, err_msg
        ) = row

        parsed = {}
        try:
            parsed = json.loads(results_json) if results_json else {}
        except Exception:
            pass

        return {
            "ok": True,
            "job_id": j_id,
            "agent_session_id": a_sess_id,
            "owner_id": o_id,
            "job_name": j_name,
            "status": status,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(c_at)) if c_at else None,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(s_at)) if s_at else None,
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(comp_at)) if comp_at else None,
            "exit_code": exit_code,
            "stdout_tail": tail or "",
            "parsed_results": parsed,
            "error_message": err_msg,
        }




def get_job_artifacts(
    job_id: str,
    owner_id: Optional[str] = None,
    state_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Retrieves full job artifacts including input, output, and geometry for download."""
    db_path = _get_db_path(state_dir)
    _init_db(db_path)

    with _REGISTRY_LOCK, _db_connection(db_path) as conn:
        cursor = conn.cursor()
        query = """
            SELECT job_id, agent_session_id, owner_id, job_name, status,
                   input_text, output_text, xyz_structure, stdout_tail,
                   exit_code, parsed_results_json, error_message
            FROM agent_jobs
            WHERE job_id = ?
        """
        params = [job_id]
        if owner_id:
            query += " AND owner_id = ?"
            params.append(owner_id)

        cursor.execute(query, tuple(params))
        row = cursor.fetchone()
        if not row:
            return {"ok": False, "error": "JOB_NOT_FOUND"}

        (
            j_id, a_sess, o_id, j_name, status,
            inp, out, xyz, tail,
            exit_code, res_json, err
        ) = row

        return {
            "ok": True,
            "job_id": j_id,
            "job_name": j_name,
            "status": status,
            "input_text": inp or "",
            "output_text": out or tail or "",
            "xyz_structure": xyz or "",
            "stdout_tail": tail or "",
            "exit_code": exit_code,
            "error_message": err,
        }


def build_job_artifacts_zip(
    job_id: str,
    owner_id: Optional[str] = None,
    state_dir: Optional[str] = None,
) -> tuple[bytes, str]:
    """Creates a downloadable ZIP archive containing the job's input, output, and 3D geometry."""
    import io
    import zipfile
    art = get_job_artifacts(job_id=job_id, owner_id=owner_id, state_dir=state_dir)
    if not art.get("ok"):
        raise ValueError(art.get("error", "Job not found"))

    buf = io.BytesIO()
    j_name = art.get("job_name") or "job"
    safe_name = re.sub(r'[^A-Za-z0-9_\-\.]', '_', j_name)

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if art.get("input_text"):
            zf.writestr(f"{safe_name}.inp", art["input_text"])
        if art.get("output_text"):
            zf.writestr(f"{safe_name}.out", art["output_text"])
        elif art.get("stdout_tail"):
            zf.writestr(f"{safe_name}.log", art["stdout_tail"])
        if art.get("xyz_structure"):
            zf.writestr(f"{safe_name}.xyz", art["xyz_structure"])

    return buf.getvalue(), f"{safe_name}_{job_id[:8]}.zip"
