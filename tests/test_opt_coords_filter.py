# -*- coding: utf-8 -*-
"""Coordinate-import eligibility tests (requirement: only genuinely completed
OPT results are importable).

The gate lives in /api/kaggle/extract-opt-coords: classification is
comment-safe (detect_job_kind masks comments), completion uses the ORCA
convergence markers (a normal exit alone is not convergence), and the returned
geometry must be the FINAL optimized geometry - never the initial input.
"""
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CONVERGED_OUT = """
CARTESIAN COORDINATES (ANGSTROEM)
---------------------------------
O      0.000000    0.000000    0.117000
H      0.000000    0.757000   -0.467000
H      0.000000   -0.757000   -0.467000

CARTESIAN COORDINATES (ANGSTROEM)
---------------------------------
O      0.000000    0.000000    0.200000
H      0.000000    0.760000   -0.500000
H      0.000000   -0.760000   -0.500000

THE OPTIMIZATION HAS CONVERGED

ORCA TERMINATED NORMALLY
"""

UNCONVERGED_OUT = """
CARTESIAN COORDINATES (ANGSTROEM)
---------------------------------
O      0.000000    0.000000    0.117000
H      0.000000    0.757000   -0.467000
H      0.000000   -0.757000   -0.467000

ORCA TERMINATED NORMALLY
"""

ABORTED_OUT = """
O      0.000000    0.000000    0.117000
_child process aborted
"""


def _archive(tmp_path, inp_text, out_text, extra=None, name="stage"):
    import app as webapp
    stage = tmp_path / name
    stage.mkdir(parents=True, exist_ok=True)
    zip_path = stage / "results.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("h2o_opt.inp", inp_text)
        zf.writestr("h2o_opt.out", out_text)
        for name2, data in (extra or {}).items():
            zf.writestr(name2, data)
    return webapp, str(zip_path), stage


def _client(monkeypatch, webapp, zip_path, tmp_path):
    import kaggle_runner
    import orca_orchestrator.service as service_mod

    monkeypatch.setattr(
        kaggle_runner, "fetch_job_results",
        lambda username, key, job_id: (zip_path, str(tmp_path)))

    # Keep the orchestrator singleton OUT of these tests entirely: its
    # watchdog thread is process-global and other suites own its lifecycle.
    class _NoService:
        def fetch_results(self, *_a, **_k):
            # Mirror the real service: an unknown job raises, which sends the
            # endpoint down the legacy-fetch path this test controls.
            raise RuntimeError("no such orchestrator job in the test store")

    monkeypatch.setattr(service_mod, "get_service", lambda *a, **k: _NoService())

    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        yield c


def _post(client, job_id="chem-tools-imp0-1a2b3c4d"):
    return client.post(
        "/api/kaggle/extract-opt-coords",
        json={"kaggle_username": "tester", "kaggle_key": "0" * 32, "job_id": job_id})


OPT_INP = "! B3LYP def2-SVP Opt\n* xyz 0 1\nO 0 0 0\nH 0 0 0.96\nH 0 0 -0.96\n*\n"
SP_INP = "! B3LYP def2-SVP\n# opt discussion and scan notes\n* xyz 0 1\nO 0 0 0\n*\n"
FREQ_INP = "! B3LYP Freq\n* xyz 0 1\nO 0 0 0\n*\n"


def test_converged_opt_imports_final_geometry(tmp_path, monkeypatch):
    webapp, zip_path, d = _archive(tmp_path, OPT_INP, CONVERGED_OUT)
    c = next(_client(monkeypatch, webapp, zip_path, d))
    resp = _post(c)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["converged"] is True and data["job_kind"] == "opt"
    # the FINAL Cartesian block is the optimized geometry (0.200 z on oxygen)
    assert "0.200000" in data["coords"]
    assert "0.117000" not in data["coords"], "an earlier geometry step must not be returned"


def test_sp_with_opt_comment_is_rejected(tmp_path, monkeypatch):
    """'# opt test' in a comment must never turn a single point into a
    coordinate source (comment-safe classification)."""
    webapp, zip_path, d = _archive(tmp_path, SP_INP, CONVERGED_OUT)
    c = next(_client(monkeypatch, webapp, zip_path, d))
    resp = _post(c)
    assert resp.status_code == 422
    assert resp.get_json()["code"] == "not_optimization"


def test_freq_is_rejected(tmp_path, monkeypatch):
    webapp, zip_path, d = _archive(tmp_path, FREQ_INP, CONVERGED_OUT)
    c = next(_client(monkeypatch, webapp, zip_path, d))
    resp = _post(c)
    assert resp.status_code == 422 and resp.get_json()["code"] == "not_optimization"


def test_unconverged_opt_is_rejected(tmp_path, monkeypatch):
    """Normal termination WITHOUT the convergence banner = MaxIter -> the
    optimization has no final optimized geometry to import."""
    webapp, zip_path, d = _archive(tmp_path, OPT_INP, UNCONVERGED_OUT)
    c = next(_client(monkeypatch, webapp, zip_path, d))
    resp = _post(c)
    assert resp.status_code == 422
    assert resp.get_json()["code"] == "optimization_not_complete"


def test_aborted_opt_is_rejected(tmp_path, monkeypatch):
    webapp, zip_path, d = _archive(tmp_path, OPT_INP, ABORTED_OUT)
    c = next(_client(monkeypatch, webapp, zip_path, d))
    resp = _post(c)
    assert resp.status_code == 422
    assert resp.get_json()["code"] == "optimization_not_complete"


def test_input_geometry_is_never_substituted(tmp_path, monkeypatch):
    """A converged opt with NO .xyz artifact must fail loudly rather than
    silently returning the INITIAL input geometry."""
    converged_no_xyz = CONVERGED_OUT  # .out carries CARTESIAN blocks -> still ok
    webapp, zip_path, d = _archive(tmp_path, OPT_INP, converged_no_xyz)
    c = next(_client(monkeypatch, webapp, zip_path, d))
    resp = _post(c)
    assert resp.status_code == 200
    # a DEGENERATE all-zero "geometry" must be rejected, not passed through
    degenerate = ("CARTESIAN COORDINATES (ANGSTROEM)\n---------------------------------\n"
                  "O      0.000000    0.000000    0.000000\n"
                  "H      0.000000    0.000000    0.000000\n"
                  "H      0.000000    0.000000    0.000000\n"
                  "THE OPTIMIZATION HAS CONVERGED\nORCA TERMINATED NORMALLY\n")
    webapp2, zip_path2, d2 = _archive(tmp_path, OPT_INP, degenerate, name="stage2")
    c2 = next(_client(monkeypatch, webapp2, zip_path2, d2))
    resp2 = _post(c2)
    assert resp2.status_code == 422


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
