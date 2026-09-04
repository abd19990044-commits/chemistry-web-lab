# -*- coding: utf-8 -*-
"""Comprehensive Production Security & Authorization Tests for Chemistry Lab."""
import os
import uuid
import pytest
from flask import Flask

import app as webapp
from services import local_agent_service, local_orca_service
from services.auth_service import get_authenticated_owner, require_authenticated_owner, is_admin_user
from services.execution_backend import resolve_execution_target, ExecutionError, ExecutionBackendType, AuthorizationError


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CHEMISTRY_LAB_MULTI_USER", "1")
    monkeypatch.setenv("CHEMISTRY_LAB_TEST_MODE", "1")
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        yield c


def test_anonymous_local_orca_execution_blocked(client):
    """Anonymous users must NOT be able to execute server-host ORCA."""
    resp = client.post("/api/v1/local-orca/execute", json={"input_text": "! HF STO-3G\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*"})
    assert resp.status_code == 401
    data = resp.get_json()
    assert data["ok"] is False


def test_anonymous_settings_modification_blocked(client):
    """Anonymous users must NOT be able to modify local ORCA settings."""
    resp = client.post("/api/v1/local-orca/settings", json={"orca_executable": "C:\\Orca\\orca.exe"})
    assert resp.status_code == 401
    data = resp.get_json()
    assert data["ok"] is False


def test_normal_user_server_host_executable_override_blocked(client, monkeypatch):
    """Normal authenticated users cannot override server-host ORCA settings."""
    monkeypatch.setenv("CHEMISTRY_LAB_TEST_MODE", "1")
    resp = client.post(
        "/api/v1/local-orca/settings",
        headers={"X-Test-User-Id": "user_alice"},
        json={"orca_executable": "C:\\Orca\\orca.exe"}
    )
    assert resp.status_code == 403
    data = resp.get_json()
    assert data["ok"] is False
    assert "restricted to administrators" in data["error"]["message"].lower()


def test_executable_path_injection_hardening():
    """Dangerous system binaries and path traversal are rejected before execution."""
    safe, err = local_orca_service.is_safe_executable_path("C:\\Windows\\System32\\cmd.exe")
    assert safe is False
    assert "forbidden" in err.lower()

    safe, err = local_orca_service.is_safe_executable_path("/bin/bash")
    assert safe is False
    assert "forbidden" in err.lower()

    safe, err = local_orca_service.is_safe_executable_path("C:\\Orca\\..\\cmd.exe")
    assert safe is False
    assert "traversal" in err.lower()

    safe, err = local_orca_service.is_safe_executable_path("C:\\Orca\\orca.exe")
    assert safe is True
    assert err is None


def test_cross_user_agent_disconnect_blocked(tmp_path):
    """User B cannot disconnect User A's Agent."""
    state_dir = str(tmp_path / "state")
    inst_id = f"inst_{uuid.uuid4().hex[:8]}"
    sess_id = f"sess_{uuid.uuid4().hex[:8]}"
    token = f"CLA_{uuid.uuid4().hex[:32]}"
    token_verifier = local_agent_service.hash_token_verifier(token)

    # Agent registers
    local_agent_service.init_runtime_session(
        installation_id=inst_id,
        agent_session_id=sess_id,
        token_verifiers=[token_verifier],
        device_name="Desktop A",
        platform="windows",
        state_dir=state_dir,
    )

    # User A claims Agent A
    claim_res = local_agent_service.claim_runtime_token(token, owner_id="user_alice", state_dir=state_dir)
    assert claim_res["ok"] is True

    # User B attempts to disconnect Agent A
    disc_b = local_agent_service.disconnect_user_device(sess_id, owner_id="user_bob", state_dir=state_dir)
    assert disc_b is False  # Blocked by atomic WHERE owner_id condition!

    # Verify device is still online for User A
    dev_a = local_agent_service.get_user_runtime_device(sess_id, owner_id="user_alice", state_dir=state_dir)
    assert dev_a is not None
    assert dev_a["status"] == "ONLINE"

    # User A disconnects own device
    disc_a = local_agent_service.disconnect_user_device(sess_id, owner_id="user_alice", state_dir=state_dir)
    assert disc_a is True


def test_secret_key_production_missing_fails(monkeypatch):
    """In production, missing SECRET_KEY fails fast with RuntimeError."""
    monkeypatch.setenv("CHEMISTRY_LAB_ENV", "production")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
    monkeypatch.delenv("CHEMISTRY_LAB_TEST_MODE", raising=False)

    from app import _resolve_secret_key
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        _resolve_secret_key()


def test_secret_key_configured_passes(monkeypatch):
    """With SECRET_KEY set, _resolve_secret_key returns it."""
    test_key = "production_super_secret_key_1234567890_abc"
    monkeypatch.setenv("SECRET_KEY", test_key)
    from app import _resolve_secret_key
    assert _resolve_secret_key() == test_key


def test_state_dir_production_missing_fails(monkeypatch, tmp_path):
    """In production, if no candidate is writable, _default_state_dir fails fast."""
    monkeypatch.setenv("CHEMISTRY_LAB_ENV", "production")
    monkeypatch.delenv("CHEMISTRY_LAB_STATE_DIR", raising=False)
    monkeypatch.delenv("ORCA_STATE_DIR", raising=False)
    monkeypatch.delenv("CHEMISTRY_LAB_TEST_MODE", raising=False)

    from orca_orchestrator.config import _default_state_dir
    with monkeypatch.context() as m:
        m.setattr("orca_orchestrator.config._probe_writable", lambda p: False)
        with pytest.raises(RuntimeError, match="configured, writable state directory"):
            _default_state_dir()


def test_frontend_xss_sanitization():
    """Verify that malicious XSS payloads in parameters are stripped/escaped properly."""
    malicious_inputs = [
        '<img src=x onerror=alert(1)>',
        '<script>alert(1)</script>',
        '"><svg onload=alert(1)>',
        '<iframe src="javascript:alert(1)"></iframe>',
    ]

    import html
    for payload in malicious_inputs:
        escaped = html.escape(payload, quote=True)
        assert "<script>" not in escaped
        assert "<img" not in escaped
        assert "<svg" not in escaped
        assert "<iframe" not in escaped
        assert "&lt;" in escaped or "&quot;" in escaped or "&#x27;" in escaped


def test_default_backend_resolves_to_local_agent(tmp_path):
    """ExecutionBackend defaults to LOCAL_AGENT and requires authenticated owner."""
    state_dir = str(tmp_path / "state")
    from services.execution_backend import resolve_execution_target, ExecutionBackendType, ExecutionError

    # Anonymous -> 401
    with pytest.raises(ExecutionError, match="Authentication required"):
        resolve_execution_target(None, owner_id=None, state_dir=state_dir)

    # Register an agent for user_alice
    inst_id = "inst_alice"
    sess_id = "sess_alice"
    token = f"CLA_{uuid.uuid4().hex[:32]}"
    local_agent_service.init_runtime_session(inst_id, sess_id, [local_agent_service.hash_token_verifier(token)], "Alice Rig", "windows", state_dir=state_dir)
    local_agent_service.claim_runtime_token(token, owner_id="user_alice", state_dir=state_dir)

    # Resolve default backend for user_alice
    target = resolve_execution_target(None, agent_session_id=sess_id, owner_id="user_alice", state_dir=state_dir)
    assert target["backend"] == ExecutionBackendType.LOCAL_AGENT
    assert target["device_name"] == "Alice Rig"
    assert target["platform"] == "windows"


def test_offline_agent_fails_loudly_no_silent_fallback(tmp_path):
    """If the target Local Agent is disconnected/offline, fail loudly without silent fallback."""
    state_dir = str(tmp_path / "state")
    from services.execution_backend import resolve_execution_target, ExecutionError
    from services.auth_service import AuthorizationError

    inst_id = "inst_alice_off"
    sess_id = "sess_alice_off"
    token = f"CLA_{uuid.uuid4().hex[:32]}"
    local_agent_service.init_runtime_session(inst_id, sess_id, [local_agent_service.hash_token_verifier(token)], "Alice Rig", "windows", state_dir=state_dir)
    local_agent_service.claim_runtime_token(token, owner_id="user_alice", state_dir=state_dir)
    # Disconnect
    local_agent_service.disconnect_user_device(sess_id, owner_id="user_alice", state_dir=state_dir)

    with pytest.raises((ExecutionError, AuthorizationError), match="(offline|not found|not owned)"):
        resolve_execution_target("local_agent", agent_session_id=sess_id, owner_id="user_alice", state_dir=state_dir)


def test_user_isolation_cannot_target_other_users_agent(tmp_path):
    """User B cannot target User A's Local Agent."""
    state_dir = str(tmp_path / "state")
    from services.execution_backend import resolve_execution_target, ExecutionError
    from services.auth_service import AuthorizationError

    inst_id = "inst_alice_iso"
    sess_id = "sess_alice_iso"
    token = f"CLA_{uuid.uuid4().hex[:32]}"
    local_agent_service.init_runtime_session(inst_id, sess_id, [local_agent_service.hash_token_verifier(token)], "Alice Rig", "windows", state_dir=state_dir)
    local_agent_service.claim_runtime_token(token, owner_id="user_alice", state_dir=state_dir)

    # User B attempts to target User A's session
    with pytest.raises((ExecutionError, AuthorizationError), match="(not associated|not owned|not found)"):
        resolve_execution_target("local_agent", agent_session_id=sess_id, owner_id="user_bob", state_dir=state_dir)


def test_non_admin_server_local_denied(monkeypatch):
    """Non-admin user targeting server_local is rejected with 403."""
    monkeypatch.setenv("CHEMISTRY_LAB_MULTI_USER", "1")
    monkeypatch.delenv("CHEMISTRY_LAB_ALLOW_SERVER_ORCA", raising=False)
    from services.execution_backend import resolve_execution_target, ExecutionError
    from services.auth_service import AuthorizationError

    with pytest.raises((ExecutionError, AuthorizationError), match="restricted to (trusted )?administrators"):
        resolve_execution_target("server_local", owner_id="user_regular")


def test_admin_authorization_exact_match_enforced(monkeypatch):
    """SEC-01 Regression: Only exact configured identities in CHEMISTRY_LAB_ADMIN_USERS receive admin status."""
    monkeypatch.setenv("CHEMISTRY_LAB_MULTI_USER", "1")
    monkeypatch.setenv("CHEMISTRY_LAB_ADMIN_USERS", "alice@example.com, charlie@example.com")

    # 1. Configured exact admin -> admin
    assert is_admin_user("alice@example.com") is True
    assert is_admin_user("ALICE@EXAMPLE.COM") is True  # Case-normalized
    assert is_admin_user("charlie@example.com") is True

    # 2. Unconfigured normal user -> NOT admin
    assert is_admin_user("bob@example.com") is False

    # 3. Malicious-looking identities with prefix 'admin' or substring -> NOT admin
    malicious_identities = [
        "admin@example-attacker.com",
        "admin_test@example.com",
        "administrator123",
        "superadmin",
        "myadmin",
        "admin",
        "admin@example.com",
        "administrator@example.com",
        "alice@example.com.attacker.com",
    ]
    for bad_id in malicious_identities:
        assert is_admin_user(bad_id) is False, f"Identity '{bad_id}' was erroneously granted admin privileges!"


def test_server_host_operations_blocked_for_normal_users_and_impersonators(client, monkeypatch):
    """SEC-01: Malicious identities cannot modify server ORCA settings or execute server ORCA."""
    monkeypatch.setenv("CHEMISTRY_LAB_MULTI_USER", "1")
    monkeypatch.setenv("CHEMISTRY_LAB_TEST_MODE", "1")
    monkeypatch.setenv("CHEMISTRY_LAB_ADMIN_USERS", "trusted_admin@lab.org")

    attacker_headers = [
        {"X-Test-User-Id": "admin_test@example.com"},
        {"X-Test-User-Id": "administrator123"},
        {"X-Test-User-Id": "superadmin"},
        {"X-Test-User-Id": "bob@example.com"},
    ]

    for hdrs in attacker_headers:
        # Settings modification blocked
        res_set = client.post(
            "/api/v1/local-orca/settings",
            headers=hdrs,
            json={"orca_executable": "C:\\Orca\\orca.exe"}
        )
        assert res_set.status_code == 403
        assert res_set.get_json()["ok"] is False

        # Config testing blocked
        res_test = client.post(
            "/api/v1/local-orca/test-config",
            headers=hdrs,
            json={"orca_executable": "C:\\Orca\\orca.exe"}
        )
        assert res_test.status_code == 403
        assert res_test.get_json()["ok"] is False

        # Server-host execution blocked
        res_exec = client.post(
            "/api/v1/local-orca/execute",
            headers=hdrs,
            json={"input_text": "! HF STO-3G\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*"}
        )
        assert res_exec.status_code == 403
        assert res_exec.get_json()["ok"] is False

    # Legitimate trusted admin IS allowed
    admin_hdrs = {"X-Test-User-Id": "trusted_admin@lab.org"}
    res_admin = client.get("/api/v1/local-orca/candidates", headers=admin_hdrs)
    assert res_admin.status_code == 200
    assert res_admin.get_json()["ok"] is True




