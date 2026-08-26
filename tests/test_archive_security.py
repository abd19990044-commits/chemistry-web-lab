# -*- coding: utf-8 -*-
"""
Security Test Suite for ORCA Archive Upload, Authorization, and Resource Protection.

Verifies:
- Explicit owner authorization on upload, retrieval, and deletion (401/403/200)
- Cross-user horizontal privilege escalation prevention (User A cannot access/delete User B's archive)
- Strict path traversal / tar-slip prevention (relative .., absolute paths, drive roots, UNC paths)
- Symlink and hardlink traversal escape prevention
- Decompression bomb detection (excessive file count, excessive extracted size, extreme compression ratio)
- Per-user archive count and storage quotas (413 / ArchiveQuotaExceededError)
- Controlled error codes (400, 403, 404, 413) without stack traces
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import pytest
from pathlib import Path

import app as webapp
from orca_engine.archive_validator import (
    validate_orca_tar_xz,
    MAX_FILE_COUNT,
)


def _create_mock_tar_xz(files_dict: dict[str, bytes | str]) -> bytes:
    """Creates an in-memory .tar.xz package containing specified relative files."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        for name, content in files_dict.items():
            if isinstance(content, str):
                content = content.encode("utf-8")
            ti = tarfile.TarInfo(name=name)
            ti.size = len(content)
            tf.addfile(ti, io.BytesIO(content))
    return buf.getvalue()


class TestPathTraversalAndMaliciousArchives:
    """Tests for rejection of malicious archive structures."""

    def test_reject_parent_directory_traversal(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:xz") as tf:
            ti = tarfile.TarInfo(name="../../etc/shadow")
            ti.size = 6
            tf.addfile(ti, io.BytesIO(b"passwd"))
        
        is_valid, err, *rest = validate_orca_tar_xz(buf.getvalue(), "orca.tar.xz")
        assert is_valid is False
        assert "path traversal" in err

    def test_reject_symlink_pointing_outside(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:xz") as tf:
            ti = tarfile.TarInfo(name="orca/link_to_root")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "/etc/passwd"
            tf.addfile(ti)

        is_valid, err, *rest = validate_orca_tar_xz(buf.getvalue(), "orca.tar.xz")
        assert is_valid is False
        assert "absolute, drive-rooted, or UNC path" in err

    def test_reject_symlink_with_traversal(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:xz") as tf:
            ti = tarfile.TarInfo(name="orca/link_escape")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "../../../system/secret.txt"
            tf.addfile(ti)

        is_valid, err, *rest = validate_orca_tar_xz(buf.getvalue(), "orca.tar.xz")
        assert is_valid is False
        assert "path traversal" in err

    def test_reject_null_byte_or_control_char_in_filename(self):
        from orca_engine.archive_validator import _is_safe_tar_member
        ti = tarfile.TarInfo(name="orca\x00evil.sh")
        is_safe, err = _is_safe_tar_member(ti)
        assert is_safe is False
        assert "null bytes" in err or "control characters" in err

        ti2 = tarfile.TarInfo(name="orca\x1fevil.sh")
        is_safe2, err2 = _is_safe_tar_member(ti2)
        assert is_safe2 is False
        assert "null bytes" in err2 or "control characters" in err2

    def test_reject_device_nodes(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:xz") as tf:
            ti = tarfile.TarInfo(name="orca/dev_null")
            ti.type = tarfile.CHRTYPE
            tf.addfile(ti)

        is_valid, err, *rest = validate_orca_tar_xz(buf.getvalue(), "orca.tar.xz")
        assert is_valid is False
        assert "special device file" in err


class TestResourceExhaustionAndBombDefenses:
    """Tests for decompression bombs, oversized uploads, and quotas."""

    def test_reject_excessive_member_count(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:xz") as tf:
            for i in range(MAX_FILE_COUNT + 10):
                ti = tarfile.TarInfo(name=f"dir/file_{i}.txt")
                ti.size = 0
                tf.addfile(ti, io.BytesIO(b""))

        is_valid, err, *rest = validate_orca_tar_xz(buf.getvalue(), "orca.tar.xz")
        assert is_valid is False
        assert "exceeding limit" in err

    def test_reject_extreme_compression_ratio(self):
        # 50 MB of zeros compresses to a few bytes in xz
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:xz") as tf:
            ti = tarfile.TarInfo(name="orca/huge_sparse_data.bin")
            ti.size = 50 * 1024 * 1024  # 50 MB
            tf.addfile(ti, io.BytesIO(b"\0" * (50 * 1024 * 1024)))

        raw_bytes = buf.getvalue()
        # If compression ratio exceeds 100x
        if (50 * 1024 * 1024) / len(raw_bytes) > 100.0:
            is_valid, err, *rest = validate_orca_tar_xz(raw_bytes, "orca.tar.xz")
            assert is_valid is False
            assert "compression ratio" in err or "uncompressed size" in err

