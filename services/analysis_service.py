# -*- coding: utf-8 -*-
"""Framework-independent Quantum Chemistry Analysis Service wrapping orca_engine."""
from __future__ import annotations

import sys
import os
from typing import Any, Dict, List, Optional, Tuple

_BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_ORCA_ENGINE_SRC = os.path.join(_BASE_DIR, "orca_engine", "src")
if os.path.isdir(_ORCA_ENGINE_SRC) and _ORCA_ENGINE_SRC not in sys.path:
    sys.path.insert(0, _ORCA_ENGINE_SRC)

from orca_engine.parser import OrcaParser
from orca_engine.reporting import job_to_dict


def analyze_output_text(output_text: str) -> Dict[str, Any]:
    """Parses raw ORCA output text and returns authoritative scientific summary."""
    if not output_text or not output_text.strip():
        return {"ok": False, "error": "Empty or whitespace-only ORCA output.", "error_code": "EMPTY_OUTPUT"}

    parser = OrcaParser(output_text)
    jobs = parser.parse()
    if not jobs:
        return {"ok": False, "error": "No recognizable ORCA jobs found in output.", "error_code": "NO_JOBS_FOUND"}

    parsed_jobs = []
    for idx, job in enumerate(jobs, start=1):
        d = job_to_dict(idx, job)
        d["converged"] = getattr(job, "converged", False)
        d["energy_hartree"] = d.get("electronic_energy_hartree") or d.get("scf_energy_hartree")
        d["frequencies"] = getattr(job, "vibrational_frequencies_cm", []) or []
        parsed_jobs.append(d)

    last_job = parsed_jobs[-1]
    return {
        "ok": True,
        "job_count": len(parsed_jobs),
        "jobs": parsed_jobs,
        "final_job": last_job,
        "final_energy_hartree": last_job.get("energy_hartree"),
        "converged": last_job.get("converged", False),
    }


def extract_frequencies_and_ir(output_text: str) -> Dict[str, Any]:
    """Extracts vibrational frequencies and IR/Raman intensities."""
    res = analyze_output_text(output_text)
    if not res.get("ok"):
        return res

    final_job = res["final_job"]
    freqs = final_job.get("frequencies", [])
    ir_ints = final_job.get("ir_intensities", [])

    return {
        "ok": True,
        "frequencies": freqs,
        "ir_intensities": ir_ints,
        "has_imaginary": any(f < 0 for f in freqs),
        "zero_point_energy_hartree": final_job.get("zero_point_energy_hartree"),
    }
