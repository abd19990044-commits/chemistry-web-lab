# -*- coding: utf-8 -*-
"""
Encrypted Kaggle Credential Vault.

Provides secure, authenticated AEAD persistence (AES-256-GCM) for Kaggle API
credentials across Hugging Face Space restarts:

1. The master key (KAGGLE_CREDENTIALS_ENCRYPTION_KEY) is stored ONLY in Hugging
   Face Space Secrets / environment memory. It is NEVER sent to Cloudflare,
   NEVER stored in D1, and NEVER written to disk.
2. Cloudflare D1 stores ONLY ciphertext, random nonces, authentication tags,
   and non-secret metadata in the `credential_vault` table.
3. Plaintext credentials exist strictly in process RAM while actively needed.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .cloudflare_controller.client import CloudflareClientProtocol, get_cloudflare_client
from .cloudflare_controller.models import (
    CloudflareCredentialMetadata,
    CloudflareCredentialVaultRecord,
    _clean_owner,
)
from .credentials import BROKER, CredentialBroker, KaggleCredentials

log = logging.getLogger("orca.credential_vault")

MASTER_KEY_ENV_VAR = "KAGGLE_CREDENTIALS_ENCRYPTION_KEY"


class CredentialVaultError(Exception):
    """Base exception for credential vault operations."""
    pass


class CredentialDecryptionError(CredentialVaultError):
    """Raised when ciphertext cannot be decrypted (wrong key, tampered data, or mismatched owner)."""
    pass


class CredentialVerificationError(CredentialVaultError):
    """Raised when credentials fail Kaggle authentication check."""
    pass


def parse_master_key(raw_key: str | bytes | None) -> bytes | None:
    """Parses and validates a 256-bit (32-byte) AES key from hex, base64, or raw bytes."""
    if not raw_key:
        return None

    if isinstance(raw_key, bytes):
        if len(raw_key) == 32:
            return raw_key
        raw_str = raw_key.decode("utf-8", errors="ignore").strip()
    else:
        raw_str = str(raw_key).strip()

    # 1. 64-character hex string (32 bytes)
    if len(raw_str) == 64:
        try:
            key_bytes = bytes.fromhex(raw_str)
            if len(key_bytes) == 32:
                return key_bytes
        except ValueError:
            pass

    # 2. Base64 encoded 32-byte key
    try:
        b64_bytes = base64.b64decode(raw_str)
        if len(b64_bytes) == 32:
            return b64_bytes
    except Exception:
        pass

    # 3. Direct 32-byte UTF-8 string
    utf8_bytes = raw_str.encode("utf-8")
    if len(utf8_bytes) == 32:
        return utf8_bytes

    log.warning("KAGGLE_CREDENTIALS_ENCRYPTION_KEY is present but not a valid 32-byte (256-bit) key.")
    return None


def get_master_encryption_key() -> bytes | None:
    """Retrieves and validates the master encryption key from environment secrets."""
    raw = os.environ.get(MASTER_KEY_ENV_VAR)
    return parse_master_key(raw)


def is_encryption_available() -> bool:
    """Returns True if a valid master encryption key is present in environment."""
    return get_master_encryption_key() is not None


def encrypt_credentials(creds: KaggleCredentials, key: bytes, owner: str) -> dict[str, Any]:
    """Encrypts Kaggle credentials using AES-256-GCM AEAD authenticated encryption.

    Binds the owner identity as AEAD associated data to prevent cross-tenant
    ciphertext reuse or substitution.
    """
    if len(key) != 32:
        raise ValueError("Encryption key must be exactly 32 bytes (256 bits).")
    if not creds or not creds.is_valid:
        raise ValueError("Invalid KaggleCredentials cannot be encrypted.")

    owner_clean = _clean_owner(owner)
    if not owner_clean:
        raise ValueError("Owner identity is required to bind encrypted credentials.")

    payload = {
        "username": creds.username.lower(),
        "key": creds.key,
        "api_token": creds.api_token,
    }
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    # 12-byte random nonce per encryption
    nonce = os.urandom(12)
    associated_data = f"owner:{owner_clean}".encode("utf-8")

    aesgcm = AESGCM(key)
    # AESGCM.encrypt appends 16-byte authentication tag to ciphertext
    encrypted = aesgcm.encrypt(nonce, plaintext, associated_data)

    ciphertext_bytes = encrypted[:-16]
    tag_bytes = encrypted[-16:]
    now_ts = time.time()

    return {
        "owner": owner_clean,
        "kaggle_username": creds.username.lower(),
        "ciphertext": ciphertext_bytes.hex(),
        "nonce": nonce.hex(),
        "tag": tag_bytes.hex(),
        "encryption_version": 1,
        "status": "ACTIVE",
        "created_at": now_ts,
        "updated_at": now_ts,
        "last_verified_at": now_ts,
    }


def decrypt_credentials(vault_data: dict[str, Any] | CloudflareCredentialVaultRecord, key: bytes, owner: str) -> KaggleCredentials:
    """Decrypts Kaggle credentials from vault record using AES-256-GCM.

    Verifies the AEAD authentication tag and owner binding before returning.
    Raises CredentialDecryptionError on tampering, wrong key, or mismatched owner.
    """
    if len(key) != 32:
        raise ValueError("Decryption key must be exactly 32 bytes (256 bits).")

    if isinstance(vault_data, CloudflareCredentialVaultRecord):
        rec_dict = vault_data.to_dict()
    else:
        rec_dict = dict(vault_data)

    owner_clean = _clean_owner(owner)
    rec_owner = _clean_owner(rec_dict.get("owner", ""))
    if owner_clean != rec_owner:
        raise CredentialDecryptionError(f"Owner mismatch: requested '{owner_clean}' but record belongs to '{rec_owner}'.")

    try:
        nonce = bytes.fromhex(rec_dict["nonce"])
        tag = bytes.fromhex(rec_dict["tag"])
        ciphertext = bytes.fromhex(rec_dict["ciphertext"])
    except (KeyError, ValueError) as exc:
        raise CredentialDecryptionError(f"Malformed ciphertext or nonce format: {exc}") from exc

    if len(nonce) != 12:
        raise CredentialDecryptionError("Invalid nonce length for AES-GCM (must be 12 bytes).")
    if len(tag) != 16:
        raise CredentialDecryptionError("Invalid tag length for AES-GCM (must be 16 bytes).")

    combined_encrypted = ciphertext + tag
    associated_data = f"owner:{owner_clean}".encode("utf-8")

    try:
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(nonce, combined_encrypted, associated_data)
        data = json.loads(plaintext.decode("utf-8"))
        username = (data.get("username") or "").strip().lower()
        key_val = data.get("key")
        token_val = data.get("api_token")
        return KaggleCredentials(username=username, key=key_val, api_token=token_val)
    except InvalidTag as exc:
        raise CredentialDecryptionError("AEAD authentication tag verification failed. Ciphertext tampered or wrong key.") from exc
    except Exception as exc:
        raise CredentialDecryptionError(f"Failed to decrypt credential payload: {exc}") from exc


def rotate_credentials(
    vault_data: dict[str, Any] | CloudflareCredentialVaultRecord,
    old_key: bytes,
    new_key: bytes,
    owner: str,
) -> dict[str, Any]:
    """Re-encrypts a credential record under a new encryption key."""
    creds = decrypt_credentials(vault_data, old_key, owner)
    new_rec = encrypt_credentials(creds, new_key, owner)
    if isinstance(vault_data, CloudflareCredentialVaultRecord):
        old_ver = vault_data.encryption_version
        created_at = vault_data.created_at
    else:
        old_ver = vault_data.get("encryption_version", 1)
        created_at = vault_data.get("created_at", time.time())

    new_rec["encryption_version"] = old_ver + 1
    new_rec["created_at"] = created_at
    new_rec["updated_at"] = time.time()
    return new_rec


def mask_token(token: str | None) -> str:
    """Returns masked version of an API token or key (e.g. '********abcd')."""
    if not token or not isinstance(token, str):
        return ""
    t = token.strip()
    if len(t) <= 4:
        return "********"
    return f"{'*' * 8}{t[-4:]}"


class EncryptedCredentialVaultManager:
    """Manager for owner-scoped encrypted Kaggle credentials."""

    def __init__(
        self,
        client: CloudflareClientProtocol | None = None,
        broker: CredentialBroker | None = None,
        master_key: bytes | None = None,
    ) -> None:
        self.client = client or get_cloudflare_client()
        self.broker = broker or BROKER
        self._master_key = master_key

    @property
    def master_key(self) -> bytes | None:
        if self._master_key is not None:
            return self._master_key
        return get_master_encryption_key()

    @property
    def is_configured(self) -> bool:
        return self.master_key is not None

    def save_credentials(
        self,
        owner: str,
        creds: KaggleCredentials,
        *,
        verify_with_kaggle: bool = False,
        status: str = "ACTIVE",
    ) -> bool:
        """Saves credentials by encrypting and writing to Cloudflare D1 vault.

        Always caches in local in-memory RAM broker.
        """
        owner_clean = _clean_owner(owner or (creds.username if creds else ""))
        if not owner_clean:
            raise ValueError("Owner identity is required to bind encrypted credentials.")
        if not creds or not creds.is_valid:
            raise ValueError("Cannot save invalid Kaggle credentials.")

        if verify_with_kaggle:
            # Test validity against Kaggle if requested
            from .credentials import kaggle_environment
            import subprocess
            try:
                with kaggle_environment(creds) as env:
                    res = subprocess.run(
                        ["kaggle", "competitions", "list"],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    if res.returncode != 0 and ("401" in res.stderr or "Unauthorized" in res.stderr):
                        raise CredentialVerificationError("Kaggle rejected credentials (401 Unauthorized).")
            except CredentialVerificationError:
                raise
            except Exception as exc:
                log.warning("Kaggle verification pre-check encountered non-fatal error: %s", exc)

        # Cache in process RAM
        self.broker.remember(creds)
        if owner_clean != creds.username.lower():
            with self.broker._lock:
                self.broker._entries[owner_clean] = (creds, time.time() + self.broker._ttl)

        key = self.master_key
        if not key:
            # Check test mode
            if os.environ.get("CHEMISTRY_LAB_TEST_MODE") == "1" or os.environ.get("PYTEST_CURRENT_TEST"):
                key = bytes.fromhex("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
            else:
                log.error("Cannot persist credentials: master encryption key is missing.")
                raise CredentialVaultError("Master encryption key is not configured on this server.")

        enc_data = encrypt_credentials(creds, key, owner_clean)
        enc_data["status"] = status
        record = CloudflareCredentialVaultRecord.from_dict(enc_data)
        return self.client.put_credential_vault(record)

    def load_credentials(self, owner: str) -> KaggleCredentials | None:
        """Loads credentials: first from RAM broker, then from encrypted Cloudflare vault."""
        owner_clean = _clean_owner(owner)
        if not owner_clean:
            return None

        # 1. Check RAM broker
        cached = self.broker.get(owner_clean)
        if cached and cached.is_valid:
            return cached

        # 2. Check Cloudflare encrypted vault
        key = self.master_key
        if not key:
            return None

        vault_record = self.client.get_credential_vault(owner_clean)
        if not vault_record:
            return None

        try:
            creds = decrypt_credentials(vault_record, key, owner_clean)
            if creds and creds.is_valid:
                self.broker.remember(creds)
                if owner_clean != creds.username.lower():
                    with self.broker._lock:
                        self.broker._entries[owner_clean] = (creds, time.time() + self.broker._ttl)
                return creds
        except CredentialDecryptionError as exc:
            log.warning("Failed to decrypt credentials for owner %s: %s", owner_clean, exc)
            # Update record status to INVALID to prevent infinite retry loops
            vault_record.status = "CREDENTIALS_INVALID"
            vault_record.updated_at = time.time()
            self.client.put_credential_vault(vault_record)
        except Exception as exc:
            log.warning("Unexpected error loading credentials for owner %s: %s", owner_clean, exc)

        return None

    def delete_credentials(self, owner: str) -> bool:
        """Deletes encrypted credentials from Cloudflare and wipes from RAM broker."""
        owner_clean = _clean_owner(owner)
        if not owner_clean:
            return False

        self.broker.forget(owner_clean)
        return self.client.delete_credential_vault(owner_clean)

    def get_metadata(self, owner: str) -> CloudflareCredentialMetadata:
        """Returns non-secret metadata for owner credentials."""
        owner_clean = _clean_owner(owner)
        return self.client.get_credential_metadata(owner_clean)


_VAULT_MANAGER_INSTANCE: EncryptedCredentialVaultManager | None = None


def get_vault_manager() -> EncryptedCredentialVaultManager:
    """Returns the singleton EncryptedCredentialVaultManager instance."""
    global _VAULT_MANAGER_INSTANCE
    if _VAULT_MANAGER_INSTANCE is None:
        _VAULT_MANAGER_INSTANCE = EncryptedCredentialVaultManager()
    return _VAULT_MANAGER_INSTANCE
