"""Regression coverage for companion execution safety and recovery."""
from unittest.mock import Mock

import pytest

from local_agent.agent import LocalCompanionAgent
from local_agent.config import AgentConfig
from local_agent.hpc.base import HpcValidationError, validate_numeric_resource, validate_scheduler_identifier, validate_filename
from local_agent.hpc.lsf import LsfAdapter
from local_agent.hpc.pbs import PbsAdapter
from local_agent.hpc.slurm import SlurmAdapter
from local_agent.security import ProcessLock


def test_release_keeps_lock_inode_when_next_owner_acquires_during_close(tmp_path, monkeypatch):
    """A third process must not get a new inode while the next owner holds the lock."""
    import local_agent.security as security

    first, second, third = [ProcessLock(tmp_path) for _ in range(3)]
    assert first.acquire()
    original_close = security.os.close
    first_fd = first._fd

    def close_and_acquire_next(fd):
        original_close(fd)
        if fd == first_fd:
            assert second.acquire()

    monkeypatch.setattr(security.os, "close", close_and_acquire_next)
    try:
        first.release()
        assert not third.acquire(), "release must not unlink an inode already locked by the next owner"
    finally:
        monkeypatch.setattr(security.os, "close", original_close)
        first.release()
        second.release()
        third.release()


def test_reacquiring_same_lock_does_not_leak_descriptor(tmp_path):
    first, second = ProcessLock(tmp_path), ProcessLock(tmp_path)
    try:
        assert first.acquire()
        fd = first._fd
        assert first.acquire()
        assert first._fd == fd
        first.release()
        assert second.acquire()
    finally:
        first.release()
        second.release()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "NaN", "Infinity", True, False])
@pytest.mark.parametrize("allow_float", [False, True])
def test_hpc_resources_reject_nonfinite_and_boolean_values(value, allow_float):
    with pytest.raises(HpcValidationError):
        validate_numeric_resource(value, "allocation", allow_float=allow_float)


@pytest.mark.parametrize("value", [1.5, 2.9, -1.1])
def test_integer_resource_cannot_silently_truncate(value):
    with pytest.raises(HpcValidationError):
        validate_numeric_resource(value, "cpu_cores")


@pytest.mark.parametrize("validator", [validate_scheduler_identifier, validate_filename])
@pytest.mark.parametrize("value", ["-a", "--help", "-123"])
def test_scheduler_values_cannot_be_command_options(validator, value):
    with pytest.raises(HpcValidationError):
        validator(value)


@pytest.mark.parametrize("adapter", [SlurmAdapter, PbsAdapter, LsfAdapter])
def test_cancellation_rejects_option_before_invoking_scheduler(adapter, monkeypatch):
    run = Mock()
    monkeypatch.setattr("subprocess.run", run)
    assert adapter({}).cancel_job("-a") is False
    run.assert_not_called()


def test_corrupt_recovery_registry_blocks_new_job_dispatch():
    agent = object.__new__(LocalCompanionAgent)
    agent.runtime_session_secret = "runtime_secret"
    agent._recovery_checked = False
    agent._load_process_registry = Mock(side_effect=RuntimeError("corrupt"))
    agent._flush_completion_outbox = Mock()
    agent.config = AgentConfig()
    agent.poll_and_execute_jobs()
    assert agent._recovery_checked is False
    agent._flush_completion_outbox.assert_not_called()


def test_live_pid_identity_change_during_recovery_never_finalizes(monkeypatch):
    agent = object.__new__(LocalCompanionAgent)
    record = {"pid": 123, "job_id": "job123"}
    original_process = Mock()
    original_process.is_running.return_value = True
    original_process.status.return_value = "running"
    agent._load_process_registry = Mock(return_value=record)
    agent._verified_process = Mock(side_effect=[original_process, None])
    agent._register_process_with_server = Mock(return_value=True)
    agent._tail_progress = Mock()
    agent._finalize_registered_process = Mock()
    agent.config = AgentConfig()
    agent.running = True
    agent._stop_event = Mock()
    agent._stop_event.is_set.return_value = False
    agent._recovery_checked = True
    monkeypatch.setattr("local_agent.agent.psutil.pid_exists", lambda pid: True)
    probe = Mock()
    probe.status.return_value = "running"
    monkeypatch.setattr("local_agent.agent.psutil.Process", lambda pid: probe)
    assert agent._recover_registered_process() is True
    assert agent._recovery_checked is False
    agent._finalize_registered_process.assert_not_called()


def test_hpc_backend_never_launches_orca_on_login_node():
    agent = object.__new__(LocalCompanionAgent)
    agent.config = AgentConfig(backend_kind="hpc", scheduler_type="slurm")
    agent._queue_completion = Mock()
    agent._flush_completion_outbox = Mock()
    agent.find_orca_executable = Mock(side_effect=AssertionError("must not execute directly"))
    agent._run_job({"job_id": "hpc123", "job_name": "test", "input_text": "! SP"})
    payload = agent._queue_completion.call_args.args[1]
    assert payload["exit_code"] != 0
    assert "HPC_SCHEDULER_EXECUTION_UNAVAILABLE" in payload["error_message"]
    agent._flush_completion_outbox.assert_called_once()
    agent.find_orca_executable.assert_not_called()


def test_zombie_pid_reuse_does_not_bypass_create_time_check(monkeypatch):
    import psutil

    process = Mock()
    process.create_time.return_value = 200.0
    process.status.return_value = psutil.STATUS_ZOMBIE
    monkeypatch.setattr("local_agent.agent.psutil.Process", lambda pid: process)
    assert LocalCompanionAgent._verified_process({"pid": 123, "process_start_time": 100.0}) is None


def test_finished_recovery_rebinds_session_before_completion(monkeypatch):
    import psutil

    agent = object.__new__(LocalCompanionAgent)
    record = {"pid": 123, "job_id": "finished123"}
    finished = Mock()
    finished.is_running.return_value = True
    finished.status.return_value = psutil.STATUS_ZOMBIE
    agent._load_process_registry = Mock(return_value=record)
    agent._verified_process = Mock(return_value=finished)
    calls = []
    agent._register_process_with_server = Mock(side_effect=lambda *a, **k: calls.append("rebind") or True)
    agent._tail_progress = Mock(side_effect=lambda *a, **k: calls.append("progress"))
    agent._finalize_registered_process = Mock(side_effect=lambda *a: calls.append("complete"))
    assert agent._recover_registered_process() is True
    assert calls == ["rebind", "progress", "complete"]


def test_quiet_job_heartbeat_receives_cancellation(tmp_path, monkeypatch):
    agent = object.__new__(LocalCompanionAgent)
    output = tmp_path / "quiet.out"
    output.write_text("")
    record = {"job_id": "quiet123", "output_path": str(output), "last_read_offset": 0}
    agent.runtime_session_secret = "runtime_secret"
    agent.agent_session_id = "session123"
    agent.config = AgentConfig()
    agent._write_process_registry = Mock()
    process = Mock()
    agent._verified_process = Mock(return_value=process)
    agent._terminate_process_tree = Mock()
    response = Mock(status_code=200)
    response.json.return_value = {"ok": True, "cancel_requested": True}
    post = Mock(return_value=response)
    monkeypatch.setattr("local_agent.agent.requests.post", post)
    assert agent._tail_progress(record, force=True)
    assert post.call_args.kwargs["json"] == {"stdout_chunk": ""}
    agent._terminate_process_tree.assert_called_once_with(process)


@pytest.mark.parametrize("registry_failure", [True, False])
def test_monitor_failure_cannot_orphan_or_overwrite_running_process(tmp_path, monkeypatch, registry_failure):
    """Only a durably registered child may survive a monitor error for adoption."""
    agent = object.__new__(LocalCompanionAgent)
    agent.config = AgentConfig(data_dir=str(tmp_path), orca_working_dir=str(tmp_path))
    agent.runtime_session_secret = "runtime_secret"
    agent.agent_session_id = "session123"
    agent._recovery_checked = True
    agent.process_registry_path = tmp_path / "active.json"
    agent.find_orca_executable = Mock(return_value="/fake/orca")
    agent._set_current_process = Mock()
    agent._queue_completion = Mock()
    agent._flush_completion_outbox = Mock()
    agent._write_process_registry = Mock(side_effect=OSError("disk full") if registry_failure else None)
    agent._register_process_with_server = Mock(side_effect=RuntimeError("monitor failed"))
    process = Mock(pid=123)
    process.poll.return_value = None
    agent._terminate_process_tree = Mock(side_effect=lambda p: setattr(p.poll, "return_value", 1))
    monkeypatch.setattr("local_agent.agent.subprocess.Popen", lambda *a, **k: process)
    observed = Mock()
    observed.cmdline.return_value = ["/fake/orca", "test.inp"]
    observed.create_time.return_value = 123.5
    monkeypatch.setattr("local_agent.agent.psutil.Process", lambda pid: observed)
    agent._run_job({"job_id": "monitor123", "job_name": "test", "input_text": "! SP"})
    if registry_failure:
        agent._terminate_process_tree.assert_called_once_with(process)
        assert agent._queue_completion.call_args.args[1]["exit_code"] == 1
    else:
        agent._terminate_process_tree.assert_not_called()
        agent._queue_completion.assert_not_called()
        assert agent._recovery_checked is False
