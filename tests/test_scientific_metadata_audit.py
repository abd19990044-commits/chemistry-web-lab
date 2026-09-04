# -*- coding: utf-8 -*-
"""Scientific Metadata Comparability & Extraction Tests for Reaction Thermochemistry."""
import pytest
from services.reaction_workflow_service import (
    extract_stage_result,
    compute_reaction_thermodynamics,
    ReactionValidationError,
)

SAMPLE_ORCA_OUTPUT_B3LYP = """
  ************************************************************
  *                        ORCA 6.0.0                        *
  ************************************************************
! B3LYP def2-SVP OPT FREQ

--------------------------------------------------
THE OPTIMIZATION HAS CONVERGED
--------------------------------------------------
FINAL SINGLE POINT ENERGY      -76.383146775728
Total Thermal Energy           -76.350000000000 Eh
Total Enthalpy                 -76.349000000000 Eh
Final Gibbs free energy        -76.372000000000 Eh
Total Entropy                  45.200000000000 cal/mol-K

THERMOCHEMISTRY AT 298.15 K
VIBRATIONAL FREQUENCIES
 0:      0.00 cm**-1
 1:      0.00 cm**-1
 2:      0.00 cm**-1
 3:   1590.20 cm**-1
 4:   3750.10 cm**-1
 5:   3850.40 cm**-1

*** ORCA TERMINATED NORMALLY ***
"""

SAMPLE_ORCA_OUTPUT_PBE0 = """
  ************************************************************
  *                        ORCA 6.0.0                        *
  ************************************************************
! PBE0 def2-TZVP OPT FREQ

--------------------------------------------------
THE OPTIMIZATION HAS CONVERGED
--------------------------------------------------
FINAL SINGLE POINT ENERGY      -76.423146775728
Total Thermal Energy           -76.390000000000 Eh
Total Enthalpy                 -76.389000000000 Eh
Final Gibbs free energy        -76.412000000000 Eh
Total Entropy                  45.200000000000 cal/mol-K

THERMOCHEMISTRY AT 298.15 K
VIBRATIONAL FREQUENCIES
 0:      0.00 cm**-1
 1:      0.00 cm**-1
 2:      0.00 cm**-1
 3:   1610.20 cm**-1
 4:   3770.10 cm**-1
 5:   3870.40 cm**-1

*** ORCA TERMINATED NORMALLY ***
"""


def test_extract_stage_result_captures_metadata():
    """extract_stage_result must capture real method, basis set, and thermodynamics."""
    res = extract_stage_result(SAMPLE_ORCA_OUTPUT_B3LYP)
    assert res["converged"] is True
    assert res["energy_hartree"] == pytest.approx(-76.383146775728)
    assert res["gibbs_hartree"] == pytest.approx(-76.372)
    assert res["enthalpy_hartree"] == pytest.approx(-76.349)
    assert res["temperature_k"] == 298.15
    assert res["imaginary_count"] == 0
    # Method and basis extracted
    assert res["method"] in ("B3LYP", "DFT", "HF/B3LYP") or "B3LYP" in str(res["method"])
    assert res["basis_set"] == "def2-SVP" or "def2-SVP" in str(res["basis_set"])


def test_compute_reaction_thermodynamics_detects_mixed_theory():
    """compute_reaction_thermodynamics must flag mixed levels of theory across species."""
    sp_a = {
        "nu": -1.0,
        "display_name": "Species A",
        "role": "reactant",
        "final_result": {
            "E_final": -76.383146775728,
            "H_final": -76.349,
            "G_final": -76.372,
            "zpe_hartree": 0.033,
            "entropy_j_mol_k": 189.1,
            "temperature_k": 298.15,
            "method": "B3LYP",
            "basis_set": "def2-SVP",
            "solvent_model": None,
            "solvent": None,
            "complete_thermochemistry": True,
        }
    }

    sp_b = {
        "nu": 1.0,
        "display_name": "Species B",
        "role": "product",
        "final_result": {
            "E_final": -76.423146775728,
            "H_final": -76.389,
            "G_final": -76.412,
            "zpe_hartree": 0.034,
            "entropy_j_mol_k": 189.1,
            "temperature_k": 298.15,
            "method": "PBE0",
            "basis_set": "def2-TZVP",
            "solvent_model": None,
            "solvent": None,
            "complete_thermochemistry": True,
        }
    }

    res = compute_reaction_thermodynamics([sp_a, sp_b])
    assert res["delta_G_hartree"] == pytest.approx(-0.040, abs=1e-4)
    # Check that mixed theory warning is present
    has_mixed_theory_warn = any("MIXED LEVEL OF THEORY" in w for w in res.get("warnings", []))
    assert has_mixed_theory_warn is True


def test_compute_reaction_thermodynamics_same_level_no_warning():
    """Consistent method, basis set, and solvent across species yields no comparability warnings."""
    sp_a = {
        "nu": -1.0,
        "display_name": "Reactant A",
        "role": "reactant",
        "final_result": {
            "E_final": -100.0,
            "H_final": -99.9,
            "G_final": -99.95,
            "zpe_hartree": 0.05,
            "entropy_j_mol_k": 200.0,
            "temperature_k": 298.15,
            "method": "B3LYP",
            "basis_set": "def2-TZVP",
            "solvent_model": "CPCM",
            "solvent": "water",
            "complete_thermochemistry": True,
        }
    }
    sp_b = {
        "nu": 1.0,
        "display_name": "Product B",
        "role": "product",
        "final_result": {
            "E_final": -100.05,
            "H_final": -99.95,
            "G_final": -100.0,
            "zpe_hartree": 0.05,
            "entropy_j_mol_k": 200.0,
            "temperature_k": 298.15,
            "method": "B3LYP",
            "basis_set": "def2-TZVP",
            "solvent_model": "CPCM",
            "solvent": "water",
            "complete_thermochemistry": True,
        }
    }
    res = compute_reaction_thermodynamics([sp_a, sp_b])
    assert res["delta_G_hartree"] == pytest.approx(-0.05, abs=1e-4)
    assert not any("MIXED" in w for w in res.get("warnings", []))


def test_compute_reaction_thermodynamics_mixed_basis():
    """Mixed basis set with identical method triggers MIXED LEVEL OF THEORY warning."""
    sp_a = {
        "nu": -1.0, "role": "reactant", "display_name": "A",
        "final_result": {
            "E_final": -50.0, "H_final": -49.9, "G_final": -49.95, "zpe_hartree": 0.02,
            "entropy_j_mol_k": 150.0, "temperature_k": 298.15,
            "method": "r2SCAN-3c", "basis_set": "def2-mTZVP",
            "solvent_model": None, "solvent": None, "complete_thermochemistry": True,
        }
    }
    sp_b = {
        "nu": 1.0, "role": "product", "display_name": "B",
        "final_result": {
            "E_final": -50.02, "H_final": -49.92, "G_final": -49.97, "zpe_hartree": 0.02,
            "entropy_j_mol_k": 150.0, "temperature_k": 298.15,
            "method": "r2SCAN-3c", "basis_set": "def2-TZVP",
            "solvent_model": None, "solvent": None, "complete_thermochemistry": True,
        }
    }
    res = compute_reaction_thermodynamics([sp_a, sp_b])
    assert any("MIXED LEVEL OF THEORY" in w for w in res.get("warnings", []))


def test_compute_reaction_thermodynamics_mixed_solvent():
    """Mixed solvent models across species triggers MIXED SOLVENT MODELS warning."""
    sp_a = {
        "nu": -1.0, "role": "reactant", "display_name": "A",
        "final_result": {
            "E_final": -50.0, "H_final": -49.9, "G_final": -49.95, "zpe_hartree": 0.02,
            "entropy_j_mol_k": 150.0, "temperature_k": 298.15,
            "method": "B3LYP", "basis_set": "def2-SVP",
            "solvent_model": "CPCM", "solvent": "water", "complete_thermochemistry": True,
        }
    }
    sp_b = {
        "nu": 1.0, "role": "product", "display_name": "B",
        "final_result": {
            "E_final": -50.02, "H_final": -49.92, "G_final": -49.97, "zpe_hartree": 0.02,
            "entropy_j_mol_k": 150.0, "temperature_k": 298.15,
            "method": "B3LYP", "basis_set": "def2-SVP",
            "solvent_model": "SMD", "solvent": "toluene", "complete_thermochemistry": True,
        }
    }
    res = compute_reaction_thermodynamics([sp_a, sp_b])
    assert any("MIXED SOLVENT MODELS" in w for w in res.get("warnings", []))

