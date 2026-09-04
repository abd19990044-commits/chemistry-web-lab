# -*- coding: utf-8 -*-
"""FastAPI endpoint contract tests for Ephemeral Wave 3 routes."""
import pytest
from fastapi.testclient import TestClient
from api.main import app
from local_agent.security import generate_process_session, hash_token_verifier

client = TestClient(app)


def test_fastapi_packages_list():
    resp = client.get("/api/v1/local-agent/packages")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert len(data["packages"]) == 4


def test_fastapi_runtime_init_claim_and_finalize():
    sess_id, api = generate_process_session()
    v = hash_token_verifier(api)

    init_resp = client.post("/api/v1/local-agent/runtime/init", json={
        "installation_id": "inst_test_001",
        "agent_session_id": sess_id,
        "device_name": "Test PC",
        "token_verifiers": [v],
        "platform": "windows",
        "backend_kind": "local",
        "agent_version": "1.0.3",
        "protocol_version": 1,
        "capabilities": {},
    })
    assert init_resp.status_code == 200
    assert init_resp.json()["ok"] is True

    # User claims device on website
    claim_resp = client.post(
        "/api/v1/local-agent/runtime/claim",
        headers={"X-User-Id": "test_user_w3"},
        json={"connection_api": api, "custom_device_name": "My Super Lab PC"},
    )
    assert claim_resp.status_code == 200
    claim_data = claim_resp.json()
    assert claim_data["ok"] is True
    assert claim_data["agent_session_id"] == sess_id
    assert claim_data["display_name"] == "My Super Lab PC"
    # CRITICAL: Verify NO runtime_session_secret in browser response!
    assert "runtime_session_secret" not in claim_data

    # Agent process calls finalize endpoint to retrieve in-memory secret
    fin_resp = client.post("/api/v1/local-agent/runtime/finalize", json={
        "agent_session_id": sess_id,
        "connection_api": api,
    })
    assert fin_resp.status_code == 200
    fin_data = fin_resp.json()
    assert fin_data["ok"] is True
    assert fin_data["runtime_session_secret"].startswith("CRS_")

    # Verify device listing returns it
    devs_resp = client.get("/api/v1/local-agent/devices", headers={"X-User-Id": "test_user_w3"})
    assert devs_resp.status_code == 200
    assert len(devs_resp.json()["devices"]) >= 1


def test_fastapi_analysis_endpoint():
    sample_out = """
------------------------------------------------------------------------------
                                 TOTAL RUN TIME
------------------------------------------------------------------------------
                          ****ORCA TERMINATED NORMALLY****
TOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds 100 msec
"""
    resp = client.post("/api/v1/analysis/analyze", json={"output_text": sample_out})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["job_count"] == 1
