import hashlib
import json

import pytest

from orca_orchestrator import callback_auth
from orca_orchestrator.service import OrchestratorService
from orca_orchestrator.config import StoreConfig
from orca_orchestrator.store import JobStore


def _state(job_id='chem-tools-prod-1234abcd', epoch=1, slug=None):
    slug = slug or f'{job_id}-r1'
    job = {
        'job_id': job_id, 'owner': 'alice', 'title': 'prod',
        'created_at': 1.0, 'updated_at': 2.0,
        'state': 'QUEUED', 'epoch': epoch, 'current_slug': slug,
        'chain_slugs': [job_id, slug], 'input_filename': 'molecule.inp',
    }
    doc = {'schema_version': 2, 'written_at': 2.0, 'run_token': 'r',
           'job': job, 'checkpoint': None, 'disk_report': {}, 'extra': {}}
    stable = json.dumps(doc, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str)
    doc['_digest'] = hashlib.sha256(stable.encode()).hexdigest()
    return doc


def test_callback_token_is_restart_safe_and_job_bound(monkeypatch):
    monkeypatch.setenv('ORCA_CALLBACK_SECRET', 'x' * 48)
    a = callback_auth.issue('chem-tools-a-1234abcd')
    assert callback_auth.verify('chem-tools-a-1234abcd', a)
    assert not callback_auth.verify('chem-tools-b-1234abcd', a)
    assert not callback_auth.verify('chem-tools-a-1234abcd', a + 'tamper')


def test_callback_is_accepted_without_preexisting_sqlite_record(monkeypatch, tmp_path):
    monkeypatch.setenv('ORCA_CALLBACK_SECRET', 'y' * 48)
    monkeypatch.setenv('ORCA_STATE_DIR', str(tmp_path))
    store = JobStore(StoreConfig(state_dir=str(tmp_path), db_filename='state.db'))
    svc = OrchestratorService(store=store, start_watchdog=False)
    job_id = 'chem-tools-prod-1234abcd'
    token = callback_auth.issue(job_id)
    result = svc.ingest_kernel_update({'state': _state(job_id)}, token)
    assert result['job_id'] == job_id
    rebuilt = store.require_job(job_id)
    assert rebuilt.epoch == 1
    assert rebuilt.current_slug.endswith('-r1')


def test_callback_spoof_rejected_after_restart(monkeypatch, tmp_path):
    monkeypatch.setenv('ORCA_CALLBACK_SECRET', 'z' * 48)
    monkeypatch.setenv('ORCA_STATE_DIR', str(tmp_path))
    store = JobStore(StoreConfig(state_dir=str(tmp_path), db_filename='state.db'))
    svc = OrchestratorService(store=store, start_watchdog=False)
    with pytest.raises(Exception):
        svc.ingest_kernel_update({'state': _state()}, 'v1.chem-tools-prod-1234abcd.attacker')
    assert store.get_job('chem-tools-prod-1234abcd') is None


def test_watchdog_source_has_no_cloudflare_vault_fallback():
    src = open('orca_orchestrator/watchdog.py', encoding='utf-8').read()
    assert 'credential_vault' not in src
    assert 'get_vault_manager' not in src


def test_runner_handoff_records_next_epoch_and_slug():
    src = open('orca_orchestrator/runner/kernel_runner.py', encoding='utf-8').read()
    assert 'durable_epoch = EPOCH + 1 if (state == "QUEUED" and next_slug) else EPOCH' in src
    assert 'durable_slug = next_slug if (state == "QUEUED" and next_slug)' in src
    assert 'timeout=8' in src


def test_launch_failure_is_fatal_not_restartable():
    from orca_orchestrator import orca_artifacts as art
    outcome = art.classify_outcome('', job_kind='opt', killed_by='launch')
    assert outcome.is_fatal
    assert not outcome.is_continuable
