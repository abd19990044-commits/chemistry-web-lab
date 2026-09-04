# -*- coding: utf-8 -*-
"""Comprehensive CSRF Protection, Session Cookie Hardening & Security Headers Tests.

Verifies:
1. Mutating requests (POST, PUT, PATCH, DELETE) require a valid CSRF token when authenticated via session.
2. Missing or mismatched CSRF tokens are rejected with HTTP 403.
3. Valid CSRF tokens (via X-CSRF-Token header, JSON body, or form data) succeed.
4. Non-browser / machine callers (Bearer auth, agent secrets, agent polling endpoints) are properly exempt.
5. Safe HTTP methods (GET, HEAD, OPTIONS) are exempt.
6. Session cookies are configured with HttpOnly=True, SameSite=Lax, and environment-aware Secure flag.
7. Crucial security headers are sent on all responses:
   - X-Content-Type-Options: nosniff
   - X-Frame-Options: DENY
   - Referrer-Policy: strict-origin-when-cross-origin
   - Content-Security-Policy with frame-ancestors 'none' and 3Dmol/worker support.
8. HTML responses include the <meta name="csrf-token"> tag.
"""
import os
import re
import pytest

import app as webapp


@pytest.fixture
def csrf_client():
    """Client configured with CSRF enforcement explicitly enabled."""
    app = webapp.app
    original_testing = app.config.get("TESTING")
    original_csrf = app.config.get("CSRF_ENABLED")
    app.config["TESTING"] = True
    app.config["CSRF_ENABLED"] = True
    app.config["SECRET_KEY"] = "csrf_test_secret_key_1234567890abcdef"
    with app.test_client() as client:
        yield client
    app.config["TESTING"] = original_testing
    if original_csrf is None:
        app.config.pop("CSRF_ENABLED", None)
    else:
        app.config["CSRF_ENABLED"] = original_csrf


def test_security_headers_present_on_response(csrf_client):
    """Verify all standard security headers are injected into HTTP responses."""
    res = csrf_client.get("/")
    assert res.status_code == 200
    headers = res.headers

    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("X-Frame-Options") == "DENY"
    assert headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    csp = headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "worker-src 'self' blob:" in csp
    assert "child-src 'self' blob:" in csp
    assert "https://cdnjs.cloudflare.com" in csp


def test_meta_csrf_token_rendered_in_index_html(csrf_client):
    """Verify that index.html contains the session-bound csrf-token meta tag."""
    res = csrf_client.get("/")
    assert res.status_code == 200
    html_text = res.get_data(as_text=True)

    match = re.search(r'<meta name="csrf-token" content="([a-f0-9]{64})">', html_text)
    assert match is not None, "csrf-token meta tag with 64-char hex token must be present in HTML"
    rendered_token = match.group(1)

    # Header and cookie should also match
    assert res.headers.get("X-CSRF-Token") == rendered_token


def test_session_cookie_hardening_configuration():
    """Verify Flask session cookie hardening configurations."""
    assert webapp.app.config.get("SESSION_COOKIE_HTTPONLY") is True
    assert webapp.app.config.get("SESSION_COOKIE_SAMESITE") == "Lax"
    assert isinstance(webapp.app.config.get("SESSION_COOKIE_SECURE"), bool)


def test_csrf_rejection_when_token_missing(csrf_client):
    """Mutating request without CSRF token must be rejected with 403."""
    # Establish session first
    init_res = csrf_client.get("/")
    assert init_res.status_code == 200

    # Attempt POST without token
    res = csrf_client.post("/api/v1/reactions", json={"equation": "H2 + O2 -> H2O"})
    assert res.status_code == 403
    data = res.get_json()
    assert data["ok"] is False
    assert "CSRF" in data["error"]
    assert data.get("code") == "CSRF_FORBIDDEN"


def test_csrf_rejection_when_token_invalid(csrf_client):
    """Mutating request with incorrect CSRF token must be rejected with 403."""
    init_res = csrf_client.get("/")
    assert init_res.status_code == 200

    # Attempt POST with bogus token
    res = csrf_client.post(
        "/api/v1/reactions",
        json={"equation": "H2 + O2 -> H2O"},
        headers={"X-CSRF-Token": "bad_token_12345"}
    )
    assert res.status_code == 403
    data = res.get_json()
    assert data["ok"] is False
    assert "CSRF" in data["error"]


def test_csrf_success_with_header_token(csrf_client):
    """Mutating request with valid X-CSRF-Token header succeeds."""
    init_res = csrf_client.get("/")
    assert init_res.status_code == 200
    valid_token = init_res.headers.get("X-CSRF-Token")
    assert valid_token is not None

    # Valid POST with header
    res = csrf_client.post(
        "/api/v1/reactions",
        json={"equation": "H2 + Cl2 -> 2HCl"},
        headers={"X-CSRF-Token": valid_token}
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert "reaction" in data


def test_csrf_success_with_json_body_token(csrf_client):
    """Mutating request with valid csrf_token in JSON body succeeds."""
    init_res = csrf_client.get("/")
    assert init_res.status_code == 200
    valid_token = init_res.headers.get("X-CSRF-Token")

    res = csrf_client.post(
        "/api/v1/reactions",
        json={
            "equation": "N2 + 3H2 -> 2NH3",
            "csrf_token": valid_token
        }
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True


def test_csrf_exemption_for_bearer_token_requests(csrf_client):
    """API requests using valid Bearer authentication are exempt from CSRF checks."""
    init_res = csrf_client.get("/")
    assert init_res.status_code == 200

    # Send POST with valid Bearer token (no CSRF token)
    res = csrf_client.post(
        "/api/v1/reactions",
        json={"equation": "H2 + Cl2 -> 2HCl"},
        headers={"Authorization": "Bearer test_api_token_value_abc"}
    )
    # Valid Bearer token must NOT be blocked by CSRF
    assert res.status_code != 403 or "CSRF" not in (res.get_json() or {}).get("error", "")


def test_csrf_rejection_for_fake_bearer_token(csrf_client):
    """Fake / unvalidated Bearer tokens MUST NOT bypass CSRF validation."""
    init_res = csrf_client.get("/")
    assert init_res.status_code == 200

    res = csrf_client.post(
        "/api/v1/reactions",
        json={"equation": "H2 + Cl2 -> 2HCl"},
        headers={"Authorization": "Bearer fake_bogus_garbage_token"}
    )
    assert res.status_code == 403
    data = res.get_json() or {}
    assert data.get("ok") is False
    assert data.get("code") == "CSRF_FORBIDDEN"


def test_csrf_rejection_for_fake_agent_secret_on_browser_route(csrf_client):
    """Fake X-Agent-Secret headers MUST NOT bypass CSRF on browser routes."""
    init_res = csrf_client.get("/")
    assert init_res.status_code == 200

    res = csrf_client.post(
        "/api/v1/reactions",
        json={"equation": "H2 + Cl2 -> 2HCl"},
        headers={"X-Agent-Secret": "attacker_fake_agent_secret"}
    )
    assert res.status_code == 403
    data = res.get_json() or {}
    assert data.get("ok") is False
    assert data.get("code") == "CSRF_FORBIDDEN"


def test_csrf_rejection_for_fake_runtime_session_secret_on_browser_route(csrf_client):
    """Fake X-Runtime-Session-Secret MUST NOT bypass CSRF on browser routes."""
    init_res = csrf_client.get("/")
    assert init_res.status_code == 200

    res = csrf_client.post(
        "/api/v1/reactions",
        json={"equation": "H2 + Cl2 -> 2HCl"},
        headers={"X-Runtime-Session-Secret": "attacker_fake_runtime_secret"}
    )
    assert res.status_code == 403
    data = res.get_json() or {}
    assert data.get("ok") is False
    assert data.get("code") == "CSRF_FORBIDDEN"


def test_csrf_rejection_for_runtime_claim_without_csrf_token(csrf_client):
    """Browser claiming companion device (/runtime/claim) requires valid CSRF token."""
    init_res = csrf_client.get("/")
    assert init_res.status_code == 200

    res = csrf_client.post(
        "/api/v1/local-agent/runtime/claim",
        json={"connection_api": "test_claim_code_123"},
    )
    assert res.status_code == 403
    data = res.get_json() or {}
    assert data.get("code") == "CSRF_FORBIDDEN"


def test_csrf_exemption_for_agent_runtime_machine_endpoints(csrf_client):
    """Headless agent daemon machine endpoints (e.g. heartbeat, init) are exempt from CSRF."""
    init_res = csrf_client.get("/")
    assert init_res.status_code == 200

    res = csrf_client.post(
        "/api/v1/local-agent/heartbeat",
        json={"agent_session_id": "test_sess_hb"},
    )
    # Machine endpoint must NOT be rejected with CSRF_FORBIDDEN (403)
    data = res.get_json() or {}
    assert res.status_code != 403 or data.get("code") != "CSRF_FORBIDDEN"


def test_csrf_cross_session_binding_rejected():
    """CSRF token from Session A cannot be used to authenticate requests in Session B."""
    app = webapp.app
    original_testing = app.config.get("TESTING")
    original_csrf = app.config.get("CSRF_ENABLED")
    app.config["TESTING"] = True
    app.config["CSRF_ENABLED"] = True
    app.config["SECRET_KEY"] = "csrf_cross_session_key_998877"

    try:
        with app.test_client() as client_a, app.test_client() as client_b:
            res_a = client_a.get("/")
            token_a = res_a.headers.get("X-CSRF-Token")
            assert token_a is not None

            res_b = client_b.get("/")
            token_b = res_b.headers.get("X-CSRF-Token")
            assert token_b is not None
            assert token_a != token_b

            # Attempt to execute mutation on Session B using Session A's CSRF token
            cross_res = client_b.post(
                "/api/v1/reactions",
                json={"equation": "H2 + O2 -> H2O"},
                headers={"X-CSRF-Token": token_a},
            )
            assert cross_res.status_code == 403
            data = cross_res.get_json() or {}
            assert data.get("ok") is False
            assert data.get("code") == "CSRF_FORBIDDEN"
    finally:
        app.config["TESTING"] = original_testing
        if original_csrf is None:
            app.config.pop("CSRF_ENABLED", None)
        else:
            app.config["CSRF_ENABLED"] = original_csrf


def test_session_fixation_csrf_token_rotation_on_login_and_logout(monkeypatch):
    """CSRF token must rotate upon login and logout to prevent session fixation."""
    app = webapp.app
    original_testing = app.config.get("TESTING")
    original_csrf = app.config.get("CSRF_ENABLED")
    app.config["TESTING"] = True
    app.config["CSRF_ENABLED"] = True
    app.config["SECRET_KEY"] = "fixation_test_key_abc123"

    try:
        with app.test_client() as client:
            # 1. Anonymous session initial visit
            init_res = client.get("/")
            anon_token = init_res.headers.get("X-CSRF-Token")
            assert anon_token is not None

            # 2. Simulate Google login
            monkeypatch.setattr(webapp, "GOOGLE_CLIENT_ID", "mock-google-client-id")

            class MockGoogleIdToken:
                @staticmethod
                def verify_oauth2_token(token, request, client_id):
                    return {
                        "sub": "google_sub_12345",
                        "name": "Dr. Marie Curie",
                        "email": "marie@curie-lab.org",
                        "picture": "https://curie.org/pic.png",
                    }

            import sys
            import types
            mock_google = types.ModuleType("google")
            mock_oauth2 = types.ModuleType("google.oauth2")
            mock_id_token = types.ModuleType("google.oauth2.id_token")
            mock_id_token.verify_oauth2_token = MockGoogleIdToken.verify_oauth2_token
            mock_auth = types.ModuleType("google.auth")
            mock_auth_transport = types.ModuleType("google.auth.transport")
            mock_requests = types.ModuleType("google.auth.transport.requests")
            mock_requests.Request = lambda: None

            monkeypatch.setitem(sys.modules, "google", mock_google)
            monkeypatch.setitem(sys.modules, "google.oauth2", mock_oauth2)
            monkeypatch.setitem(sys.modules, "google.oauth2.id_token", mock_id_token)
            monkeypatch.setitem(sys.modules, "google.auth", mock_auth)
            monkeypatch.setitem(sys.modules, "google.auth.transport", mock_auth_transport)
            monkeypatch.setitem(sys.modules, "google.auth.transport.requests", mock_requests)

            login_res = client.post(
                "/api/auth/google",
                json={"credential": "mock_valid_google_jwt"},
                headers={"X-CSRF-Token": anon_token},
            )
            assert login_res.status_code == 200
            login_data = login_res.get_json()
            assert login_data.get("ok") is True

            # CSRF token must have rotated upon login
            post_login_token = login_res.headers.get("X-CSRF-Token")
            assert post_login_token is not None
            assert post_login_token != anon_token

            # 3. Simulate logout
            logout_res = client.post(
                "/api/auth/logout",
                headers={"X-CSRF-Token": post_login_token},
            )
            assert logout_res.status_code == 200
            post_logout_token = logout_res.headers.get("X-CSRF-Token")
            assert post_logout_token is not None
            assert post_logout_token != post_login_token
            assert post_logout_token != anon_token
    finally:
        app.config["TESTING"] = original_testing
        if original_csrf is None:
            app.config.pop("CSRF_ENABLED", None)
        else:
            app.config["CSRF_ENABLED"] = original_csrf


def test_csrf_exemption_for_safe_methods(csrf_client):
    """Safe HTTP methods (GET, HEAD, OPTIONS) do not require CSRF tokens."""
    res_get = csrf_client.get("/api/v1/reactions")
    assert res_get.status_code == 200

    res_options = csrf_client.open("/api/v1/reactions", method="OPTIONS")
    assert res_options.status_code in (200, 204)
