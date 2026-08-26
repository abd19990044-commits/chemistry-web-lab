# -*- coding: utf-8 -*-
"""
Tests for ORCA Source Selection (Kaggle Dataset, Google Drive, .tar.xz Archive Upload)
and Full ZIP Result Downloads with Molden Support.
"""
import io
import json
import os
import tarfile
import tempfile
import zipfile
import pytest

from orca_engine.archive_validator import (
    ArchiveValidationError,
    OrcaArchiveMetadata,
    validate_orca_tar_xz,
)
from orca_orchestrator.result_store import (
    ArtifactRecord,
    ResultArtifactStore,
    ResultDurabilityState,
    ResultManifest,
)
import app as webapp


def _create_mock_tar_xz(members_dict: dict[str, bytes]) -> bytes:
    """Helper to create an in-memory .tar.xz archive with specified members."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        for name, data in members_dict.items():
            ti = tarfile.TarInfo(name=name)
            ti.size = len(data)
            ti.mtime = 1700000000
            ti.mode = 0o755 if "orca" in name else 0o644
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


class TestOrcaArchiveValidation:
    """Tests for .tar.xz ORCA distribution archive validation and security."""

    def test_valid_tar_xz_with_orca_binary(self):
        tar_bytes = _create_mock_tar_xz({
            "orca_6_0_0/orca": b"#!/bin/sh\necho ORCA",
            "orca_6_0_0/orca_2mkl": b"#!/bin/sh\necho 2mkl",
            "orca_6_0_0/liborca.so": b"\x7fELF...",
        })
        is_valid, err, members, *rest = validate_orca_tar_xz(tar_bytes, "orca_6_0_0_linux.tar.xz")
        assert is_valid is True
        assert err == ""
        assert len(members) == 3
        assert "orca_6_0_0/orca" in members

    def test_reject_invalid_extension(self):
        is_valid, err, *rest = validate_orca_tar_xz(b"some bytes", "orca.zip")
        assert is_valid is False
        assert "Only .tar.xz archives are accepted" in err

        is_valid2, err2, *rest = validate_orca_tar_xz(b"some bytes", "molecule.inp")
        assert is_valid2 is False
        assert "Only .tar.xz archives are accepted" in err2

    def test_reject_tar_slip_path_traversal(self):
        # Create tar with ../ traversal
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:xz") as tf:
            ti = tarfile.TarInfo(name="../etc/passwd")
            ti.size = 4
            tf.addfile(ti, io.BytesIO(b"root"))
        
        is_valid, err, *rest = validate_orca_tar_xz(buf.getvalue(), "orca.tar.xz")
        assert is_valid is False
        assert "path traversal" in err

    def test_reject_absolute_path_tar_member(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:xz") as tf:
            ti = tarfile.TarInfo(name="/usr/bin/orca")
            ti.size = 4
            tf.addfile(ti, io.BytesIO(b"root"))
        
        is_valid, err, *rest = validate_orca_tar_xz(buf.getvalue(), "orca.tar.xz")
        assert is_valid is False
        assert "absolute, drive-rooted, or UNC path" in err

    def test_reject_windows_drive_tar_member(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:xz") as tf:
            ti = tarfile.TarInfo(name="C:/Windows/System32/orca.exe")
            ti.size = 4
            tf.addfile(ti, io.BytesIO(b"root"))
        
        is_valid, err, *rest = validate_orca_tar_xz(buf.getvalue(), "orca.tar.xz")
        assert is_valid is False
        assert "absolute, drive-rooted, or UNC path" in err

class TestResultStoreMoldenInclusion:
    """Tests for Molden file inclusion and provenance in ResultArtifactStore."""

    def test_store_includes_molden_artifact(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = ResultArtifactStore(base_dir=os.path.join(tmp_dir, "results"))
            
            # Create a mock calculation results.zip containing out, xyz, and molden
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("benzene.out", "ORCA TERMINATED NORMALLY\nFINAL SINGLE POINT ENERGY -232.123")
                zf.writestr("benzene.xyz", "12\n\nC 0 0 0\n...")
                zf.writestr("benzene.property.txt", "ORCA PROPERTY FILE")
                zf.writestr("benzene.molden.input", "[Molden Format]\n[Atoms] AU\nC 1 6 0 0 0\n[MO]\nSym=1a\nEne=-10.5\nSpin=Alpha\nOccup=2.0\n")

            stage_zip = os.path.join(tmp_dir, "mock_results.zip")
            with open(stage_zip, "wb") as f:
                f.write(zip_buf.getvalue())

            manifest = store.store(
                job_id="chem-tools-test-molden-123",
                owner="chemist",
                raw_zip_or_dir_path=stage_zip,
                provenance={"title": "Benzene SP", "job_kind": "SP"},
            )

            assert manifest.job_id == "chem-tools-test-molden-123"
            art_types = {a.artifact_type: a for a in manifest.artifacts}
            assert "archive_zip" in art_types
            assert "orca_output" in art_types
            assert "geometry_xyz" in art_types
            assert "molden" in art_types
            assert art_types["molden"].name == "benzene.molden.input"
            assert len(art_types["molden"].sha256) == 64

            # Retrieve and verify
            zip_path, retrieved_manifest = store.retrieve("chem-tools-test-molden-123", "chemist")
            assert zip_path is not None and os.path.isfile(zip_path)
            assert retrieved_manifest.bundle_sha256 == manifest.bundle_sha256
            retrieved_types = [a.artifact_type for a in retrieved_manifest.artifacts]
            assert "molden" in retrieved_types
