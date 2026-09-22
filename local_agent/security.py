# -*- coding: utf-8 -*-
"""Local Agent Ephemeral Token Generation, Process Identity, and Single-Instance Lock.

CRITICAL SECURITY RULES:
- Every Agent process launch generates a NEW cryptographically random Connection API.
- The installation proof is persistent, private local state; ephemeral pairing
  and runtime credentials are never persisted in plaintext by the server.
- Single active process per installation directory enforced via cross-process file lock.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import secrets
import stat
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

TOKEN_PREFIX = "CLA_"


class AgentAlreadyRunningError(Exception):
    """Raised when another Agent instance is already running for the same data directory."""
    pass


class ProcessLock:
    """Cross-process lock preventing two Agent instances on the same installation folder."""
    def __init__(self, data_dir: Path):
        self.lock_file = data_dir / "agent.lock"
        self._fd: Optional[int] = None
        self._locked = False

    def acquire(self) -> bool:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            if platform.system() == "Windows":
                import msvcrt
                # Open with read/write mode (creating if not exist)
                self._fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR | os.O_BINARY, 0o600)
                # Try non-blocking exclusive lock on first byte
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                # Write current PID into file
                os.lseek(self._fd, 0, os.SEEK_SET)
                os.write(self._fd, f"{os.getpid()}\n".encode("utf-8"))
                self._locked = True
                return True
            else:
                import fcntl
                self._fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR, 0o600)
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.lseek(self._fd, 0, os.SEEK_SET)
                os.write(self._fd, f"{os.getpid()}\n".encode("utf-8"))
                self._locked = True
                return True
        except (IOError, OSError):
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except Exception:
                    pass
                self._fd = None
            return False

    def release(self) -> None:
        if self._locked and self._fd is not None:
            try:
                if platform.system() == "Windows":
                    import msvcrt
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(self._fd)
            except Exception:
                pass
            self._fd = None
            try:
                if self.lock_file.is_file():
                    self.lock_file.unlink()
            except Exception:
                pass
            self._locked = False


def hash_token_verifier(token: str) -> str:
    """Computes SHA-256 verifier hash of the connection token for safe registration."""
    clean = token.strip()
    return hashlib.sha256(f"chemlab_verifier_salt_{clean}".encode("utf-8")).hexdigest()


def redact_token(token: str) -> str:
    """Redacts a connection API for safe logging, e.g. CLA_4H7...a9Z."""
    clean = (token or "").strip()
    if len(clean) <= 10:
        return "CLA_***"
    return f"{clean[:7]}...{clean[-4:]}"


def _write_private_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    """Write security state without following links or exposing partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"refusing to write installation credentials through symlink: {path}")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(tmp), flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def load_or_create_installation_credentials(data_dir: Path) -> Tuple[str, str]:
    """Loads persistent (installation_id, installation_secret) or creates them if fresh install."""
    data_dir.mkdir(parents=True, exist_ok=True)
    inst_file = data_dir / "installation_id.json"
    if inst_file.is_symlink():
        raise RuntimeError("installation credential file must not be a symlink")
    if inst_file.exists():
        if not inst_file.is_file():
            raise RuntimeError("installation credential path is not a regular file")
        try:
            with open(inst_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                inst_id = data.get("installation_id")
                inst_sec = data.get("installation_secret")
                if inst_id and isinstance(inst_id, str):
                    if not inst_sec or not isinstance(inst_sec, str):
                        inst_sec = secrets.token_hex(32)
                        data["installation_secret"] = inst_sec
                        _write_private_json_atomic(inst_file, data)
                    return inst_id, inst_sec
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            # Rotating the installation identity silently would orphan server
            # records and can let a corrupted file bypass ownership checks.
            raise RuntimeError("installation credential file is unreadable or corrupt") from exc
        raise RuntimeError("installation credential file is missing a valid installation_id")

    new_inst_id = f"inst_{uuid.uuid4().hex}"
    new_inst_sec = secrets.token_hex(32)
    _write_private_json_atomic(inst_file, {
        "installation_id": new_inst_id,
        "installation_secret": new_inst_sec,
        "created_at": time.time(),
    })
    return new_inst_id, new_inst_sec


def load_or_create_installation_id(data_dir: Path) -> str:
    """Loads persistent non-secret installation_id or creates one if fresh install."""
    inst_id, _ = load_or_create_installation_credentials(data_dir)
    return inst_id


def generate_process_session() -> Tuple[str, str]:
    """Generates (agent_session_id, connection_api) with >= 256 bits entropy for the current process."""
    session_id = f"sess_{uuid.uuid4().hex}"
    raw_secret = secrets.token_urlsafe(32)  # 256 bits of cryptographic entropy
    connection_api = f"{TOKEN_PREFIX}{raw_secret}"
    return session_id, connection_api


def generate_process_tokens(token_count: int = 1) -> Tuple[str, List[str]]:
    """Generates agent_session_id and 1..10 independently random ephemeral Connection APIs."""
    count = max(1, min(10, int(token_count)))
    session_id = f"sess_{uuid.uuid4().hex}"
    tokens: List[str] = []
    for _ in range(count):
        raw_secret = secrets.token_urlsafe(32)
        tokens.append(f"{TOKEN_PREFIX}{raw_secret}")
    return session_id, tokens


def write_pairing_txt(
    file_path: Path,
    device_name: str,
    installation_id: str,
    agent_session_id: str,
    connection_api: str,
) -> None:
    """Writes temporary chemistry_lab_pairing.txt for the CURRENT process only."""
    content = f"""===============================================================
CHEMISTRY LAB LOCAL AGENT - EPHEMERAL CONNECTION API
===============================================================

Device Name:
    {device_name}

Installation ID:
    {installation_id}

Agent Session ID:
    {agent_session_id}

Connection API:
    {connection_api}

Generated:
    {time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}

Instructions:
    1. Open Chemistry Lab website.
    2. Go to: Local & HPC Execution -> Connect Computer.
    3. Enter the Connection API above and click Connect.

IMPORTANT:
    This API is valid ONLY while THIS Agent process is running.
    If you close or restart the Agent, this API becomes invalid
    and a NEW API will be generated.
===============================================================
"""
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        # Do not follow a pre-created symlink and do not briefly expose the
        # ephemeral connection token with the caller's default umask.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(str(file_path), flags, 0o600)
        except FileExistsError:
            # The single-instance process lock makes replacement safe.  Remove
            # only a regular file owned by this pairing path; refuse links.
            if file_path.is_symlink() or not file_path.is_file():
                raise
            file_path.unlink()
            fd = os.open(str(file_path), flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)

        try:
            os.chmod(file_path, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass
    except Exception:
        pass


def remove_pairing_txt(file_path: Path) -> None:
    """Deletes temporary chemistry_lab_pairing.txt on process exit."""
    try:
        if file_path.is_file():
            file_path.unlink()
    except Exception:
        pass
