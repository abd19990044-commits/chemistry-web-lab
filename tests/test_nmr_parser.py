# -*- coding: utf-8 -*-
"""Unit tests for the ORCA NMR parser."""

from __future__ import annotations

import pytest

from orca_engine.nmr import NMRNucleus, OrcaNMRParser
from orca_engine.parser import OrcaParser


SAMPLE_ORCA_NMR_SUMMARY = """
  ------------------
  CHEMICAL SHIELDING
  ------------------

Nucleus  Element    Isotropic     Anisotropy
-------  -------  ------------  ------------
    0       C          229.484        59.356
    1       C          227.642        62.878
    2       H           56.015        12.469
    3       H           55.460        15.284
    4       H           55.460        15.284
    5       O          334.125       110.616
    6       H           47.337        27.101
    7       H           47.337        27.101
    8       H           64.252        32.114
"""

SAMPLE_ORCA_NMR_TENSORS = """
  ------------------
  CHEMICAL SHIELDING
  ------------------

  --------------
  Nucleus   0C :
  --------------
  Diamagnetic contribution to the shielding tensor (ppm) :
          200.382      0.000      0.000
            0.000    214.507      0.000
            0.000      0.000    210.998
  Paramagnetic contribution to the shielding tensor (ppm):
           -2.770      0.000      0.000
            0.000      7.279      0.000
            0.000      0.000     58.057
  Total shielding tensor (ppm):
          197.611      0.000      0.000
            0.000    221.786      0.000
            0.000      0.000    269.055
  Diagonalized sT*s matrix:
  sDSO    200.382    214.507    210.998  iso=   208.629
  sPSO     -2.770      7.279     58.057  iso=    20.855
  -------------------------------------------------------
  Total   197.611    221.786    269.055  iso=   229.484

  --------------
  Nucleus   2H :
  --------------
  Diagonalized sT*s matrix:
  sDSO     55.000     56.000     57.000  iso=    56.000
  sPSO      0.010      0.015      0.020  iso=     0.015
  -------------------------------------------------------
  Total    55.010     56.015     57.020  iso=    56.015
"""


def test_nmr_parser_summary_table():
    parser = OrcaNMRParser()
    res = parser.parse_text(SAMPLE_ORCA_NMR_SUMMARY)

    assert len(res.atoms) == 9
    assert res.has_h1 is True
    assert res.has_c13 is True

    # Check specific atom assignments
    c0 = res.atoms[0]
    assert c0.atom_index == 0
    assert c0.element == "C"
    assert c0.isotope == "13C"
    assert c0.isotropic_shielding == pytest.approx(229.484, rel=1e-5)
    assert c0.anisotropy == pytest.approx(59.356, rel=1e-5)
    assert c0.assignment == "C0"

    h2 = res.atoms[2]
    assert h2.atom_index == 2
    assert h2.element == "H"
    assert h2.isotope == "1H"
    assert h2.isotropic_shielding == pytest.approx(56.015, rel=1e-5)
    assert h2.anisotropy == pytest.approx(12.469, rel=1e-5)
    assert h2.assignment == "H2"

    o5 = res.atoms[5]
    assert o5.atom_index == 5
    assert o5.element == "O"
    assert o5.isotropic_shielding == pytest.approx(334.125, rel=1e-5)


def test_nmr_parser_tensor_blocks_with_anisotropy_calc():
    parser = OrcaNMRParser()
    res = parser.parse_text(SAMPLE_ORCA_NMR_TENSORS)

    assert len(res.atoms) == 2
    c0 = res.atoms[0]
    assert c0.atom_index == 0
    assert c0.element == "C"
    assert c0.isotropic_shielding == pytest.approx(229.484, rel=1e-5)
    assert c0.diamagnetic_iso == pytest.approx(208.629, rel=1e-5)
    assert c0.paramagnetic_iso == pytest.approx(20.855, rel=1e-5)
    assert c0.total_tensor is not None
    assert len(c0.total_tensor) == 3
    # Anisotropy computed from diagonalized eigenvalues 197.611, 221.786, 269.055:
    # 269.055 - (197.611 + 221.786)/2 = 59.3565
    assert c0.anisotropy == pytest.approx(59.3565, rel=1e-4)


def test_nmr_parser_empty_or_non_nmr():
    parser = OrcaNMRParser()
    res = parser.parse_text("ORCA calculation without any NMR properties...")
    assert len(res.atoms) == 0
    assert res.has_h1 is False
    assert res.has_c13 is False
    assert len(res.warnings) > 0
    assert res.is_complete is False


def test_orca_parser_integration():
    full_output = f"""
    * O   R   C   A *
    Program Version 6.0.0
    DFT CALCULATION
    Your calculation utilizes the basis: def2-TZVP
    CARTESIAN COORDINATES (ANGSTROEM)
    ---------------------------------
      C      0.000000    0.000000    0.000000
      H      0.000000    0.000000    1.090000
    {SAMPLE_ORCA_NMR_SUMMARY}
    TOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds
    ****ORCA TERMINATION NORMALLY****
    """
    jobs = OrcaParser(full_output).parse()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.nmr_result is not None
    assert len(job.nmr_result.atoms) == 9
    assert job.nmr_result.has_h1 is True
    assert job.nmr_result.has_c13 is True
