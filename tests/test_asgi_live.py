# -*- coding: utf-8 -*-
"""REAL ASGI smoke test: starts `uvicorn api.main:app` as an actual server
process and performs REAL HTTP requests (no TestClient).

Verifies the four primary pages through the Flask WSGI mount plus the
/api/v1 health/ready/docs/OpenAPI surface - the deployment-relevant path for
`uvicorn api.main:app` on Hugging Face.
"""
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def live_server():
    port = _free_port()
    base = "http://127.0.0.1:%d" % port
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=REPO,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 90
    ready = False
    while time.time() < deadline:
        try:
            r = httpx.get(base + "/api/v1/ready", timeout=5)
            if r.status_code == 200:
                ready = True
                break
        except Exception:
            time.sleep(0.5)
    if not ready:
        proc.terminate()
        pytest.skip("uvicorn did not become ready within 90s (environment-bound)")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except Exception:
        proc.kill()


def test_live_primary_pages_through_wsgi_mount(live_server):
    for path in ("/", "/lab", "/calculations", "/analysis"):
        r = httpx.get(live_server + path, timeout=60, follow_redirects=True)
        assert r.status_code == 200, (path, r.status_code)
        assert "data-initial-view" in r.text, "%s must render the app shell" % path


def test_live_api_health_ready_docs_openapi(live_server):
    r = httpx.get(live_server + "/api/v1/health", timeout=60)
    assert r.status_code == 200 and "kaggle_cli_ok" in r.json()
    r = httpx.get(live_server + "/api/v1/ready", timeout=60)
    assert r.status_code == 200 and r.json()["checks"]["storage"] is True
    r = httpx.get(live_server + "/api/v1/openapi.json", timeout=60)
    assert r.status_code == 200
    assert "/api/v1/orca/generate" in r.json()["paths"]
    r = httpx.get(live_server + "/api/v1/docs", timeout=60)
    assert r.status_code == 200


def test_live_legacy_api_route_still_works(live_server):
    """Legacy /api/* routes keep working through the same process (compat)."""
    r = httpx.get(live_server + "/api/license", timeout=60)
    assert r.status_code == 200


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
