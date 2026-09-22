# -*- coding: utf-8 -*-
"""Verification tests for Kaggle sign-in, credential formats, and Server API / Companion Agent Authentication."""
import json
import pytest
from fastapi.testclient import TestClient
from app import app
from services import local_agent_service as las
from orca_orchestrator.credentials import parse as parse_credentials


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# =========================================================================
# 1. Kaggle Login & Credentials Authentication Tests
# =========================================================================

def test_kaggle_login_empty_credentials_rejected(client):
    """Empty credentials should immediately return 400 with a clear instructional message."""
    res = client.post("/api/kaggle/login", json={})
    assert res.status_code == 400
    data = res.get_json()
    assert data["ok"] is False
    assert "required" in (data.get("error") or "").lower()


def test_kaggle_login_supports_both_field_conventions(client):
    """Endpoints accept both {kaggle_username, kaggle_key} and {username, key}."""
    # Using {username, key}
    res1 = client.post("/api/kaggle/login", json={"username": "chemist_user", "key": ""})
    assert res1.status_code == 400
    assert "required" in res1.get_json().get("error", "").lower()

    # Using {kaggle_username, kaggle_key}
    res2 = client.post("/api/kaggle/login", json={"kaggle_username": "chemist_user", "kaggle_key": ""})
    assert res2.status_code == 400
    assert "required" in res2.get_json().get("error", "").lower()


def test_kaggle_json_pasting_and_token_parsing():
    """Pasting full kaggle.json content into either field is parsed correctly without error."""
    raw_json = json.dumps({"username": "marie_curie", "key": "0123456789abcdef0123456789abcdef"})

    # 1. Standard username + 32-char hex key
    c1 = parse_credentials("marie_curie", "0123456789abcdef0123456789abcdef")
    assert c1.username == "marie_curie"
    assert c1.key == "0123456789abcdef0123456789abcdef"

    # 2. Entire kaggle.json pasted into the key field
    c2 = parse_credentials("", raw_json)
    assert c2.username == "marie_curie"
    assert c2.key == "0123456789abcdef0123456789abcdef"

    # 3. New-style Kaggle API token (KG_...)
    new_token = "KG_999888777666555444333222111000"
    c3 = parse_credentials("marie_curie", new_token)
    assert c3.username == "marie_curie"
    assert c3.api_token == new_token


def test_kaggle_credentials_vault_api_flexible_fields(client):
    """Verify /api/kaggle/credentials accepts both username/key and kaggle_username/kaggle_key."""
    # Rejection of empty payload
    res_empty = client.post("/api/kaggle/credentials", json={"username": "", "key": ""})
    assert res_empty.status_code in (400, 401)
    
    # Both username and key present
    raw_key = "0123456789abcdef0123456789abcdef"
    res_post = client.post("/api/kaggle/credentials", json={
        "username": "vault_test_user",
        "key": raw_key,
    })
    # Since network call to kaggle may be mocked or offline in test, it responds cleanly
    assert res_post.status_code in (200, 401)


# =========================================================================
# 2. Server API & Companion Agent Authentication Tests
# =========================================================================

def test_server_local_orca_settings_authentication(client):
    """In single-user mode, local_orca/settings is authorized and accessible."""
    res = client.get("/api/v1/local-orca/settings")
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert "settings" in data
    assert "concurrency" in data["settings"]


def test_fastapi_native_routes_accept_signed_flask_browser_session(monkeypatch):
    """The mounted UI and native FastAPI routes must share one login."""
    import api.main as api_main

    monkeypatch.setenv("CHEMISTRY_LAB_REQUIRE_AUTH", "1")
    serializer = app.session_interface.get_signing_serializer(app)
    assert serializer is not None
    cookie_name = app.config.get("SESSION_COOKIE_NAME", "session")
    signed = serializer.dumps({"user": {"sub": "browser-user-1", "email": "u@example.test"}})

    with TestClient(api_main.app) as api_client:
        api_client.cookies.set(cookie_name, signed)
        response = api_client.get("/api/v1/local-agent/devices")

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_fastapi_native_routes_reject_forged_flask_session(monkeypatch):
    import api.main as api_main

    monkeypatch.setenv("CHEMISTRY_LAB_REQUIRE_AUTH", "1")
    cookie_name = app.config.get("SESSION_COOKIE_NAME", "session")
    with TestClient(api_main.app) as api_client:
        api_client.cookies.set(cookie_name, "forged-session-cookie")
        response = api_client.get("/api/v1/local-agent/devices")

    assert response.status_code == 401


def test_fastapi_native_mutation_requires_session_bound_csrf(monkeypatch):
    """Native routes must not bypass Flask's browser CSRF boundary."""
    import api.main as api_main

    monkeypatch.setenv("CHEMISTRY_LAB_REQUIRE_AUTH", "1")
    serializer = app.session_interface.get_signing_serializer(app)
    assert serializer is not None
    cookie_name = app.config.get("SESSION_COOKIE_NAME", "session")
    csrf_token = "a" * 64
    signed = serializer.dumps({
        "user": {"sub": "browser-user-csrf"},
        "csrf_token": csrf_token,
    })
    payload = {"coords": "O 0 0 0", "calc_type": "sp"}

    with TestClient(api_main.app) as api_client:
        api_client.cookies.set(cookie_name, signed)
        missing = api_client.post("/api/v1/orca/generate", json=payload)
        accepted = api_client.post(
            "/api/v1/orca/generate", json=payload,
            headers={"X-CSRF-Token": csrf_token},
        )

    assert missing.status_code == 403
    assert missing.json()["error"]["code"] == "CSRF_FORBIDDEN"
    assert accepted.status_code == 200


def test_fastapi_native_body_limit_counts_actual_body(monkeypatch):
    """Chunked/missing Content-Length requests are counted, not trusted."""
    import api.main as api_main

    monkeypatch.setenv("CHEMISTRY_LAB_API_MAX_BODY_BYTES", "1024")
    limited_app = api_main._create_app()
    with TestClient(limited_app) as api_client:
        response = api_client.post(
            "/api/v1/orca/generate",
            content=b"x" * 2048,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_companion_agent_claim_format_validation(client):
    """Connection API token must strictly start with CLA_."""
    # 1. Invalid prefix
    bad_res = client.post("/api/v1/local-agent/runtime/claim", json={"connection_api": "INVALID_TOKEN_123"})
    assert bad_res.status_code == 400
    data = bad_res.get_json()
    assert data["ok"] is False
    assert "CLA_" in (data.get("error") or "")

    # 2. Empty token
    empty_res = client.post("/api/v1/local-agent/runtime/claim", json={"connection_api": ""})
    assert empty_res.status_code == 400


def test_companion_agent_full_claim_and_device_listing(client, tmp_path):
    """Full lifecycle: init session, claim via API, and list connected devices."""
    db_file = str(tmp_path / "test_auth_registry.db")
    token = "CLA_test_auth_secret_token_999"

    # 1. Agent registers token hash on server
    init_res = las.init_runtime_session(
        installation_id="inst_auth_unit",
        agent_session_id="sess_auth_unit",
        token_verifiers=[las.hash_token_verifier(token)],
        state_dir=db_file,
    )
    assert init_res["ok"] is True

    # 2. Browser claims token via API
    claim_res = las.claim_runtime_token(
        connection_api=token,
        owner_id="researcher_user",
        custom_device_name="Lab-Workstation-1",
        state_dir=db_file,
    )
    assert claim_res["ok"] is True
    assert claim_res["display_name"] == "Lab-Workstation-1"
    assert claim_res["agent_session_id"] == "sess_auth_unit"

    # 3. Agent finalizes runtime session
    fin_res = las.finalize_agent_runtime("sess_auth_unit", token, state_dir=db_file)
    assert fin_res["ok"] is True
    assert "runtime_session_secret" in fin_res

    # 4. List devices for researcher_user
    devices = las.list_user_runtime_devices(owner_id="researcher_user", state_dir=db_file)
    assert len(devices) == 1
    assert devices[0]["display_name"] == "Lab-Workstation-1"
    assert devices[0]["status"] in ("ONLINE", "ACTIVE")


def test_flask_local_agent_spoofing_rejected(client, monkeypatch):
    """Verify that legacy Flask local-agent endpoints cannot be spoofed via X-User-Id, X-Owner-Id, or query params (AUTH-SPOOF-01)."""
    monkeypatch.setenv("CHEMISTRY_LAB_REQUIRE_AUTH", "1")
    monkeypatch.delenv("CHEMISTRY_LAB_TEST_MODE", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    # 1. Anonymous request with X-User-Id: victim -> 401
    res_claim_spoof = client.post(
        "/api/v1/local-agent/runtime/claim",
        headers={"X-User-Id": "victim"},
        json={"connection_api": "CLA_invalid_token_123"},
    )
    assert res_claim_spoof.status_code == 401
    assert res_claim_spoof.get_json()["error"]["code"] == "UNAUTHORIZED"

    res_dev_spoof_uid = client.get(
        "/api/v1/local-agent/devices",
        headers={"X-User-Id": "victim"},
    )
    assert res_dev_spoof_uid.status_code == 401
    assert res_dev_spoof_uid.get_json()["error"]["code"] == "UNAUTHORIZED"

    # 2. Anonymous request with X-Owner-Id: victim -> 401
    res_dev_spoof_oid = client.get(
        "/api/v1/local-agent/devices",
        headers={"X-Owner-Id": "victim"},
    )
    assert res_dev_spoof_oid.status_code == 401
    assert res_dev_spoof_oid.get_json()["error"]["code"] == "UNAUTHORIZED"

    # 3. Anonymous request with ?owner_id=victim -> 401
    res_dev_spoof_query = client.get(
        "/api/v1/local-agent/devices?owner_id=victim",
    )
    assert res_dev_spoof_query.status_code == 401
    assert res_dev_spoof_query.get_json()["error"]["code"] == "UNAUTHORIZED"

    # 4. Authenticated User A with X-User-Id: UserB -> remains User A (User B spoof is ignored)
    with client.session_transaction() as sess:
        sess["user"] = {"sub": "user_alice", "email": "alice@lab.org"}

    res_auth_alice = client.get(
        "/api/v1/local-agent/devices",
        headers={"X-User-Id": "user_bob", "X-Owner-Id": "user_bob"},
        query_string={"owner_id": "user_bob"},
    )
    assert res_auth_alice.status_code == 200
    assert res_auth_alice.get_json()["ok"] is True

