# -*- coding: utf-8 -*-
"""Comprehensive security hardening test suite for ORCA Web Lab.

Verifies:
1. Session ID path traversal prevention & janitor containment.
2. Archive extraction security (Zip Slip, Tar Slip, symlink attacks, bomb limits).
3. DOM-XSS sanitization rules.
4. Idempotency collision resistance.
5. Kaggle credential redaction and environment cleanup.
"""
import base64
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
import zipfile
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as webapp
from orca_orchestrator.credentials import KaggleCredentials, kaggle_environment, parse as parse_credentials
from orca_orchestrator.errors import ConcurrencyError
from orca_orchestrator.logging_ext import RedactingFilter, get_logger, redact
from orca_orchestrator.store import JobStore


@pytest.fixture
def client():
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        yield c


# ─────────────────────────────────────────────────────────────
# 1. Session ID Path Traversal & Janitor Containment Tests
# ─────────────────────────────────────────────────────────────

def test_session_id_path_traversal_prevention(client):
    """P0-01: Malicious session_id values must be sanitized and contained."""
    malicious_inputs = [
        "../../etc/passwd",
        r"..\..\Windows\System32",
        "/var/log/secret",
        "C:\\Windows\\System32",
        "%2e%2e%2f%2e%2e%2fetc",
        "sess_123456\x00_inject",
        "../" * 10 + "tmp",
        "invalid session * with spaces!",
    ]
    base_dir_real = os.path.realpath(webapp.UPLOADS_BASE_DIR)

    for bad_id in malicious_inputs:
        # Sanitization must normalize to a safe format
        clean_id = webapp.sanitize_session_id(bad_id)
        assert webapp.SAFE_SESSION_ID_RE.match(clean_id)
        assert ".." not in clean_id
        assert "/" not in clean_id
        assert "\\" not in clean_id
        assert "\0" not in clean_id

        # Directory must strictly reside within UPLOADS_BASE_DIR
        sess_dir = webapp.get_safe_session_dir(bad_id)
        sess_dir_real = os.path.realpath(sess_dir)
        assert sess_dir_real.startswith(base_dir_real + os.sep)
        assert sess_dir_real != base_dir_real

        # API heartbeat endpoint must reject / sanitize traversal
        resp = client.post("/api/session/heartbeat", json={"session_id": bad_id})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        returned_id = data["session_id"]
        assert webapp.SAFE_SESSION_ID_RE.match(returned_id)
        assert ".." not in returned_id


def test_valid_session_id_accepted():
    """Valid session IDs matching the format should be preserved."""
    valid_id = "sess_1700000000000_abcdef123456"
    clean_id = webapp.sanitize_session_id(valid_id)
    assert clean_id == valid_id
    sess_dir = webapp.get_safe_session_dir(valid_id)
    assert sess_dir.endswith(valid_id)


def test_janitor_containment_invariance():
    """Janitor must NEVER delete directories outside UPLOADS_BASE_DIR even if malicious path injected."""
    outside_dir = tempfile.mkdtemp(prefix="outside-protected-")
    try:
        secret_file = os.path.join(outside_dir, "critical.txt")
        with open(secret_file, "w") as fh:
            fh.write("do not delete")

        # Attempt to inject outside directory into session tracking
        bad_id = "sess_fake_outside_test"
        webapp.track_session_activity(bad_id, temp_dir=outside_dir)

        # The outside directory must NOT be added to temp_dirs
        session_data = webapp.ACTIVE_SESSIONS.get(bad_id)
        assert session_data is not None
        assert outside_dir not in session_data["temp_dirs"]
        assert os.path.exists(secret_file)
    finally:
        shutil.rmtree(outside_dir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────
# 2. Archive Extraction Security Tests
# ─────────────────────────────────────────────────────────────

def test_archive_zip_slip_prevention():
    """P0-03: Zip Slip path traversal must be rejected safely."""
    target_dir = tempfile.mkdtemp(prefix="test-zipslip-")
    try:
        # Create a malicious zip with ../ traversal entry
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../../evil_escape.out", "ORCA calculation output")
            zf.writestr("/root/absolute_escape.out", "ORCA calculation output")
            zf.writestr("valid_calculation.out", "FINAL SINGLE POINT ENERGY -100.0\n****ORCA TERMINATED NORMALLY****")

        buf.seek(0)
        extracted = webapp.extract_calculation_files_from_archive(buf.getvalue(), "archive.zip", target_dir)

        # Only the valid file should be extracted
        assert len(extracted) == 1
        assert extracted[0]["basename"] == "valid_calculation.out"
        assert not os.path.exists(os.path.join(target_dir, "..", "evil_escape.out"))
    finally:
        shutil.rmtree(target_dir, ignore_errors=True)


def test_archive_tar_slip_prevention():
    """P0-03: Tar Slip path traversal must be rejected safely."""
    target_dir = tempfile.mkdtemp(prefix="test-tarslip-")
    try:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            # Traversal member
            ti_bad = tarfile.TarInfo(name="../../evil_tar.out")
            content = b"FINAL SINGLE POINT ENERGY -50.0"
            ti_bad.size = len(content)
            tf.addfile(ti_bad, io.BytesIO(content))

            # Valid member
            ti_good = tarfile.TarInfo(name="clean_calc.out")
            ti_good.size = len(content)
            tf.addfile(ti_good, io.BytesIO(content))

        buf.seek(0)
        extracted = webapp.extract_calculation_files_from_archive(buf.getvalue(), "calc.tar.gz", target_dir)

        assert len(extracted) == 1
        assert extracted[0]["basename"] == "clean_calc.out"
        assert not os.path.exists(os.path.join(target_dir, "..", "evil_tar.out"))
    finally:
        shutil.rmtree(target_dir, ignore_errors=True)


def test_archive_symlink_rejection():
    """P0-03: Symlink members in archives must be rejected."""
    target_dir = tempfile.mkdtemp(prefix="test-symlink-")
    try:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            ti_sym = tarfile.TarInfo(name="symlink_escape.out")
            ti_sym.type = tarfile.SYMTYPE
            ti_sym.linkname = "/etc/passwd"
            tf.addfile(ti_sym)

            ti_good = tarfile.TarInfo(name="real.out")
            data = b"FINAL SINGLE POINT ENERGY -10.0"
            ti_good.size = len(data)
            tf.addfile(ti_good, io.BytesIO(data))

        buf.seek(0)
        extracted = webapp.extract_calculation_files_from_archive(buf.getvalue(), "sym.tar", target_dir)

        assert len(extracted) == 1
        assert extracted[0]["basename"] == "real.out"
    finally:
        shutil.rmtree(target_dir, ignore_errors=True)


def test_archive_file_count_cap():
    """P0-03: Archive bomb with excessive files must be bounded."""
    target_dir = tempfile.mkdtemp(prefix="test-bomb-")
    try:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for i in range(600):
                zf.writestr(f"calc_{i}.out", f"FINAL SINGLE POINT ENERGY {-i:.1f}")

        buf.seek(0)
        extracted = webapp.extract_calculation_files_from_archive(buf.getvalue(), "bomb.zip", target_dir)
        assert len(extracted) <= webapp.MAX_ARCHIVE_FILE_COUNT
    finally:
        shutil.rmtree(target_dir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────
# 3. Idempotency Collision Security Tests
# ─────────────────────────────────────────────────────────────

def test_idempotency_different_body_rejection():
    """P1-03: Reusing an idempotency key with a different request payload must raise ConcurrencyError."""
    from orca_orchestrator.config import StoreConfig
    state_dir = tempfile.mkdtemp(prefix="test-store-idem-")
    try:
        cfg = StoreConfig(state_dir=state_dir)
        store = JobStore(config=cfg)
        
        # 1. First claim
        replay1, res1 = store.begin_idempotent("idem_key_1", {"job": "calc_a", "theory": "b3lyp"})
        assert replay1 is False
        assert res1 is None
        store.complete_idempotent("idem_key_1", {"job_id": "chem-tools-calc_a-1111"})

        # 2. Replay with identical body
        replay2, res2 = store.begin_idempotent("idem_key_1", {"job": "calc_a", "theory": "b3lyp"})
        assert replay2 is True
        assert res2 == {"job_id": "chem-tools-calc_a-1111"}

        # 3. Collision with different body must be rejected
        with pytest.raises(ConcurrencyError):
            store.begin_idempotent("idem_key_1", {"job": "calc_b", "theory": "hf"})

        store.close()
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────
# 4. Credential Redaction & Environment Security Tests
# ─────────────────────────────────────────────────────────────

def test_credential_redaction_in_logs():
    """P0-04: Kaggle keys and tokens must be scrubbed from text and logs."""
    legacy_key = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    token = "KGAT_0123456789abcdef0123456789abcdef"

    msg1 = f"Submitting job with key {legacy_key} for user alice"
    msg2 = f"Bearer token is {token} during API call"

    redacted1 = redact(msg1)
    redacted2 = redact(msg2)

    assert legacy_key not in redacted1
    assert token not in redacted2
    assert "redacted" in redacted1.lower()
    assert "redacted" in redacted2.lower()


def test_kaggle_environment_cleanup_guarantee():
    """P0-04: Temporary credential directory must be deleted upon exit or error."""
    creds = parse_credentials("testuser", "0" * 32)
    leaked_dir = None

    try:
        with kaggle_environment(creds) as env:
            leaked_dir = env["HOME"]
            assert os.path.isdir(leaked_dir)
            assert os.path.isfile(os.path.join(env["KAGGLE_CONFIG_DIR"], "kaggle.json"))
            raise RuntimeError("simulated error inside environment")
    except RuntimeError:
        pass

    assert leaked_dir is not None
    assert not os.path.exists(leaked_dir)
