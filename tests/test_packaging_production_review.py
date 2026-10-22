"""Package filters apply to source content without discarding checkout parents."""
from pathlib import Path
import pytest
from tools import build_deployment_packages as builder


@pytest.mark.parametrize('parent_name', ['scratch', 'GitHub', 'venv'])
def test_checkout_parent_name_cannot_drop_runtime_packages(tmp_path, monkeypatch, parent_name):
    root = tmp_path / parent_name / 'chemistry-web-lab'
    source = root / 'api'
    source.mkdir(parents=True)
    (source / 'main.py').write_text('APP = True\n')
    (source / 'scratch').mkdir()
    (source / 'scratch' / 'temporary.py').write_text('TEMP = True\n')
    monkeypatch.setattr(builder, 'REPO_ROOT', root)
    dest = tmp_path / 'output'
    builder.copy_tree_filtered(source, dest)
    assert (dest / 'main.py').read_text() == 'APP = True\n'
    assert not (dest / 'scratch').exists()


@pytest.mark.parametrize('filename', ['.env', '.env.production', '.dev.vars', 'kaggle.json', 'state.sqlite3', 'state.sqlite3-wal', 'state.db-shm'])
def test_packages_exclude_runtime_secrets_and_databases(filename):
    assert builder.is_global_excluded(builder.REPO_ROOT / 'services' / filename)


def test_environment_example_remains_distributable():
    assert not builder.is_global_excluded(builder.REPO_ROOT / '.env.example')


def test_huggingface_package_includes_live_helpers_without_development_artifacts(tmp_path):
    import subprocess
    import sys

    target = tmp_path / 'space'
    provenance = {
        'package_mode': 'RELEASE_FROM_COMMIT', 'source_type': 'commit',
        'source_git_state': 'clean_commit', 'source_git_sha': 'test-commit',
        'source_git_dirty': False,
    }
    builder.build_huggingface_package(target, provenance)
    for relative in ('app.py', 'api/main.py', 'Dockerfile', 'docker-entrypoint.sh',
                     'schema/canonical_scientific.schema.json',
                     'tools/normalize_scientific_json.py', 'tools/build_local_agent_packages.py'):
        assert (target / relative).is_file()
    for relative in ('tests', '.github', 'cloudflare-control-plane', 'dist',
                     'tools/sync_deploy_manager.py', 'tools/build_deployment_packages.py'):
        assert not (target / relative).exists()
    # Run outside the checkout so namespace imports cannot borrow source files.
    subprocess.run([sys.executable, '-c',
                    'from tools.normalize_scientific_json import sanitize_secrets; '
                    'from tools.build_local_agent_packages import get_package_manifests; '
                    'assert len(get_package_manifests()) == 4'],
                   cwd=target, check=True, capture_output=True, text=True)
    assert 'license: mit' in (target / 'README.md').read_text()
