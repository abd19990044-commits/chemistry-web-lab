"""MIT distribution terms and application-facing license contract."""
from pathlib import Path
import json
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('path', ['LICENSE', 'LICENSE.txt', 'orca_engine/LICENSE', 'cloudflare-control-plane/LICENSE'])
def test_canonical_mit_license(path):
    text = (ROOT / path).read_text(encoding='utf-8')
    assert text == (ROOT / 'LICENSE').read_text(encoding='utf-8')
    assert text.startswith('MIT License\n')
    assert 'Copyright (c) 2026 Abdulsalam S. Hasan' in text
    for clause in ('Permission is hereby granted, free of charge', 'sublicense, and/or sell',
                   'The above copyright notice and this permission notice',
                   'THE SOFTWARE IS PROVIDED "AS IS"'):
        assert clause in text


@pytest.mark.parametrize('path', ['pyproject.toml', 'orca_engine/pyproject.toml'])
def test_python_distribution_license(path):
    data = tomllib.loads((ROOT / path).read_text(encoding='utf-8'))
    assert data['project']['license'] == 'MIT'
    assert data['project']['license-files'] == ['LICENSE']


def test_metadata_and_citation_license():
    assert json.loads((ROOT / 'PROJECT_METADATA.json').read_text())['license']['spdx_id'] == 'MIT'
    assert json.loads((ROOT / 'cloudflare-control-plane/package.json').read_text())['license'] == 'MIT'
    for path in ('CITATION.cff', 'orca_engine/CITATION.cff'):
        assert 'license: MIT' in (ROOT / path).read_text()


def test_license_api():
    from app import app
    with app.test_client() as client:
        response = client.get('/api/license')
        assert response.status_code == 200
        payload = response.get_json()
        assert payload['spdx_id'] == 'MIT'
        assert payload['license_name'] == 'MIT License'
        assert 'commercial_use' in payload['permitted_uses']
        assert 'sublicensing' in payload['permitted_uses']
        assert 'notices' in payload['conditions']
        assert 'non_commercial_standard' not in payload


@pytest.mark.parametrize('path', ['README.md', 'CONTRIBUTING.md', 'THIRD_PARTY_LICENSES.md',
                                  'orca_engine/README.md', 'orca_engine/CONTRIBUTING.md',
                                  'templates/index.html', 'tools/build_deployment_packages.py',
                                  'scripts/generate_academic_manual.py'])
def test_no_stale_application_license(path):
    text = (ROOT / path).read_text(encoding='utf-8')
    assert 'MIT' in text
    assert 'ORCA Web Lab Academic and Non-Commercial License' not in text
    assert 'requires a separate written **Commercial License**' not in text


def test_downloadable_agent_packages_carry_license_notices():
    import zipfile
    from tools.build_local_agent_packages import build_all_packages
    for package in build_all_packages().values():
        with zipfile.ZipFile(package['file_path']) as archive:
            assert archive.read('LICENSE') == (ROOT / 'LICENSE').read_bytes()
            assert archive.read('THIRD_PARTY_LICENSES.md') == (ROOT / 'THIRD_PARTY_LICENSES.md').read_bytes()
