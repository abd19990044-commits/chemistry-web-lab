"""HPC downloads cannot accidentally start ORCA on a login node."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name == 'nt' or not shutil.which('bash'), reason='Unix Bash launcher check')
@pytest.mark.parametrize('name', ['install_hpc_agent.sh', 'start_hpc_agent.sh'])
def test_hpc_launcher_refuses_login_node_before_setup(tmp_path, name):
    env = dict(os.environ)
    for key in ('SLURM_JOB_ID', 'PBS_JOBID', 'LSB_JOBID'):
        env.pop(key, None)
    data = tmp_path / 'must-not-be-created'
    env['XDG_DATA_HOME'] = str(data)
    script = ROOT / 'bootstrap/hpc' / name
    syntax = subprocess.run(['bash', '-n', str(script)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr
    proc = subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True, timeout=5)
    assert proc.returncode == 2
    assert 'existing compute allocation' in proc.stderr
    assert not data.exists()
