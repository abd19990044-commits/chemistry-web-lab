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
