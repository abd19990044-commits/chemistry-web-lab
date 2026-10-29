"""Authenticated encryption for saved connection settings, never for browser storage."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def connection_key(state_dir: str) -> bytes:
    from orca_orchestrator.credential_vault import MASTER_KEY_ENV_VAR, parse_master_key

    explicit = os.environ.get(MASTER_KEY_ENV_VAR)
    if explicit:
        parsed = parse_master_key(explicit)
        if parsed is None:
            raise ValueError("Invalid connection encryption key configuration")
        return parsed
    secret = os.environ.get("SECRET_KEY") or os.environ.get("FLASK_SECRET_KEY")
    if secret:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"orca-web-lab/connection-vault/v1").derive(
            secret.encode("utf-8")
        )
    if (
        os.environ.get("SPACE_ID")
        or os.environ.get("HF_SPACE_ID")
        or os.environ.get("CHEMISTRY_LAB_ENV") == "production"
    ):
        raise ValueError("Production connection storage requires a configured encryption key or SECRET_KEY")
    # Standalone installations use a private installation key, shared by workers.
    # Keep this file when upgrading; copying the database alone reveals no secrets.
    base = Path(state_dir)
    base.mkdir(parents=True, exist_ok=True)
    key_path = base / "connection_vault.key"
    from services.local_orca_service import CrossProcessFileLock

    with CrossProcessFileLock(str(base / ".connection-key.lock")):
        if not key_path.exists():
            fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(os.urandom(32))
                stream.flush()
                os.fsync(stream.fileno())
        key = key_path.read_bytes()
    if len(key) != 32:
        raise ValueError("Invalid installation encryption key")
    return key


def seal_settings(data: dict, state_dir: str, purpose: str) -> dict:
    nonce = os.urandom(12)
    ciphertext = AESGCM(connection_key(state_dir)).encrypt(
        nonce, json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"), purpose.encode("utf-8")
    )
    return {
        "encrypted_connection_settings": 1,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }


def open_settings(envelope: dict, state_dir: str, purpose: str) -> dict:
    if envelope.get("encrypted_connection_settings") != 1:
        return envelope  # Read compatibility; the next save migrates legacy settings.
    plaintext = AESGCM(connection_key(state_dir)).decrypt(
        base64.b64decode(envelope["nonce"], validate=True),
        base64.b64decode(envelope["ciphertext"], validate=True),
        purpose.encode("utf-8"),
    )
    data = json.loads(plaintext)
    if not isinstance(data, dict):
        raise ValueError("Invalid connection settings")
    return data
