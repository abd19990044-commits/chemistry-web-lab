import os

import pytest

from orca_orchestrator.config import StoreConfig
from orca_orchestrator.credentials import KaggleCredentials
from orca_orchestrator.service import OrchestratorService
from orca_orchestrator.store import JobStore
from orca_orchestrator.runner.builder import build_header


def _service(monkeypatch, tmp_path):
    monkeypatch.setenv('ORCA_CALLBACK_SECRET', 'w' * 48)
    monkeypatch.setenv('ORCA_LOCAL_MODE', '1')
    monkeypatch.setenv('ORCA_STATE_DIR', str(tmp_path))
    store = JobStore(StoreConfig(state_dir=str(tmp_path), db_filename='workflow.db'))
    return OrchestratorService(store=store, start_watchdog=False)


def _steps():
    return [
        {
            'step_name': 'OPT',
            'input_template': '! B3LYP def2-SVP Opt\n* xyz 0 1\nO 0 0 0\nH 0 0 1\nH 1 0 0\n*\n',
        },
        {
            'step_name': 'TDDFT',
            'input_template': '! PBE0 def2-SVP TightSCF\n%tddft nroots 10 end\n* xyz 0 1\nO 0 0 0\nH 0 0 1\nH 1 0 0\n*\n',
            'prerequisites': [0],
        },
    ]


def test_workflow_submission_is_kaggle_native(monkeypatch, tmp_path):
    svc = _service(monkeypatch, tmp_path)
    creds = KaggleCredentials(username='alice', key='0' * 32)
    result = svc.submit_workflow(
        creds, 'water chain', _steps(), dataset_sources=['owner/orca-linux'],
        callback_base_url='https://example.invalid',
    )
    assert result['ok'] is True
    job_id = result['step_0_job']['job_id']
    job = svc.store.require_job(job_id)
    assert job._extra['workflow_id'] == result['workflow_id']
    assert len(job._extra['workflow_plan']) == 2
    assert job._extra['step_index'] == 0
    header = build_header(job=job, epoch=0, creds=creds)
    assert header['workflow_plan'][1]['step_name'] == 'TDDFT'
    assert header['workflow_step_index'] == 0


def test_workflow_rejects_non_linear_dag(monkeypatch, tmp_path):
    svc = _service(monkeypatch, tmp_path)
    steps = _steps() + [{
        'step_name': 'SP',
        'input_template': '! B3LYP def2-TZVP\n* xyz 0 1\nO 0 0 0\nH 0 0 1\nH 1 0 0\n*\n',
        'prerequisites': [0],
    }]
    with pytest.raises(Exception):
        svc._normalise_workflow_steps(steps)


def test_workflow_rejects_gbw_dependency(monkeypatch, tmp_path):
    svc = _service(monkeypatch, tmp_path)
    steps = _steps()
    steps[1]['required_artifacts'] = ['molecule.gbw']
    with pytest.raises(Exception):
        svc._normalise_workflow_steps(steps)


def test_runner_contains_in_kaggle_stage_advance():
    src = open('orca_orchestrator/runner/kernel_runner.py', encoding='utf-8').read()
    assert 'def advance_workflow_stage' in src
    assert 'workflow_step_advanced' in src
    assert 'workflow_handoff_failed' in src
    assert 'stage_and_verify_checkpoint(' in src
