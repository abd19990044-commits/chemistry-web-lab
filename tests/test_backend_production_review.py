"""Regression coverage for production review of shared backend boundaries."""
import threading

import pytest


@pytest.fixture
def native_api():
    from fastapi.testclient import TestClient
    from api.main import app
    with TestClient(app) as client:
        yield client


def test_native_cdxml_route_imports_and_returns_a_download(native_api, monkeypatch):
    import chem_core
    monkeypatch.setattr(chem_core, "resolve_compound_to_smiles", lambda query: "CCO")
    result = native_api.post("/api/v1/compound/export-cdxml", json={"query": "ethanol"})
    assert result.status_code == 200
    assert result.headers["content-type"].startswith("chemical/x-cdxml")
    assert result.content.startswith(b"<?xml")


def test_tddft_defaults_to_ten_roots_across_api_schema(native_api):
    result = native_api.post("/api/v1/orca/generate", json={
        "coords": "He 0 0 0", "theory": "B3LYP", "basis": "def2-SVP", "calc_type": "tddft",
    })
    assert result.status_code == 200
    assert "nroots 10" in result.json()["input_text"]


def test_native_generator_propagates_domain_validation_error(native_api):
    result = native_api.post("/api/v1/orca/generate", json={"coords": "He 0 0 0", "calc_type": "unsupported"})
    assert result.status_code == 400
    assert result.json()["error"]["code"] == "BAD_REQUEST"


@pytest.mark.parametrize("field,value", [
    ("temp", "not-a-number"), ("temp", float("nan")), ("pressure", float("inf")),
    ("pressure", 0), ("maxdisk", 0), ("maxdisk", -1), ("cores", 1.5),
    ("nroots", -1), ("nroots", "bad"), ("coords", ["He 0 0 0"]),
])
def test_shared_generator_rejects_invalid_numbers_and_types(field, value):
    from services.orca_service import generate_inputs
    body, status = generate_inputs({"coords": "He 0 0 0", "calc_type": "tddft", field: value})
    assert status == 400
    assert body["ok"] is False


def test_generator_rejects_non_object_payload():
    from services.orca_service import generate_inputs
    assert generate_inputs([1, 2])[1] == 400


def test_local_lock_nonblocking_returns_false_when_contended(tmp_path):
    from services.local_orca_service import CrossProcessFileLock
    lock_path = str(tmp_path / "orca.lock")
    with CrossProcessFileLock(lock_path):
        results = []
        thread = threading.Thread(target=lambda: results.append(CrossProcessFileLock(lock_path).acquire(blocking=False)))
        thread.start()
        thread.join(timeout=2)
        assert results == [False]
    available = CrossProcessFileLock(lock_path)
    assert available.acquire(blocking=False) is True
    available.release()


def test_native_analysis_reports_termination_without_claiming_convergence(native_api):
    response = native_api.post("/api/v1/analysis/analyze", json={"output_text":
        "FINAL SINGLE POINT ENERGY -2.0\nORCA TERMINATED NORMALLY\n"})
    assert response.status_code == 200
    parsed = response.json()
    assert parsed["final_energy_hartree"] == -2.0
    assert parsed["execution_succeeded"] is True
    assert parsed["terminated_normally"] is True
    assert parsed["converged"] is None
    assert parsed["final_job"]["energy_hartree"] == -2.0


def test_ir_service_preserves_frequency_intensity_pairs(monkeypatch):
    from services import analysis_service
    monkeypatch.setattr(analysis_service, "analyze_output_text", lambda text: {"ok": True, "final_job": {
        "frequencies": [-20.0, 0.0, 0.0, 1000.0, 1200.0],
        "ir_frequencies_cm": [1000.0, 1200.0], "ir_intensities_km_mol": [5.0, 10.0],
        "zero_point_energy_hartree": 0.01,
    }})
    result = analysis_service.extract_frequencies_and_ir("unused")
    assert result["frequencies"] == [1000.0, 1200.0]
    assert result["ir_intensities"] == [5.0, 10.0]
    assert result["has_imaginary"] is True
    assert len(result["vibrational_frequencies"]) == 5


def test_readiness_returns_unavailable_http_status(native_api, monkeypatch):
    from api.routes import health
    monkeypatch.setattr(health.health_service, "build_ready", lambda **kwargs: {"ok": False, "ready": False})
    result = native_api.get("/api/v1/ready")
    assert result.status_code == 503
    assert result.json()["ready"] is False


def test_concurrent_health_probes_do_not_share_a_file(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import tempfile
    import kaggle_runner
    from services.health_service import build_health
    monkeypatch.setattr(kaggle_runner, "cli_health", lambda: {"ok": True, "version": "test"})
    real_tempfile = tempfile.TemporaryFile
    opened_files = []
    def tracked_file(**kwargs):
        file = real_tempfile(**kwargs)
        opened_files.append(file)
        return file
    monkeypatch.setattr(tempfile, "TemporaryFile", tracked_file)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: build_health(orchestrator_available=False, orca_engine_available=True), range(16)))
    assert all(result["storage_writable"] for result in results)
    assert len(opened_files) == 16
    assert all(file.closed for file in opened_files)


def test_bearer_identities_hash_entire_tokens_without_exposing_prefix(monkeypatch):
    import hashlib
    from services.auth_service import get_authenticated_owner
    first = "shared-prefix-123456789-primary-secret"
    second = "shared-prefix-123456789-second-secret"
    monkeypatch.setenv("CHEMISTRY_LAB_API_TOKEN", first)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", second)
    monkeypatch.delenv("CHEMISTRY_LAB_API_TOKEN_OWNER_ID", raising=False)
    first_owner = get_authenticated_owner(headers={"Authorization": "Bearer " + first})
    second_owner = get_authenticated_owner(headers={"Authorization": "Bearer " + second})
    assert first_owner != second_owner
    assert first_owner == "token_user_" + hashlib.sha256(first.encode()).hexdigest()
    assert first[:16] not in first_owner


def test_legacy_bearer_owner_override_is_server_scoped_to_primary_token(monkeypatch):
    from services.auth_service import get_authenticated_owner
    primary = "shared-prefix-123456789-primary"
    secondary = "shared-prefix-123456789-secondary"
    legacy_owner = "token_user_" + primary[:16]
    monkeypatch.setenv("CHEMISTRY_LAB_API_TOKEN", primary)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", secondary)
    monkeypatch.setenv("CHEMISTRY_LAB_API_TOKEN_OWNER_ID", legacy_owner)
    assert get_authenticated_owner(headers={"Authorization": "Bearer " + primary}) == legacy_owner
    assert get_authenticated_owner(headers={"Authorization": "Bearer " + secondary}) != legacy_owner


def test_non_ascii_invalid_bearer_is_rejected_without_crashing(monkeypatch):
    from services.auth_service import is_valid_bearer_token
    monkeypatch.setenv("CHEMISTRY_LAB_API_TOKEN", "configured-ascii-token")
    assert is_valid_bearer_token("untrusted-credential-\u2713") is False


def test_native_csrf_unicode_input_is_rejected(monkeypatch):
    from types import SimpleNamespace
    from services import auth_service
    monkeypatch.setattr(auth_service, "_signed_flask_session_from_request", lambda request: {"user": {"sub": "owner"}, "csrf_token": "ascii-token"})
    request = SimpleNamespace(method="POST", headers={"x-csrf-token": "untrusted-\u2713"})
    assert auth_service.browser_csrf_is_valid(request) is False


def test_flask_csrf_unicode_input_is_rejected(monkeypatch):
    import app as webapp
    monkeypatch.setitem(webapp.app.config, "CSRF_ENABLED", True)
    with webapp.app.test_client() as client:
        with client.session_transaction() as session:
            session["csrf_token"] = "ascii-token"
        response = client.post("/api/v1/reactions", json={"csrf_token": "untrusted-\u2713"})
    assert response.status_code == 403
    assert response.json["code"] == "CSRF_FORBIDDEN"
