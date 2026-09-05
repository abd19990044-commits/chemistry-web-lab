# -*- coding: utf-8 -*-
"""Comprehensive test suite for Kaggle execution authorization passcode and Privacy Policy verification.

Validates:
1. When secret is unset in environment:
   - /api/kaggle/config reports passcode_required: False
   - /api/kaggle/submit does not require a passcode
   - An empty passcode passes smoothly
2. When secret is empty string or whitespace:
   - /api/kaggle/config reports passcode_required: False
   - Submissions proceed without passcode requirement
3. When secret is configured:
   - Missing passcode is rejected with HTTP 403 and code INVALID_KAGGLE_PASSCODE
   - Incorrect passcode is rejected with HTTP 403
   - Valid passcode (via form, JSON, or header) passes through the authorization gate
4. All other platform features remain completely independent and operate without passcode:
   - Thermochemistry calculations
   - Spectra imports
   - Local/HPC companion agent job submissions
   - Chemical search
5. Privacy policy contains direct user operation notice and explicit declaration that
   the site does not operate Kaggle as a third party.
"""
from __future__ import annotations

import io
import json
import os
from unittest.mock import patch, MagicMock
import pytest

import app as webapp


@pytest.fixture
def client():
    webapp.app.config["TESTING"] = True
    webapp.app.config["SECRET_KEY"] = "test_passcode_suite_secret_key"
    with webapp.app.test_client() as c:
        yield c


class MockSubmitResult:
    url = "https://www.kaggle.com/code/valid_user/chem-tools-test"
    job_id = "chem-tools-test"
    title = "test_job"
    replayed = False


@pytest.fixture(autouse=True)
def mock_submits():
    with patch("orca_orchestrator.legacy_compat.enforce_capacity"), \
         patch("orca_orchestrator.legacy_compat.get_service") as mock_legacy_gs, \
         patch("orca_orchestrator.api.enforce_capacity"), \
         patch("orca_orchestrator.api.get_service") as mock_api_gs, \
         patch("services.kaggle_service.submit_job") as mock_ks:
        mock_legacy_gs.return_value.submit.return_value = MockSubmitResult()
        mock_api_gs.return_value.submit.return_value = MagicMock(
            to_dict=lambda: {"job_id": "chem-tools-test", "url": "https://www.kaggle.com/code/valid_user/chem-tools-test"}
        )
        mock_ks.return_value = ({"ok": True, "job_id": "chem-tools-test"}, 200)
        yield


def _clean_env(monkeypatch):
    """Ensure all passcode-related environment variables are cleared."""
    for var in ("KAGGLE_EXECUTION_PASSCODE", "KAGGLE_ACCESS_CODE", "KAGGLE_PASSCODE"):
        monkeypatch.delenv(var, raising=False)


# ===========================================================================
# 1. Unset Secret Behavior
# ===========================================================================
def test_passcode_unset_allows_free_execution(client, monkeypatch):
    """When no execution passcode secret is set, submission proceeds without a passcode."""
    _clean_env(monkeypatch)

    # Config endpoint reports false
    resp = client.get("/api/kaggle/config")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["passcode_required"] is False

    # Submission attempt succeeds through the gate without passcode
    sub_resp = client.post("/api/kaggle/submit", data={
        "kaggle_username": "valid_user",
        "kaggle_key": "a" * 32,
        "dataset_sources": "user/dataset",
        "input_filename": "test.inp",
        "input_content": "! HF Def2-SVP\n* xyz 0 1\nO 0 0 0\n*",
        "job_name": "test_job",
    })
    assert sub_resp.status_code == 200
    assert sub_resp.get_json()["ok"] is True


def test_passcode_unset_allows_blank_passcode(client, monkeypatch):
    """When no secret is set, an empty/blank passcode passed by the user is accepted."""
    _clean_env(monkeypatch)

    sub_resp = client.post("/api/kaggle/submit", data={
        "kaggle_username": "valid_user",
        "kaggle_key": "a" * 32,
        "dataset_sources": "user/dataset",
        "input_filename": "test.inp",
        "input_content": "! HF Def2-SVP\n* xyz 0 1\nO 0 0 0\n*",
        "job_name": "test_job",
        "kaggle_passcode": "",
    })
    assert sub_resp.status_code == 200
    assert sub_resp.get_json()["ok"] is True


# ===========================================================================
# 2. Empty / Whitespace Secret Behavior
# ===========================================================================
def test_passcode_empty_string_treated_as_unset(client, monkeypatch):
    """If server admin sets KAGGLE_EXECUTION_PASSCODE to empty string or spaces, no code required."""
    monkeypatch.setenv("KAGGLE_EXECUTION_PASSCODE", "   ")

    resp = client.get("/api/kaggle/config")
    assert resp.status_code == 200
    assert resp.get_json()["passcode_required"] is False

    sub_resp = client.post("/api/kaggle/submit", data={
        "kaggle_username": "valid_user",
        "kaggle_key": "a" * 32,
        "dataset_sources": "user/dataset",
        "input_filename": "test.inp",
        "input_content": "! HF Def2-SVP\n* xyz 0 1\nO 0 0 0\n*",
        "job_name": "test_job",
    })
    assert sub_resp.status_code == 200


# ===========================================================================
# 3. Configured Secret Behavior (Rejection & Acceptance)
# ===========================================================================
def test_configured_secret_rejects_missing_or_wrong_passcode(client, monkeypatch):
    """When secret is set, submission without matching passcode is blocked with 403."""
    secret = "SiteSecret2026!"
    monkeypatch.setenv("KAGGLE_EXECUTION_PASSCODE", secret)

    # Config endpoint confirms required
    resp = client.get("/api/kaggle/config")
    assert resp.status_code == 200
    assert resp.get_json()["passcode_required"] is True

    # 1. Missing passcode
    sub_resp1 = client.post("/api/kaggle/submit", data={
        "kaggle_username": "valid_user",
        "kaggle_key": "a" * 32,
        "dataset_sources": "user/dataset",
        "input_filename": "test.inp",
        "input_content": "! HF Def2-SVP\n* xyz 0 1\nO 0 0 0\n*",
    })
    assert sub_resp1.status_code == 403
    data1 = sub_resp1.get_json()
    assert data1["ok"] is False
    assert data1["error"] == "INVALID_KAGGLE_PASSCODE"

    # 2. Empty passcode
    sub_resp2 = client.post("/api/kaggle/submit", data={
        "kaggle_username": "valid_user",
        "kaggle_key": "a" * 32,
        "dataset_sources": "user/dataset",
        "input_filename": "test.inp",
        "input_content": "! HF Def2-SVP\n* xyz 0 1\nO 0 0 0\n*",
        "kaggle_passcode": "   ",
    })
    assert sub_resp2.status_code == 403
    assert sub_resp2.get_json()["error"] == "INVALID_KAGGLE_PASSCODE"

    # 3. Incorrect passcode
    sub_resp3 = client.post("/api/kaggle/submit", data={
        "kaggle_username": "valid_user",
        "kaggle_key": "a" * 32,
        "dataset_sources": "user/dataset",
        "input_filename": "test.inp",
        "input_content": "! HF Def2-SVP\n* xyz 0 1\nO 0 0 0\n*",
        "kaggle_passcode": "WrongSecret",
    })
    assert sub_resp3.status_code == 403
    assert sub_resp3.get_json()["error"] == "INVALID_KAGGLE_PASSCODE"


def test_configured_secret_accepts_valid_passcode_in_form(client, monkeypatch):
    """When secret is set, supplying correct passcode in form field succeeds."""
    secret = "SiteSecret2026!"
    monkeypatch.setenv("KAGGLE_EXECUTION_PASSCODE", secret)

    sub_resp = client.post("/api/kaggle/submit", data={
        "kaggle_username": "valid_user",
        "kaggle_key": "a" * 32,
        "dataset_sources": "user/dataset",
        "input_filename": "test.inp",
        "input_content": "! HF Def2-SVP\n* xyz 0 1\nO 0 0 0\n*",
        "job_name": "test_job",
        "kaggle_passcode": secret,
    })
    assert sub_resp.status_code == 200
    assert sub_resp.get_json()["ok"] is True


def test_configured_secret_accepts_valid_passcode_in_header(client, monkeypatch):
    """When secret is set, supplying correct passcode in X-Kaggle-Passcode header succeeds."""
    secret = "HeaderSecret999"
    monkeypatch.setenv("KAGGLE_ACCESS_CODE", secret)

    sub_resp = client.post(
        "/api/kaggle/submit",
        data={
            "kaggle_username": "valid_user",
            "kaggle_key": "a" * 32,
            "dataset_sources": "user/dataset",
            "input_filename": "test.inp",
            "input_content": "! HF Def2-SVP\n* xyz 0 1\nO 0 0 0\n*",
            "job_name": "test_job",
        },
        headers={"X-Kaggle-Passcode": secret},
    )
    assert sub_resp.status_code == 200
    assert sub_resp.get_json()["ok"] is True


# ===========================================================================
# 4. Other Platform Features Remain Independent and Unaffected
# ===========================================================================
def test_other_features_unaffected_when_secret_configured(client, monkeypatch):
    """Even when Kaggle execution secret is strictly enforced, other features operate without code."""
    monkeypatch.setenv("KAGGLE_EXECUTION_PASSCODE", "StrictLockdownPasscode!")

    # 1. Chemical Formula / SMILES Search
    pubchem_resp = client.get("/api/pubchem/search?q=benzene")
    # Endpoint should not return 403 INVALID_KAGGLE_PASSCODE
    assert pubchem_resp.status_code in (200, 404, 502, 503)
    if pubchem_resp.is_json:
        assert pubchem_resp.get_json().get("error") != "INVALID_KAGGLE_PASSCODE"

    # 2. Thermochemistry Endpoint
    thermo_resp = client.post("/api/thermo/calculate-reaction", json={
        "reactants": [],
        "products": [],
        "temperature": 298.15,
        "pressure": 1.0,
    })
    # Should evaluate normally without 403
    assert thermo_resp.status_code != 403
    if thermo_resp.is_json:
        assert thermo_resp.get_json().get("error") != "INVALID_KAGGLE_PASSCODE"

    # 3. Spectra Output Import
    import_resp = client.post("/api/orca/engine/import-orca-output", data={
        "output_content": "ORCA calculation output dummy",
    })
    assert import_resp.status_code != 403
    if import_resp.is_json:
        assert import_resp.get_json().get("error") != "INVALID_KAGGLE_PASSCODE"


# ===========================================================================
# 5. Privacy Policy & Legal Content Verification
# ===========================================================================
def test_privacy_policy_contains_no_third_party_and_user_operation_statements(client):
    """Verify that the rendered HTML contains the explicit privacy & legal statements."""
    resp = client.get("/lab")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # 1. Direct User Operation & No Third-Party Intermediation in Section 2
    assert "Direct User Operation &amp; No Third-Party Intermediation" in html
    assert "does not run, manage, or broker Kaggle as a third-party intermediary" in html

    # 2. Section 4 direct execution and user sole responsibility
    assert "Direct Personal Execution &amp; User Responsibility:" in html
    assert "solely and entirely responsible for compliance with Kaggle's Terms of Service" in html
    assert "bears full legal and operational responsibility for any violation thereof" in html

    # 3. Section 7 GDPR / EU notice
    assert "The Site is designed for individual user operation and does not run Kaggle as a third-party broker or intermediary" in html

    # 4. Section 11 Third-party services & sole responsibility
    assert "The Site does not run Kaggle as a third party on your behalf" in html
    assert "user is solely and exclusively responsible for compliance with Kaggle's policies and terms" in html


# ===========================================================================
# 6. Verify Passcode Endpoint Tests (/api/kaggle/verify-passcode)
# ===========================================================================
def test_verify_passcode_endpoint_unset_allows_blank(client, monkeypatch):
    """When secret is unset, /api/kaggle/verify-passcode accepts blank passcode (local mode)."""
    _clean_env(monkeypatch)
    resp = client.post("/api/kaggle/verify-passcode", json={"passcode": ""})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["passcode_required"] is False


def test_verify_passcode_endpoint_configured_rejects_wrong(client, monkeypatch):
    """When secret is configured, wrong or empty passcode is rejected with HTTP 403."""
    monkeypatch.setenv("KAGGLE_EXECUTION_PASSCODE", "site_super_secret_999")

    # Empty passcode
    resp_empty = client.post("/api/kaggle/verify-passcode", json={"passcode": ""})
    assert resp_empty.status_code == 403
    assert resp_empty.get_json()["error"] == "INVALID_KAGGLE_PASSCODE"

    # Wrong passcode
    resp_wrong = client.post("/api/kaggle/verify-passcode", json={"passcode": "wrong_code"})
    assert resp_wrong.status_code == 403
    assert resp_wrong.get_json()["error"] == "INVALID_KAGGLE_PASSCODE"


def test_verify_passcode_endpoint_configured_accepts_valid(client, monkeypatch):
    """When secret is configured, correct passcode returns HTTP 200."""
    monkeypatch.setenv("KAGGLE_EXECUTION_PASSCODE", "site_super_secret_999")
    resp = client.post("/api/kaggle/verify-passcode", json={"passcode": "site_super_secret_999"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["passcode_required"] is True


def test_verify_passcode_endpoint_configured_accepts_header(client, monkeypatch):
    """Header X-Kaggle-Passcode is accepted for passcode verification."""
    monkeypatch.setenv("KAGGLE_EXECUTION_PASSCODE", "site_super_secret_999")
    resp = client.post("/api/kaggle/verify-passcode", headers={"X-Kaggle-Passcode": "site_super_secret_999"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True


def test_readme_and_security_documentation_contain_passcode_and_liability_rules():
    """Verify README.md, DEPLOY.md, and SECURITY.md contain local vs cloud passcode rules and user liability."""
    with open("README.md", "r", encoding="utf-8") as f:
        readme = f.read()
    assert "Direct Operation Without Third-Party Intermediation" in readme
    assert "solely and entirely responsible for compliance with Kaggle's Terms of Service" in readme
    assert "leave the password field empty and press **Enter**" in readme
    assert "KAGGLE_EXECUTION_PASSCODE" in readme

    with open("DEPLOY.md", "r", encoding="utf-8") as f:
        deploy = f.read()
    assert "KAGGLE_EXECUTION_PASSCODE" in deploy
    assert "leave the password input empty and press Enter" in deploy

    with open("SECURITY.md", "r", encoding="utf-8") as f:
        sec = f.read()
    assert "Direct User Execution & Cloud Passcode Protection" in sec
    assert "KAGGLE_EXECUTION_PASSCODE" in sec
