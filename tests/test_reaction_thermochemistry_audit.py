# -*- coding: utf-8 -*-
"""Comprehensive regression test suite for Reaction Thermochemistry Audit (Tests A through F).

Validates:
- Test A: Atom-balanced reaction with multi-character elements (Toluene + NBS -> Benzyl bromide + Succinimide)
- Test B: Intentionally imbalanced reaction with specific element diagnostic reporting
- Test C: Multi-level composite energy assembly (E_high, H_comp = E_high + dH_therm, G_comp = E_high + dG_therm, E0 = E_high + ZPE)
- Test D: Multi-level species atom counting non-duplication
- Test E: Incompatible Opt/Freq and SP molecular compositions flagged
- Test F: Single-level backward compatibility
"""
import pytest

from orca_engine.models import EnergyKind, JobData, MoleculeData, Reaction, ReactionTerm
from orca_engine.parser import OrcaParser
from orca_engine.thermochemistry import ThermochemistryEngine


def _make_orca_output(
    energy: float,
    elements: list[str],
    coords: list[tuple[float, float, float]],
    zpe: float | None = None,
    enthalpy: float | None = None,
    gibbs: float | None = None,
    method: str = "B3LYP",
    basis: str = "def2-SVP",
    charge: int = 0,
    multiplicity: int = 1,
    temp_k: float = 298.15,
) -> str:
    """Generate deterministic synthetic ORCA output."""
    lines = [
        "************************************************************",
        "*                        ORCA 6.0.0                        *",
        "************************************************************",
        f"! {method} {basis} Opt Freq",
        "",
        f"Total Charge           Charge          ...    {charge}",
        f"Multiplicity           Mult            ...    {multiplicity}",
        "",
        "CARTESIAN COORDINATES (ANGSTROEM)",
        "---------------------------------",
    ]
    for el, (x, y, z) in zip(elements, coords):
        lines.append(f"  {el:<4} {x:12.6f} {y:12.6f} {z:12.6f}")
    lines.extend([
        "",
        f"FINAL SINGLE POINT ENERGY      {energy:.12f}",
        "",
    ])
    if enthalpy is not None or gibbs is not None or zpe is not None:
        lines.extend([
            "-------------------",
            f"THERMOCHEMISTRY AT {temp_k:.2f} K",
            "-------------------",
            f"Temperature         ... {temp_k:.2f} K",
            "Pressure            ... 1.00 atm",
        ])
        if zpe is not None:
            lines.append(f"Zero point energy    ... {zpe:.8f} Eh")
        if enthalpy is not None:
            lines.append(f"Total Enthalpy       ... {enthalpy:.8f} Eh")
        if gibbs is not None:
            lines.append(f"Final Gibbs free energy ... {gibbs:.8f} Eh")
    lines.extend([
        "",
        "****ORCA TERMINATED NORMALLY****",
    ])
    return "\n".join(lines)


# Synthetic geometries for Test A & B
# Toluene: C7H8 (15 atoms)
TOLUENE_ELEMENTS = ["C"] * 7 + ["H"] * 8
TOLUENE_COORDS = [(float(i) * 0.1, float(i) * 0.2, 0.0) for i in range(15)]

# NBS: C4H4BrNO2 (12 atoms: 4 C, 4 H, 1 Br, 1 N, 2 O)
NBS_ELEMENTS = ["C"] * 4 + ["H"] * 4 + ["Br"] + ["N"] + ["O"] * 2
NBS_COORDS = [(float(i) * 0.15, float(i) * 0.1, 0.5) for i in range(12)]

# Benzyl bromide: C7H7Br (15 atoms: 7 C, 7 H, 1 Br)
BENZYL_BR_ELEMENTS = ["C"] * 7 + ["H"] * 7 + ["Br"]
BENZYL_BR_COORDS = [(float(i) * 0.1, float(i) * 0.2, 0.1) for i in range(15)]

# Succinimide: C4H5NO2 (12 atoms: 4 C, 5 H, 1 N, 2 O)
SUCCINIMIDE_ELEMENTS = ["C"] * 4 + ["H"] * 5 + ["N"] + ["O"] * 2
SUCCINIMIDE_COORDS = [(float(i) * 0.12, float(i) * 0.15, 0.3) for i in range(12)]


def test_audit_test_a_balanced_reaction_with_multi_character_elements():
    """Test A: Balanced reaction (Toluene + NBS -> Benzyl bromide + Succinimide)

    C7H8 + C4H4BrNO2 -> C7H7Br + C4H5NO2
    Reactants: C=11, H=12, Br=1, N=1, O=2
    Products:  C=11, H=12, Br=1, N=1, O=2
    Must be recognized as atom_balanced = True and charge_balanced = True.
    """
    tol_out = _make_orca_output(-271.50, TOLUENE_ELEMENTS, TOLUENE_COORDS, zpe=0.12, enthalpy=-271.37, gibbs=-271.41)
    nbs_out = _make_orca_output(-2950.20, NBS_ELEMENTS, NBS_COORDS, zpe=0.08, enthalpy=-2950.11, gibbs=-2950.15)
    bbr_out = _make_orca_output(-2841.00, BENZYL_BR_ELEMENTS, BENZYL_BR_COORDS, zpe=0.11, enthalpy=-2840.88, gibbs=-2840.92)
    suc_out = _make_orca_output(-380.70, SUCCINIMIDE_ELEMENTS, SUCCINIMIDE_COORDS, zpe=0.09, enthalpy=-380.60, gibbs=-380.64)

    molecules = {
        "Toluene": MoleculeData(name="Toluene", jobs=OrcaParser(tol_out).parse(), sources=["Toluene.out"]),
        "NBS": MoleculeData(name="NBS", jobs=OrcaParser(nbs_out).parse(), sources=["NBS.out"]),
        "Benzyl_bromide": MoleculeData(name="Benzyl_bromide", jobs=OrcaParser(bbr_out).parse(), sources=["Benzyl_bromide.out"]),
        "Succinimide": MoleculeData(name="Succinimide", jobs=OrcaParser(suc_out).parse(), sources=["Succinimide.out"]),
    }

    engine = ThermochemistryEngine(molecules)
    result = engine.evaluate("Toluene + NBS -> Benzyl_bromide + Succinimide")

    assert result.consistency.atom_balanced is True
    assert result.consistency.charge_balanced is True
    assert result.consistency.atom_imbalance == {}
    assert not any("NOT ATOM BALANCED" in w.upper() for w in result.consistency.warnings)


def test_audit_test_b_intentionally_imbalanced_reaction_diagnostics():
    """Test B: Intentionally imbalanced reaction (missing Succinimide product)

    Toluene + NBS -> Benzyl_bromide
    Must flag atom_balanced = False with specific missing element counts.
    """
    tol_out = _make_orca_output(-271.50, TOLUENE_ELEMENTS, TOLUENE_COORDS, zpe=0.12, enthalpy=-271.37, gibbs=-271.41)
    nbs_out = _make_orca_output(-2950.20, NBS_ELEMENTS, NBS_COORDS, zpe=0.08, enthalpy=-2950.11, gibbs=-2950.15)
    bbr_out = _make_orca_output(-2841.00, BENZYL_BR_ELEMENTS, BENZYL_BR_COORDS, zpe=0.11, enthalpy=-2840.88, gibbs=-2840.92)

    molecules = {
        "Toluene": MoleculeData(name="Toluene", jobs=OrcaParser(tol_out).parse(), sources=["Toluene.out"]),
        "NBS": MoleculeData(name="NBS", jobs=OrcaParser(nbs_out).parse(), sources=["NBS.out"]),
        "Benzyl_bromide": MoleculeData(name="Benzyl_bromide", jobs=OrcaParser(bbr_out).parse(), sources=["Benzyl_bromide.out"]),
    }

    engine = ThermochemistryEngine(molecules)
    result = engine.evaluate("Toluene + NBS -> Benzyl_bromide")

    assert result.consistency.atom_balanced is False
    assert result.consistency.atom_imbalance.get("C") == -4
    assert result.consistency.atom_imbalance.get("H") == -5
    assert result.consistency.atom_imbalance.get("N") == -1
    assert result.consistency.atom_imbalance.get("O") == -2
    assert any("not atom balanced" in w.lower() for w in result.consistency.warnings)


def test_audit_test_c_multi_level_energy_replacement():
    """Test C: Multi-level composite energy assembly

    Low-level (Opt+Freq):
      E_low = -100.0 Eh
      H_low = -99.99 Eh  ==> dH_thermal = +0.01 Eh
      G_low = -100.005 Eh ==> dG_thermal = -0.005 Eh
      ZPE_low = 0.015 Eh
    High-level (SP):
      E_high = -100.10 Eh

    Expected composite quantities:
      E_final = -100.10 Eh
      H_final = -100.10 + 0.010 = -100.090 Eh
      G_final = -100.10 - 0.005 = -100.105 Eh
      E0_final = -100.10 + 0.015 = -100.085 Eh
    """
    freq_out = _make_orca_output(
        energy=-100.00,
        elements=["H", "H"],
        coords=[(0.0, 0.0, 0.0), (0.0, 0.0, 0.74)],
        zpe=0.015,
        enthalpy=-99.99,
        gibbs=-100.005,
        method="B3LYP",
        basis="def2-SVP",
    )
    sp_out = _make_orca_output(
        energy=-100.10,
        elements=["H", "H"],
        coords=[(0.0, 0.0, 0.0), (0.0, 0.0, 0.74)],
        method="DLPNO-CCSD(T)",
        basis="def2-QZVPP",
    )

    freq_jobs = OrcaParser(freq_out).parse()
    sp_jobs = OrcaParser(sp_out).parse()
    combined_jobs = freq_jobs + sp_jobs

    molecules = {
        "H2": MoleculeData(name="H2", jobs=combined_jobs, sources=["h2_freq.out", "h2_sp.out"]),
    }

    engine = ThermochemistryEngine(molecules)

    e_best = engine._get_best_electronic("H2", combined_jobs)
    assert abs(e_best - (-100.10)) < 1e-9

    h_best = engine._get_best_energy("H2", EnergyKind.ENTHALPY)
    assert abs(h_best - (-100.090)) < 1e-9

    g_best = engine._get_best_energy("H2", EnergyKind.GIBBS)
    assert abs(g_best - (-100.105)) < 1e-9

    e0_best = engine._get_best_e0("H2", combined_jobs)
    assert abs(e0_best - (-100.085)) < 1e-9


def test_audit_test_d_multi_level_atom_counting_non_duplication():
    """Test D: SP file does not duplicate atoms for multi-level species."""
    freq_out = _make_orca_output(
        energy=-271.50,
        elements=TOLUENE_ELEMENTS,
        coords=TOLUENE_COORDS,
        zpe=0.12,
        enthalpy=-271.37,
        gibbs=-271.41,
    )
    sp_out = _make_orca_output(
        energy=-271.65,
        elements=TOLUENE_ELEMENTS,
        coords=TOLUENE_COORDS,
        method="DLPNO-CCSD(T)",
    )

    jobs = OrcaParser(freq_out).parse() + OrcaParser(sp_out).parse()
    molecules = {
        "Toluene": MoleculeData(name="Toluene", jobs=jobs, sources=["tol_opt.out", "tol_sp.out"]),
    }

    engine = ThermochemistryEngine(molecules)
    # Toluene -> Toluene identity reaction
    result = engine.evaluate("Toluene -> Toluene")
    assert result.consistency.atom_balanced is True

    # Check that stoichiometry counts 7 C and 8 H, NOT 14 C and 16 H
    counts, known = engine._side_stoichiometry([ReactionTerm(coefficient=1.0, molecule_name="Toluene")])
    assert known is True
    assert counts == {"C": 7.0, "H": 8.0}


def test_audit_test_e_incompatible_sp_composition_flagged():
    """Test E: Incompatible Opt/Freq and SP formulas for the same species are flagged."""
    freq_out = _make_orca_output(
        energy=-271.50,
        elements=TOLUENE_ELEMENTS,  # C7H8
        coords=TOLUENE_COORDS,
        zpe=0.12,
        enthalpy=-271.37,
        gibbs=-271.41,
    )
    # Incorrect SP file attached (Benzyl bromide instead of Toluene)
    sp_out = _make_orca_output(
        energy=-2841.10,
        elements=BENZYL_BR_ELEMENTS,  # C7H7Br
        coords=BENZYL_BR_COORDS,
        method="DLPNO-CCSD(T)",
    )

    jobs = OrcaParser(freq_out).parse() + OrcaParser(sp_out).parse()
    molecules = {
        "Toluene": MoleculeData(name="Toluene", jobs=jobs, sources=["tol_opt.out", "tol_sp_wrong.out"]),
    }

    engine = ThermochemistryEngine(molecules)
    report = engine.check_consistency(Reaction(equation="Toluene -> Toluene", reactants=[ReactionTerm(1.0, "Toluene")], products=[ReactionTerm(1.0, "Toluene")]))

    assert any("Incompatible Opt/Freq and SP compositions" in w for w in report.warnings)


def test_audit_test_f_single_level_backward_compatibility():
    """Test F: Single-level calculation remains 100% backward compatible."""
    freq_out = _make_orca_output(
        energy=-76.40,
        elements=["O", "H", "H"],
        coords=[(0.0, 0.0, 0.0), (0.0, 0.75, 0.6), (0.0, -0.75, 0.6)],
        zpe=0.021,
        enthalpy=-76.375,
        gibbs=-76.398,
        method="B3LYP",
        basis="def2-SVP",
    )

    jobs = OrcaParser(freq_out).parse()
    molecules = {
        "H2O": MoleculeData(name="H2O", jobs=jobs, sources=["h2o.out"]),
    }

    engine = ThermochemistryEngine(molecules)
    e = engine._get_best_electronic("H2O", jobs)
    h = engine._get_best_energy("H2O", EnergyKind.ENTHALPY)
    g = engine._get_best_energy("H2O", EnergyKind.GIBBS)
    e0 = engine._get_best_e0("H2O", jobs)

    assert abs(e - (-76.40)) < 1e-9
    assert abs(h - (-76.375)) < 1e-9
    assert abs(g - (-76.398)) < 1e-9
    assert abs(e0 - (-76.379)) < 1e-9
