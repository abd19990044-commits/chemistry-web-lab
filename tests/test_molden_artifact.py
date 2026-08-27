# -*- coding: utf-8 -*-
"""Regression tests for the Molden / orca_2mkl artifact chain.

The runner must convert the window's final GBW with `orca_2mkl <base> -molden`,
verify the conversion (exit code + a fresh, non-empty output file), and report
an honest status - MOLDEN_GENERATED / MOLDEN_UNAVAILABLE / MOLDEN_FAILED -
instead of the old behaviour that announced success unconditionally and could
archive a stale *.molden* in place of this run's orbitals.
"""
import importlib
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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


def seed_gbw(kr, body=b"binary-wavefunction"):
    with open(kr.wp("h2o.gbw"), "wb") as fh:
        fh.write(body)


def stub_which(monkeypatch, kr, exe_path):
    real = __import__("shutil").which
    monkeypatch.setattr(kr.shutil, "which",
                        lambda name: (str(exe_path) if exe_path is not None else None)
                        if name == "orca_2mkl" else real(name))


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_molden_generated_when_2mkl_succeeds(runner, tmp_path, monkeypatch):
    kr = runner
    seed_gbw(kr)
    exe = tmp_path / "orca_2mkl"
    exe.write_text("# fake\n")
    stub_which(monkeypatch, kr, exe)

    def fake_run(cmd, **kw):
        # the real orca_2mkl writes <base>.molden.input next to the GBW
        with open(kr.wp("h2o.molden.input"), "w", encoding="utf-8") as fh:
            fh.write("[Molden Format]\n[Atoms] (AU)\n")
        return FakeCompleted(0, "", "")

    monkeypatch.setattr(kr.subprocess, "run", fake_run)
    status = kr.generate_molden_artifact()
    assert status["status"] == "MOLDEN_GENERATED", status
    assert "h2o.molden.input" in status["detail"]


def test_molden_failed_when_2mkl_exits_nonzero(runner, monkeypatch):
    kr = runner
    seed_gbw(kr)
    exe = tmp_path2 = __import__("tempfile").mkdtemp()
    stub_which(monkeypatch, kr, os.path.join(exe, "orca_2mkl"))

    monkeypatch.setattr(kr.subprocess, "run",
                        lambda cmd, **kw: FakeCompleted(3, "", "cannot read gbw"))
    status = kr.generate_molden_artifact()
    assert status["status"] == "MOLDEN_FAILED"
    assert "exited 3" in status["detail"]


def test_molden_failed_when_2mkl_exits_zero_without_output(runner, monkeypatch):
    kr = runner
    seed_gbw(kr)
    stub_which(monkeypatch, kr, "whatever")
    monkeypatch.setattr(kr.subprocess, "run",
                        lambda cmd, **kw: FakeCompleted(0, "", ""))
    status = kr.generate_molden_artifact()
    assert status["status"] == "MOLDEN_FAILED"
    assert "no fresh Molden file" in status["detail"]


def test_molden_unavailable_without_gbw(runner):
    kr = runner
    status = kr.generate_molden_artifact()
    assert status["status"] == "MOLDEN_UNAVAILABLE"
    assert "no .gbw" in status["detail"]


def test_molden_unavailable_without_executable(runner, monkeypatch):
    kr = runner
    seed_gbw(kr)
    stub_which(monkeypatch, kr, None)          # not on PATH, not in orca_dir
    status = kr.generate_molden_artifact()
    assert status["status"] == "MOLDEN_UNAVAILABLE"


def test_stale_molden_is_never_reported_as_generated(runner, tmp_path, monkeypatch):
    """A *.molden* left over from an EARLIER calculation must not be reported
    as this run's orbital artifact: the freshness check (mtime >= invocation)
    plus the honest failure status make stale reuse impossible to miss."""
    kr = runner
    seed_gbw(kr, body=b"NEW-CALCULATION")
    stale = kr.wp("h2o.molden.input")
    with open(stale, "w", encoding="utf-8") as fh:
        fh.write("[Molden Format]\n[Atoms] of the OLD calculation\n")
    old = time.time() - 10_000
    os.utime(stale, (old, old))
    stub_which(monkeypatch, kr, str(tmp_path / "missing-exe"))

    # orca_2mkl now fails for the new calculation
    monkeypatch.setattr(kr.subprocess, "run",
                        lambda cmd, **kw: FakeCompleted(1, "", "unsupported basis"))
    status = kr.generate_molden_artifact()
    assert status["status"] == "MOLDEN_FAILED"
    assert os.path.exists(stale), "pre-existing file is left in place (never deleted input)"
    # ...but the failure is loudly recorded, so the stale file cannot be
    # mistaken for this calculation's orbitals:
    kr.package_results = kr.package_results  # (package path covered below)
    assert status["status"] != "MOLDEN_GENERATED"


def test_package_results_appends_molden_status_to_job_note(runner, monkeypatch):
    kr = runner
    seed_gbw(kr)
    stub_which(monkeypatch, kr, None)          # 2mkl unavailable -> explicit status
    kr.package_results(note="Calculation finished.")
    note_path = os.path.join(kr.OUTPUT_DIR, "JOB_NOTE.txt")
    with open(note_path, encoding="utf-8") as fh:
        note = fh.read()
    assert "MOLDEN_UNAVAILABLE" in note
    assert "Calculation finished." in note


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
