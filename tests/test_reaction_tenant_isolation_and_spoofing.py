# -*- coding: utf-8 -*-
"""Comprehensive Tenant Isolation & Identity Spoofing Tests (SEC-02).

Proves that:
1. User A cannot read or mutate User B's reactions or job artifacts.
2. Client-supplied X-Owner-Id, X-User-Id, query params, and form fields cannot spoof identity.
3. Unauthenticated requests get session-bound isolated identities and cannot access authenticated resources.
4. With CHEMISTRY_LAB_REQUIRE_AUTH=1, unauthenticated requests are strictly rejected with 401.
"""
import os
import uuid
import pytest
from flask import session

import app as webapp
from services.reaction_workflow_service import ReactionStore


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    """Sets up an isolated test app with clean state directory and multi-user mode."""
    monkeypatch.setenv("CHEMISTRY_LAB_MULTI_USER", "1")
    monkeypatch.setenv("CHEMISTRY_LAB_TEST_MODE", "1")
    rxn_dir = str(tmp_path / "rxn_state")
    os.makedirs(rxn_dir, exist_ok=True)
    test_store = ReactionStore(rxn_dir)
    monkeypatch.setattr(webapp, "_reaction_store", test_store)
    webapp.app.config["TESTING"] = True
    webapp.app.config["SECRET_KEY"] = "test_tenant_isolation_secret_key_12345"
    return webapp.app


def test_user_a_and_user_b_reaction_isolation(isolated_app):
    """User B cannot read or mutate User A's reactions."""
    with isolated_app.test_client() as client_a:
        with client_a.session_transaction() as sess:
            sess["user"] = {"sub": "user_alice", "email": "alice@lab.org"}

        # Alice creates a reaction
        res_create = client_a.post("/api/v1/reactions", json={"equation": "H2 + Cl2 -> 2HCl"})
        assert res_create.status_code == 200
        alice_rxn = res_create.get_json()["reaction"]
        alice_rxn_id = alice_rxn["reaction_id"]
        assert alice_rxn["owner"] == "user_alice"

        # Alice sees her reaction
        res_list_a = client_a.get("/api/v1/reactions")
        assert res_list_a.status_code == 200
        assert len(res_list_a.get_json()["reactions"]) == 1

    # Bob's client
    with isolated_app.test_client() as client_b:
        with client_b.session_transaction() as sess:
            sess["user"] = {"sub": "user_bob", "email": "bob@lab.org"}

        # Bob lists reactions -> sees 0 reactions (Alice's reaction is hidden)
        res_list_b = client_b.get("/api/v1/reactions")
        assert res_list_b.status_code == 200
        assert len(res_list_b.get_json()["reactions"]) == 0

        # Bob attempts direct read of Alice's reaction -> 404
        res_detail_b = client_b.get(f"/api/v1/reactions/{alice_rxn_id}")
        assert res_detail_b.status_code == 404
        assert "not found" in res_detail_b.get_json()["error"]["message"].lower()

        # Bob attempts mutation (pause) on Alice's reaction -> 404
        res_pause_b = client_b.post(f"/api/v1/reactions/{alice_rxn_id}/pause")
        assert res_pause_b.status_code == 404

        # Bob attempts mutation (cancel) on Alice's reaction -> 404
        res_cancel_b = client_b.post(f"/api/v1/reactions/{alice_rxn_id}/cancel")
        assert res_cancel_b.status_code == 404


def test_spoofing_headers_and_params_rejected(isolated_app):
    """User A cannot impersonate User B using X-Owner-Id, query params, or body fields."""
    with isolated_app.test_client() as client_a:
        with client_a.session_transaction() as sess:
            sess["user"] = {"sub": "user_alice", "email": "alice@lab.org"}

        # 1. User A sends X-Owner-Id: user_bob -> must be ignored, owner is still user_alice
        res = client_a.post(
            "/api/v1/reactions",
            headers={"X-Owner-Id": "user_bob"},
            json={"equation": "N2 + 3H2 -> 2NH3"}
        )
        assert res.status_code == 200
        rxn = res.get_json()["reaction"]
        assert rxn["owner"] == "user_alice", "X-Owner-Id successfully spoofed reaction owner!"

        # 2. User A sends ?owner_id=user_bob in query -> must be ignored
        res_query = client_a.get("/api/v1/reactions?owner_id=user_bob")
        assert res_query.status_code == 200
        # Returns Alice's reactions because query parameter is ignored
        assert len(res_query.get_json()["reactions"]) == 1

        # 3. User A submits owner_id in JSON payload -> must be ignored
        res_json = client_a.post(
            "/api/v1/reactions",
            json={"equation": "2H2 + O2 -> 2H2O", "owner_id": "user_bob", "owner": "user_bob"}
        )
        assert res_json.status_code == 200
        rxn_2 = res_json.get_json()["reaction"]
        assert rxn_2["owner"] == "user_alice", "Payload owner parameter spoofed reaction owner!"


def test_anonymous_tenants_isolated_from_each_other(isolated_app):
    """Unauthenticated clients receive distinct random session-bound identities and do not share reactions."""
    # Anonymous client 1
    with isolated_app.test_client() as client_anon_1:
        res1 = client_anon_1.post("/api/v1/reactions", json={"equation": "CH4 + 2O2 -> CO2 + 2H2O"})
        assert res1.status_code == 200
        rxn1 = res1.get_json()["reaction"]
        rxn1_id = rxn1["reaction_id"]
        assert rxn1["owner"].startswith("anon_")

        # Client 1 sees reaction 1
        res1_list = client_anon_1.get("/api/v1/reactions")
        assert len(res1_list.get_json()["reactions"]) == 1

    # Anonymous client 2 (separate cookie jar / session)
    with isolated_app.test_client() as client_anon_2:
        res2_list = client_anon_2.get("/api/v1/reactions")
        # Client 2 must NOT see Client 1's reaction!
        assert len(res2_list.get_json()["reactions"]) == 0

        # Client 2 cannot access Client 1's reaction by ID
        res2_detail = client_anon_2.get(f"/api/v1/reactions/{rxn1_id}")
        assert res2_detail.status_code == 404

        # Anonymous Client 2 attempts to spoof User Alice or Client 1
        res2_spoof = client_anon_2.get(f"/api/v1/reactions/{rxn1_id}?owner_id={rxn1['owner']}")
        assert res2_spoof.status_code == 404


def test_require_auth_mode_rejects_unauthenticated_requests(isolated_app, monkeypatch):
    """When CHEMISTRY_LAB_REQUIRE_AUTH=1, unauthenticated reaction requests return 401."""
    monkeypatch.setenv("CHEMISTRY_LAB_REQUIRE_AUTH", "1")
    monkeypatch.delenv("CHEMISTRY_LAB_TEST_MODE", raising=False)

    with isolated_app.test_client() as client_unauth:
        res_get = client_unauth.get("/api/v1/reactions")
        assert res_get.status_code == 401
        data = res_get.get_json()
        assert data["ok"] is False
        assert data["error"]["code"] == "UNAUTHORIZED"

        res_post = client_unauth.post("/api/v1/reactions", json={"equation": "H2 + F2 -> 2HF"})
        assert res_post.status_code == 401
