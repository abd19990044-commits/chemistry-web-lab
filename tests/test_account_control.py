from orca_orchestrator.account_control import max_active_jobs_per_account


def test_default_active_job_limit(monkeypatch):
    monkeypatch.delenv("ORCA_MAX_ACTIVE_JOBS_PER_ACCOUNT", raising=False)
    assert max_active_jobs_per_account() == 5


def test_active_job_limit_is_configurable(monkeypatch):
    monkeypatch.setenv("ORCA_MAX_ACTIVE_JOBS_PER_ACCOUNT", "3")
    assert max_active_jobs_per_account() == 3


def test_invalid_active_job_limit_falls_back_to_five(monkeypatch):
    monkeypatch.setenv("ORCA_MAX_ACTIVE_JOBS_PER_ACCOUNT", "not-a-number")
    assert max_active_jobs_per_account() == 5


def test_active_job_limit_has_positive_floor(monkeypatch):
    monkeypatch.setenv("ORCA_MAX_ACTIVE_JOBS_PER_ACCOUNT", "0")
    assert max_active_jobs_per_account() == 1
