# -*- coding: utf-8 -*-
"""Automated Scientific Reference Benchmarks Verification Suite
=============================================================
Validates quantum chemical property parsing, thermochemistry calculations,
vibrational frequencies, dipole moments, frontier orbital energies, and
TD-DFT electronic transitions against verified canonical ORCA benchmarks.
"""
import math
import os
import sys
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BENCHMARKS_DIR = os.path.join(ROOT, "benchmarks")
sys.path.insert(0, os.path.join(ROOT, "orca_engine", "src"))
sys.path.insert(0, ROOT)

from orca_engine.parser import OrcaParser
from orca_engine.thermochemistry import ThermochemistryEngine


def test_water_benchmark_observables():
    """Verify H2O calculation: electronic energy, dipole, HOMO/LUMO, frequencies, and Gibbs energy."""
    out_path = os.path.join(BENCHMARKS_DIR, "water_sp_freq.out")
    assert os.path.isfile(out_path), "Benchmark file water_sp_freq.out must exist"

    with open(out_path, "r", encoding="utf-8") as f:
        content = f.read()

    parser = OrcaParser(content, source_name="water_sp_freq.out")
    jobs = parser.parse()
    assert len(jobs) == 1, "Expected 1 job block"
    job = jobs[0]

    # Electronic single-point energy
    assert job.e_elec_eh is not None
    assert math.isclose(job.e_elec_eh, -76.41893254, rel_tol=1e-6)

    # Dipole Moment
    assert job.dipole_moment_debye is not None
    assert math.isclose(job.dipole_moment_debye, 1.8542, rel_tol=1e-3)

    # Vibrational frequencies (3 non-zero modes for non-linear H2O: 3N - 6 = 3)
    assert len(job.vibrational_frequencies_cm) == 9
    non_zero_freqs = [f for f in job.vibrational_frequencies_cm if f > 10.0]
    assert len(non_zero_freqs) == 3
    assert math.isclose(non_zero_freqs[0], 1640.50, abs_tol=0.5)
    assert math.isclose(non_zero_freqs[1], 3810.20, abs_tol=0.5)
    assert math.isclose(non_zero_freqs[2], 3920.80, abs_tol=0.5)

    # Thermochemistry
    assert job.zpe_eh is not None and math.isclose(job.zpe_eh, 0.02135000, rel_tol=1e-4)
    assert job.gibbs_free_energy_eh is not None and math.isclose(job.gibbs_free_energy_eh, -76.41525800, rel_tol=1e-6)
    assert job.total_enthalpy_eh is not None and math.isclose(job.total_enthalpy_eh, -76.39380800, rel_tol=1e-6)

    # Frontier Orbitals in eV
    assert job.homo_ev is not None
    assert math.isclose(job.homo_ev, -7.116, rel_tol=1e-3)
    assert job.lumo_ev is not None
    assert math.isclose(job.lumo_ev, 1.312, rel_tol=1e-3)
    assert job.homo_lumo_gap_ev is not None
    assert math.isclose(job.homo_lumo_gap_ev, 1.312 - (-7.116), rel_tol=1e-3)


def test_benzene_tddft_benchmark():
    """Verify Benzene TD-DFT calculation: transition energies and oscillator strengths."""
    out_path = os.path.join(BENCHMARKS_DIR, "benzene_tddft.out")
    assert os.path.isfile(out_path), "Benchmark file benzene_tddft.out must exist"

    with open(out_path, "r", encoding="utf-8") as f:
        content = f.read()

    parser = OrcaParser(content, source_name="benzene_tddft.out")
    jobs = parser.parse()
    assert len(jobs) == 1
    job = jobs[0]

    assert len(job.tddft_cm) == 3
    assert len(job.tddft_fosc) == 3

    # Transitions in cm^-1
    assert math.isclose(job.tddft_cm[0], 39118.2, abs_tol=1.0)
    assert math.isclose(job.tddft_cm[1], 47945.6, abs_tol=1.0)
    assert math.isclose(job.tddft_cm[2], 55773.0, abs_tol=1.0)

    # Oscillator strengths
    assert math.isclose(job.tddft_fosc[0], 0.0000, abs_tol=1e-4)
    assert math.isclose(job.tddft_fosc[1], 0.09845, abs_tol=1e-4)
    assert math.isclose(job.tddft_fosc[2], 0.85240, abs_tol=1e-4)
