# -*- coding: utf-8 -*-
"""Scientific golden tests for NMR shielding and chemical shift conversion."""

from __future__ import annotations

import pytest

from orca_engine.nmr import (
    NMR_REFERENCE_CATALOG,
    NMRNucleus,
    NMRReference,
    OrcaNMRParser,
    build_nmr_spectrum,
    check_reference_compatibility,
)

# Golden Benchmark 1: Ethanol HF/SVP from ORCA Manual Section 5.21 (p. 1032-1033)
ORCA_MANUAL_ETHANOL_OUTPUT = """
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

# Golden Benchmark 2: Propionic acid 13C from ORCA Manual Section 5.21.1 (p. 1034)
# At B3LYP/TZVPP, TMS isotropic shielding is 184.3 ppm.
# Theoretical shieldings for Propionic Acid C1, C2, C3:
# C1 (CH3): 173.9 ppm -> delta = 184.3 - 173.9 = 10.4 ppm
# C2 (CH2): 155.0 ppm -> delta = 184.3 - 155.0 = 29.3 ppm
# C3 (COOH): 2.9 ppm  -> delta = 184.3 - 2.9 = 181.4 ppm
ORCA_MANUAL_PROPIONIC_ACID_13C = """
  ------------------
  CHEMICAL SHIELDING
  ------------------

Nucleus  Element    Isotropic     Anisotropy
-------  -------  ------------  ------------
    0       C          173.900        25.100
    1       C          155.000        32.400
    2       C            2.900       110.800
"""


def test_golden_ethanol_hf_svp_shieldings():
    """Verify exact isotropic shielding extraction against ORCA Manual Section 5.21 benchmark."""
    parser = OrcaNMRParser()
    result = parser.parse_text(ORCA_MANUAL_ETHANOL_OUTPUT)

    expected_shieldings = {
        0: 229.484,
        1: 227.642,
        2: 56.015,
        3: 55.460,
        4: 55.460,
        5: 334.125,
        6: 47.337,
        7: 47.337,
        8: 64.252,
    }

    expected_anisotropies = {
        0: 59.356,
        1: 62.878,
        2: 12.469,
        3: 15.284,
        4: 15.284,
        5: 110.616,
        6: 27.101,
        7: 27.101,
        8: 32.114,
    }

    assert len(result.atoms) == 9
    for atom in result.atoms:
        assert atom.atom_index in expected_shieldings
        assert atom.isotropic_shielding == pytest.approx(expected_shieldings[atom.atom_index], rel=1e-5)
        assert atom.anisotropy == pytest.approx(expected_anisotropies[atom.atom_index], rel=1e-5)


def test_golden_propionic_acid_13c_chemical_shifts():
    """Verify chemical shift conversion delta = sigma_ref - sigma_sample matching manual Table 5.21.1."""
    parser = OrcaNMRParser()
    result = parser.parse_text(ORCA_MANUAL_PROPIONIC_ACID_13C)

    # TMS reference at B3LYP/TZVPP from ORCA Manual Section 5.21.1 (p. 1034)
    tms_ref = NMRReference(
        nucleus=NMRNucleus.C13,
        reference_shielding=184.30,
        reference_method="B3LYP",
        reference_basis="TZVPP",
        reference_source="ORCA Manual Table 5.21.1",
    )

    spectrum = build_nmr_spectrum(
        result.atoms,
        nucleus=NMRNucleus.C13,
        reference=tms_ref,
        method="B3LYP",
        basis_set="TZVPP",
    )

    assert spectrum.is_reference_applied is True
    assert spectrum.x_quantity == "chemical_shift"

    # Verify calculated delta: 10.4 ppm, 29.3 ppm, 181.4 ppm
    deltas = [a.chemical_shift for a in spectrum.atoms]
    assert deltas[0] == pytest.approx(10.40, abs=1e-2)
    assert deltas[1] == pytest.approx(29.30, abs=1e-2)
    assert deltas[2] == pytest.approx(181.40, abs=1e-2)


def test_golden_uncalibrated_shielding_mode():
    """Verify that when no reference is configured, delta is None and isotropic shielding is kept."""
    parser = OrcaNMRParser()
    result = parser.parse_text(ORCA_MANUAL_PROPIONIC_ACID_13C)

    spectrum = build_nmr_spectrum(result.atoms, nucleus=NMRNucleus.C13, reference=None)

    assert spectrum.is_reference_applied is False
    assert spectrum.x_quantity == "isotropic_shielding"
    assert len(spectrum.warnings) > 0
    assert "Reference shielding not configured" in spectrum.warnings[0]

    for atom in spectrum.atoms:
        assert atom.chemical_shift is None
        assert atom.isotropic_shielding > 0


def test_reference_catalog_exact_manual_values():
    """Verify standard reference presets against exact values in ORCA 6.1 Manual."""
    assert "1H" in NMR_REFERENCE_CATALOG
    assert "13C" in NMR_REFERENCE_CATALOG

    # 1H verified preset: TPSS / pcSseg-3 = 31.77 ppm (Manual p. 1037)
    h1_presets = {x["id"]: x for x in NMR_REFERENCE_CATALOG["1H"]}
    assert "tms_tpss_pcsseg3" in h1_presets
    assert h1_presets["tms_tpss_pcsseg3"]["shielding"] == 31.77
    assert h1_presets["tms_tpss_pcsseg3"]["method"] == "TPSS"
    assert h1_presets["tms_tpss_pcsseg3"]["basis_set"] == "pcSseg-3"

    # 13C verified presets: Table 5.21.1 (p. 1034) & Section 5.21.3 (p. 1037)
    c13_presets = {x["id"]: x for x in NMR_REFERENCE_CATALOG["13C"]}
    assert c13_presets["tms_b3lyp_tzvpp"]["shielding"] == 184.30
    assert c13_presets["tms_b3lyp_tzvpp"]["basis_set"] == "TZVPP"
    assert c13_presets["tms_bp86_tzvpp"]["shielding"] == 184.80
    assert c13_presets["tms_bp86_tzvpp"]["basis_set"] == "TZVPP"
    assert c13_presets["tms_hf_tzvpp"]["shielding"] == 194.10
    assert c13_presets["tms_hf_tzvpp"]["basis_set"] == "TZVPP"
    assert c13_presets["tms_tpss_pcsseg3"]["shielding"] == 188.10
    assert c13_presets["tms_tpss_pcsseg3"]["basis_set"] == "pcSseg-3"


def test_level_of_theory_compatibility_validation():
    """Verify strict functional and basis set compatibility checking."""
    ref_b3lyp_tzvpp = NMRReference(
        nucleus=NMRNucleus.C13,
        reference_shielding=184.30,
        reference_method="B3LYP",
        reference_basis="TZVPP",
        reference_source="Preset",
    )

    # 1. Exact match -> compatible
    compat, warn = check_reference_compatibility(ref_b3lyp_tzvpp, "B3LYP", "TZVPP")
    assert compat is True
    assert warn is None

    # 2. Method mismatch (HF calculation with B3LYP reference) -> incompatible
    compat, warn = check_reference_compatibility(ref_b3lyp_tzvpp, "HF", "TZVPP")
    assert compat is False
    assert warn is not None
    assert "Level of theory mismatch" in warn

    # 3. Basis set mismatch (def2-TZVP != TZVPP) -> incompatible
    compat, warn = check_reference_compatibility(ref_b3lyp_tzvpp, "B3LYP", "def2-TZVP")
    assert compat is False
    assert warn is not None
    assert "TZVPP" in warn and "def2-TZVP" in warn
