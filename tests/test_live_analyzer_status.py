"""Scientific status regressions found through the deployed analyzer UI."""
import subprocess
from pathlib import Path

import pytest

from orca_engine.adapters.web_adapter import job_to_web_json
from orca_engine.parser import OrcaParser

ENERGY = "FINAL SINGLE POINT ENERGY -76.4\n"
NORMAL = "****ORCA TERMINATED NORMALLY****\n"


def test_real_tddft_output_reports_termination_without_optimization_certificate():
    path = Path(__file__).resolve().parents[1] / "orca_engine/data/orcafile/orca6.1/napc9oxcuv.out"
    with path.open(encoding="utf-8") as stream:
        job = OrcaParser(stream).parse()[-1]
    data = job_to_web_json(job, "test", 1)
    assert data["terminated_normally"] is True
    assert data["geometry_optimization_converged"] is None


@pytest.mark.parametrize("comment", [
    "| 2> # | Geometry Optimization | B3LYP/def2-TZVP",
    "| 3> # THE OPTIMIZATION HAS CONVERGED",
    "# THE OPTIMIZATION HAS CONVERGED",
])
def test_input_comments_do_not_certify_optimized_geometry(comment):
    job = OrcaParser(comment + "\n" + ENERGY + NORMAL).parse()[-1]
    assert job.geometry_optimization_converged is None


@pytest.mark.parametrize("marker", ["THE OPTIMIZATION HAS CONVERGED", "*** THE OPTIMIZATION HAS CONVERGED ***"])
def test_output_convergence_marker_is_reported(marker):
    job = OrcaParser(ENERGY + marker + "\n" + NORMAL).parse()[-1]
    assert job.geometry_optimization_converged is True
    assert job_to_web_json(job, "test")["geometry_optimization_converged"] is True


def test_a_new_cycle_invalidates_an_earlier_appended_optimization_certificate():
    text = ENERGY + "THE OPTIMIZATION HAS CONVERGED\nGEOMETRY OPTIMIZATION CYCLE 1\n" + NORMAL
    job = OrcaParser(text).parse()[-1]
    assert job.terminated_normally is True
    assert job.geometry_optimization_converged is False


def test_optimization_certificate_does_not_leak_to_next_calculation():
    text = ENERGY + "THE OPTIMIZATION HAS CONVERGED\n" + NORMAL + "$new_job\n" + ENERGY + NORMAL
    jobs = OrcaParser(text).parse()
    assert len(jobs) == 2
    assert jobs[0].geometry_optimization_converged is True
    assert jobs[1].geometry_optimization_converged is None


def test_frontend_uses_authoritative_status_fields_without_conflating_convergence():
    source = (Path(__file__).resolve().parents[1] / "static/js/app.js").read_text(encoding="utf-8")
    start = source.index("  function engineCalculationStatus(job) {")
    end = source.index("  // Helper to extract session CSRF", start)
    code = source[start:end] + r"""
    const assert=require('assert');
    assert.deepStrictEqual(engineCalculationStatus({terminated_normally:true}),
      {normal:true,label:'Normal Termination',optimized:false});
    assert.strictEqual(engineCalculationStatus({terminated_normally:false,termination_status:'NORMAL'}).normal,false);
    assert.strictEqual(engineCalculationStatus({termination_status:'NORMAL'}).normal,true);
    assert.strictEqual(engineCalculationStatus({calc_type:'opt'}).optimized,false);
    assert.strictEqual(engineCalculationStatus({geometry_optimization_converged:true}).optimized,true);
    assert.strictEqual(engineCalculationStatus({geometry_only:true,terminated_normally:true}).label,'Geometry only');
    """
    result = subprocess.run(["node", "-e", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
