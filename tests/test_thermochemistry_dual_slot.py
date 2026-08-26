"""Tests for Reaction Thermochemistry dual-slot (Opt/Freq + SP) and conditions."""

import io
import pytest
from orca_engine.parser import OrcaParser
from orca_engine.models import MoleculeData, EnergyKind
from orca_engine.thermochemistry import ThermochemistryEngine, Reaction, ReactionTerm, ReactionResult


SAMPLE_OPT_FREQ_H2 = """
  ************************************************************
  *                        ORCA 6.0.0                        *
  ************************************************************
! B3LYP def2-SVP Opt Freq

FINAL SINGLE POINT ENERGY      -1.165000000000

-------------------
THERMOCHEMISTRY AT 298.15 K
-------------------
Temperature         ... 298.15 K
Pressure            ... 1.00 atm
Total Thermal Energy ... -1.15500000 Eh
Total Enthalpy       ... -1.15405600 Eh
Final Gibbs free energy ... -1.17000000 Eh
Zero point energy    ... 0.01000000 Eh
Electronic energy    ... -1.16500000 Eh
G-E(el)              ... -0.00500000 Eh
H-E(el)              ... 0.01094400 Eh
"""

SAMPLE_OPT_FREQ_O2 = """
  ************************************************************
  *                        ORCA 6.0.0                        *
  ************************************************************
! B3LYP def2-SVP Opt Freq

FINAL SINGLE POINT ENERGY      -150.200000000000

-------------------
THERMOCHEMISTRY AT 298.15 K
-------------------
Temperature         ... 298.15 K
Pressure            ... 1.00 atm
Total Thermal Energy ... -150.19000000 Eh
Total Enthalpy       ... -150.18905600 Eh
Final Gibbs free energy ... -150.21000000 Eh
Zero point energy    ... 0.01000000 Eh
Electronic energy    ... -150.20000000 Eh
G-E(el)              ... -0.01000000 Eh
H-E(el)              ... 0.01094400 Eh
"""

SAMPLE_OPT_FREQ_H2O = """
  ************************************************************
  *                        ORCA 6.0.0                        *
  ************************************************************
! B3LYP def2-SVP Opt Freq

FINAL SINGLE POINT ENERGY      -76.400000000000

-------------------
THERMOCHEMISTRY AT 298.15 K
-------------------
Temperature         ... 298.15 K
Pressure            ... 1.00 atm
Total Thermal Energy ... -76.38000000 Eh
Total Enthalpy       ... -76.37905600 Eh
Final Gibbs free energy ... -76.40500000 Eh
Zero point energy    ... 0.02000000 Eh
Electronic energy    ... -76.40000000 Eh
G-E(el)              ... -0.00500000 Eh
H-E(el)              ... 0.02094400 Eh
"""

SAMPLE_SP_HIGH_LEVEL_H2O = """
  ************************************************************
  *                        ORCA 6.0.0                        *
  ************************************************************
! DLPNO-CCSD(T) def2-QZVPP TightSCF

FINAL SINGLE POINT ENERGY      -76.450000000000
"""


def test_thermochemistry_with_spaces_in_names():
    """Verify that reaction equations with spaced species names parse cleanly."""
    parsed = ThermochemistryEngine.parse_reaction("2 'Reactant 1' + 1 'Reactant 2' -> 2 'Product 1'")
    assert len(parsed.reactants) == 2
    assert parsed.reactants[0].molecule_name == "Reactant 1"
    assert parsed.reactants[0].coefficient == 2.0
    assert parsed.reactants[1].molecule_name == "Reactant 2"
    assert parsed.reactants[1].coefficient == 1.0
    assert len(parsed.products) == 1
    assert parsed.products[0].molecule_name == "Product 1"
    assert parsed.products[0].coefficient == 2.0


def test_thermochemistry_single_level_evaluation():
    """Verify standard single-level reaction thermochemistry evaluation."""
    h2_jobs = OrcaParser(io.StringIO(SAMPLE_OPT_FREQ_H2), source_name="h2").parse()
    o2_jobs = OrcaParser(io.StringIO(SAMPLE_OPT_FREQ_O2), source_name="o2").parse()
    h2o_jobs = OrcaParser(io.StringIO(SAMPLE_OPT_FREQ_H2O), source_name="h2o").parse()

    molecules = {
        "H2": MoleculeData(name="H2", jobs=h2_jobs, sources=["H2"]),
        "O2": MoleculeData(name="O2", jobs=o2_jobs, sources=["O2"]),
        "H2O": MoleculeData(name="H2O", jobs=h2o_jobs, sources=["H2O"]),
    }

    engine = ThermochemistryEngine(molecules)
    result = engine.evaluate("2 H2 + O2 -> 2 H2O")

    assert result.delta_g_kcal_mol is not None
    assert result.delta_h_kcal_mol is not None
    assert result.equilibrium_constant_keq is not None
    assert result.temperature_k == 298.15


def test_thermochemistry_multi_level_composite_evaluation():
    """Verify multi-level composite (Opt/Freq + high-level SP) evaluation."""
    h2_jobs = OrcaParser(io.StringIO(SAMPLE_OPT_FREQ_H2), source_name="h2").parse()
    o2_jobs = OrcaParser(io.StringIO(SAMPLE_OPT_FREQ_O2), source_name="o2").parse()
    
    # H2O with Opt/Freq + high-level SP
    h2o_freq = OrcaParser(io.StringIO(SAMPLE_OPT_FREQ_H2O), source_name="h2o_opt").parse()
    h2o_sp = OrcaParser(io.StringIO(SAMPLE_SP_HIGH_LEVEL_H2O), source_name="h2o_sp").parse()
    h2o_jobs = h2o_freq + h2o_sp

    molecules = {
        "H2": MoleculeData(name="H2", jobs=h2_jobs, sources=["H2"]),
        "O2": MoleculeData(name="O2", jobs=o2_jobs, sources=["O2"]),
        "H2O": MoleculeData(name="H2O", jobs=h2o_jobs, sources=["H2O"]),
    }

    engine = ThermochemistryEngine(molecules)
    result = engine.evaluate("2 H2 + O2 -> 2 H2O")

    assert result.delta_g_kcal_mol is not None
    # Verify that H2O electronic energy used was the high-level SP (-76.45 Eh)
    best_e_h2o = engine._get_best_electronic("H2O", h2o_jobs)
    assert abs(best_e_h2o - (-76.45)) < 1e-6
