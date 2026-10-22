"""Regression coverage for the UI embedded by the official Spaces page."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('path', ['/', '/lab', '/calculations', '/analysis'])
def test_space_pages_allow_the_official_parent_only(monkeypatch, path):
    import app as webapp
    monkeypatch.setattr(webapp, '_IS_HF_SPACE', True)
    response = webapp.app.test_client().get(path)
    assert response.status_code == 200
    assert 'X-Frame-Options' not in response.headers
    csp = response.headers['Content-Security-Policy']
    assert "frame-ancestors 'self' https://huggingface.co;" in csp
    assert 'https://*.huggingface.co' not in csp
    assert "form-action 'self'" in csp


def test_request_headers_cannot_enable_space_embedding(monkeypatch):
    import app as webapp
    monkeypatch.setattr(webapp, '_IS_HF_SPACE', False)
    response = webapp.app.test_client().get('/', headers={
        'Origin': 'https://huggingface.co',
        'Referer': 'https://huggingface.co/spaces/example/app',
    })
    assert response.headers['X-Frame-Options'] == 'DENY'
    assert "frame-ancestors 'none';" in response.headers['Content-Security-Policy']


@pytest.mark.parametrize('space_variable', ['SPACE_ID', 'HF_SPACE_ID'])
def test_embedded_session_cookies_and_csrf_work_together(tmp_path, space_variable):
    env = os.environ.copy()
    env.pop('SPACE_ID', None)
    env.pop('HF_SPACE_ID', None)
    env[space_variable] = 'mc2hf1999/orcaweb'
    env['SECRET_KEY'] = 'embedding-test-secret-0123456789abcdef'
    env['ORCA_STATE_DIR'] = str(tmp_path)
    env['CHEMISTRY_LAB_STATE_DIR'] = str(tmp_path)
    script = """
import app as webapp
app = webapp.app
app.config['TESTING'] = True
app.config['CSRF_ENABLED'] = True
with app.test_client() as client:
    response = client.get('/', base_url='https://example.hf.space')
    cookies = response.headers.getlist('Set-Cookie')
    for name in ('session=', 'csrf_token='):
        cookie = next(value for value in cookies if value.startswith(name))
        assert 'SameSite=None' in cookie, cookie
        assert 'Secure' in cookie, cookie
        assert 'Partitioned' in cookie, cookie
    token = response.headers['X-CSRF-Token']
    body = {'coords': 'He 0 0 0'}
    assert client.post('/api/orca/generate', json=body,
                       base_url='https://example.hf.space').status_code == 403
    response = client.post('/api/orca/generate', json=body,
                           base_url='https://example.hf.space',
                           headers={'X-CSRF-Token': token})
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.json['ok'] is True
"""
    result = subprocess.run([sys.executable, '-c', script], env=env,
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
