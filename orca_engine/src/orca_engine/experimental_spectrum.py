# -*- coding: utf-8 -*-
"""
Interactive Multi-Spectrum Analysis: Experimental Spectrum Parser & Separation Layer.

Core Scientific Principle:
==========================
Experimental spectrum is an IMMUTABLE REFERENCE LAYER.
Raw experimental measurements are parsed, validated, hashed, and preserved intact.
Theoretical line broadening (Gaussian), energy/wavelength shifts, and scaling
apply EXCLUSIVELY to theoretical calculations and never mutate the experimental data.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Sequence


# Physical Constants
PLANCK_C_EV_NM = 1239.8419843320026  # hc in eV*nm (1239.841984... eV*nm)
CM_TO_EV = 0.00012398419843320026    # 1 cm^-1 in eV


# Column Header Regular Expressions
WAVELENGTH_PATTERNS = [
    re.compile(r"^\s*wavenumber(\s*\(cm(?:\*\*|-|\^)-?1\))?\s*$", re.IGNORECASE),
    re.compile(r"^\s*cm(?:\*\*|-|\^)-?1\s*$", re.IGNORECASE),
    re.compile(r"^\s*freq(uency)?(\s*\(cm(?:\*\*|-|\^)-?1\))?\s*$", re.IGNORECASE),
    re.compile(r"^\s*wavelength(\s*\(nm\))?\s*$", re.IGNORECASE),
    re.compile(r"^\s*w\.?l\.?(\s*\(nm\))?\s*$", re.IGNORECASE),
    re.compile(r"^\s*lambda(\s*\(nm\))?\s*$", re.IGNORECASE),
    re.compile(r"^\s*λ(\s*\(nm\))?\s*$", re.IGNORECASE),
    re.compile(r"^\s*nm\s*$", re.IGNORECASE),
    re.compile(r"^\s*wavelen(gth)?\s*$", re.IGNORECASE),
    re.compile(r"^\s*x(\s*\(nm\))?\s*$", re.IGNORECASE),
    re.compile(r"wavenumber|cm-1|cm\*\*-1", re.IGNORECASE),
    re.compile(r"wavelength", re.IGNORECASE),
    re.compile(r"lambda|λ", re.IGNORECASE),
]

ABSORBANCE_PATTERNS = [
    re.compile(r"^\s*transmittance(\s*\(%?\))?\s*$", re.IGNORECASE),
    re.compile(r"^\s*%?\s*t\s*$", re.IGNORECASE),
    re.compile(r"^\s*transmission(\s*\(%?\))?\s*$", re.IGNORECASE),
    re.compile(r"^\s*absorbance(\s*\(a\.?u\.?\))?\s*$", re.IGNORECASE),
    re.compile(r"^\s*abs(\s*\(a\.?u\.?\))?\s*$", re.IGNORECASE),
    re.compile(r"^\s*absorption\s*$", re.IGNORECASE),
    re.compile(r"^\s*optical\s*density\s*$", re.IGNORECASE),
    re.compile(r"^\s*od\s*$", re.IGNORECASE),
    re.compile(r"^\s*extinction\s*$", re.IGNORECASE),
    re.compile(r"^\s*intensity(\s*\(a\.?u\.?\))?\s*$", re.IGNORECASE),
    re.compile(r"^\s*a\s*$", re.IGNORECASE),
    re.compile(r"^\s*au\s*$", re.IGNORECASE),
    re.compile(r"^\s*y\s*$", re.IGNORECASE),
    re.compile(r"transmittance|transmission", re.IGNORECASE),
    re.compile(r"absorb", re.IGNORECASE),
]



class ExperimentalSpectrumError(ValueError):
    """Raised when an experimental spectrum file cannot be parsed or validated."""


@dataclass(frozen=True)
class ExperimentalSpectrum:
    """Immutable representation of an experimental UV-Vis / absorption spectrum.

    Fields:
        source_type: Always 'experimental' to identify reference layer.
        file_name: Name of the uploaded file.
        sheet_name: Sheet name if loaded from Excel.
        column_wavelength: Detected or user-selected wavelength column header.
        column_absorbance: Detected or user-selected absorbance column header.
        units_wavelength: Wavelength unit ('nm').
        units_y: Intensity/Absorbance unit ('AU').
        y_quantity: Physical quantity ('absorbance').
        raw_data: Immutable sequence of (wavelength_nm, absorbance) tuples.
        raw_hash: Cryptographic SHA-256 hash ensuring raw data immutability.
    """
    file_name: str
    sheet_name: str | None
    column_wavelength: str
    column_absorbance: str
    raw_data: tuple[tuple[float, float], ...]
    raw_hash: str
    units_wavelength: str = "nm"
    units_y: str = "AU"
    y_quantity: str = "absorbance"
    source_type: str = "experimental"
    detected_peaks: tuple[dict[str, float], ...] = field(default_factory=tuple)

    @property
    def points_count(self) -> int:
        return len(self.raw_data)

    @property
    def min_wavelength(self) -> float:
        return min(p[0] for p in self.raw_data) if self.raw_data else 0.0

    @property
    def max_wavelength(self) -> float:
        return max(p[0] for p in self.raw_data) if self.raw_data else 0.0

    @property
    def min_absorbance(self) -> float:
        return min(p[1] for p in self.raw_data) if self.raw_data else 0.0

    @property
    def max_absorbance(self) -> float:
        return max(p[1] for p in self.raw_data) if self.raw_data else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "file_name": self.file_name,
            "sheet_name": self.sheet_name,
            "column_wavelength": self.column_wavelength,
            "column_absorbance": self.column_absorbance,
            "units_wavelength": self.units_wavelength,
            "units_y": self.units_y,
            "y_quantity": self.y_quantity,
            "points_count": self.points_count,
            "min_wavelength": round(self.min_wavelength, 2),
            "max_wavelength": round(self.max_wavelength, 2),
            "min_absorbance": round(self.min_absorbance, 5),
            "max_absorbance": round(self.max_absorbance, 5),
            "raw_hash": self.raw_hash,
            "points": [{"wavelength_nm": p[0], "absorbance": p[1]} for p in self.raw_data],
            "detected_peaks": list(self.detected_peaks),
        }


def compute_raw_data_hash(data: Sequence[tuple[float, float]]) -> str:
    """Compute deterministic SHA-256 hash for raw experimental spectrum points."""
    h = hashlib.sha256()
    for wl, abs_val in data:
        h.update(f"{wl:.6f}:{abs_val:.8f};".encode("utf-8"))
    return h.hexdigest()


def detect_peaks(data: Sequence[tuple[float, float]], threshold_fraction: float = 0.05, min_dist_nm: float = 8.0) -> list[dict[str, float]]:
    """Detect local absorption maxima for experimental peak markers.

    This is an analytical helper for displaying reference markers on the
    experimental curve; it does NOT alter the raw measurement points.
    """
    if len(data) < 3:
        return []

    max_y = max(p[1] for p in data)
    min_y = min(p[1] for p in data)
    y_range = max_y - min_y
    if y_range <= 1e-9:
        return []

    threshold = min_y + threshold_fraction * y_range
    peaks: list[dict[str, float]] = []

    for i in range(1, len(data) - 1):
        wl_prev, y_prev = data[i - 1]
        wl_curr, y_curr = data[i]
        wl_next, y_next = data[i + 1]

        if y_curr > threshold and y_curr >= y_prev and y_curr >= y_next and (y_curr > y_prev or y_curr > y_next):
            # Check minimum distance to last accepted peak
            if not peaks or (wl_curr - peaks[-1]["wavelength_nm"]) >= min_dist_nm:
                peaks.append({"wavelength_nm": round(wl_curr, 2), "absorbance": round(y_curr, 5)})
            elif peaks and y_curr > peaks[-1]["absorbance"]:
                # Replace with higher peak within the distance window
                peaks[-1] = {"wavelength_nm": round(wl_curr, 2), "absorbance": round(y_curr, 5)}

    return peaks


def _match_column_name(header: str, patterns: list[re.Pattern]) -> bool:
    clean = header.strip()
    return any(pat.search(clean) is not None for pat in patterns)


def _clean_and_sort_points(points: Sequence[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    """Sort ascending by wavelength and resolve duplicate wavelength points by averaging."""
    if not points:
        return ()

    by_wl: dict[float, list[float]] = {}
    for wl, y in points:
        wl_rounded = round(wl, 4)
        by_wl.setdefault(wl_rounded, []).append(y)

    sorted_unique = []
    for wl in sorted(by_wl.keys()):
        avg_y = sum(by_wl[wl]) / len(by_wl[wl])
        sorted_unique.append((wl, round(avg_y, 6)))

    return tuple(sorted_unique)


def parse_experimental_text(
    content: str,
    file_name: str = "spectrum.txt",
    wavelength_col: str | int | None = None,
    absorbance_col: str | int | None = None,
    delimiter: str | None = None,
) -> ExperimentalSpectrum:
    """Parse experimental UV-Vis spectrum from text, CSV, TSV, or whitespace-delimited data.

    Handles:
        - Arbitrary whitespace, tabs, commas, semicolons.
        - Header rows with descriptive labels (e.g. 'Wavelength (nm)', 'Absorbance').
        - Comment lines starting with #, !, %, //, ;.
        - Headerless two-column raw data.
    """
    if not content or not content.strip():
        raise ExperimentalSpectrumError("The uploaded file or text content is empty.")

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        raise ExperimentalSpectrumError("No non-empty lines found in experimental spectrum file.")

    # Filter out initial comment lines
    data_lines = []
    for line in lines:
        if line.startswith(("#", "!", "%", "//", ";")):
            continue
        data_lines.append(line)

    if not data_lines:
        raise ExperimentalSpectrumError("File contains only comment lines without numeric data.")

    # Detect delimiter if not provided
    sample = "\n".join(data_lines[:15])
    if delimiter is None:
        if "\t" in sample:
            delimiter = "\t"
        elif "," in sample:
            delimiter = ","
        elif ";" in sample:
            delimiter = ";"
        else:
            delimiter = None  # whitespace split

    # Parse rows
    rows: list[list[str]] = []
    for line in data_lines:
        if delimiter:
            parts = [p.strip() for p in line.split(delimiter) if p.strip()]
        else:
            parts = [p.strip() for p in line.split() if p.strip()]
        if parts:
            rows.append(parts)

    if not rows:
        raise ExperimentalSpectrumError("Failed to extract data rows from experimental file.")

    # Determine header vs data
    header_idx = -1
    col_w_idx: int | None = None
    col_a_idx: int | None = None
    col_w_name = "Wavelength"
    col_a_name = "Absorbance"

    # Check if first row contains column headers
    first_row = rows[0]
    has_text_headers = any(not _is_number(cell) for cell in first_row)

    if has_text_headers:
        header_idx = 0
        headers = first_row
        for idx, h in enumerate(headers):
            if col_w_idx is None and _match_column_name(h, WAVELENGTH_PATTERNS):
                col_w_idx = idx
                col_w_name = h
            elif col_a_idx is None and _match_column_name(h, ABSORBANCE_PATTERNS):
                col_a_idx = idx
                col_a_name = h

    # Override with user-selected columns if provided
    if wavelength_col is not None:
        if isinstance(wavelength_col, int):
            col_w_idx = wavelength_col
        elif isinstance(wavelength_col, str) and has_text_headers and wavelength_col in first_row:
            col_w_idx = first_row.index(wavelength_col)
            col_w_name = wavelength_col

    if absorbance_col is not None:
        if isinstance(absorbance_col, int):
            col_a_idx = absorbance_col
        elif isinstance(absorbance_col, str) and has_text_headers and absorbance_col in first_row:
            col_a_idx = first_row.index(absorbance_col)
            col_a_name = absorbance_col

    # Fallback to column 0 and column 1 if not identified
    if col_w_idx is None:
        col_w_idx = 0
    if col_a_idx is None:
        col_a_idx = 1 if len(rows[0]) > 1 else 0

    start_row = 1 if has_text_headers and header_idx == 0 else 0

    # Ensure indices do not exceed actual data row dimensions
    if start_row < len(rows):
        sample_row_len = len(rows[start_row])
        if col_w_idx >= sample_row_len:
            col_w_idx = 0
        if col_a_idx >= sample_row_len:
            col_a_idx = sample_row_len - 1 if sample_row_len > 1 else 0

    raw_points: list[tuple[float, float]] = []


    for r_idx in range(start_row, len(rows)):
        row = rows[r_idx]
        if len(row) <= max(col_w_idx, col_a_idx):
            continue
        try:
            val_w = float(row[col_w_idx].replace(",", "."))
            val_a = float(row[col_a_idx].replace(",", "."))
            if val_w > 0:  # Physical wavelengths are positive
                raw_points.append((val_w, val_a))
        except (ValueError, TypeError):
            continue  # Skip unparseable lines or subheaders

    if len(raw_points) < 2:
        raise ExperimentalSpectrumError(
            f"Experimental file '{file_name}' does not contain at least 2 valid numeric (Wavelength, Absorbance) data points."
        )

    cleaned_points = _clean_and_sort_points(raw_points)
    raw_hash = compute_raw_data_hash(cleaned_points)
    peaks = tuple(detect_peaks(cleaned_points))

    return ExperimentalSpectrum(
        file_name=file_name,
        sheet_name=None,
        column_wavelength=col_w_name,
        column_absorbance=col_a_name,
        raw_data=cleaned_points,
        raw_hash=raw_hash,
        detected_peaks=peaks,
    )


def _resolve_excel_column(col_spec: Any, first_row: list[str]) -> tuple[int | None, str | None]:
    """Resolve user-selected Excel column by name, trimmed name, index string, or letter."""
    if col_spec is None:
        return None, None
    if isinstance(col_spec, int):
        if 0 <= col_spec < len(first_row):
            return col_spec, first_row[col_spec] or f"Column {col_spec + 1}"
        return None, None

    spec_str = str(col_spec).strip()
    if not spec_str or spec_str.lower() in ("auto", "none", "auto-detect wavelength", "auto-detect absorbance"):
        return None, None

    if spec_str in first_row:
        return first_row.index(spec_str), spec_str

    for idx, h in enumerate(first_row):
        if h.strip().lower() == spec_str.lower():
            return idx, h

    for idx, h in enumerate(first_row):
        if h and (spec_str.lower() in h.lower() or h.lower() in spec_str.lower()):
            return idx, h

    if spec_str.isdigit():
        idx = int(spec_str)
        if 0 <= idx < len(first_row):
            return idx, first_row[idx] or f"Column {idx + 1}"

    if len(spec_str) == 1 and spec_str.upper() in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        idx = ord(spec_str.upper()) - ord('A')
        if 0 <= idx < len(first_row):
            return idx, first_row[idx] or f"Column {spec_str.upper()}"

    return None, None


def parse_experimental_excel(
    file_bytes: bytes,
    file_name: str = "spectrum.xlsx",
    sheet_name: str | None = None,
    wavelength_col: str | int | None = None,
    absorbance_col: str | int | None = None,
) -> ExperimentalSpectrum:
    """Parse experimental UV-Vis spectrum from Excel workbook (.xlsx).

    Supports:
        1. Single sheet with 2 columns (e.g. Wavelength & Absorbance).
        2. Multi-sheet workbook with explicit sheet selection.
        3. Two-sheet layout where Sheet 1 contains Wavelengths and Sheet 2 contains Absorbance values.
    """
    try:
        import openpyxl
    except ImportError as exc:
        raise ExperimentalSpectrumError("openpyxl library is required to parse Excel experimental files.") from exc

    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    except Exception as exc:
        raise ExperimentalSpectrumError(f"Failed to open Excel workbook '{file_name}': {exc}") from exc

    available_sheets = wb.sheetnames
    if not available_sheets:
        raise ExperimentalSpectrumError("Excel workbook contains no sheets.")

    # Check for 2-sheet pattern: Sheet 1 = Wavelength, Sheet 2 = Absorbance
    if sheet_name is None and len(available_sheets) == 2:
        s1_name, s2_name = available_sheets[0], available_sheets[1]
        s1 = wb[s1_name]
        s2 = wb[s2_name]

        s1_vals = _extract_single_column_from_sheet(s1)
        s2_vals = _extract_single_column_from_sheet(s2)

        if len(s1_vals) >= 2 and len(s2_vals) >= 2:
            min_len = min(len(s1_vals), len(s2_vals))
            raw_points = [(s1_vals[i], s2_vals[i]) for i in range(min_len) if s1_vals[i] > 0]
            if len(raw_points) >= 2:
                cleaned = _clean_and_sort_points(raw_points)
                raw_hash = compute_raw_data_hash(cleaned)
                peaks = tuple(detect_peaks(cleaned))
                return ExperimentalSpectrum(
                    file_name=file_name,
                    sheet_name=f"{s1_name} + {s2_name}",
                    column_wavelength=f"Sheet 1 ({s1_name})",
                    column_absorbance=f"Sheet 2 ({s2_name})",
                    raw_data=cleaned,
                    raw_hash=raw_hash,
                    detected_peaks=peaks,
                )

    target_sheet_name = sheet_name if (sheet_name and sheet_name in available_sheets) else available_sheets[0]
    ws = wb[target_sheet_name]


    # Read all rows from target worksheet
    rows: list[list[Any]] = []
    for r in ws.iter_rows(values_only=True):
        if r and any(cell is not None and str(cell).strip() != "" for cell in r):
            rows.append(list(r))

    if not rows:
        raise ExperimentalSpectrumError(f"Sheet '{target_sheet_name}' in '{file_name}' contains no data.")

    # Find headers and identify columns
    first_row = [str(c).strip() if c is not None else "" for c in rows[0]]
    has_text_headers = any(not _is_number(c) and c != "" for c in first_row)

    col_w_idx: int | None = None
    col_a_idx: int | None = None
    col_w_name = "Wavelength"
    col_a_name = "Absorbance"

    if has_text_headers:
        for idx, h in enumerate(first_row):
            if col_w_idx is None and _match_column_name(h, WAVELENGTH_PATTERNS):
                col_w_idx = idx
                col_w_name = h
            elif col_a_idx is None and _match_column_name(h, ABSORBANCE_PATTERNS):
                col_a_idx = idx
                col_a_name = h

    # User overrides with robust matching
    over_w_idx, over_w_name = _resolve_excel_column(wavelength_col, first_row)
    if over_w_idx is not None:
        col_w_idx = over_w_idx
        col_w_name = over_w_name or f"Column {col_w_idx + 1}"

    over_a_idx, over_a_name = _resolve_excel_column(absorbance_col, first_row)
    if over_a_idx is not None:
        col_a_idx = over_a_idx
        col_a_name = over_a_name or f"Column {col_a_idx + 1}"

    # Fallback to column 0 and column 1
    if col_w_idx is None:
        col_w_idx = 0
    if col_a_idx is None:
        col_a_idx = 1 if len(first_row) > 1 else 0


    start_row = 1 if has_text_headers else 0
    raw_points: list[tuple[float, float]] = []

    for r_idx in range(start_row, len(rows)):
        row = rows[r_idx]
        if len(row) <= max(col_w_idx, col_a_idx):
            continue
        val_w_raw = row[col_w_idx]
        val_a_raw = row[col_a_idx]
        try:
            if val_w_raw is not None and val_a_raw is not None:
                val_w = float(str(val_w_raw).replace(",", "."))
                val_a = float(str(val_a_raw).replace(",", "."))
                if val_w > 0:
                    raw_points.append((val_w, val_a))
        except (ValueError, TypeError):
            continue

    if len(raw_points) < 2:
        raise ExperimentalSpectrumError(
            f"Sheet '{target_sheet_name}' in '{file_name}' does not contain at least 2 valid numeric data points."
        )

    cleaned = _clean_and_sort_points(raw_points)
    raw_hash = compute_raw_data_hash(cleaned)
    peaks = tuple(detect_peaks(cleaned))

    return ExperimentalSpectrum(
        file_name=file_name,
        sheet_name=target_sheet_name,
        column_wavelength=col_w_name,
        column_absorbance=col_a_name,
        raw_data=cleaned,
        raw_hash=raw_hash,
        detected_peaks=peaks,
    )


def _extract_single_column_from_sheet(sheet) -> list[float]:
    """Extract numeric values from a single-column or first-column sheet."""
    vals: list[float] = []
    for row in sheet.iter_rows(values_only=True):
        if not row:
            continue
        for cell in row:
            if cell is not None and str(cell).strip() != "":
                try:
                    num = float(str(cell).replace(",", "."))
                    vals.append(num)
                    break
                except (ValueError, TypeError):
                    continue
    return vals


def parse_experimental_multi_series_text(
    content: str,
    file_name: str = "spectrum.txt",
    wavelength_col: str | int | None = None,
    absorbance_cols: Sequence[str | int] | str | None = None,
    delimiter: str | None = None,
) -> list[ExperimentalSpectrum]:
    """Parse experimental multi-series spectrum from text, CSV, TSV.

    Supports 1 X column and multiple selectable Y columns.
    Preserves column names as legend labels.
    """
    if not content or not content.strip():
        raise ExperimentalSpectrumError("The uploaded file or text content is empty.")

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        raise ExperimentalSpectrumError("No non-empty lines found in experimental spectrum file.")

    data_lines = [l for l in lines if not l.startswith(("#", "!", "%", "//", ";"))]
    if not data_lines:
        raise ExperimentalSpectrumError("File contains only comment lines without numeric data.")

    sample = "\n".join(data_lines[:15])
    if delimiter is None:
        if "\t" in sample:
            delimiter = "\t"
        elif "," in sample:
            delimiter = ","
        elif ";" in sample:
            delimiter = ";"
        else:
            delimiter = None

    rows: list[list[str]] = []
    for line in data_lines:
        parts = [p.strip() for p in (line.split(delimiter) if delimiter else line.split()) if p.strip()]
        if parts:
            rows.append(parts)

    if not rows:
        raise ExperimentalSpectrumError("Failed to extract data rows from experimental file.")

    first_row = rows[0]
    has_text_headers = any(not _is_number(cell) for cell in first_row)
    headers = first_row if has_text_headers else [f"Column {i+1}" for i in range(len(first_row))]

    # Resolve X column
    col_w_idx: int | None = None
    col_w_name = "Wavelength"
    if has_text_headers:
        for idx, h in enumerate(headers):
            if col_w_idx is None and _match_column_name(h, WAVELENGTH_PATTERNS):
                col_w_idx = idx
                col_w_name = h
                break

    if wavelength_col is not None:
        if isinstance(wavelength_col, int) and 0 <= wavelength_col < len(headers):
            col_w_idx = wavelength_col
            col_w_name = headers[col_w_idx]
        elif isinstance(wavelength_col, str):
            w_str = wavelength_col.strip()
            if w_str in headers:
                col_w_idx = headers.index(w_str)
                col_w_name = w_str
            elif w_str.isdigit() and int(w_str) < len(headers):
                col_w_idx = int(w_str)
                col_w_name = headers[col_w_idx]

    if col_w_idx is None:
        col_w_idx = 0
        col_w_name = headers[0] if headers else "Wavelength"

    start_row = 1 if has_text_headers else 0

    # Resolve Y columns
    y_target_specs = []
    if absorbance_cols is not None:
        if isinstance(absorbance_cols, str):
            y_target_specs = [s.strip() for s in absorbance_cols.split(",") if s.strip() and s.strip().lower() != "auto"]
        elif isinstance(absorbance_cols, (list, tuple)):
            y_target_specs = [s for s in absorbance_cols if s is not None and str(s).strip().lower() != "auto"]

    col_y_list: list[tuple[int, str]] = []
    if y_target_specs:
        for spec in y_target_specs:
            resolved_idx = None
            resolved_name = str(spec)
            if isinstance(spec, int) and 0 <= spec < len(headers):
                resolved_idx = spec
                resolved_name = headers[spec]
            elif str(spec) in headers:
                resolved_idx = headers.index(str(spec))
                resolved_name = str(spec)
            elif str(spec).isdigit() and int(str(spec)) < len(headers):
                resolved_idx = int(str(spec))
                resolved_name = headers[resolved_idx]
            else:
                for idx, h in enumerate(headers):
                    if h.strip().lower() == str(spec).strip().lower():
                        resolved_idx = idx
                        resolved_name = h
                        break

            if resolved_idx is not None:
                if resolved_idx == col_w_idx:
                    raise ExperimentalSpectrumError(f"Cannot select column '{resolved_name}' as both X and Y axis.")
                if (resolved_idx, resolved_name) not in col_y_list:
                    col_y_list.append((resolved_idx, resolved_name))
    else:
        # Auto-detect all other numeric columns as Y series
        num_cols = len(rows[start_row]) if start_row < len(rows) else len(headers)
        for c_idx in range(num_cols):
            if c_idx == col_w_idx:
                continue
            num_valid = 0
            for r_idx in range(start_row, min(len(rows), start_row + 50)):
                if c_idx < len(rows[r_idx]) and _is_number(rows[r_idx][c_idx]):
                    num_valid += 1
            if num_valid >= 2:
                c_name = headers[c_idx] if c_idx < len(headers) else f"Column {c_idx+1}"
                col_y_list.append((c_idx, c_name))

    if not col_y_list:
        fallback_idx = 1 if (len(headers) > 1 and col_w_idx == 0) else 0
        if fallback_idx != col_w_idx:
            col_y_list.append((fallback_idx, headers[fallback_idx]))

    spectra: list[ExperimentalSpectrum] = []
    for y_idx, y_name in col_y_list:
        raw_points = []
        for r_idx in range(start_row, len(rows)):
            row = rows[r_idx]
            if len(row) <= max(col_w_idx, y_idx):
                continue
            try:
                val_w = float(row[col_w_idx].replace(",", "."))
                val_a = float(row[y_idx].replace(",", "."))
                if val_w > 0:
                    raw_points.append((val_w, val_a))
            except (ValueError, TypeError):
                continue
        if len(raw_points) >= 2:
            cleaned = _clean_and_sort_points(raw_points)
            raw_hash = compute_raw_data_hash(cleaned)
            peaks = tuple(detect_peaks(cleaned))
            spectra.append(ExperimentalSpectrum(
                file_name=file_name,
                sheet_name=None,
                column_wavelength=col_w_name,
                column_absorbance=y_name,
                raw_data=cleaned,
                raw_hash=raw_hash,
                detected_peaks=peaks,
            ))

    if not spectra:
        raise ExperimentalSpectrumError(f"Experimental file '{file_name}' does not contain at least 2 valid numeric data points.")

    return spectra


def parse_experimental_multi_series_excel(
    file_bytes: bytes,
    file_name: str = "spectrum.xlsx",
    sheet_name: str | None = None,
    wavelength_col: str | int | None = None,
    absorbance_cols: Sequence[str | int] | str | None = None,
) -> list[ExperimentalSpectrum]:
    """Parse experimental multi-series spectrum from Excel workbook (.xlsx)."""
    try:
        import openpyxl
    except ImportError as exc:
        raise ExperimentalSpectrumError("openpyxl library is required to parse Excel experimental files.") from exc

    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    except Exception as exc:
        raise ExperimentalSpectrumError(f"Failed to open Excel workbook '{file_name}': {exc}") from exc

    available_sheets = wb.sheetnames
    if not available_sheets:
        raise ExperimentalSpectrumError("Excel workbook contains no sheets.")

    target_sheet_name = sheet_name if (sheet_name and sheet_name in available_sheets) else available_sheets[0]
    ws = wb[target_sheet_name]

    rows: list[list[Any]] = []
    for r in ws.iter_rows(values_only=True):
        if r and any(cell is not None and str(cell).strip() != "" for cell in r):
            rows.append(list(r))

    if not rows:
        raise ExperimentalSpectrumError(f"Sheet '{target_sheet_name}' in '{file_name}' contains no data.")

    first_row = [str(c).strip() if c is not None else "" for c in rows[0]]
    has_text_headers = any(not _is_number(c) and c != "" for c in first_row)
    headers = first_row if has_text_headers else [f"Column {i+1}" for i in range(len(first_row))]

    # Resolve X column
    col_w_idx: int | None = None
    col_w_name = "Wavelength"
    if has_text_headers:
        for idx, h in enumerate(first_row):
            if col_w_idx is None and _match_column_name(h, WAVELENGTH_PATTERNS):
                col_w_idx = idx
                col_w_name = h

    over_w_idx, over_w_name = _resolve_excel_column(wavelength_col, first_row)
    if over_w_idx is not None:
        col_w_idx = over_w_idx
        col_w_name = over_w_name or f"Column {col_w_idx + 1}"

    if col_w_idx is None:
        col_w_idx = 0
        col_w_name = headers[0] if headers else "Wavelength"

    start_row = 1 if has_text_headers else 0

    # Resolve Y columns
    y_target_specs = []
    if absorbance_cols is not None:
        if isinstance(absorbance_cols, str):
            y_target_specs = [s.strip() for s in absorbance_cols.split(",") if s.strip() and s.strip().lower() != "auto"]
        elif isinstance(absorbance_cols, (list, tuple)):
            y_target_specs = [s for s in absorbance_cols if s is not None and str(s).strip().lower() != "auto"]

    col_y_list: list[tuple[int, str]] = []
    if y_target_specs:
        for spec in y_target_specs:
            idx, name = _resolve_excel_column(spec, first_row)
            if idx is not None:
                if idx == col_w_idx:
                    raise ExperimentalSpectrumError(f"Cannot select column '{name}' as both X and Y axis.")
                if (idx, name) not in col_y_list:
                    col_y_list.append((idx, name))
    else:
        # Auto-detect all other numeric columns
        num_cols = len(rows[start_row]) if start_row < len(rows) else len(first_row)
        for c_idx in range(num_cols):
            if c_idx == col_w_idx:
                continue
            num_valid = 0
            for r_idx in range(start_row, min(len(rows), start_row + 50)):
                if c_idx < len(rows[r_idx]) and _is_number(rows[r_idx][c_idx]):
                    num_valid += 1
            if num_valid >= 2:
                col_name = first_row[c_idx] if (has_text_headers and first_row[c_idx]) else f"Column {c_idx+1}"
                col_y_list.append((c_idx, col_name))

    if not col_y_list:
        fallback_idx = 1 if (len(first_row) > 1 and col_w_idx == 0) else 0
        if fallback_idx != col_w_idx:
            col_y_list.append((fallback_idx, headers[fallback_idx] if fallback_idx < len(headers) else f"Column {fallback_idx+1}"))

    spectra: list[ExperimentalSpectrum] = []
    for y_idx, y_name in col_y_list:
        raw_points = []
        for r_idx in range(start_row, len(rows)):
            row = rows[r_idx]
            if len(row) <= max(col_w_idx, y_idx):
                continue
            val_w_raw = row[col_w_idx]
            val_a_raw = row[y_idx]
            try:
                if val_w_raw is not None and val_a_raw is not None:
                    val_w = float(str(val_w_raw).replace(",", "."))
                    val_a = float(str(val_a_raw).replace(",", "."))
                    if val_w > 0:
                        raw_points.append((val_w, val_a))
            except (ValueError, TypeError):
                continue
        if len(raw_points) >= 2:
            cleaned = _clean_and_sort_points(raw_points)
            raw_hash = compute_raw_data_hash(cleaned)
            peaks = tuple(detect_peaks(cleaned))
            spectra.append(ExperimentalSpectrum(
                file_name=file_name,
                sheet_name=target_sheet_name,
                column_wavelength=col_w_name,
                column_absorbance=y_name,
                raw_data=cleaned,
                raw_hash=raw_hash,
                detected_peaks=peaks,
            ))

    if not spectra:
        raise ExperimentalSpectrumError(
            f"Sheet '{target_sheet_name}' in '{file_name}' does not contain at least 2 valid numeric data points."
        )

    return spectra


def _is_number(val: Any) -> bool:
    if val is None:
        return False
    try:
        float(str(val).replace(",", "."))
        return True
    except (ValueError, TypeError):
        return False


def inspect_experimental_file(file_bytes: bytes, file_name: str) -> dict[str, Any]:
    """Inspect structure, sheets, headers, and column options of an uploaded spectrum file."""
    ext = os.path.splitext(file_name.lower())[1]

    if ext in (".xlsx", ".xlsm", ".xltx"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
            sheets_info = []
            for sname in wb.sheetnames:
                ws = wb[sname]
                sample_rows = []
                for r in ws.iter_rows(values_only=True):
                    if r and any(cell is not None and str(cell).strip() != "" for cell in r):
                        sample_rows.append([str(c) if c is not None else "" for c in r[:10]])
                        if len(sample_rows) >= 5:
                            break
                headers = sample_rows[0] if sample_rows else []
                sheets_info.append({
                    "name": sname,
                    "headers": headers,
                    "sample_rows": sample_rows[:4],
                })
            return {
                "file_name": file_name,
                "file_type": "excel",
                "sheets": sheets_info,
                "default_sheet": wb.sheetnames[0] if wb.sheetnames else None,
            }
        except Exception as exc:
            return {
                "file_name": file_name,
                "file_type": "excel",
                "error": f"Failed to inspect Excel file: {exc}",
                "sheets": [],
            }
    else:
        # Text/CSV file
        try:
            text = file_bytes.decode("utf-8", errors="replace")
            lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith(("#", "!", "%", "//", ";"))]
            sample_rows = []
            for l in lines[:6]:
                sample_rows.append([p.strip() for p in re.split(r"[\t,;]| {2,}", l) if p.strip()])
            headers = sample_rows[0] if sample_rows else []
            return {
                "file_name": file_name,
                "file_type": "text",
                "headers": headers,
                "sample_rows": sample_rows[:5],
                "sheets": [],
            }
        except Exception as exc:
            return {
                "file_name": file_name,
                "file_type": "text",
                "error": f"Failed to inspect text file: {exc}",
                "sheets": [],
            }


def convolute_theoretical_spectrum(
    transitions: Sequence[dict[str, Any]],
    sigma_nm: float = 20.0,
    sigma_ev: float | None = None,
    shift_nm: float = 0.0,
    shift_ev: float | None = None,
    start_nm: float = 180.0,
    end_nm: float = 800.0,
    step_nm: float = 1.0,
) -> list[dict[str, float]]:
    """Convolute TD-DFT stick transitions into a Gaussian absorption spectrum.

    Gaussian Line Broadening Formula:
        ε(λ) = Σ_i [ f_i / (σ * √(2π)) ] * exp( -(λ - λ_eff)^2 / (2σ^2) )
        where λ_eff = λ_i + shift_nm

    Strict Scientific Guarantee:
        Applies ONLY to theoretical calculations. Never touches experimental data.
    """
    if not transitions:
        return []

    valid_transitions: list[tuple[float, float, float]] = []

    for t in transitions:
        wl = t.get("wavelength_nm")
        if not wl and t.get("energy_cm") and float(t["energy_cm"]) > 0:
            wl = 1.0e7 / float(t["energy_cm"])
        elif not wl and t.get("energy_ev") and float(t["energy_ev"]) > 0:
            wl = PLANCK_C_EV_NM / float(t["energy_ev"])

        fosc = t.get("oscillator_strength", 0.0)
        if wl and float(wl) > 0 and float(fosc) >= 0:
            eff_wl = float(wl) + shift_nm
            if shift_ev is not None and shift_ev != 0:
                e_ev = (PLANCK_C_EV_NM / float(wl)) + shift_ev
                if e_ev > 0:
                    eff_wl = PLANCK_C_EV_NM / e_ev

            # Determine effective sigma in nm
            eff_sigma = sigma_nm
            if sigma_ev is not None and sigma_ev > 0:
                eff_sigma = ((float(wl) ** 2) / PLANCK_C_EV_NM) * sigma_ev
                eff_sigma = max(eff_sigma, 1.0)

            valid_transitions.append((eff_wl, float(fosc), eff_sigma))

    if not valid_transitions:
        return []

    curve: list[dict[str, float]] = []
    current_nm = start_nm

    while current_nm <= end_nm:
        intensity = 0.0
        for eff_wl, fosc, eff_sigma in valid_transitions:
            if eff_sigma <= 0:
                continue
            two_sig_sq = 2.0 * eff_sigma * eff_sigma
            norm_fact = 1.0 / (eff_sigma * math.sqrt(2.0 * math.pi))
            diff = current_nm - eff_wl
            if abs(diff) <= 5.0 * eff_sigma:
                intensity += fosc * norm_fact * math.exp(-(diff * diff) / two_sig_sq)

        curve.append({
            "wavelength_nm": round(current_nm, 2),
            "intensity": round(intensity * 1000.0, 6),
        })
        current_nm += step_nm

    return curve


def build_multi_spectrum_overlay(
    experimental_spectra: Sequence[ExperimentalSpectrum],
    theoretical_spectra: Sequence[dict[str, Any]],
    normalize_mode: str = "none",  # none | theoretical_only | experimental_only | all
    common_range_only: bool = False,
) -> dict[str, Any]:
    """Assemble a combined multi-spectrum overlay for experimental and theoretical datasets.

    Guarantees:
        - Experimental raw points are never mutated or resampled.
        - Theoretical curves are computed using their respective Gaussian broadening and shift.
        - Metadata (units, quantities, source files, data hashes) are strictly isolated.
    """
    exp_layers = []
    all_min_x = []
    all_max_x = []
    all_max_y_exp = 0.0
    all_max_y_theo = 0.0

    # 1. Process Experimental Spectra (Immutable Reference Layers)
    for exp in experimental_spectra:
        max_y = exp.max_absorbance if exp.max_absorbance > 0 else 1.0
        if max_y > all_max_y_exp:
            all_max_y_exp = max_y

        all_min_x.append(exp.min_wavelength)
        all_max_x.append(exp.max_wavelength)

        display_pts = []
        for wl, abs_val in exp.raw_data:
            disp_y = abs_val
            if normalize_mode in ("experimental_only", "all") and max_y > 0:
                disp_y = abs_val / max_y
            display_pts.append({"wavelength_nm": wl, "intensity": round(disp_y, 6), "raw_absorbance": abs_val})

        exp_layers.append({
            "layer_type": "experimental",
            "name": f"Experimental - {exp.file_name}" + (f" ({exp.sheet_name})" if exp.sheet_name else ""),
            "file_name": exp.file_name,
            "sheet_name": exp.sheet_name,
            "column_wavelength": exp.column_wavelength,
            "column_absorbance": exp.column_absorbance,
            "units_wavelength": exp.units_wavelength,
            "units_y": exp.units_y,
            "y_quantity": exp.y_quantity,
            "raw_hash": exp.raw_hash,
            "points_count": exp.points_count,
            "points": display_pts,
            "peaks": list(exp.detected_peaks),
        })

    # 2. Process Theoretical Spectra
    theo_layers = []
    for theo in theoretical_spectra:
        transitions = theo.get("transitions") or []
        sigma_nm = float(theo.get("sigma_nm", 20.0))
        sigma_ev = float(theo["sigma_ev"]) if theo.get("sigma_ev") is not None else None
        shift_nm = float(theo.get("shift_nm") if theo.get("shift_nm") is not None else theo.get("wavelength_shift_nm", 0.0))
        shift_ev = float(theo["shift_ev"]) if theo.get("shift_ev") is not None else None
        start_nm = float(theo.get("start_nm", 180.0))
        end_nm = float(theo.get("end_nm", 800.0))
        step_nm = float(theo.get("step_nm", 1.0))

        curve = convolute_theoretical_spectrum(
            transitions=transitions,
            sigma_nm=sigma_nm,
            sigma_ev=sigma_ev,
            shift_nm=shift_nm,
            shift_ev=shift_ev,
            start_nm=start_nm,
            end_nm=end_nm,
            step_nm=step_nm,
        )

        max_y_curve = max((pt["intensity"] for pt in curve), default=1.0)
        if max_y_curve > all_max_y_theo:
            all_max_y_theo = max_y_curve

        if curve:
            all_min_x.append(curve[0]["wavelength_nm"])
            all_max_x.append(curve[-1]["wavelength_nm"])

        display_curve = []
        for pt in curve:
            disp_y = pt["intensity"]
            if normalize_mode in ("theoretical_only", "all") and max_y_curve > 0:
                disp_y = pt["intensity"] / max_y_curve
            display_curve.append({
                "wavelength_nm": pt["wavelength_nm"],
                "intensity": round(disp_y, 6),
                "raw_intensity": pt["intensity"],
            })

        display_transitions = []
        for t in transitions:
            wl = t.get("wavelength_nm") or (1e7 / t.get("energy_cm") if t.get("energy_cm") else None)
            fosc = t.get("oscillator_strength", 0.0)
            if wl and float(wl) > 0:
                eff_wl = float(wl) + shift_nm
                display_transitions.append({
                    "state": t.get("state", ""),
                    "original_wavelength_nm": round(float(wl), 2),
                    "wavelength_nm": round(eff_wl, 2),
                    "energy_cm": t.get("energy_cm"),
                    "energy_ev": t.get("energy_ev"),
                    "oscillator_strength": fosc,
                })

        theo_layers.append({
            "layer_type": "theoretical",
            "name": theo.get("name") or "Theoretical TD-DFT",
            "calculation_id": theo.get("calculation_id") or theo.get("job_id"),
            "method": theo.get("method") or "ORCA TD-DFT",
            "basis": theo.get("basis") or "",
            "units_wavelength": "nm",
            "units_y": "a.u." if normalize_mode in ("theoretical_only", "all") else "arbitrary_epsilon",
            "y_quantity": "convoluted_intensity",
            "parameters": {
                "sigma_nm": sigma_nm,
                "sigma_ev": sigma_ev,
                "shift_nm": shift_nm,
                "shift_ev": shift_ev,
            },
            "curve": display_curve,
            "transitions": display_transitions,
        })

    viewport_min_x = min(all_min_x) if all_min_x else 200.0
    viewport_max_x = max(all_max_x) if all_max_x else 800.0

    if common_range_only and experimental_spectra and theoretical_spectra:
        exp_min = max(exp.min_wavelength for exp in experimental_spectra)
        exp_max = min(exp.max_wavelength for exp in experimental_spectra)
        viewport_min_x = max(viewport_min_x, exp_min)
        viewport_max_x = min(viewport_max_x, exp_max)

    return {
        "ok": True,
        "experimental_layers": exp_layers,
        "theoretical_layers": theo_layers,
        "total_layers": len(exp_layers) + len(theo_layers),
        "viewport": {
            "min_wavelength_nm": round(viewport_min_x, 1),
            "max_wavelength_nm": round(viewport_max_x, 1),
            "max_y_experimental": round(all_max_y_exp, 5),
            "max_y_theoretical": round(all_max_y_theo, 5),
            "normalize_mode": normalize_mode,
        },
        "scientific_notice": (
            "Experimental spectra are immutable reference layers. "
            "Gaussian broadening and spectral shifts are applied exclusively to theoretical models."
        ),
    }
