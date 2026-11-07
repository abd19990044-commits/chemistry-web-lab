"""A real CLI import catches undeclared dependencies hidden by test environments."""
import subprocess

import pytest

from tools.check_kaggle_cli import check_cli


def test_installed_cli_starts_without_cloud_credentials():
    assert check_cli().startswith("Kaggle CLI ")


def test_missing_runtime_dependency_fails_the_build_probe(monkeypatch):
    monkeypatch.setenv("KAGGLE_API_TOKEN", "synthetic-secret-not-for-probe")
    def broken_cli(args, **kwargs):
        assert "--version" in args
        assert "KAGGLE_API_TOKEN" not in kwargs["env"]
        assert "KAGGLE_KEY" not in kwargs["env"]
        return subprocess.CompletedProcess(args, 1, "", "ModuleNotFoundError: No module named 'six'")
    monkeypatch.setattr(subprocess, "run", broken_cli)
    with pytest.raises(RuntimeError, match="No module named 'six'"):
        check_cli()
