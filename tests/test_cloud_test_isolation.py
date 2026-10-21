"""The test harness refuses accidental live cloud operations."""
import subprocess

import pytest
import requests


@pytest.mark.parametrize('command', [['kaggle', 'kernels', 'push', '-p', 'unused'],
                                      ['python', '-m', 'kaggle', 'kernels', 'status', 'unused'],
                                      ['python', '-c', 'from kaggle.cli import main; main()', 'kernels', 'status']])
def test_unmocked_kaggle_cli_is_blocked(command):
    with pytest.raises(RuntimeError, match='Live Kaggle CLI disabled'):
        subprocess.run(command, check=True)


def test_unmocked_kaggle_http_is_blocked():
    with pytest.raises(RuntimeError, match='Live Kaggle HTTP disabled'):
        requests.get('https://www.kaggle.com/api/v1/kernels/list', timeout=1)
