# -*- coding: utf-8 -*-
"""MaxDisk enforcement + Molden-in-archive acceptance tests.

MaxDisk = 20000 MB is mandatory for EVERY calculation Chemistry Lab executes:
the runners normalize (never duplicate) the %maxdisk MaxDisk directive. The
final downloadable archive must contain <base>.molden.input when generation
succeeded, and must contain NO molden file when it did not.
"""
import hashlib
import importlib
import os
import stat
import sys
import tarfile
import zipfile
from io import BytesIO

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orca_orchestrator import orca_artifacts as art  # noqa: E402


# ---------------------------------------------------------------------------
# MaxDisk directive enforcement (orca_artifacts)
# ---------------------------------------------------------------------------
def test_maxdisk_inserted_when_absent():
    inp = "! B3LYP Opt\n* xyz 0 1\nO 0 0 0\n*"
    out, effective, action = art.set_maxdisk(inp, 20000)
    assert out.count("MaxDisk 20000") == 1
    assert "%scf" in out
    assert "%maxdisk" not in out
    assert (effective, action) == (20000, "inserted")


def test_maxdisk_explicit_20000_is_preserved():
    inp = "! B3LYP Opt\n%maxdisk\n  MaxDisk 20000\nend\n* xyz 0 1\nO 0 0 0\n"
    out, effective, action = art.set_maxdisk(inp, 20000)
    assert (effective, action) == (20000, "preserved")
    assert out.count("MaxDisk") == 1
    assert "%maxdisk" not in out
    assert "%scf" in out


def test_maxdisk_custom_valid_value_is_preserved():
    """The generated runner must NOT silently replace a caller-supplied valid
    MaxDisk with the 20000 default: a future cloud backend is allowed to raise
    the budget."""
    inp = "! B3LYP Opt\n%maxdisk\n  MaxDisk 50000\nend\n* xyz 0 1\nO 0 0 0\n"
    out, effective, action = art.set_maxdisk(inp, 20000)
    assert (effective, action) == (50000, "preserved")
    assert "MaxDisk 50000" in out and out.count("MaxDisk") == 1
    assert "%maxdisk" not in out
    assert "%scf" in out


def test_maxdisk_invalid_value_is_rejected_to_the_default():
    inp = "! B3LYP Opt\n%maxdisk\n  MaxDisk not-a-number\nend\n* xyz 0 1\nO 0 0 0\n"
    out, effective, action = art.set_maxdisk(inp, 20000)
    assert (effective, action) == (20000, "rejected-invalid")
    assert "MaxDisk 20000" in out and "not-a-number" not in out
    assert "%maxdisk" not in out
    assert "%scf" in out


def test_maxdisk_duplicates_collapse_to_one():
    inp = "! B3LYP Opt\n%maxdisk\n  MaxDisk 50000\n  MaxDisk 30000\nend\n* xyz 0 1\nO 0 0 0\n"
    out, effective, action = art.set_maxdisk(inp, 20000)
    assert (effective, action) == (50000, "collapsed")
    assert out.count("MaxDisk") == 1 and "MaxDisk 50000" in out
    assert "%maxdisk" not in out
    assert "%scf" in out


def test_maxdisk_inserted_into_existing_block_without_duplicate():
    inp = "! B3LYP Opt\n%maxdisk\n  end\n* xyz 0 1\nO 0 0 0\n"
    out, effective, action = art.set_maxdisk(inp, 20000)
    assert out.count("MaxDisk 20000") == 1
    assert (effective, action) == (20000, "inserted")
    assert "%maxdisk" not in out
    assert "%scf" in out


def test_maxdisk_native_scf_block_preserved_and_inserted():
    inp = "! B3LYP Opt\n%scf\n  MaxIter 150\nend\n* xyz 0 1\nO 0 0 0\n"
    out, effective, action = art.set_maxdisk(inp, 20000)
    assert out.count("MaxDisk 20000") == 1
    assert "MaxIter 150" in out
    assert out.count("%scf") == 1
    assert (effective, action) == (20000, "inserted")

    inp_with_disk = "! B3LYP Opt\n%scf\n  MaxDisk 40000\n  MaxIter 150\nend\n* xyz 0 1\nO 0 0 0\n"
    out2, eff2, act2 = art.set_maxdisk(inp_with_disk, 20000)
    assert eff2 == 40000 and act2 == "preserved"
    assert "MaxDisk 40000" in out2 and out2.count("MaxDisk") == 1


def test_maxdisk_does_not_touch_maxcore():
    inp = "! B3LYP Opt\n%maxcore\n  6000\nend\n* xyz 0 1\nO 0 0 0\n"
    out, effective, action = art.set_maxdisk(inp, 20000)
    assert "6000" in out, "%maxcore must be untouched"
    assert "MaxDisk 20000" in out
    assert "%scf" in out


def test_generated_kernel_script_enforces_maxdisk():
    from orca_orchestrator.credentials import KaggleCredentials
    from orca_orchestrator.models import JobManifest
    from orca_orchestrator.runner.builder import build_header, render_script

    job = JobManifest.create(job_id="chem-tools-mdsk-1a2b3c4d", owner="tester",
                             title="mdsk", input_filename="mol.inp",
                             original_input_sha256="s", job_kind="opt")
    header = build_header(job=job, epoch=0,
                          creds=KaggleCredentials(username="tester", key="0" * 32),
                          inline_files={"mol.inp": b"! B3LYP Opt\n* xyz 0 1\nO 0 0 0\n*\n"})
    script = render_script(header)
    compile(script, "script.py", "exec")
    assert "set_maxdisk" in script, "the generated kernel must enforce MaxDisk"
    assert "MAXDISK_MB" in script


# ---------------------------------------------------------------------------
# Molden acceptance: present + verified in the final ZIP when generated,
# strictly absent when generation failed.
# ---------------------------------------------------------------------------
class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def runner(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    working = tmp_path / "working"
    monkeypatch.setenv("ORCA_RUNNER_SCRATCH_ROOT", str(scratch))
    monkeypatch.setenv("ORCA_RUNNER_OUTPUT_DIR", str(working))
    from orca_orchestrator.runner import kernel_runner as kr
    importlib.reload(kr)
    kr.BASENAME = "h2o"
    yield kr
    monkeypatch.undo()
    importlib.reload(kr)


def seed_gbw(kr):
    with open(kr.wp("h2o.gbw"), "wb") as fh:
        fh.write(b"binary-wavefunction")


def stub_which(monkeypatch, kr, exe_path):
    import shutil as _shutil
    real = _shutil.which
    monkeypatch.setattr(kr.shutil, "which",
                        lambda name: (str(exe_path) if exe_path is not None else None)
                        if name == "orca_2mkl" else real(name))


def _write_molden(kr):
    with open(kr.wp("h2o.molden.input"), "w", encoding="utf-8") as fh:
        fh.write("[Molden Format]\n[Atoms] (AU)\nO 1 0 0 0\n")


def test_molden_input_is_included_in_the_final_archive(runner, monkeypatch):
    """ORCA succeeded + orca_2mkl succeeded -> the ZIP must contain the fresh
    h2o.molden.input, non-empty, byte-identical to the generated artifact."""
    kr = runner
    seed_gbw(kr)
    stub_which(monkeypatch, kr, "fake-orca-2mkl")

    def fake_run(cmd, **kw):
        _write_molden(kr)
        return FakeCompleted(0, "", "")

    monkeypatch.setattr(kr.subprocess, "run", fake_run)
    kr.package_results(note="done")

    zip_path = os.path.join(kr.OUTPUT_DIR, "results.zip")
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert "h2o.molden.input" in names, "the verified Molden artifact must ship in the archive"
        data = zf.read("h2o.molden.input")
    assert len(data) > 0
    with open(kr.wp("h2o.molden.input"), "rb") as fh:
        assert hashlib.sha256(data).hexdigest() == hashlib.sha256(fh.read()).hexdigest(), \
            "the archived Molden must be the CURRENT artifact, byte for byte"


def test_failed_molden_is_excluded_and_reported(runner, tmp_path, monkeypatch):
    """When orca_2mkl fails, no molden file may ship in the archive (a stale
    one must never stand in for this run's orbitals) and JOB_NOTE must carry
    MOLDEN_FAILED."""
    kr = runner
    seed_gbw(kr)
    stub_which(monkeypatch, kr, str(tmp_path / "missing-exe"))
    # a STALE molden from an older calculation is lying around
    with open(kr.wp("h2o.molden.input"), "w", encoding="utf-8") as fh:
        fh.write("[Molden Format]\n[Atoms] of the OLD calculation\n")
    old = __import__("time").time() - 10_000
    os.utime(kr.wp("h2o.molden.input"), (old, old))

    monkeypatch.setattr(kr.subprocess, "run",
                        lambda cmd, **kw: FakeCompleted(1, "", "unsupported basis"))
    kr.package_results(note="Calculation finished.")

    zip_path = os.path.join(kr.OUTPUT_DIR, "results.zip")
    with zipfile.ZipFile(zip_path) as zf:
        molden_names = [n for n in zf.namelist() if n.lower().endswith((".molden", ".molden.input"))]
    assert molden_names == [], "a failed/stale molden must never ship in the archive"
    with open(os.path.join(kr.OUTPUT_DIR, "JOB_NOTE.txt"), encoding="utf-8") as fh:
        note = fh.read()
    assert "MOLDEN_FAILED" in note
    assert "Calculation finished." in note


@pytest.mark.parametrize("member", ["../../escape", "/absolute/escape", "C:/escape"])
def test_kernel_runner_rejects_zip_path_escape(runner, tmp_path, member):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(member, b"not-orca")
    dest = tmp_path / "extract"
    assert runner.extract_archive(str(archive), str(dest)) is False
    assert not dest.exists(), "a rejected archive must leave no partial executable tree"


def test_kernel_runner_rejects_zip_symlink(runner, tmp_path):
    archive = tmp_path / "symlink.zip"
    info = zipfile.ZipInfo("orca-link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(info, "../../outside")
    dest = tmp_path / "extract"
    assert runner.extract_archive(str(archive), str(dest)) is False
    assert not dest.exists()


def test_kernel_runner_rejects_tar_link(runner, tmp_path):
    archive = tmp_path / "symlink.tar"
    with tarfile.open(archive, "w") as tf:
        info = tarfile.TarInfo("orca-link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../outside"
        tf.addfile(info)
    dest = tmp_path / "extract"
    assert runner.extract_archive(str(archive), str(dest)) is False
    assert not dest.exists()


def test_kernel_runner_rejects_archive_over_budget(runner, tmp_path, monkeypatch):
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("huge.bin", BytesIO(b"0" * 2048).getvalue())
    monkeypatch.setattr(runner, "MAX_ARCHIVE_EXTRACTED_BYTES", 1024)
    dest = tmp_path / "extract"
    assert runner.extract_archive(str(archive), str(dest)) is False
    assert not dest.exists()


def test_kernel_runner_allows_safe_internal_tar_symlink(runner, tmp_path):
    archive = tmp_path / "safe_symlink.tar"
    with tarfile.open(archive, "w") as tf:
        f = tarfile.TarInfo("lib/libreal.so")
        f.size = 6
        tf.addfile(f, BytesIO(b"binary"))
        link = tarfile.TarInfo("lib/libalias.so")
        link.type = tarfile.SYMTYPE
        link.linkname = "libreal.so"
        tf.addfile(link)
    dest = tmp_path / "extract"
    assert runner.extract_archive(str(archive), str(dest)) is True
    assert (dest / "lib" / "libreal.so").exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
