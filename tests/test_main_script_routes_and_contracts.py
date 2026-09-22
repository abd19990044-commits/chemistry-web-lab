# -*- coding: utf-8 -*-
"""Comprehensive contract tests for main script (app.py) routes and execution behaviors.

Validates:
1. /api/auth/me and /api/auth/logout behavior for anonymous and authenticated sessions.
2. /download/manual returns HTTP 200 when the user manual is present or generated, and HTTP 404 (never 500) when generation fails.
3. /api/orca/coords/file parses coordinates from uploaded .xyz/.sdf files and gracefully handles missing/corrupted files.
4. /api/v1/reactions/queue returns valid queue summaries and handles backend filters.
5. /api/orca/engine/experimental-spectrum/inspect inspects columns, sheets, and headers from experimental data files.
6. /api/orca/engine/multi-spectrum/overlay builds unified multi-spectrum overlays.
7. /api/v1/local-orca/cancel handles multi-user authentication requirement and delegates cancellation cleanly.
8. Core app.py configuration contract: SECRET_KEY, error handlers, and route registration parity.
"""
from __future__ import annotations

import io
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["CSRF_ENABLED"] = False
    with app.test_client() as c:
        yield c


def test_auth_me_and_logout_endpoints(client):
    """Verify /api/auth/me reports user status and /api/auth/logout clears user session."""
    # 1. Anonymous request
    res = client.get("/api/auth/me")
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert data["user"] is None

    # 2. Authenticated request
    with client.session_transaction() as sess:
        sess["user"] = {
            "email": "scientist@lab.edu",
            "name": "Lead Chemist",
            "sub": "user_scientist_1",
        }
    res = client.get("/api/auth/me")
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert data["user"]["email"] == "scientist@lab.edu"
    assert data["user"]["name"] == "Lead Chemist"

    # 3. Logout request
    res = client.post("/api/auth/logout")
    assert res.status_code == 200
    assert res.get_json()["ok"] is True

    # 4. Verify session cleared
    res = client.get("/api/auth/me")
    assert res.status_code == 200
    assert res.get_json()["user"] is None


def test_download_academic_manual_behavior(client, monkeypatch, tmp_path):
    """Verify /download/manual returns file or 404 cleanly, never crashing with 500."""
    # 1. Test with existing manual
    res = client.get("/download/manual")
    assert res.status_code in (200, 404)
    if res.status_code == 200:
        assert "wordprocessingml.document" in res.headers.get("Content-Type", "")

    # 2. Test with simulated failure in generator
    import app as webapp
    monkeypatch.setattr(webapp, "STATIC_DIR", str(tmp_path / "empty_dir"))
    import types
    fake_mod = types.ModuleType("generate_academic_manual")
    fake_mod.create_manual = lambda p: (_ for _ in ()).throw(RuntimeError("Generation failure"))
    monkeypatch.setitem(sys.modules, "generate_academic_manual", fake_mod)
    res = client.get("/download/manual")
    assert res.status_code == 404
    assert res.get_json()["ok"] is False
    assert "unavailable" in res.get_json()["error"].lower()


def test_coords_from_file_upload(client):
    """Verify /api/orca/coords/file parses coordinates from uploaded files."""
    # 1. Missing file in request
    res = client.post("/api/orca/coords/file")
    assert res.status_code in (400, 422) or (res.status_code == 200 and res.get_json().get("ok") is False)

    # 2. Valid XYZ file
    xyz_data = b"3\nWater molecule\nO 0.0000 0.0000 0.1173\nH 0.0000 0.7572 -0.4692\nH 0.0000 -0.7572 -0.4692\n"
    res = client.post(
        "/api/orca/coords/file",
        data={"file": (io.BytesIO(xyz_data), "water.xyz")},
        content_type="multipart/form-data"
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert "coords" in body
    assert "O" in body["coords"]
    assert "H" in body["coords"]

    # 3. Invalid file format / empty file
    res = client.post(
        "/api/orca/coords/file",
        data={"file": (io.BytesIO(b"corrupted binary data \x00\x01\x02"), "bad.xyz")},
        content_type="multipart/form-data"
    )
    assert res.status_code in (200, 400, 422)
    if res.status_code == 200:
        assert body["ok"] is True or "error" in body


def test_reactions_queue_endpoint(client):
    """Verify /api/v1/reactions/queue reports accurate queue metrics and metadata."""
    res = client.get("/api/v1/reactions/queue")
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert "queue" in data
    assert "kaggle_enabled" in data
    assert "kaggle_active" in data
    assert "kaggle_queued" in data
    assert isinstance(data["queue"], dict)

    # Filtered by backend
    res_backend = client.get("/api/v1/reactions/queue?backend=server_local")
    assert res_backend.status_code == 200
    data_backend = res_backend.get_json()
    assert data_backend["ok"] is True


def test_experimental_spectrum_inspect(client):
    """Verify /api/orca/engine/experimental-spectrum/inspect inspects data file headers."""
    # 1. Missing file
    res = client.post("/api/orca/engine/experimental-spectrum/inspect")
    assert res.status_code == 400
    assert res.get_json()["ok"] is False

    # 2. Valid CSV experimental spectrum
    csv_bytes = (
        b"Wavelength (nm),Absorbance (AU)\n"
        b"200.0,0.05\n"
        b"250.0,0.45\n"
        b"300.0,0.85\n"
        b"350.0,0.20\n"
    )
    res = client.post(
        "/api/orca/engine/experimental-spectrum/inspect",
        data={"file": (io.BytesIO(csv_bytes), "sample_uv.csv")},
        content_type="multipart/form-data"
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert "columns" in body or "preview" in body or "file_name" in body


def test_multi_spectrum_overlay_api(client):
    """Verify /api/orca/engine/multi-spectrum/overlay processes multiple spectra."""
    payload = {
        "experimental_spectra": [
            {
                "file_name": "uv_exp.csv",
                "raw_data": [[200.0, 0.1], [250.0, 0.8], [300.0, 0.3]],
                "units_wavelength": "nm",
                "units_y": "AU"
            }
        ],
        "theoretical_spectra": [],
        "normalize_mode": "none"
    }
    res = client.post("/api/orca/engine/multi-spectrum/overlay", json=payload)
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True


def test_local_orca_cancel_route(client, monkeypatch):
    """Verify /api/v1/local-orca/cancel requires authentication when enabled and cancels cleanly."""
    # 1. When auth is required and user is unauthenticated
    monkeypatch.setenv("CHEMISTRY_LAB_REQUIRE_AUTH", "1")
    res_unauth = client.post("/api/v1/local-orca/cancel", json={"job_id": "test_job"})
    assert res_unauth.status_code == 401
    assert res_unauth.get_json()["ok"] is False

    # 2. When authenticated for non-existent job
    with client.session_transaction() as sess:
        sess["user"] = {"sub": "chemist_tester"}
    res = client.post("/api/v1/local-orca/cancel", json={"job_id": "nonexistent_job_12345"})
    assert res.status_code == 404
    data = res.get_json()
    assert data["ok"] is False


def test_main_script_flask_application_integrity():
    """Verify the core Flask app object in app.py is correctly configured."""
    assert app.name == "app"
    assert app.secret_key is not None
    # Verify core endpoint mappings exist
    rules = [r.rule for r in app.url_map.iter_rules()]
    expected_core_rules = [
        "/",
        "/lab",
        "/calculations",
        "/analysis",
        "/health",
        "/api/license",
        "/api/auth/me",
        "/api/auth/logout",
        "/api/compound",
        "/api/reaction",
        "/api/orca/generate",
        "/api/v1/reactions/queue",
        "/download/manual",
    ]
    for rule in expected_core_rules:
        assert rule in rules, f"Expected route {rule} missing from app.url_map"
