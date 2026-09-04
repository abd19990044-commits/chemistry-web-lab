# -*- coding: utf-8 -*-
"""Calculated NMR Spectrum Analyzer and Chemical Shift Module.

Supports:
- 1H NMR (Proton)
- 13C NMR (Carbon-13)

Distinguishes rigorously between absolute isotropic shielding (sigma, in ppm)
and chemical shift (delta = sigma_ref - sigma_sample, in ppm).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Sequence

LOGGER = logging.getLogger("orca_engine.nmr")


class NMRNucleus(str, Enum):
    """Supported NMR active nuclei."""

    H1 = "1H"
    C13 = "13C"

    @classmethod
    def from_element(cls, element: str) -> NMRNucleus | None:
        elem = (element or "").strip().upper()
        if elem in ("H", "1H"):
            return cls.H1
        if elem in ("C", "13C"):
            return cls.C13
        return None

    @property
    def element_symbol(self) -> str:
        return "H" if self == NMRNucleus.H1 else "C"

    @property
    def full_name(self) -> str:
        return "¹H NMR (Proton)" if self == NMRNucleus.H1 else "¹³C NMR (Carbon-13)"


@dataclass
class NMRReference:
    """Standard reference shielding model for NMR chemical shift conversion.

    Equation:
        delta (chemical shift, ppm) = sigma_ref - sigma_sample
    """

    nucleus: NMRNucleus
    reference_shielding: float  # sigma_ref in ppm
    reference_method: str = "Custom"
    reference_basis: str | None = None
    reference_source: str = "User Supplied"
    solvent: str | None = None
    notes: str | None = None

    def compute_chemical_shift(self, isotropic_shielding: float) -> float:
        """Compute delta in ppm: sigma_ref - sigma_sample."""
        return round(self.reference_shielding - isotropic_shielding, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nucleus": self.nucleus.value,
            "reference_shielding": self.reference_shielding,
            "reference_method": self.reference_method,
            "reference_basis": self.reference_basis,
            "reference_source": self.reference_source,
            "solvent": self.solvent,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NMRReference:
        nucleus_str = data.get("nucleus", "1H")
        nucleus = NMRNucleus(nucleus_str) if nucleus_str in ("1H", "13C") else NMRNucleus.H1
        return cls(
            nucleus=nucleus,
            reference_shielding=float(data["reference_shielding"]),
            reference_method=str(data.get("reference_method", "Custom")),
            reference_basis=data.get("reference_basis"),
            reference_source=str(data.get("reference_source", "User Supplied")),
            solvent=data.get("solvent"),
            notes=data.get("notes"),
        )


# Reference presets strictly verified against the official ORCA 6.1 Manual (Section 5.21)
# Any reference not traceable to ORCA 6.1 documentation has been removed to preserve scientific integrity.
NMR_REFERENCE_CATALOG: dict[str, list[dict[str, Any]]] = {
    "1H": [
        {
            "id": "tms_tpss_pcsseg3",
            "name": "TMS (TPSS / pcSseg-3) - 31.77 ppm",
            "shielding": 31.77,
            "method": "TPSS",
            "basis_set": "pcSseg-3",
            "source": "ORCA Manual Section 5.21.3 (p. 1037)",
            "notes": "ORCA 6.1 manual benchmark for 1H NMR spectrum simulation (TMS at TPSS/pcSseg-3).",
        },
    ],
    "13C": [
        {
            "id": "tms_b3lyp_tzvpp",
            "name": "TMS (B3LYP / TZVPP) - 184.30 ppm",
            "shielding": 184.30,
            "method": "B3LYP",
            "basis_set": "TZVPP",
            "source": "ORCA Manual Section 5.21.1 (p. 1034)",
            "notes": "ORCA 6.1 manual Table 5.21.1 benchmark for 13C chemical shift conversion (TMS at B3LYP/TZVPP). Note: TZVPP != def2-TZVP.",
        },
        {
            "id": "tms_bp86_tzvpp",
            "name": "TMS (BP86 / TZVPP) - 184.80 ppm",
            "shielding": 184.80,
            "method": "BP86",
            "basis_set": "TZVPP",
            "source": "ORCA Manual Section 5.21.1 (p. 1034)",
            "notes": "ORCA 6.1 manual Table 5.21.1 benchmark for 13C chemical shift conversion (TMS at BP86/TZVPP).",
        },
        {
            "id": "tms_hf_tzvpp",
            "name": "TMS (HF / TZVPP) - 194.10 ppm",
            "shielding": 194.10,
            "method": "HF",
            "basis_set": "TZVPP",
            "source": "ORCA Manual Section 5.21.1 (p. 1034)",
            "notes": "ORCA 6.1 manual Table 5.21.1 benchmark for 13C chemical shift conversion (TMS at HF/TZVPP).",
        },
        {
            "id": "tms_tpss_pcsseg3",
            "name": "TMS (TPSS / pcSseg-3) - 188.10 ppm",
            "shielding": 188.10,
            "method": "TPSS",
            "basis_set": "pcSseg-3",
            "source": "ORCA Manual Section 5.21.3 (p. 1037)",
            "notes": "ORCA 6.1 manual Section 5.21.3 benchmark for 13C NMR spectrum simulation (TMS at TPSS/pcSseg-3).",
        },
    ],
}


def normalize_level_tag(tag: str | None) -> str:
    """Normalize method or basis set string for strict scientific comparison."""
    if not tag:
        return ""
    # Strip spaces, hyphens, underscores and convert to upper
    return re.sub(r"[\s\-_/]+", "", str(tag)).upper()


def check_reference_compatibility(
    reference: NMRReference | None,
    calc_method: str | None = None,
    calc_basis: str | None = None,
) -> tuple[bool, str | None]:
    """Verify if reference level of theory strictly matches calculation provenance.

    Returns:
        (is_compatible, warning_message_if_mismatched)
    """
    if reference is None:
        return True, None

    # If reference is custom user supplied, allow with generic notice
    if reference.reference_source == "User Supplied" or reference.reference_method == "Custom":
        return True, None

    ref_m = normalize_level_tag(reference.reference_method)
    ref_b = normalize_level_tag(reference.reference_basis)
    c_m = normalize_level_tag(calc_method)
    c_b = normalize_level_tag(calc_basis)

    if not c_m or c_m in ("UNKNOWN", "NONE"):
        return True, None

    # Check method match
    method_match = True
    if ref_m and ref_m not in ("CUSTOM", "UNKNOWN"):
        # Handle B86 vs BP86 synonym
        norm_ref_m = "BP86" if ref_m in ("B86", "BP86") else ref_m
        norm_c_m = "BP86" if c_m in ("B86", "BP86") else c_m
        if norm_ref_m != norm_c_m and norm_ref_m not in norm_c_m:
            method_match = False

    # Check basis set match (e.g. def2-TZVP is strictly NOT TZVPP)
    basis_match = True
    if ref_b and ref_b not in ("CUSTOM", "UNKNOWN") and c_b and c_b not in ("UNKNOWN", "NONE"):
        # Critical: def2-TZVP != TZVPP
        if ref_b != c_b:
            basis_match = False

    if not method_match or not basis_match:
        ref_desc = f"{reference.reference_method}{'/' + reference.reference_basis if reference.reference_basis else ''}"
        calc_desc = f"{calc_method or 'Unknown'}{'/' + calc_basis if calc_basis else ''}"
        warning = (
            f"Level of theory mismatch: Reference standard is calibrated for {ref_desc}, "
            f"but current calculation was performed with {calc_desc}. "
            f"According to ORCA Manual Section 5.21.1, chemical shifts should only be computed "
            f"against references calculated with the identical functional and basis set."
        )
        return False, warning

    return True, None


@dataclass
class NMRAtomRecord:
    """Atom-level NMR calculated shielding record."""

    atom_index: int  # 0-indexed atomic index
    element: str  # "H", "C", "O", etc.
    isotope: str  # "1H", "13C", etc.
    isotropic_shielding: float  # sigma_iso in ppm
    anisotropy: float | None = None  # in ppm
    chemical_shift: float | None = None  # delta in ppm (if reference applied)
    assignment: str = ""  # e.g. "H0", "C1"
    coordinate: tuple[float, float, float] | None = None
    diamagnetic_iso: float | None = None
    paramagnetic_iso: float | None = None
    total_tensor: list[list[float]] | None = None

    def __post_init__(self) -> None:
        if not self.assignment:
            self.assignment = f"{self.element}{self.atom_index}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "atom_index": self.atom_index,
            "element": self.element,
            "isotope": self.isotope,
            "isotropic_shielding": self.isotropic_shielding,
            "anisotropy": self.anisotropy,
            "chemical_shift": self.chemical_shift,
            "assignment": self.assignment,
            "coordinate": list(self.coordinate) if self.coordinate else None,
            "diamagnetic_iso": self.diamagnetic_iso,
            "paramagnetic_iso": self.paramagnetic_iso,
            "total_tensor": self.total_tensor,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NMRAtomRecord:
        coord = data.get("coordinate")
        coord_tuple = tuple(coord) if coord and len(coord) == 3 else None
        idx_val = data.get("atom_index") if data.get("atom_index") is not None else data.get("atom_idx", 0)
        iso_val = data.get("isotropic_shielding") if data.get("isotropic_shielding") is not None else data.get("isotropic_ppm", 0.0)
        cs_val = data.get("chemical_shift") if data.get("chemical_shift") is not None else data.get("chemical_shift_ppm")
        return cls(
            atom_index=int(idx_val),
            element=str(data.get("element", "H")),
            isotope=str(data.get("isotope", "1H")),
            isotropic_shielding=float(iso_val),
            anisotropy=float(data["anisotropy"]) if data.get("anisotropy") is not None else None,
            chemical_shift=float(cs_val) if cs_val is not None else None,
            assignment=str(data.get("assignment", "")),
            coordinate=coord_tuple,
            diamagnetic_iso=float(data["diamagnetic_iso"]) if data.get("diamagnetic_iso") is not None else None,
            paramagnetic_iso=float(data["paramagnetic_iso"]) if data.get("paramagnetic_iso") is not None else None,
            total_tensor=data.get("total_tensor"),
        )


@dataclass
class NMRPeak:
    """Calculated NMR spectral stick peak."""

    peak_id: str
    nucleus: str  # "1H" or "13C"
    position_ppm: float  # chemical shift delta (if ref configured) or isotropic shielding sigma
    position_type: str  # "chemical_shift" or "isotropic_shielding"
    intensity: float = 1.0  # proportional to number of atoms in peak
    atom_indices: list[int] = field(default_factory=list)
    assignments: list[str] = field(default_factory=list)
    atoms: list[NMRAtomRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "peak_id": self.peak_id,
            "nucleus": self.nucleus,
            "position_ppm": self.position_ppm,
            "position_type": self.position_type,
            "intensity": self.intensity,
            "atom_indices": self.atom_indices,
            "assignments": self.assignments,
            "atoms": [a.to_dict() for a in self.atoms],
        }


@dataclass
class NMRSpectrum:
    """Calculated stick spectrum model for 1H or 13C."""

    nucleus: str  # "1H" or "13C"
    x_quantity: str  # "chemical_shift" or "isotropic_shielding"
    x_unit: str = "ppm"
    title: str = ""
    peaks: list[NMRPeak] = field(default_factory=list)
    atoms: list[NMRAtomRecord] = field(default_factory=list)
    reference: NMRReference | None = None
    is_reference_applied: bool = False
    warnings: list[str] = field(default_factory=list)
    min_ppm: float = 0.0
    max_ppm: float = 10.0
    method: str = "Unknown"
    basis_set: str = "Unknown"
    solvent: str = "None"
    orca_version: str = "Unknown"

    def __post_init__(self) -> None:
        if not self.title:
            q_name = "Chemical Shift δ" if self.is_reference_applied else "Isotropic Shielding σ"
            self.title = f"Calculated {self.nucleus} NMR ({q_name})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "nucleus": self.nucleus,
            "x_quantity": self.x_quantity,
            "x_unit": self.x_unit,
            "title": self.title,
            "peaks": [p.to_dict() for p in self.peaks],
            "atoms": [a.to_dict() for a in self.atoms],
            "reference": self.reference.to_dict() if self.reference else None,
            "is_reference_applied": self.is_reference_applied,
            "warnings": self.warnings,
            "min_ppm": self.min_ppm,
            "max_ppm": self.max_ppm,
            "method": self.method,
            "basis_set": self.basis_set,
            "solvent": self.solvent,
            "orca_version": self.orca_version,
        }


@dataclass
class NMRResult:
    """Complete container for parsed NMR calculation results."""

    atoms: list[NMRAtomRecord] = field(default_factory=list)
    h1_spectrum: NMRSpectrum | None = None
    c13_spectrum: NMRSpectrum | None = None
    has_h1: bool = False
    has_c13: bool = False
    is_complete: bool = True
    references: dict[str, NMRReference] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "atoms": [a.to_dict() for a in self.atoms],
            "h1_spectrum": self.h1_spectrum.to_dict() if self.h1_spectrum else None,
            "c13_spectrum": self.c13_spectrum.to_dict() if self.c13_spectrum else None,
            "has_h1": self.has_h1,
            "has_c13": self.has_c13,
            "is_complete": self.is_complete,
            "references": {k: v.to_dict() for k, v in self.references.items()},
            "warnings": self.warnings,
            "provenance": self.provenance,
        }


# ===========================================================================
# Parser
# ===========================================================================

class OrcaNMRParser:
    """Parser for ORCA NMR shielding outputs."""

    # Summary table header regex: matches real ORCA header "Nucleus Element Isotropic Anisotropy"
    # or optional banner "CHEMICAL SHIELDING"
    SUMMARY_HEADER = re.compile(
        r"(?:CHEMICAL\s+SHIELDINGS?|\bNucleus\s+Element\s+Isotropic\s+Anisotropy\b)",
        re.IGNORECASE,
    )
    SUMMARY_LINE = re.compile(
        r"^\s*(\d+)\s+([A-Za-z]{1,2})\s+([-+]?\d+\.\d+)\s+([-+]?\d+\.\d+)",
        re.IGNORECASE,
    )

    # Detailed nucleus header regex
    NUCLEUS_HEADER = re.compile(r"^\s*Nucleus\s+(\d+)\s*([A-Za-z]+)(?:\s*ist\s*=\s*(\d+))?\s*:", re.IGNORECASE)
    TOTAL_ISO_LINE = re.compile(
        r"^\s*Total\s+([-+]?\d*\.?\d+)\s+([-+]?\d*\.?\d+)\s+([-+]?\d*\.?\d+)\s+iso=\s*([-+]?\d*\.?\d+)",
        re.IGNORECASE,
    )
    SDSO_ISO_LINE = re.compile(
        r"^\s*sDSO\s+[-+]?\d*\.?\d+\s+[-+]?\d*\.?\d+\s+[-+]?\d*\.?\d+\s+iso=\s*([-+]?\d*\.?\d+)",
        re.IGNORECASE,
    )
    SPSO_ISO_LINE = re.compile(
        r"^\s*sPSO\s+[-+]?\d*\.?\d+\s+[-+]?\d*\.?\d+\s+[-+]?\d*\.?\d+\s+iso=\s*([-+]?\d*\.?\d+)",
        re.IGNORECASE,
    )

    # Coordinate block detection for text-only parse path
    COORD_HEADER = re.compile(r"\bCARTESIAN\s+COORDINATES\s*\(ANGSTROEM\)\b", re.IGNORECASE)
    COORD_LINE = re.compile(r"^\s*([A-Za-z]{1,2})\s+([-+]?\d+\.\d+)\s+([-+]?\d+\.\d+)\s+([-+]?\d+\.\d+)")

    def __init__(self) -> None:
        self.atom_records: dict[int, NMRAtomRecord] = {}
        self.warnings: list[str] = []

    def parse_text(
        self,
        text: str,
        coordinates: Sequence[tuple[float, float, float]] | None = None,
        elements: Sequence[str] | None = None,
        metadata: dict[str, Any] | None = None,
        expected_active_count: int | None = None,
    ) -> NMRResult:
        """Parse NMR output from a raw string or file lines."""
        lines = text.splitlines()
        
        # If coordinates/elements not passed, attempt to extract them from embedded CARTESIAN COORDINATES block
        if not coordinates or not elements:
            extracted_coords: list[tuple[float, float, float]] = []
            extracted_elems: list[str] = []
            in_coord_block = False
            for line in lines:
                s = line.strip()
                if self.COORD_HEADER.search(s):
                    in_coord_block = True
                    extracted_coords = []
                    extracted_elems = []
                    continue
                if in_coord_block:
                    if re.match(r"^[-=\s]+$", s):
                        continue
                    m_c = self.COORD_LINE.match(s)
                    if m_c:
                        extracted_elems.append(m_c.group(1).capitalize())
                        extracted_coords.append((float(m_c.group(2)), float(m_c.group(3)), float(m_c.group(4))))
                    else:
                        in_coord_block = False
            if extracted_coords and not coordinates:
                coordinates = extracted_coords
            if extracted_elems and not elements:
                elements = extracted_elems

        return self.parse_lines(
            lines,
            coordinates=coordinates,
            elements=elements,
            metadata=metadata,
            expected_active_count=expected_active_count,
        )

    def parse_lines(
        self,
        lines: Sequence[str],
        coordinates: Sequence[tuple[float, float, float]] | None = None,
        elements: Sequence[str] | None = None,
        metadata: dict[str, Any] | None = None,
        expected_active_count: int | None = None,
    ) -> NMRResult:
        """Parse NMR output from lines."""
        self.atom_records.clear()
        self.warnings.clear()

        meta = dict(metadata or {})
        coords_list = list(coordinates or [])
        elems_list = [e.upper() for e in (elements or [])]

        in_summary_table = False
        current_nucleus_idx: int | None = None
        current_nucleus_elem: str | None = None
        current_tensor_rows: list[list[float]] = []
        in_total_tensor = False

        for line_num, line in enumerate(lines, 1):
            sline = line.strip()
            if not sline:
                continue

            # 1. Detect Summary Table
            if self.SUMMARY_HEADER.search(sline):
                in_summary_table = True
                continue

            if in_summary_table:
                # Table divider lines or column header repetitions
                if re.match(r"^[-=\s]+$", sline) or re.search(r"Nucleus\s+Element\s+Isotropic", sline, re.I):
                    continue

                m_sum = self.SUMMARY_LINE.match(sline)
                if m_sum:
                    idx = int(m_sum.group(1))
                    elem = m_sum.group(2).capitalize()
                    iso = float(m_sum.group(3))
                    aniso = float(m_sum.group(4))

                    coord = coords_list[idx] if idx < len(coords_list) else None
                    isotope = "1H" if elem == "H" else ("13C" if elem == "C" else elem)

                    if idx in self.atom_records:
                        rec = self.atom_records[idx]
                        rec.isotropic_shielding = iso
                        rec.anisotropy = aniso
                        rec.element = elem
                        rec.isotope = isotope
                        if coord and not rec.coordinate:
                            rec.coordinate = coord
                    else:
                        self.atom_records[idx] = NMRAtomRecord(
                            atom_index=idx,
                            element=elem,
                            isotope=isotope,
                            isotropic_shielding=iso,
                            anisotropy=aniso,
                            assignment=f"{elem}{idx}",
                            coordinate=coord,
                        )
                    continue
                else:
                    # End of summary table
                    in_summary_table = False

            # 2. Authoritative Detailed Per-Nucleus Blocks
            m_nuc = self.NUCLEUS_HEADER.search(sline)
            if m_nuc:
                current_nucleus_idx = int(m_nuc.group(1))
                current_nucleus_elem = m_nuc.group(2).capitalize()
                custom_ist = m_nuc.group(3)
                current_tensor_rows = []
                in_total_tensor = False

                coord = coords_list[current_nucleus_idx] if current_nucleus_idx < len(coords_list) else None
                
                if custom_ist:
                    isotope = f"{custom_ist}{current_nucleus_elem}"
                else:
                    isotope = "1H" if current_nucleus_elem == "H" else ("13C" if current_nucleus_elem == "C" else current_nucleus_elem)

                if current_nucleus_idx not in self.atom_records:
                    self.atom_records[current_nucleus_idx] = NMRAtomRecord(
                        atom_index=current_nucleus_idx,
                        element=current_nucleus_elem,
                        isotope=isotope,
                        isotropic_shielding=0.0,
                        assignment=f"{current_nucleus_elem}{current_nucleus_idx}",
                        coordinate=coord,
                    )
                continue

            if current_nucleus_idx is not None:
                rec = self.atom_records[current_nucleus_idx]

                if "Total shielding tensor (ppm):" in sline:
                    in_total_tensor = True
                    current_tensor_rows = []
                    continue

                if in_total_tensor:
                    row_parts = sline.split()
                    if len(row_parts) == 3:
                        try:
                            current_tensor_rows.append([float(x) for x in row_parts])
                            if len(current_tensor_rows) == 3:
                                rec.total_tensor = current_tensor_rows
                                in_total_tensor = False
                        except ValueError:
                            in_total_tensor = False
                    else:
                        in_total_tensor = False

                m_dso = self.SDSO_ISO_LINE.search(sline)
                if m_dso:
                    rec.diamagnetic_iso = float(m_dso.group(1))
                    continue

                m_pso = self.SPSO_ISO_LINE.search(sline)
                if m_pso:
                    rec.paramagnetic_iso = float(m_pso.group(1))
                    continue

                m_tot = self.TOTAL_ISO_LINE.search(sline)
                if m_tot:
                    s1 = float(m_tot.group(1))
                    s2 = float(m_tot.group(2))
                    s3 = float(m_tot.group(3))
                    iso = float(m_tot.group(4))
                    rec.isotropic_shielding = iso
                    # Anisotropy definition from diagonalized tensor eigenvalues: sigma_3 - (sigma_1 + sigma_2)/2
                    if rec.anisotropy is None:
                        rec.anisotropy = round(s3 - (s1 + s2) / 2.0, 4)
                    continue

        sorted_atoms = [self.atom_records[k] for k in sorted(self.atom_records.keys())]

        has_h1 = any(a.element == "H" for a in sorted_atoms)
        has_c13 = any(a.element == "C" for a in sorted_atoms)

        is_complete = True
        if not sorted_atoms:
            self.warnings.append("No NMR shielding data found in output.")
            is_complete = False
        elif expected_active_count is not None and expected_active_count > 0:
            if len(sorted_atoms) < expected_active_count:
                is_complete = False
                self.warnings.append(
                    f"NMR_INCOMPLETE: Parsed {len(sorted_atoms)} atom shieldings, "
                    f"fewer than the {expected_active_count} expected NMR-active atoms."
                )
        elif elems_list:
            expected_h_c = sum(1 for e in elems_list if e in ("H", "C"))
            if expected_h_c > 0 and len(sorted_atoms) < expected_h_c:
                is_complete = False
                self.warnings.append(
                    f"NMR_INCOMPLETE: Incomplete NMR output; parsed {len(sorted_atoms)} atom shieldings "
                    f"for {expected_h_c} total 1H/13C atoms in geometry."
                )

        # Build initial spectra without reference (displaying isotropic shielding)
        h1_spectrum = build_nmr_spectrum(
            sorted_atoms,
            nucleus=NMRNucleus.H1,
            reference=None,
            method=meta.get("method", "Unknown"),
            basis_set=meta.get("basis_set", "Unknown"),
            solvent=meta.get("solvent", "None"),
            orca_version=meta.get("orca_version", "Unknown"),
        )

        c13_spectrum = build_nmr_spectrum(
            sorted_atoms,
            nucleus=NMRNucleus.C13,
            reference=None,
            method=meta.get("method", "Unknown"),
            basis_set=meta.get("basis_set", "Unknown"),
            solvent=meta.get("solvent", "None"),
            orca_version=meta.get("orca_version", "Unknown"),
        )

        return NMRResult(
            atoms=sorted_atoms,
            h1_spectrum=h1_spectrum,
            c13_spectrum=c13_spectrum,
            has_h1=has_h1,
            has_c13=has_c13,
            is_complete=is_complete,
            references={},
            warnings=list(self.warnings),
            provenance=meta,
        )


# ===========================================================================
# Spectrum Builder & Reference Transformer
# ===========================================================================

def build_nmr_spectrum(
    atoms: Sequence[NMRAtomRecord],
    nucleus: NMRNucleus | str = NMRNucleus.H1,
    reference: NMRReference | None = None,
    group_close_peaks: bool = False,
    group_tolerance_ppm: float = 0.02,
    method: str = "Unknown",
    basis_set: str = "Unknown",
    solvent: str = "None",
    orca_version: str = "Unknown",
) -> NMRSpectrum:
    """Construct an NMRSpectrum object for 1H or 13C.

    Args:
        atoms: Full list of atom records.
        nucleus: Target nucleus ('1H' or '13C').
        reference: Reference model for chemical shift calculation (None = shielding).
        group_close_peaks: Whether to visually coalesce peaks within tolerance.
        group_tolerance_ppm: Coalescence tolerance in ppm.
        method: Provenance method name.
        basis_set: Provenance basis set.
        solvent: Solvent model.
        orca_version: ORCA version.

    Returns:
        Structured NMRSpectrum.
    """
    nuc_enum = NMRNucleus(nucleus) if isinstance(nucleus, str) else nucleus
    elem_symbol = nuc_enum.element_symbol

    # Filter matching atoms
    target_atoms = [a for a in atoms if a.element.upper() == elem_symbol]

    warnings: list[str] = []
    is_ref_applied = reference is not None

    if is_ref_applied and reference:
        # Check method and basis set compatibility
        is_compat, compat_warn = check_reference_compatibility(
            reference,
            calc_method=method,
            calc_basis=basis_set,
        )
        if not is_compat and compat_warn:
            warnings.append(compat_warn)

        x_quantity = "chemical_shift"
        # Calculate chemical shifts for individual atoms
        updated_atoms: list[NMRAtomRecord] = []
        for a in target_atoms:
            delta = reference.compute_chemical_shift(a.isotropic_shielding)
            # Create copy with chemical shift
            atom_copy = NMRAtomRecord(
                atom_index=a.atom_index,
                element=a.element,
                isotope=a.isotope,
                isotropic_shielding=a.isotropic_shielding,
                anisotropy=a.anisotropy,
                chemical_shift=delta,
                assignment=a.assignment,
                coordinate=a.coordinate,
                diamagnetic_iso=a.diamagnetic_iso,
                paramagnetic_iso=a.paramagnetic_iso,
                total_tensor=a.total_tensor,
            )
            updated_atoms.append(atom_copy)
        target_atoms = updated_atoms
    else:
        x_quantity = "isotropic_shielding"
        warnings.append(
            "Reference shielding not configured; displaying absolute isotropic shielding (ppm). "
            "Select a compatible reference standard or enter a custom reference shielding (σ_ref) "
            "to calculate chemical shifts (δ = σ_ref - σ_sample)."
        )
        # Ensure chemical_shift is None
        updated_atoms = []
        for a in target_atoms:
            atom_copy = NMRAtomRecord(
                atom_index=a.atom_index,
                element=a.element,
                isotope=a.isotope,
                isotropic_shielding=a.isotropic_shielding,
                anisotropy=a.anisotropy,
                chemical_shift=None,
                assignment=a.assignment,
                coordinate=a.coordinate,
                diamagnetic_iso=a.diamagnetic_iso,
                paramagnetic_iso=a.paramagnetic_iso,
                total_tensor=a.total_tensor,
            )
            updated_atoms.append(atom_copy)
        target_atoms = updated_atoms

    # Generate peaks
    peaks: list[NMRPeak] = []
    if target_atoms:
        if group_close_peaks and len(target_atoms) > 1:
            # Sort atoms by position
            sorted_atoms = sorted(
                target_atoms,
                key=lambda x: (x.chemical_shift if is_ref_applied else x.isotropic_shielding) or 0.0,
            )
            current_group: list[NMRAtomRecord] = [sorted_atoms[0]]

            for atom in sorted_atoms[1:]:
                prev_pos = (current_group[-1].chemical_shift if is_ref_applied else current_group[-1].isotropic_shielding) or 0.0
                curr_pos = (atom.chemical_shift if is_ref_applied else atom.isotropic_shielding) or 0.0

                if abs(curr_pos - prev_pos) <= group_tolerance_ppm:
                    current_group.append(atom)
                else:
                    # Flush current group
                    avg_pos = sum(
                        (a.chemical_shift if is_ref_applied else a.isotropic_shielding) or 0.0
                        for a in current_group
                    ) / len(current_group)
                    p_id = f"peak_{len(peaks) + 1}"
                    peaks.append(
                        NMRPeak(
                            peak_id=p_id,
                            nucleus=nuc_enum.value,
                            position_ppm=round(avg_pos, 4),
                            position_type=x_quantity,
                            intensity=float(len(current_group)),
                            atom_indices=[a.atom_index for a in current_group],
                            assignments=[a.assignment for a in current_group],
                            atoms=list(current_group),
                        )
                    )
                    current_group = [atom]

            if current_group:
                avg_pos = sum(
                    (a.chemical_shift if is_ref_applied else a.isotropic_shielding) or 0.0
                    for a in current_group
                ) / len(current_group)
                p_id = f"peak_{len(peaks) + 1}"
                peaks.append(
                    NMRPeak(
                        peak_id=p_id,
                        nucleus=nuc_enum.value,
                        position_ppm=round(avg_pos, 4),
                        position_type=x_quantity,
                        intensity=float(len(current_group)),
                        atom_indices=[a.atom_index for a in current_group],
                        assignments=[a.assignment for a in current_group],
                        atoms=list(current_group),
                    )
                )
        else:
            # Individual 1:1 atom-to-peak sticks
            for i, atom in enumerate(target_atoms, 1):
                pos = (atom.chemical_shift if is_ref_applied else atom.isotropic_shielding) or 0.0
                peaks.append(
                    NMRPeak(
                        peak_id=f"peak_{i}",
                        nucleus=nuc_enum.value,
                        position_ppm=round(pos, 4),
                        position_type=x_quantity,
                        intensity=1.0,
                        atom_indices=[atom.atom_index],
                        assignments=[atom.assignment],
                        atoms=[atom],
                    )
                )

    # Determine default axis bounds
    if peaks:
        positions = [p.position_ppm for p in peaks]
        p_min = min(positions)
        p_max = max(positions)
        margin = max(1.0, (p_max - p_min) * 0.1)
        min_ppm = max(0.0, math.floor(p_min - margin)) if is_ref_applied and p_min >= 0 else math.floor(p_min - margin)
        max_ppm = math.ceil(p_max + margin)
    else:
        if nuc_enum == NMRNucleus.H1:
            min_ppm, max_ppm = (0.0, 12.0) if is_ref_applied else (0.0, 60.0)
        else:
            min_ppm, max_ppm = (0.0, 220.0) if is_ref_applied else (0.0, 250.0)

    title = f"Calculated {nuc_enum.value} NMR ({'Chemical Shift δ' if is_ref_applied else 'Isotropic Shielding σ'})"

    return NMRSpectrum(
        nucleus=nuc_enum.value,
        x_quantity=x_quantity,
        x_unit="ppm",
        title=title,
        peaks=peaks,
        atoms=target_atoms,
        reference=reference,
        is_reference_applied=is_ref_applied,
        warnings=warnings,
        min_ppm=float(min_ppm),
        max_ppm=float(max_ppm),
        method=method,
        basis_set=basis_set,
        solvent=solvent,
        orca_version=orca_version,
    )


# ===========================================================================
# Export Utilities
# ===========================================================================

def export_nmr_csv(spectrum_or_result: NMRSpectrum | NMRResult | Sequence[NMRAtomRecord]) -> str:
    """Generate CSV string of NMR atom records and chemical shifts."""
    if isinstance(spectrum_or_result, NMRResult):
        atoms = spectrum_or_result.atoms
    elif isinstance(spectrum_or_result, NMRSpectrum):
        atoms = spectrum_or_result.atoms
    else:
        atoms = list(spectrum_or_result)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "atom_index",
        "element",
        "isotope",
        "assignment",
        "isotropic_shielding_ppm",
        "chemical_shift_ppm",
        "anisotropy_ppm",
        "coord_x",
        "coord_y",
        "coord_z",
    ])

    for a in atoms:
        cx = a.coordinate[0] if a.coordinate else ""
        cy = a.coordinate[1] if a.coordinate else ""
        cz = a.coordinate[2] if a.coordinate else ""
        writer.writerow([
            a.atom_index,
            a.element,
            a.isotope,
            a.assignment,
            f"{a.isotropic_shielding:.4f}",
            f"{a.chemical_shift:.4f}" if a.chemical_shift is not None else "",
            f"{a.anisotropy:.4f}" if a.anisotropy is not None else "",
            cx,
            cy,
            cz,
        ])

    return output.getvalue()


def export_nmr_json(result_or_spectrum: NMRResult | NMRSpectrum) -> str:
    """Serialize NMR result or spectrum to JSON."""
    return json.dumps(result_or_spectrum.to_dict(), indent=2)
