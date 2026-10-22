"""Malformed API bodies must fail before route code or external operations."""
import pytest


@pytest.fixture
def client(monkeypatch):
    import app as webapp

    monkeypatch.setitem(webapp.app.config, "TESTING", True)
    monkeypatch.setitem(webapp.app.config, "CSRF_ENABLED", True)
    with webapp.app.test_client() as client:
        token = client.get("/api/auth/me").headers["X-CSRF-Token"]
        client.environ_base["HTTP_X_CSRF_TOKEN"] = token
        yield client


@pytest.mark.parametrize("path", ["/api/compound", "/api/orca/coords", "/api/orca/generate"])
@pytest.mark.parametrize("body", ['[1]', '[]', '"text"', 'true', 'false', '42', '0', 'null', ' ', '{"query":'])
def test_invalid_json_body_is_a_client_error(client, path, body):
    response = client.post(path, data=body, content_type="application/json")
    assert response.status_code == 400
    assert response.json["ok"] is False
    assert response.json["code"] == "INVALID_JSON_BODY"


def test_object_body_still_reaches_generator(client):
    response = client.post("/api/orca/generate", json={"coords": "He 0 0 0"})
    assert response.status_code == 200
    assert "* xyz 0 1\nHe 0 0 0\n*" in response.json["input_text"]


def test_multipart_upload_still_reaches_file_validation(client):
    response = client.post("/api/orca/coords/file", data={}, content_type="multipart/form-data")
    assert response.status_code == 400
    assert "file" in response.json["error"].lower()


def test_bodyless_command_with_json_header_is_allowed(client):
    response = client.post("/api/auth/logout", content_type="application/json")
    assert response.status_code == 200
    assert response.json["ok"] is True


def test_invalid_body_does_not_bypass_csrf(client):
    response = client.post("/api/compound", data="[1]", content_type="application/json",
                           headers={"X-CSRF-Token": "invalid"})
    assert response.status_code == 403
    assert response.json["code"] == "CSRF_FORBIDDEN"
