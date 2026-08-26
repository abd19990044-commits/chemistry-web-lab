# -*- coding: utf-8 -*-
"""Unit tests for NMR spectrum generation, visual grouping, and data exports."""

from __future__ import annotations

import csv
import io
import json
import pytest

from orca_engine.nmr import (
    NMRAtomRecord,
    NMRNucleus,
    NMRReference,
    build_nmr_spectrum,
    export_nmr_csv,
    export_nmr_json,
)


def _sample_atoms() -> list[NMRAtomRecord]:
    return [
        NMRAtomRecord(atom_index=0, element="C", isotope="13C", isotropic_shielding=173.9, assignment="C0", coordinate=(0.0, 0.0, 0.0)),
        NMRAtomRecord(atom_index=1, element="C", isotope="13C", isotropic_shielding=155.0, assignment="C1", coordinate=(1.2, 0.0, 0.0)),
        NMRAtomRecord(atom_index=2, element="H", isotope="1H", isotropic_shielding=30.50, assignment="H2", coordinate=(0.0, 1.0, 0.0)),
        NMRAtomRecord(atom_index=3, element="H", isotope="1H", isotropic_shielding=30.51, assignment="H3", coordinate=(0.0, -1.0, 0.0)),
        NMRAtomRecord(atom_index=4, element="H", isotope="1H", isotropic_shielding=28.20, assignment="H4", coordinate=(1.5, 1.0, 0.0)),
    ]


def test_spectrum_generation_1h_individual():
    atoms = _sample_atoms()
    tms_1h = NMRReference(nucleus=NMRNucleus.H1, reference_shielding=31.77, reference_method="TPSS", reference_basis="pcSseg-3")

    spec = build_nmr_spectrum(
        atoms,
        nucleus=NMRNucleus.H1,
        reference=tms_1h,
        group_close_peaks=False,
        method="TPSS",
        basis_set="pcSseg-3",
    )

    assert spec.nucleus == "1H"
    assert spec.is_reference_applied is True
    assert len(spec.peaks) == 3
    assert spec.peaks[0].intensity == 1.0
    assert spec.peaks[0].assignments == ["H2"]
    assert spec.peaks[0].atom_indices == [2]
    # Check shift delta = 31.77 - 30.50 = 1.27 ppm
    assert spec.peaks[0].position_ppm == pytest.approx(1.27, abs=1e-3)


def test_spectrum_generation_1h_grouped():
    atoms = _sample_atoms()
    tms_1h = NMRReference(nucleus=NMRNucleus.H1, reference_shielding=31.77, reference_method="TPSS", reference_basis="pcSseg-3")

    # Group close peaks with tolerance 0.02 ppm (H2 and H3 have shifts 1.27 and 1.26, diff 0.01 <= 0.02)
    spec = build_nmr_spectrum(
        atoms,
        nucleus=NMRNucleus.H1,
        reference=tms_1h,
        group_close_peaks=True,
        group_tolerance_ppm=0.02,
        method="TPSS",
        basis_set="pcSseg-3",
    )

    assert len(spec.peaks) == 2
    # The coalesced peak should have intensity 2.0 and both atoms
    grouped_peak = next(p for p in spec.peaks if p.intensity == 2.0)
    assert set(grouped_peak.atom_indices) == {2, 3}
    assert set(grouped_peak.assignments) == {"H2", "H3"}
    assert len(grouped_peak.atoms) == 2


def test_export_nmr_csv():
    atoms = _sample_atoms()
    tms_1h = NMRReference(nucleus=NMRNucleus.H1, reference_shielding=31.77)
    spec = build_nmr_spectrum(atoms, nucleus=NMRNucleus.H1, reference=tms_1h)

    csv_out = export_nmr_csv(spec)
    reader = csv.DictReader(io.StringIO(csv_out))
    rows = list(reader)

    assert len(rows) == 3
    assert rows[0]["element"] == "H"
    assert rows[0]["assignment"] == "H2"
    assert float(rows[0]["isotropic_shielding_ppm"]) == pytest.approx(30.50, abs=1e-3)
    assert float(rows[0]["chemical_shift_ppm"]) == pytest.approx(1.27, abs=1e-3)


def test_export_nmr_json():
    atoms = _sample_atoms()
    tms_1h = NMRReference(nucleus=NMRNucleus.H1, reference_shielding=31.77)
    spec = build_nmr_spectrum(atoms, nucleus=NMRNucleus.H1, reference=tms_1h)

    json_out = export_nmr_json(spec)
    parsed = json.loads(json_out)

    assert parsed["nucleus"] == "1H"
    assert parsed["is_reference_applied"] is True
    assert len(parsed["peaks"]) == 3
