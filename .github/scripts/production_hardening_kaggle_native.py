from pathlib import Path


def replace_once(path: str, old: str, new: str):
    p = Path(path)
    text = p.read_text(encoding='utf-8')
    count = text.count(old)
    assert count == 1, f'{path}: expected exactly one match, found {count}'
    p.write_text(text.replace(old, new, 1), encoding='utf-8')

# ---------------------------------------------------------------------------
# 1) Restart-safe callback authentication: signed token, no DB dependency.
# ---------------------------------------------------------------------------
Path('orca_orchestrator/callback_auth.py').write_text(r'''# -*- coding: utf-8 -*-
"""Stateless authentication for Kaggle -> site callbacks.

The token is bound to a single ORCA job with HMAC-SHA256. The server can verify
it after a Space restart without SQLite, Cloudflare, or any other persistent
control plane. Kaggle only receives the opaque per-job token, never the server
secret.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from .errors import ValidationError

_ENV = "ORCA_CALLBACK_SECRET"
_FALLBACK_ENV = "FLASK_SECRET_KEY"
_CONTEXT = b"orca-kaggle-callback-v1\x00"


def _secret() -> bytes:
    raw = (os.environ.get(_ENV) or os.environ.get(_FALLBACK_ENV) or "").strip()
    # Fail closed in production. Development/local tests may explicitly set a
    # deterministic secret in the test environment.
    if len(raw.encode('utf-8')) < 32:
        raise ValidationError(
            "ORCA callback authentication is not configured. Set ORCA_CALLBACK_SECRET "
            "to a random value of at least 32 bytes in the deployment secrets."
        )
    return raw.encode('utf-8')


def is_configured() -> bool:
    try:
        _secret()
        return True
    except ValidationError:
        return False


def issue(job_id: str) -> str:
    mac = hmac.new(_secret(), _CONTEXT + job_id.encode('utf-8'), hashlib.sha256).digest()
    sig = base64.urlsafe_b64encode(mac).decode('ascii').rstrip('=')
    return f"v1.{job_id}.{sig}"


def verify(job_id: str, token: str) -> bool:
    try:
        version, token_job, sig = str(token or '').split('.', 2)
    except ValueError:
        return False
    if version != 'v1' or token_job != job_id:
        return False
    try:
        expected = issue(job_id)
    except ValidationError:
        return False
    return hmac.compare_digest(expected, str(token))


def fingerprint(token: str) -> str:
    return hashlib.sha256(str(token or '').encode('utf-8')).hexdigest()
''', encoding='utf-8')

# service.py: imports and token generation/verification.
p = Path('orca_orchestrator/service.py')
text = p.read_text(encoding='utf-8')
text = text.replace('import secrets\n', '')
text = text.replace('from . import ledger as ledger_mod\n', 'from . import ledger as ledger_mod\nfrom . import callback_auth\n', 1)
text = text.replace('"callback_token": secrets.token_urlsafe(32),', '"callback_token": callback_auth.issue(job_id),', 1)

old = '''        if not callback_token or len(callback_token) < 24:\n            raise ValidationError("kernel callback token is missing or invalid")\n'''
new = '''        if not callback_auth.verify(job_id, callback_token):\n            raise ValidationError("kernel callback token is missing, invalid, or not bound to this job")\n'''
assert old in text
text = text.replace(old, new, 1)

old = '''        current = self.store.get_job(job_id)\n        if current is not None:\n            expected = str((current._extra or {}).get("callback_token") or "")\n            if expected and not secrets.compare_digest(expected, callback_token):\n                raise ValidationError("kernel callback token does not match this job")\n            manifest = current\n            incoming = JobManifest.from_dict(job_data)\n            # Preserve local-only callback/workflow metadata while copying durable fields.\n            keep_extra = dict(current._extra or {})\n            incoming._extra.update({k: v for k, v in keep_extra.items() if k not in incoming._extra})\n            incoming._extra["callback_token"] = callback_token\n            manifest = incoming\n        else:\n            manifest = JobManifest.from_dict(job_data)\n            manifest._extra["callback_token"] = callback_token\n'''
new = '''        current = self.store.get_job(job_id)\n        incoming = JobManifest.from_dict(job_data)\n        if current is not None:\n            # Preserve local-only metadata while copying the durable Kaggle view.\n            keep_extra = dict(current._extra or {})\n            incoming._extra.update({k: v for k, v in keep_extra.items() if k not in incoming._extra})\n        incoming._extra["callback_token"] = callback_token\n        manifest = incoming\n'''
assert old in text
text = text.replace(old, new, 1)

# Make health explicitly report callback configuration.
needle = '''            "startup_recovery": self.startup_report,\n            "config": {\n'''
replacement = '''            "startup_recovery": self.startup_report,\n            "callback_auth": {\n                "configured": callback_auth.is_configured(),\n                "mode": "stateless_hmac",\n                "required_secret": "ORCA_CALLBACK_SECRET",\n            },\n            "config": {\n'''
assert needle in text
text = text.replace(needle, replacement, 1)
p.write_text(text, encoding='utf-8')

# ---------------------------------------------------------------------------
# 2) Remove hidden Cloudflare credential-vault dependency from watchdog.
# ---------------------------------------------------------------------------
p = Path('orca_orchestrator/watchdog.py')
text = p.read_text(encoding='utf-8')
old = '''                creds = self.broker.get(job.owner)\n                if creds is None:\n                    try:\n                        vm = self.vault_manager\n                        if vm is None:\n                            from .credential_vault import get_vault_manager\n                            vm = get_vault_manager()\n                        creds = vm.load_credentials(job.owner)\n                    except Exception as exc:\n                        log.debug("Could not load credentials from vault for owner %s: %s", job.owner, exc)\n\n                if creds is None:\n'''
new = '''                # Runtime is deliberately Cloudflare-free. The server may only act\n                # with credentials recently supplied by the user; otherwise the\n                # self-continuing Kaggle kernel remains responsible for the chain.\n                creds = self.broker.get(job.owner)\n                if creds is None:\n'''
assert old in text
text = text.replace(old, new, 1)
text = text.replace(
    '"credentials for its owner are cached or available in vault; the Kaggle kernel remains "',
    '"credentials for its owner are cached in RAM; the Kaggle kernel remains "',
)
p.write_text(text, encoding='utf-8')

# ---------------------------------------------------------------------------
# 3) My Jobs: distinguish a temporary list miss from a verified deletion.
# ---------------------------------------------------------------------------
p = Path('orca_orchestrator/service.py')
text = p.read_text(encoding='utf-8')
old = '''        try:\n            remote = ledger_mod.discover_jobs(client)\n        except Exception as exc:\n            log.warning("Kaggle bulk listing unavailable; serving warm local cache: %s", exc)\n            remote = []\n\n        merged, seen = [], set()\n'''
new = '''        listing_ok = True\n        try:\n            remote = ledger_mod.discover_jobs(client)\n        except Exception as exc:\n            listing_ok = False\n            log.warning("Kaggle bulk listing unavailable; serving warm local cache: %s", exc)\n            remote = []\n\n        merged, seen = [], set()\n'''
assert old in text
text = text.replace(old, new, 1)

old = '''        for job in self.store.list_jobs(auth_creds.username):\n            if job.job_id not in seen:\n                described = self.describe(job)\n                described["not_returned_by_kaggle_listing"] = True\n                described["deleted_on_kaggle"] = False\n                described["state_source"] = "local_cache_pending_kaggle"\n                merged.append(described)\n'''
new = '''        for job in self.store.list_jobs(auth_creds.username):\n            if job.job_id not in seen:\n                described = self.describe(job)\n                described["not_returned_by_kaggle_listing"] = True\n                described["deleted_on_kaggle"] = False\n                described["state_source"] = "local_cache_pending_kaggle"\n                if listing_ok:\n                    # A successful bulk listing that omits a job is ambiguous for a\n                    # short period after submission. Resolve that ambiguity with an\n                    # exact status lookup; only a verified 404 becomes deletion.\n                    try:\n                        exact = client.kernel_exists(job.current_slug or job.job_id)\n                        if exact is None:\n                            described["deleted_on_kaggle"] = True\n                            described["state_source"] = "kaggle_verified_deleted"\n                            described["phase"] = "Deleted on Kaggle"\n                            described["note"] = "The Kaggle kernel no longer exists."\n                    except OrchestratorError:\n                        pass\n                merged.append(described)\n'''
assert old in text
text = text.replace(old, new, 1)
p.write_text(text, encoding='utf-8')

# ---------------------------------------------------------------------------
# 4) Runner: preserve callback metadata in ledger, accurate handoff epoch, and
#    cap wake/callback latency so networking never consumes the handoff reserve.
# ---------------------------------------------------------------------------
p = Path('orca_orchestrator/runner/kernel_runner.py')
text = p.read_text(encoding='utf-8')
text = text.replace('    for attempt in range(4):\n', '    for attempt in range(2):\n', 1)
text = text.replace('urllib.request.urlopen(req, timeout=20 + attempt * 10)', 'urllib.request.urlopen(req, timeout=5 + attempt * 2)', 1)
text = text.replace('time.sleep(3 * (attempt + 1))', 'time.sleep(1.5 * (attempt + 1))', 1)
text = text.replace('with urllib.request.urlopen(req, timeout=45) as resp:', 'with urllib.request.urlopen(req, timeout=8) as resp:', 1)

old = '''    job = {\n        "job_id": JOB_ID, "owner": H["kaggle_username"], "title": H.get("title") or JOB_ID,\n        "created_at": START_TIME, "updated_at": time.time(),\n        "state": state, "epoch": EPOCH,\n        "current_slug": os.environ.get("KAGGLE_KERNEL_SLUG", "") or JOB_ID,\n'''
new = '''    # A successful successor push means the durable view has advanced even\n    # though this document is written by the predecessor window. Recording the\n    # next epoch/current slug prevents My Jobs from briefly regressing to the old\n    # window after a handoff callback.\n    durable_epoch = EPOCH + 1 if (state == "QUEUED" and next_slug) else EPOCH\n    durable_slug = next_slug if (state == "QUEUED" and next_slug) else (os.environ.get("KAGGLE_KERNEL_SLUG", "") or JOB_ID)\n    job = {\n        "job_id": JOB_ID, "owner": H["kaggle_username"], "title": H.get("title") or JOB_ID,\n        "created_at": START_TIME, "updated_at": time.time(),\n        "state": state, "epoch": durable_epoch,\n        "current_slug": durable_slug,\n'''
assert old in text
text = text.replace(old, new, 1)
old = '''        "last_note": note, "last_error": error,\n        "disk_report": disk_snapshot(),\n    }\n'''
new = '''        "last_note": note, "last_error": error,\n        "disk_report": disk_snapshot(),\n        # Non-secret callback metadata survives server restarts in Kaggle.\n        "callback_base_url": H.get("callback_base_url") or "",\n        "callback_token_sha256": __import__("hashlib").sha256(\n            str(H.get("callback_token") or "").encode("utf-8")\n        ).hexdigest() if H.get("callback_token") else "",\n    }\n'''
assert old in text
text = text.replace(old, new, 1)
old = '''        "extra": dict(extra or {}, next_slug=next_slug, next_url=next_url,\n                      outcome=outcome.to_dict() if outcome is not None else None),\n'''
new = '''        "extra": dict(extra or {}, next_slug=next_slug, next_url=next_url,\n                      producer_epoch=EPOCH, callback_base_url=H.get("callback_base_url") or "",\n                      callback_token_sha256=job.get("callback_token_sha256") or "",\n                      outcome=outcome.to_dict() if outcome is not None else None),\n'''
assert old in text
text = text.replace(old, new, 1)
p.write_text(text, encoding='utf-8')

# ---------------------------------------------------------------------------
# 5) Environment template: remove Cloudflare production requirements and add
#    callback secret explicitly.
# ---------------------------------------------------------------------------
Path('.env.example').write_text('''# Chemistry Lab Environment Configuration Template\nFLASK_ENV=production\nFLASK_SECRET_KEY=change_this_to_a_long_random_secret\nHOST=0.0.0.0\nPORT=7860\n\n# Required in production for stateless Kaggle -> site callback authentication.\n# Use a random secret >= 32 bytes and store it only in deployment secrets.\nORCA_CALLBACK_SECRET=replace_with_at_least_32_random_bytes\n\n# Kaggle credentials are supplied per user and kept in RAM on the site.\n# The private Kaggle runner carries its own credential only so it can launch\n# self-continuation windows; Cloudflare/D1 is not part of the ORCA runtime.\nKAGGLE_USERNAME=optional_local_development_username\nKAGGLE_KEY=optional_local_development_key\n''', encoding='utf-8')

# ---------------------------------------------------------------------------
# 6) Production regression tests for the new architecture.
# ---------------------------------------------------------------------------
Path('tests/test_kaggle_native_production.py').write_text(r'''import hashlib
import json

import pytest

from orca_orchestrator import callback_auth
from orca_orchestrator.credentials import KaggleCredentials
from orca_orchestrator.models import JobManifest
from orca_orchestrator.service import OrchestratorService
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
    store = JobStore(str(tmp_path / 'state.db'))
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
    store = JobStore(str(tmp_path / 'state.db'))
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
''', encoding='utf-8')

print('Kaggle-native production hardening patch applied')
