"""Latency repairs preserve credential checks, ownership and scientific eligibility."""
import threading
import zipfile
from types import SimpleNamespace

import pytest

from orca_orchestrator.config import StoreConfig
from orca_orchestrator.credentials import parse
from orca_orchestrator.kaggle_api import KaggleClient
from orca_orchestrator.models import JobManifest
from orca_orchestrator.service import OrchestratorService
from orca_orchestrator.states import JobState
from orca_orchestrator.store import JobStore


@pytest.fixture
def service(tmp_path):
    svc = OrchestratorService(JobStore(StoreConfig(state_dir=str(tmp_path))), start_watchdog=False)
    yield svc
    svc.shutdown()


def test_authentication_reads_only_one_notebook_page(service, monkeypatch):
    calls = []
    monkeypatch.setattr(KaggleClient, 'list_kernels', lambda self, **kw: calls.append(kw) or [])
    creds = service.authenticate('latencychemist', '0' * 32)
    assert creds.username == 'latencychemist'
    assert calls == [{'page_size': 1, 'max_pages': 1}]


def test_login_does_not_perform_remote_job_and_cloudflare_reconciliation(service, monkeypatch):
    import app as web
    import orca_orchestrator.legacy_compat as legacy
    monkeypatch.setattr(legacy, 'get_service', lambda: service)
    monkeypatch.setattr(service, 'authenticate', lambda u, k: parse(u, k))
    monkeypatch.setattr(service, 'list_jobs', lambda c: pytest.fail('Login must not perform full reconciliation'))
    monkeypatch.setattr(service.cf_controller, 'reconcile_user_session', lambda *a: pytest.fail('No Cloudflare recovery on login'))
    with web.app.test_client() as client:
        response = client.post('/api/kaggle/login', json={'kaggle_username': 'latencychemist', 'kaggle_key': '0' * 32})
    assert response.status_code == 200
    assert response.get_json()['sync_required'] is True


def test_background_metadata_deduplicates_inflight_work():
    from orca_orchestrator.background_io import defer
    ready, release, done = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def blocked():
        calls.append(1)
        ready.set()
        try:
            assert release.wait(4)
        finally:
            done.set()
    try:
        assert defer(('latency-test', id(ready)), blocked)
        assert ready.wait(1)
        assert not defer(('latency-test', id(ready)), blocked)
        assert calls == [1]
    finally:
        release.set()
        assert done.wait(2)


def test_geometry_dependency_download_excludes_large_checkpoints(service, monkeypatch, tmp_path):
    from services import kaggle_service
    import orca_orchestrator.service as services
    creds = parse('latencychemist', '0' * 32)
    job = JobManifest.create(job_id='chem-tools-geometry-latency', owner=creds.username, title='Opt', input_filename='molecule.inp', original_input_sha256='0' * 64)
    job.enter_state(JobState.FINISHED)
    job.current_slug = job.job_id
    service.store.put_job(job)
    monkeypatch.setattr(service, 'ensure_authenticated', lambda c: c)
    monkeypatch.setattr(service.result_store, 'retrieve', lambda *a: (None, None))
    monkeypatch.setattr(services, 'get_service', lambda: service)
    calls = []
    def download(client, slug, **kwargs):
        calls.append(kwargs)
        out = tmp_path / 'remote-text'
        out.mkdir()
        (out / 'molecule.inp').write_text('! B3LYP def2-SVP Opt\n* xyz 0 1\nO 0 0 0\nH 0 0 1\nH 0 1 0\n*\n')
        (out / 'molecule.out').write_text('CARTESIAN COORDINATES (ANGSTROEM)\n---------------------------------\nO 0.0 0.0 0.1\nH 0.0 0.75 -0.4\nH 0.0 -0.75 -0.4\n\nTHE OPTIMIZATION HAS CONVERGED\nFINAL SINGLE POINT ENERGY -76.1\nORCA TERMINATED NORMALLY\n')
        return str(out)
    monkeypatch.setattr(KaggleClient, 'fetch_output', download)
    monkeypatch.setattr(service, 'fetch_results', lambda *a, **k: pytest.fail('Must not download the full result archive'))
    result, status = kaggle_service.extract_opt_coords(creds.username, creds.key, job.job_id)
    assert status == 200, result
    assert result['converged'] is True
    assert '0.75' in result['coords']
    assert calls[0]['file_pattern'] == r'(?i).*\.(out|inp|xyz)$'
    assert calls[0]['timeout'] == 25
    assert not (tmp_path / 'remote-text').exists()


def test_geometry_fast_path_rejects_other_owner_before_downloading(service, monkeypatch):
    job = JobManifest.create(job_id='chem-tools-private-geometry', owner='otherchemist', title='Opt', input_filename='m.inp', original_input_sha256='0' * 64)
    service.store.put_job(job)
    monkeypatch.setattr(service, 'ensure_authenticated', lambda c: c)
    monkeypatch.setattr(KaggleClient, 'fetch_output', lambda *a, **k: pytest.fail('Owner rejection must precede network I/O'))
    with pytest.raises(Exception, match='Access denied'):
        service.fetch_geometry_results(parse('latencychemist', '0' * 32), job.job_id)
