# -*- coding: utf-8 -*-
"""Comprehensive Security & Regression Test Suite for Kaggle Credential Security (Iteration 4).

Validates:
1. Plaintext localStorage removed and legacy keys purged & migrated.
2. Encrypted persistence at rest (AES-256-GCM AEAD, no plaintext tokens in vault).
3. Secure retrieval: masked metadata returned, raw secrets never exposed.
4. Tenant isolation: multi-user authorization derives identity strictly from server auth.
5. Atomic credential replacement.
6. Deletion/disconnection behavior.
7. Connection test endpoint (/api/kaggle/test) using vault credentials.
8. Error redaction: failed authentication never leaks tokens in responses or logs.
9. Master key fail-safe model: production mode fails 503 safely without master key.
10. CSRF enforcement on Kaggle credential endpoints.
11. Centralized log redaction for modern and legacy token patterns.
12. Kaggle disabled by default & no silent fallback.
"""
from __future__ import annotations

import json
import os
import re
from unittest.mock import MagicMock, patch
import pytest

import app as webapp
from orca_orchestrator.credentials import KaggleCredentials, parse as parse_credentials
from orca_orchestrator.credential_vault import (
    EncryptedCredentialVaultManager,
    get_vault_manager,
    mask_token,
    parse_master_key,
)
from orca_orchestrator.cloudflare_controller.client import get_cloudflare_client, InMemoryCloudflareBackend
from orca_orchestrator.logging_ext import redact


@pytest.fixture(autouse=True)
def reset_vault():
    """Reset the in-memory vault and broker before each test."""
    vm = get_vault_manager()
    vm.broker.clear()
    client = get_cloudflare_client()
    if isinstance(client, InMemoryCloudflareBackend):
        with client._lock:
            client._vault.clear()
    yield
    vm.broker.clear()
    if isinstance(client, InMemoryCloudflareBackend):
        with client._lock:
            client._vault.clear()


@pytest.fixture
def test_client():
    app = webapp.app
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test_kaggle_secret_key_12345"
    with app.test_client() as client:
        yield client


@pytest.fixture
def csrf_client():
    app = webapp.app
    orig_testing = app.config.get("TESTING")
    orig_csrf = app.config.get("CSRF_ENABLED")
    app.config["TESTING"] = True
    app.config["CSRF_ENABLED"] = True
    app.config["SECRET_KEY"] = "test_kaggle_secret_key_csrf_12345"
    with app.test_client() as client:
        yield client
    app.config["TESTING"] = orig_testing
    if orig_csrf is None:
        app.config.pop("CSRF_ENABLED", None)
    else:
        app.config["CSRF_ENABLED"] = orig_csrf


# ===========================================================================
# 1. Plaintext LocalStorage Removed and Purged
# ===========================================================================
def test_plaintext_localstorage_removed_and_migrated():
    """Verifies that static JS files never store raw Kaggle secrets in localStorage."""
    app_js_path = os.path.join(webapp.BASE_DIR, "static", "js", "app.js")
    with open(app_js_path, "r", encoding="utf-8") as f:
        app_js = f.read()

    kaggle_js_path = os.path.join(webapp.BASE_DIR, "static", "js", "kaggle_credentials.js")
    with open(kaggle_js_path, "r", encoding="utf-8") as f:
        kaggle_js = f.read()

    # Neither script should ever write Kaggle API key to localStorage
    assert 'localStorage.setItem(LS_KEYS.kaggleKey' not in app_js
    assert 'localStorage.setItem("chemlab_kaggle_key"' not in app_js
    assert 'localStorage.setItem(KEY_KEY' not in kaggle_js

    # Both scripts must actively remove legacy chemlab_kaggle_key
    assert 'localStorage.removeItem("chemlab_kaggle_key")' in app_js
    assert 'localStorage.removeItem(KEY_KEY)' in kaggle_js

    # credsFor must not read kaggleKey from localStorage
    assert 'localStorage.getItem(LS_KEYS.kaggleKey)' not in app_js


def test_legacy_migration_flow_deletes_only_on_confirmed_vault_persistence():
    """Verify legacy key deletion occurs ONLY after confirmed successful vault persistence."""
    app_js_path = os.path.join(webapp.BASE_DIR, "static", "js", "app.js")
    with open(app_js_path, "r", encoding="utf-8") as f:
        app_js = f.read()

    # Verify that in autoSignIn, removeItem occurs conditionally upon confirmed vault persistence
    assert "if (migResp && migResp.ok && migResp.saved_to_vault)" in app_js
    # Verify bounded attempts to prevent indefinite retransmission
    assert "chemlab_migration_attempts" in app_js


# ===========================================================================
# 2. Encrypted Persistence at Rest (AES-256-GCM AEAD)
# ===========================================================================
def test_vault_save_encrypted_at_rest():
    """Verifies that saved credentials are encrypted via AES-256-GCM and contain no plaintext."""
    vm = get_vault_manager()
    secret_token = "KGAT_super_secure_auth_token_987654321"
    creds = parse_credentials("alice_chem", secret_token)

    saved = vm.save_credentials("user_alice", creds, verify_with_kaggle=False)
    assert saved is True

    # Inspect the underlying Cloudflare/D1 record
    client = get_cloudflare_client()
    rec = client.get_credential_vault("user_alice")
    assert rec is not None
    rec_dict = rec.to_dict()

    # Must contain cryptographic artifacts
    assert "ciphertext" in rec_dict
    assert "nonce" in rec_dict
    assert "tag" in rec_dict
    assert len(bytes.fromhex(rec_dict["nonce"])) == 12
    assert len(bytes.fromhex(rec_dict["tag"])) == 16

    # Must NOT contain the plaintext secret anywhere
    serialized = json.dumps(rec_dict)
    assert secret_token not in serialized
    assert "super_secure" not in serialized


# ===========================================================================
# 3. Secure Retrieval: Masked Metadata Only, Never Raw Secret
# ===========================================================================
def test_vault_read_returns_masked_metadata_never_raw_secret(test_client):
    """GET /api/kaggle/credentials returns masked token metadata and never the raw secret."""
    vm = get_vault_manager()
    secret_token = "KGAT_classified_api_token_55554321"
    creds = parse_credentials("dr_watson", secret_token)
    vm.save_credentials("local_user", creds, verify_with_kaggle=False)

    resp = test_client.get("/api/kaggle/credentials")
    assert resp.status_code == 200
    data = resp.get_json()

    assert data["ok"] is True
    assert data["configured"] is True
    assert data["username"] == "dr_watson"
    assert data["token_masked"] == "********4321"
    assert data["status"] == "ACTIVE"

    # Zero occurrence of raw secret in response
    resp_text = resp.get_data(as_text=True)
    assert secret_token not in resp_text
    assert "classified_api_token" not in resp_text


def test_vault_read_unconfigured(test_client):
    """GET /api/kaggle/credentials for unconfigured user returns configured: False."""
    resp = test_client.get("/api/kaggle/credentials")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["configured"] is False
    assert data["status"] == "NOT_CONFIGURED"
    assert data["username"] == ""
    assert data["token_masked"] == ""


# ===========================================================================
# 4. Strict Tenant Isolation (Multi-User)
# ===========================================================================
def test_vault_tenant_isolation_multi_user(test_client, monkeypatch):
    """Verifies that User A cannot read, override, or delete User B's vault credentials."""
    monkeypatch.setenv("CHEMISTRY_LAB_MULTI_USER", "1")

    # 1. User Alpha saves credentials
    alpha_token = "KGAT_alpha_secret_token_1111"
    with test_client.session_transaction() as sess:
        sess["user_id"] = "user_alpha"

    with patch("kaggle_runner.verify_kaggle_credentials", return_value={"username": "alpha_kaggle"}):
        res1 = test_client.post("/api/kaggle/credentials", json={
            "kaggle_username": "alpha_kaggle",
            "kaggle_key": alpha_token,
        })
        assert res1.status_code == 200
        assert res1.get_json()["owner"] == "user_alpha"

    # 2. User Beta visits /api/kaggle/credentials
    with test_client.session_transaction() as sess:
        sess["user_id"] = "user_beta"

    res_b = test_client.get("/api/kaggle/credentials")
    assert res_b.status_code == 200
    assert res_b.get_json()["configured"] is False

    # 3. User Beta attempts to overwrite User Alpha's vault by sending owner in payload
    beta_token = "KGAT_beta_secret_token_2222"
    with patch("kaggle_runner.verify_kaggle_credentials", return_value={"username": "beta_kaggle"}):
        res_spoof = test_client.post("/api/kaggle/credentials", json={
            "owner": "user_alpha",
            "kaggle_username": "beta_kaggle",
            "kaggle_key": beta_token,
        })
        assert res_spoof.status_code == 200
        # Authoritative owner derived by server MUST be user_beta
        assert res_spoof.get_json()["owner"] == "user_beta"

    # Verify User Alpha's credentials were NOT overwritten
    vm = get_vault_manager()
    alpha_creds = vm.load_credentials("user_alpha")
    assert alpha_creds.username == "alpha_kaggle"
    assert alpha_creds.api_token == alpha_token

    # 4. User Beta attempts to delete User Alpha's credentials
    res_del = test_client.delete("/api/kaggle/credentials")
    assert res_del.status_code == 200
    assert res_del.get_json()["owner"] == "user_beta"

    # Alpha's credentials still intact
    alpha_creds_after = vm.load_credentials("user_alpha")
    assert alpha_creds_after is not None
    assert alpha_creds_after.api_token == alpha_token


# ===========================================================================
# 5. Atomic Credential Replacement
# ===========================================================================
def test_vault_replace_credential_flow(test_client):
    """Replacing credentials updates the encrypted record cleanly."""
    with patch("kaggle_runner.verify_kaggle_credentials", return_value={"username": "alice"}):
        res1 = test_client.post("/api/kaggle/credentials", json={
            "kaggle_username": "alice",
            "kaggle_key": "KGAT_first_token_11111111",
        })
        assert res1.status_code == 200
        assert res1.get_json()["token_masked"] == "********1111"

        res2 = test_client.post("/api/kaggle/credentials", json={
            "kaggle_username": "alice",
            "kaggle_key": "KGAT_replacement_token_22222222",
        })
        assert res2.status_code == 200
        assert res2.get_json()["token_masked"] == "********2222"

    read_res = test_client.get("/api/kaggle/credentials")
    assert read_res.get_json()["token_masked"] == "********2222"


# ===========================================================================
# 6. Deletion and Disconnection Flow
# ===========================================================================
def test_vault_delete_disconnect_flow(test_client):
    """DELETE /api/kaggle/credentials wipes the record from vault and RAM broker."""
    vm = get_vault_manager()
    creds = parse_credentials("bob", "KGAT_bob_token_99999999")
    vm.save_credentials("local_user", creds, verify_with_kaggle=False)

    assert vm.load_credentials("local_user") is not None

    del_res = test_client.delete("/api/kaggle/credentials")
    assert del_res.status_code == 200
    assert del_res.get_json()["deleted"] is True

    # Vault record is gone
    assert vm.load_credentials("local_user") is None

    # GET returns configured: False
    get_res = test_client.get("/api/kaggle/credentials")
    assert get_res.get_json()["configured"] is False


# ===========================================================================
# 7. Kaggle Connection Test Endpoint (/api/kaggle/test)
# ===========================================================================
def test_kaggle_connection_test_endpoint_with_stored_vault(test_client):
    """POST /api/kaggle/test uses stored vault credentials without needing key in payload."""
    vm = get_vault_manager()
    creds = parse_credentials("chemist_sam", "KGAT_sam_token_8888")
    vm.save_credentials("local_user", creds, verify_with_kaggle=False)

    with patch("kaggle_runner.verify_kaggle_credentials", return_value={"username": "chemist_sam"}) as mock_verify:
        res = test_client.post("/api/kaggle/test", json={})
        assert res.status_code == 200
        data = res.get_json()
        assert data["ok"] is True
        assert data["validated"] is True
        assert data["username"] == "chemist_sam"
        # verify was called with the stored credentials
        mock_verify.assert_called_once_with("chemist_sam", "KGAT_sam_token_8888")


def test_kaggle_connection_test_endpoint_ephemeral_token(test_client):
    """POST /api/kaggle/test can test new ephemeral credentials without persisting."""
    with patch("kaggle_runner.verify_kaggle_credentials", return_value={"username": "temp_user"}):
        res = test_client.post("/api/kaggle/test", json={
            "kaggle_username": "temp_user",
            "kaggle_key": "KGAT_ephemeral_token_7777",
        })
        assert res.status_code == 200
        assert res.get_json()["ok"] is True

    # Vault should remain empty
    vm = get_vault_manager()
    assert vm.load_credentials("local_user") is None


# ===========================================================================
# 8. Error Response Redaction
# ===========================================================================
def test_kaggle_error_response_redaction(test_client):
    """Kaggle errors never echo unredacted secrets in JSON response bodies."""
    leaked_token = "KGAT_leaked_secret_token_abcdef12"

    with patch("kaggle_runner.verify_kaggle_credentials", side_effect=Exception(f"Kaggle API returned 401 Unauthorized for {leaked_token}")):
        res = test_client.post("/api/kaggle/test", json={
            "kaggle_username": "victim",
            "kaggle_key": leaked_token,
        })
        assert res.status_code == 401
        res_text = res.get_data(as_text=True)
        assert leaked_token not in res_text
        assert "<redacted:kaggle_token>" in res_text


# ===========================================================================
# 9. Master Key Failure Model Fails Safely
# ===========================================================================
def test_master_key_failure_model_fails_safely(test_client, monkeypatch):
    """When master key is missing in production, save returns 503 and never saves plaintext."""
    monkeypatch.delenv("KAGGLE_CREDENTIALS_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("CHEMISTRY_LAB_TEST_MODE", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    with patch("kaggle_runner.verify_kaggle_credentials", return_value={"username": "prod_user"}):
        res = test_client.post("/api/kaggle/credentials", json={
            "kaggle_username": "prod_user",
            "kaggle_key": "KGAT_production_token_12345678",
        })
        assert res.status_code == 503
        assert "Master encryption key is not configured" in res.get_json()["error"]


# ===========================================================================
# 10. CSRF Enforcement on Kaggle Credential Endpoints
# ===========================================================================
def test_csrf_enforcement_on_kaggle_credential_routes(csrf_client):
    """Mutating Kaggle credential endpoints reject requests without CSRF token."""
    # 1. POST /api/kaggle/credentials without CSRF token
    res1 = csrf_client.post("/api/kaggle/credentials", json={
        "kaggle_username": "test_user",
        "kaggle_key": "KGAT_test_token_12345678",
    })
    assert res1.status_code == 403
    assert res1.get_json()["code"] == "CSRF_FORBIDDEN"

    # 2. DELETE /api/kaggle/credentials without CSRF token
    res2 = csrf_client.delete("/api/kaggle/credentials")
    assert res2.status_code == 403
    assert res2.get_json()["code"] == "CSRF_FORBIDDEN"

    # 3. POST /api/kaggle/test without CSRF token
    res3 = csrf_client.post("/api/kaggle/test", json={
        "kaggle_username": "test_user",
        "kaggle_key": "KGAT_test_token_12345678",
    })
    assert res3.status_code == 403
    assert res3.get_json()["code"] == "CSRF_FORBIDDEN"

    # 4. Request with valid CSRF token succeeds
    # Initialize session to generate CSRF token
    init_res = csrf_client.get("/")
    assert init_res.status_code == 200
    csrf_token = init_res.headers.get("X-CSRF-Token")
    assert csrf_token is not None

    with patch("kaggle_runner.verify_kaggle_credentials", return_value={"username": "test_user"}):
        res_valid = csrf_client.post(
            "/api/kaggle/credentials",
            headers={"X-CSRF-Token": csrf_token},
            json={"kaggle_username": "test_user", "kaggle_key": "KGAT_valid_token_12345678"},
        )
        assert res_valid.status_code == 200
        assert res_valid.get_json()["ok"] is True


# ===========================================================================
# 11. Centralized Log Redaction
# ===========================================================================
def test_log_redaction_for_new_and_legacy_tokens():
    """Validates redaction of modern KG/KGAT tokens, legacy 32-hex keys, and key=value patterns."""
    # Modern KGAT token
    text1 = "Submitting with token KGAT_aBcD_1234-xyz for user"
    assert redact(text1) == "Submitting with token <redacted:kaggle_token> for user"

    # Modern KG token
    text2 = "Using token KG_9876543210_token in execution"
    assert redact(text2) == "Using token <redacted:kaggle_token> in execution"

    # Legacy 32-character hex key
    text3 = "Legacy key 0123456789abcdef0123456789abcdef found"
    assert redact(text3) == "Legacy key <redacted:32hex> found"

    # JSON dictionary pattern
    text4 = '{"kaggle_username": "sam", "kaggle_key": "my_secret_key_value"}'
    redacted4 = redact(text4)
    assert "my_secret_key_value" not in redacted4
    assert '<redacted>' in redacted4


# ===========================================================================
# 12. Kaggle Disabled by Default and No Silent Fallback
# ===========================================================================
def test_kaggle_disabled_by_default_and_no_silent_fallback(test_client):
    """Verifies that calculations do not silently route to Kaggle when unconfigured."""
    # Attempting to submit to Kaggle without credentials returns 400
    res = test_client.post("/api/kaggle/submit", data={
        "dataset_sources": "test/orca",
        "input_content": "! B3LYP def2-SVP\n* xyz 0 1\nO 0 0 0\n*",
    })
    assert res.status_code == 400
    assert "Kaggle username and API key/token" in res.get_json()["error"]
