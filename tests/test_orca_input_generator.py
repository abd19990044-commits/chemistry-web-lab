# -*- coding: utf-8 -*-
"""Comprehensive tests for enriched ORCA 6.1.0 input generation against official manual specifications."""

import pytest
import chem_core as core


def test_orca_6_1_scf_convergence_and_clean_ri():
    """Verify TightSCF, RIJCOSX (without redundant AutoAux), and parameters."""
    d = {
        "calc_type": "opt freq",
        "family": "f_dft",
        "theory": "B3LYP",
        "basis": "def2-TZVP",
        "disp": "D3BJ",
        "ri_type": "rijcosx",
        "scf_conv": "tightscf",
        "solv_model": "cpcm",
        "solvent": "Acetonitrile",
        "cores": 8,
        "ram": 4000,
        "maxdisk": 50000,
        "largeprint": True,
        "temp": 310.15,
        "pressure": 2.0,
        "coords": "O 0 0 0\nH 0 0.7 0.6\nH 0 -0.7 0.6",
    }
    inp = core.generate_orca_6_input(d)
    assert "! B3LYP def2-TZVP D3BJ RIJCOSX TightSCF CPCM(Acetonitrile) Opt Freq LargePrint" in inp
    assert "%pal nprocs 8 end" in inp
    assert "%maxcore 4000" in inp
    assert "%freq" in inp
    assert "Temp 310.15" in inp
    assert "Pressure 2.0" in inp


def test_orca_6_1_composite_3c_methods():
    """Verify that composite 3c methods have no basis and no extra dispersion keyword."""
    for method in ["r2SCAN-3c", "B97-3c", "wB97X-3c", "PBEh-3c", "HF-3c"]:
        d = {
            "calc_type": "opt",
            "family": "f_comp",
            "theory": method,
            "basis": "def2-SVP",  # Should be automatically suppressed
            "disp": "D4",         # Should be automatically suppressed
            "scf_conv": "tightscf",
            "cores": 4,
            "ram": 6000,
            "coords": "C 0 0 0",
        }
        inp = core.generate_orca_6_input(d)
        assert f"! {method} TightSCF Opt" in inp
        assert "def2-SVP" not in inp
        assert "D4" not in inp


def test_orca_6_1_builtin_dispersion_functionals():
    """Verify that functionals with built-in dispersion do not produce duplicate dispersion keywords."""
    d = {
        "calc_type": "sp",
        "family": "f_dft",
        "theory": "wB97X-D4",
        "basis": "def2-TZVP",
        "disp": "D4",  # User selected D4, but functional already has D4
        "cores": 4,
        "ram": 5000,
        "coords": "C 0 0 0",
    }
    inp = core.generate_orca_6_input(d)
    assert "! wB97X-D4 def2-TZVP SP" in inp
    assert "D4 D4" not in inp


def test_orca_6_1_expanded_basis_and_methods():
    """Verify pcseg, x2c, composite methods, and nmr generation."""
    # Test pcseg basis with SMD
    d_pcseg = {
        "calc_type": "freq",
        "family": "f_dft",
        "theory": "wB97M-V",
        "basis": "pcseg-2",
        "scf_conv": "verytightscf",
        "solv_model": "smd",
        "solvent": "DMF",
        "cores": 4,
        "ram": 5000,
        "coords": "C 0 0 0",
    }
    inp_pcseg = core.generate_orca_6_input(d_pcseg)
    assert "! wB97M-V pcseg-2 VeryTightSCF SMD(DMF) Freq" in inp_pcseg

    # Test NMR
    d_nmr = {
        "calc_type": "nmr",
        "family": "f_dft",
        "theory": "PBE0",
        "basis": "IGLO-III",
        "scf_conv": "tightscf",
        "cores": 4,
        "ram": 6000,
        "coords": "C 0 0 0",
    }
    inp_nmr = core.generate_orca_6_input(d_nmr)
    assert "%eprnmr" in inp_nmr
    assert "Nuclei = all H { shift }" in inp_nmr
    assert "! PBE0 IGLO-III TightSCF NMR" in inp_nmr


def test_orca_6_1_relativistic_x2c():
    """Verify X2C scalar relativistic input with recontracted basis."""
    d = {
        "calc_type": "sp",
        "family": "f_dft",
        "theory": "PBE0",
        "basis": "def2-TZVP",
        "x2c": True,
        "cores": 4,
        "ram": 6000,
        "coords": "Pt 0 0 0",
    }
    inp = core.generate_orca_6_input(d)
    assert "x2c-TZVPall" in inp
    assert "X2C" in inp


def test_orca_6_1_tddft_and_numfreq():
    """Verify TD-DFT and NumFreq calculation setups."""
    d_td = {
        "calc_type": "tddft",
        "family": "f_dft",
        "theory": "CAM-B3LYP",
        "basis": "def2-TZVP",
        "nroots": 20,
        "cores": 4,
        "ram": 6000,
        "coords": "C 0 0 0",
    }
    inp_td = core.generate_orca_6_input(d_td)
    assert "%tddft" in inp_td
    assert "nroots 20" in inp_td

    d_num = {
        "calc_type": "numfreq",
        "family": "f_dft",
        "theory": "B3LYP",
        "basis": "def2-SVP",
        "cores": 4,
        "ram": 4000,
        "coords": "C 0 0 0",
    }
    inp_num = core.generate_orca_6_input(d_num)
    assert "NumFreq" in inp_num
