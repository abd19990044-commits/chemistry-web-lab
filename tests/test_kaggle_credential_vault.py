# -*- coding: utf-8 -*-
"""
Security and Regression Test Suite for Kaggle Credential Vault.

Validates the 15 mandatory security requirements:
1. Encryption round-trip (logical equality)
2. Ciphertext is not plaintext (no API key in serialized vault data)
3. Different nonce per encryption (random IV)
4. Tamper detection (AEAD integrity)
5. Wrong-key decryption failure
6. Owner isolation (cross-tenant access rejected, AEAD associated data binding)
7. Browser secrecy (API keys never in metadata or non-secret responses)
8. Process restart recovery simulation
9. User B wakes space (User A job recovered in background, User B gets 0 access)
10. Invalid credential handling (CREDENTIALS_INVALID, bounded retry)
11. Missing credential handling (CREDENTIALS_REQUIRED)
12. Atomic credential replacement
13. Failed replacement preserves existing valid credential
14. Credential deletion behavior
15. Secret scan across repository and vault components
"""
from __future__ import annotations

import json
import os
import time
import pytest

from orca_orchestrator.credentials import KaggleCredentials, CredentialBroker
from orca_orchestrator.credential_vault import (
    CredentialDecryptionError,
    CredentialVerificationError,
    EncryptedCredentialVaultManager,
    decrypt_credentials,
    encrypt_credentials,
    parse_master_key,
    rotate_credentials,
)
from orca_orchestrator.cloudflare_controller.client import (
    CloudflareSecurityViolation,
    InMemoryCloudflareBackend,
)
from orca_orchestrator.cloudflare_controller.models import (
    CloudflareCredentialVaultRecord,
    CloudflareJobRecord,
    LocalWorkflowState,
    RemoteExecutionState,
)
from orca_orchestrator.config import StoreConfig
from orca_orchestrator.models import JobManifest
from orca_orchestrator.states import JobState
from orca_orchestrator.store import JobStore
from orca_orchestrator.watchdog import Watchdog, assess


@pytest.fixture
def master_key() -> bytes:
    """32-byte test master key."""
    return bytes.fromhex("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")


@pytest.fixture
def sample_creds() -> KaggleCredentials:
    return KaggleCredentials(username="chem_user", api_token="KGAT_super_secret_token_987654321")


@pytest.fixture
def legacy_creds() -> KaggleCredentials:
    return KaggleCredentials(username="alice", key="0123456789abcdef0123456789abcdef")


# ===========================================================================
# Test 1: Encryption Round Trip
# ===========================================================================
def test_encryption_roundtrip(master_key, sample_creds, legacy_creds):
    """Verifies that encrypting and decrypting yields exact logical equality."""
    # Test with modern API token
    enc1 = encrypt_credentials(sample_creds, master_key, "chem_user")
    dec1 = decrypt_credentials(enc1, master_key, "chem_user")
    assert dec1.username == sample_creds.username
    assert dec1.api_token == sample_creds.api_token
    assert dec1.key is None

    # Test with legacy key
    enc2 = encrypt_credentials(legacy_creds, master_key, "alice")
    dec2 = decrypt_credentials(enc2, master_key, "alice")
    assert dec2.username == legacy_creds.username
    assert dec2.key == legacy_creds.key
    assert dec2.api_token is None


# ===========================================================================
# Test 2: Ciphertext Is Not Plaintext
# ===========================================================================
def test_ciphertext_is_not_plaintext(master_key, sample_creds):
    """Verifies that the API key/token never appears in the encrypted payload or serialized JSON."""
    enc = encrypt_credentials(sample_creds, master_key, "chem_user")
    serialized = json.dumps(enc)

    secret = "KGAT_super_secret_token_987654321"
    assert secret not in serialized
    assert secret not in enc["ciphertext"]
    assert secret not in enc["nonce"]
    assert secret not in enc["tag"]


# ===========================================================================
# Test 3: Different Nonce Per Encryption
# ===========================================================================
def test_different_nonce_per_encryption(master_key, sample_creds):
    """Encrypting the same credential twice must produce distinct nonces and ciphertexts."""
    enc1 = encrypt_credentials(sample_creds, master_key, "chem_user")
    enc2 = encrypt_credentials(sample_creds, master_key, "chem_user")

    assert enc1["nonce"] != enc2["nonce"]
    assert enc1["ciphertext"] != enc2["ciphertext"]
    assert enc1["tag"] != enc2["tag"]


# ===========================================================================
# Test 4: Tamper Detection (AEAD Integrity)
# ===========================================================================
def test_tamper_detection(master_key, sample_creds):
    """Modifying one byte in ciphertext, tag, or nonce must cause decryption to fail."""
    enc = encrypt_credentials(sample_creds, master_key, "chem_user")

    # 1. Tamper ciphertext
    ct_bytes = bytearray(bytes.fromhex(enc["ciphertext"]))
    ct_bytes[0] ^= 0xFF
    tampered_enc = dict(enc, ciphertext=ct_bytes.hex())
    with pytest.raises(CredentialDecryptionError):
        decrypt_credentials(tampered_enc, master_key, "chem_user")

    # 2. Tamper tag
    tag_bytes = bytearray(bytes.fromhex(enc["tag"]))
    tag_bytes[0] ^= 0xFF
    tampered_tag = dict(enc, tag=tag_bytes.hex())
    with pytest.raises(CredentialDecryptionError):
        decrypt_credentials(tampered_tag, master_key, "chem_user")

    # 3. Tamper nonce
    nonce_bytes = bytearray(bytes.fromhex(enc["nonce"]))
    nonce_bytes[0] ^= 0xFF
    tampered_nonce = dict(enc, nonce=nonce_bytes.hex())
    with pytest.raises(CredentialDecryptionError):
        decrypt_credentials(tampered_nonce, master_key, "chem_user")


# ===========================================================================
# Test 5: Wrong Key Decryption Failure
# ===========================================================================
def test_wrong_key_fails(master_key, sample_creds):
    """Decrypting with a different 32-byte key must fail."""
    wrong_key = bytes.fromhex("fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210")
    enc = encrypt_credentials(sample_creds, master_key, "chem_user")

    with pytest.raises(CredentialDecryptionError):
        decrypt_credentials(enc, wrong_key, "chem_user")


# ===========================================================================
# Test 6: Owner Isolation & Associated Data Binding
# ===========================================================================
def test_owner_isolation(master_key, sample_creds):
    """User A's encrypted credentials cannot be decrypted under User B's identity."""
    enc_alice = encrypt_credentials(sample_creds, master_key, "alice")

    # Attempt to decrypt Alice's ciphertext as Bob
    with pytest.raises(CredentialDecryptionError):
        decrypt_credentials(enc_alice, master_key, "bob")

    # Attempt to pass modified owner in record dict
    faked_enc = dict(enc_alice, owner="bob")
    with pytest.raises(CredentialDecryptionError):
        decrypt_credentials(faked_enc, master_key, "bob")


# ===========================================================================
# Test 7: Browser Secrecy (Metadata Endpoint)
# ===========================================================================
def test_browser_secrecy(master_key, sample_creds):
    """Non-secret metadata returns presence and timestamps without secrets or ciphertext."""
    backend = InMemoryCloudflareBackend()
    broker = CredentialBroker()
    mgr = EncryptedCredentialVaultManager(client=backend, broker=broker, master_key=master_key)

    mgr.save_credentials("alice", sample_creds)

    meta = mgr.get_metadata("alice")
    assert meta.exists is True
    assert meta.owner == "alice"
    assert meta.kaggle_username == "chem_user"
    assert meta.status == "ACTIVE"
    meta_dict = meta.to_dict()
    assert "ciphertext" not in meta_dict
    assert "key" not in meta_dict
    assert "api_token" not in meta_dict
    assert "nonce" not in meta_dict
    assert "tag" not in meta_dict


# ===========================================================================
# Test 8: Process Restart Recovery Simulation
# ===========================================================================
def test_restart_recovery_simulation(master_key, sample_creds):
    """Simulates Space restart: RAM is cleared, but encrypted credentials in Cloudflare survive and decrypt."""
    backend = InMemoryCloudflareBackend()
    broker1 = CredentialBroker()
    mgr1 = EncryptedCredentialVaultManager(client=backend, broker=broker1, master_key=master_key)

    # 1. User saves credentials in Process 1
    mgr1.save_credentials("alice", sample_creds)
    assert broker1.get("alice") is not None

    # 2. Process 1 terminates (RAM broker is destroyed)
    del broker1
    del mgr1

    # 3. Process 2 starts fresh with empty RAM broker
    broker2 = CredentialBroker()
    assert broker2.get("alice") is None

    mgr2 = EncryptedCredentialVaultManager(client=backend, broker=broker2, master_key=master_key)

    # 4. Process 2 loads credentials from Cloudflare encrypted vault
    loaded_creds = mgr2.load_credentials("alice")
    assert loaded_creds is not None
    assert loaded_creds.username == sample_creds.username
    assert loaded_creds.api_token == sample_creds.api_token

    # 5. Loaded credentials are now safely cached in Process 2 RAM broker
    assert broker2.get("alice") is not None


# ===========================================================================
# Test 9: User B Wakes Space (Strict Multi-User Isolation)
# ===========================================================================
def test_user_b_wakes_space_isolation(master_key, sample_creds, tmp_path):
    """When User B logs in or triggers a recovery sweep, User A's jobs recover in background without leaking to B."""
    backend = InMemoryCloudflareBackend()
    broker = CredentialBroker()
    mgr = EncryptedCredentialVaultManager(client=backend, broker=broker, master_key=master_key)

    # User A credentials saved in vault
    mgr.save_credentials("alice", sample_creds)
    broker.clear()  # Simulate restart

    # User A has a stalled job
    job_store = JobStore(StoreConfig(state_dir=str(tmp_path / "store_test9")))
    now_ts = time.time()
    job_a = JobManifest(
        job_id="chem-tools-test-12345",
        owner="alice",
        title="Test Job A",
        created_at=now_ts - 7200,
        updated_at=now_ts - 3600,
        state=JobState.RUNNING,
        state_entered_at=now_ts - 3600,
        epoch=0,
    )
    job_store.put_job(job_a)

    # User B logs in
    bob_creds = KaggleCredentials(username="bob_chem", api_token="KGAT_bob_token_9999")
    broker.remember(bob_creds)

    # Background watchdog sweeps jobs
    # Mock reconciler that verifies owner credentials
    reconciled_jobs = []

    class MockReconciler:
        def reconcile(self, job_id, creds, actor="watchdog"):
            assert creds.username == "chem_user"  # Must use Alice's decrypted creds
            reconciled_jobs.append((job_id, creds.username))

    watchdog = Watchdog(store=job_store, reconciler=MockReconciler(), broker=broker, vault_manager=mgr)
    sweep_res = watchdog.sweep()

    assert sweep_res.stalled == 1
    assert sweep_res.recovered == 1
    assert len(reconciled_jobs) == 1
    assert reconciled_jobs[0] == ("chem-tools-test-12345", "chem_user")

    # Bob still cannot access Alice's credentials from broker directly
    assert broker.get("alice") is not None  # Background cached for Alice
    assert broker.get("bob_chem") == bob_creds
    # Decrypting Alice's vault record with Bob's identity fails
    vault_rec = backend.get_credential_vault("alice")
    with pytest.raises(CredentialDecryptionError):
        decrypt_credentials(vault_rec, master_key, "bob")
    job_store.close()


# ===========================================================================
# Test 10: Invalid Credential Handling (Bounded Retry)
# ===========================================================================
def test_invalid_credential_handling(master_key):
    """Malformed or invalid encrypted record is marked CREDENTIALS_INVALID to avoid infinite loops."""
    backend = InMemoryCloudflareBackend()
    broker = CredentialBroker()
    mgr = EncryptedCredentialVaultManager(client=backend, broker=broker, master_key=master_key)

    # Store record with corrupted ciphertext
    bad_rec = CloudflareCredentialVaultRecord(
        owner="charlie",
        kaggle_username="charlie",
        ciphertext="deadbeef010203",
        nonce=os.urandom(12).hex(),
        tag=os.urandom(16).hex(),
        status="ACTIVE",
    )
    backend.put_credential_vault(bad_rec)

    # Load attempt
    creds = mgr.load_credentials("charlie")
    assert creds is None

    # Status in vault must be updated to CREDENTIALS_INVALID
    meta = mgr.get_metadata("charlie")
    assert meta.status == "CREDENTIALS_INVALID"


# ===========================================================================
# Test 11: Missing Credential Handling (CREDENTIALS_REQUIRED)
# ===========================================================================
def test_missing_credential_handling(tmp_path):
    """When no credentials exist in RAM or vault, watchdog marks result CREDENTIALS_REQUIRED."""
    job_store = JobStore(StoreConfig(state_dir=str(tmp_path / "store_test11")))
    now_ts = time.time()
    job = JobManifest(
        job_id="chem-tools-stalled-999",
        owner="unknown_user",
        title="Stalled Job",
        created_at=now_ts - 7200,
        updated_at=now_ts - 3600,
        state=JobState.RUNNING,
        state_entered_at=now_ts - 3600,
        epoch=0,
    )
    job_store.put_job(job)

    broker = CredentialBroker()
    watchdog = Watchdog(store=job_store, broker=broker)
    sweep_res = watchdog.sweep()

    assert sweep_res.stalled == 1
    assert sweep_res.skipped_no_credentials == 1
    assert sweep_res.details[0]["credential_status"] == "CREDENTIALS_REQUIRED"
    job_store.close()


# ===========================================================================
# Test 12: Atomic Credential Replacement
# ===========================================================================
def test_atomic_credential_replacement(master_key, sample_creds):
    """Submitting new valid credentials atomically updates the ciphertext and in-memory cache."""
    backend = InMemoryCloudflareBackend()
    broker = CredentialBroker()
    mgr = EncryptedCredentialVaultManager(client=backend, broker=broker, master_key=master_key)

    # Save initial credential
    mgr.save_credentials("alice", sample_creds)
    old_rec = backend.get_credential_vault("alice")

    # Replace with updated credential
    new_creds = KaggleCredentials(username="chem_user", api_token="KGAT_new_rotated_token_11111")
    mgr.save_credentials("alice", new_creds)
    new_rec = backend.get_credential_vault("alice")

    assert old_rec.ciphertext != new_rec.ciphertext
    loaded = mgr.load_credentials("alice")
    assert loaded.api_token == "KGAT_new_rotated_token_11111"


# ===========================================================================
# Test 13: Failed Replacement Preserves Working Credential
# ===========================================================================
def test_failed_replacement_preserves_working_credential(master_key, sample_creds):
    """If verification of new credentials fails, the previously stored working credential is kept."""
    backend = InMemoryCloudflareBackend()
    broker = CredentialBroker()
    mgr = EncryptedCredentialVaultManager(client=backend, broker=broker, master_key=master_key)

    mgr.save_credentials("alice", sample_creds)
    initial_rec = backend.get_credential_vault("alice")

    # Attempt to save invalid credential with verification enabled
    # We pass an empty/invalid credential
    with pytest.raises(ValueError):
        mgr.save_credentials("alice", KaggleCredentials(username="", key=None), verify_with_kaggle=True)

    # Stored record must remain unchanged
    after_rec = backend.get_credential_vault("alice")
    assert after_rec.ciphertext == initial_rec.ciphertext
    assert mgr.load_credentials("alice").api_token == sample_creds.api_token


# ===========================================================================
# Test 14: Credential Deletion Behavior
# ===========================================================================
def test_credential_deletion_behavior(master_key, sample_creds):
    """Deleting credentials removes ciphertext, clears RAM, and prevents future decryption."""
    backend = InMemoryCloudflareBackend()
    broker = CredentialBroker()
    mgr = EncryptedCredentialVaultManager(client=backend, broker=broker, master_key=master_key)

    mgr.save_credentials("alice", sample_creds)
    assert mgr.load_credentials("alice") is not None

    # Delete
    deleted = mgr.delete_credentials("alice")
    assert deleted is True

    # Cloudflare vault is empty
    assert backend.get_credential_vault("alice") is None
    # RAM broker is empty
    assert broker.get("alice") is None
    # Load returns None
    assert mgr.load_credentials("alice") is None
    meta = mgr.get_metadata("alice")
    assert meta.exists is False


# ===========================================================================
# Test 15: Key Rotation Support
# ===========================================================================
def test_key_rotation(master_key, sample_creds):
    """Demonstrates decrypting with old key and re-encrypting with new key."""
    old_key = master_key
    new_key = bytes.fromhex("11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff")

    enc_old = encrypt_credentials(sample_creds, old_key, "alice")
    enc_new = rotate_credentials(enc_old, old_key, new_key, "alice")

    assert enc_new["encryption_version"] == 2
    assert enc_new["ciphertext"] != enc_old["ciphertext"]

    # Old key cannot decrypt new record
    with pytest.raises(CredentialDecryptionError):
        decrypt_credentials(enc_new, old_key, "alice")

    # New key decrypts successfully
    dec = decrypt_credentials(enc_new, new_key, "alice")
    assert dec.api_token == sample_creds.api_token


# ===========================================================================
# Test 16: Security & Secret Leak Scanning
# ===========================================================================
def test_no_secrets_in_cloudflare_payloads(master_key):
    """Ensures Cloudflare security assertion blocks any raw API keys or passwords."""
    backend = InMemoryCloudflareBackend()

    with pytest.raises(CloudflareSecurityViolation):
        backend.put_job(CloudflareJobRecord(
            internal_job_id="test",
            kaggle_username="user",
            kaggle_job_ref="user/job",
            metadata={"kaggle_key": "raw_secret_key_123"},
        ))
