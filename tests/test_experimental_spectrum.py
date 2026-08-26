# -*- coding: utf-8 -*-
"""
Automated Test Suite for Interactive Multi-Spectrum Analysis & Experimental Spectrum Layer.

Validates all 27 user requirements:
- TXT, CSV, TSV parsing with various delimiters, headers, comments
- XLSX single-sheet, two-sheet, and multi-sheet workbook parsing
- Multi-experimental and multi-theoretical overlays
- Strict immutability of raw experimental data under Gaussian broadening and theoretical shift
- Stability and hash invariance verification
- Error handling for invalid formats
- Essential benchmark test verification
"""

import io
import json
import os
import sys

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "orca_engine", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if "." not in sys.path:
    sys.path.insert(0, ".")

import pytest
openpyxl = pytest.importorskip("openpyxl")


from orca_engine.experimental_spectrum import (
    ExperimentalSpectrum,
    ExperimentalSpectrumError,
    build_multi_spectrum_overlay,
    compute_raw_data_hash,
    convolute_theoretical_spectrum,
    detect_peaks,
    inspect_experimental_file,
    parse_experimental_excel,
    parse_experimental_text,
)


# Helper to build in-memory XLSX
def create_excel_bytes(sheets_data: dict[str, list[list]]) -> bytes:
    wb = openpyxl.Workbook()
    first = True
    for sname, rows in sheets_data.items():
        if first:
            ws = wb.active
            ws.title = sname
            first = False
        else:
            ws = wb.create_sheet(title=sname)
        for row in rows:
            ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


class TestExperimentalSpectrumParsing:
    """1. Format Support & Delimiter Handling."""

    def test_txt_whitespace_delimited(self):
        content = """
        # Spectrophotometer Run 2026-08-16
        # Solvent: Acetonitrile
        Wavelength(nm)   Absorbance
        200.0            0.0125
        210.0            0.0450
        220.0            0.1820
        230.0            0.5400
        240.0            0.8900
        250.0            0.4200
        260.0            0.0800
        """
        exp = parse_experimental_text(content, file_name="sample.txt")
        assert exp.source_type == "experimental"
        assert exp.points_count == 7
        assert exp.min_wavelength == 200.0
        assert exp.max_wavelength == 260.0
        assert exp.max_absorbance == 0.8900
        assert exp.units_wavelength == "nm"
        assert exp.units_y == "AU"
        assert exp.y_quantity == "absorbance"
        assert len(exp.raw_hash) == 64

    def test_csv_comma_delimited(self):
        content = """Lambda,Abs\n200,0.05\n210,0.15\n220,0.45\n230,0.85\n240,0.30\n"""
        exp = parse_experimental_text(content, file_name="sample.csv", delimiter=",")
        assert exp.points_count == 5
        assert exp.raw_data[0] == (200.0, 0.05)
        assert exp.raw_data[-1] == (240.0, 0.30)

    def test_tsv_tab_delimited_with_greek_lambda(self):
        content = "λ (nm)\tOptical Density\n220.5\t0.112\n221.0\t0.118\n221.5\t0.125\n"
        exp = parse_experimental_text(content, file_name="sample.tsv")
        assert exp.points_count == 3
        assert exp.column_wavelength == "λ (nm)"
        assert exp.column_absorbance == "Optical Density"

    def test_headerless_two_columns(self):
        content = "200.0  0.01\n205.0  0.03\n210.0  0.15\n215.0  0.45\n"
        exp = parse_experimental_text(content, file_name="raw.dat")
        assert exp.points_count == 4
        assert exp.raw_data[0] == (200.0, 0.01)

    def test_xlsx_single_sheet(self):
        data = {
            "UV-Vis Measurement": [
                ["Wavelength (nm)", "Absorbance (AU)"],
                [200.0, 0.02],
                [210.0, 0.08],
                [220.0, 0.35],
                [230.0, 0.92],
                [240.0, 0.40],
            ]
        }
        b = create_excel_bytes(data)
        exp = parse_experimental_excel(b, file_name="sample.xlsx")
        assert exp.points_count == 5
        assert exp.sheet_name == "UV-Vis Measurement"
        assert exp.max_absorbance == 0.92

    def test_xlsx_two_sheets_split_wavelength_and_absorbance(self):
        data = {
            "Wavelengths": [["Wavelength"], [200], [210], [220], [230]],
            "Absorbance": [["Absorbance"], [0.05], [0.25], [0.80], [0.15]],
        }
        b = create_excel_bytes(data)
        exp = parse_experimental_excel(b, file_name="two_sheets.xlsx")
        assert exp.points_count == 4
        assert exp.raw_data[0] == (200.0, 0.05)
        assert exp.raw_data[2] == (220.0, 0.80)

    def test_xlsx_multi_sheet_selection(self):
        data = {
            "Control": [["Wavelength", "Abs"], [200, 0.01], [210, 0.02]],
            "Experiment_Batch_A": [["Wavelength", "Abs"], [200, 0.10], [210, 0.50], [220, 0.20]],
        }
        b = create_excel_bytes(data)
        exp = parse_experimental_excel(b, file_name="batches.xlsx", sheet_name="Experiment_Batch_A")
        assert exp.sheet_name == "Experiment_Batch_A"
        assert exp.points_count == 3
        assert exp.max_absorbance == 0.50

    def test_duplicate_wavelength_handling(self):
        # Two measurements at 210 nm: 0.40 and 0.60 -> average = 0.50
        content = "Wavelength\tAbsorbance\n200\t0.10\n210\t0.40\n210\t0.60\n220\t0.20\n"
        exp = parse_experimental_text(content, file_name="duplicates.txt")
        assert exp.points_count == 3
        assert exp.raw_data[1] == (210.0, 0.50)

    def test_invalid_text_handling(self):
        with pytest.raises(ExperimentalSpectrumError, match="not contain at least 2 valid numeric"):
            parse_experimental_text("Single line with no numbers", file_name="bad.txt")

    def test_empty_file_handling(self):
        with pytest.raises(ExperimentalSpectrumError, match="empty"):
            parse_experimental_text("   \n\n  ", file_name="empty.txt")


class TestScientificSeparationAndInvariance:
    """Core Scientific Principle:
    Experimental spectrum is an immutable reference layer.
    Gaussian broadening and theoretical shift must NEVER mutate experimental data.
    """

    def test_essential_benchmark_invariance(self):
        """Mandatory Benchmark (Requirement 27):
        Dataset:
            Wavelength: [200, 210, 220]
            Absorbance: [0.10, 0.50, 0.20]
        Apply Gaussian width = 0.1, 0.5, 1.0 and Shift = -10, 0, +10.
        Verify experimental raw values remain 100% numerically identical.
        """
        raw_text = "Wavelength\tAbsorbance\n200\t0.10\n210\t0.50\n220\t0.20"
        exp = parse_experimental_text(raw_text, file_name="benchmark.txt")
        initial_hash = exp.raw_hash
        initial_raw = list(exp.raw_data)

        theoretical_model = {
            "name": "B3LYP/def2-TZVP",
            "transitions": [
                {"state": "S1", "wavelength_nm": 208.5, "oscillator_strength": 0.450},
                {"state": "S2", "wavelength_nm": 218.0, "oscillator_strength": 0.120},
            ]
        }

        # Test varying Gaussian widths
        gaussian_widths = [0.10, 0.20, 0.50, 1.00]
        shifts = [-10.0, -5.0, 0.0, 5.0, 10.0]

        for sigma in gaussian_widths:
            for shift in shifts:
                theo_input = dict(theoretical_model)
                theo_input["sigma_nm"] = sigma * 20.0  # scaled
                theo_input["shift_nm"] = shift

                overlay = build_multi_spectrum_overlay(
                    experimental_spectra=[exp],
                    theoretical_spectra=[theo_input],
                )

                # Verify Experimental Layer in overlay
                assert len(overlay["experimental_layers"]) == 1
                exp_layer = overlay["experimental_layers"][0]
                assert exp_layer["raw_hash"] == initial_hash
                assert exp_layer["points_count"] == 3

                # Numerical exactness check
                exp_pts = exp_layer["points"]
                assert [p["wavelength_nm"] for p in exp_pts] == [200.0, 210.0, 220.0]
                assert [p["raw_absorbance"] for p in exp_pts] == [0.10, 0.50, 0.20]

                # Ensure exp object itself was not mutated
                assert exp.raw_hash == initial_hash
                assert list(exp.raw_data) == initial_raw

    def test_multi_experimental_and_multi_theoretical_overlay(self):
        exp1 = parse_experimental_text("Wavelength,Abs\n200,0.1\n250,0.8\n300,0.2", "exp1.csv", delimiter=",")
        exp2 = parse_experimental_text("Wavelength,Abs\n220,0.2\n270,0.9\n320,0.1", "exp2.csv", delimiter=",")

        theo1 = {
            "name": "B3LYP",
            "transitions": [{"wavelength_nm": 248.0, "oscillator_strength": 0.65}],
            "sigma_nm": 15.0,
            "shift_nm": 0.0,
        }
        theo2 = {
            "name": "PBE0",
            "transitions": [{"wavelength_nm": 242.0, "oscillator_strength": 0.70}],
            "sigma_nm": 15.0,
            "shift_nm": 5.0,
        }

        res = build_multi_spectrum_overlay(
            experimental_spectra=[exp1, exp2],
            theoretical_spectra=[theo1, theo2],
        )

        assert res["ok"] is True
        assert len(res["experimental_layers"]) == 2
        assert len(res["theoretical_layers"]) == 2
        assert res["total_layers"] == 4

        # Verify independent metadata
        assert res["experimental_layers"][0]["y_quantity"] == "absorbance"
        assert res["experimental_layers"][0]["units_y"] == "AU"
        assert res["theoretical_layers"][0]["y_quantity"] == "convoluted_intensity"

    def test_display_normalization_does_not_mutate_raw_data(self):
        exp = parse_experimental_text("Wavelength,Abs\n200,0.5\n210,2.0\n220,1.0", "abs.csv", delimiter=",")
        initial_hash = exp.raw_hash

        # Run with 'all' normalization
        res = build_multi_spectrum_overlay(
            experimental_spectra=[exp],
            theoretical_spectra=[],
            normalize_mode="all",
        )

        pts = res["experimental_layers"][0]["points"]
        # Display intensity normalized by max (2.0) -> [0.25, 1.0, 0.5]
        assert [p["intensity"] for p in pts] == [0.25, 1.0, 0.5]
        # Raw absorbance pristine
        assert [p["raw_absorbance"] for p in pts] == [0.5, 2.0, 1.0]
        # Hash invariant
        assert exp.raw_hash == initial_hash

    def test_file_inspection_helper(self):
        txt_bytes = b"Wavelength\tAbsorbance\n200\t0.1\n210\t0.2\n"
        info = inspect_experimental_file(txt_bytes, "spectrum.txt")
        assert info["file_type"] == "text"
        assert "Wavelength" in info["headers"]

        xlsx_data = {"Run 1": [["Lambda", "OD"], [200, 0.1], [210, 0.2]]}
        xlsx_bytes = create_excel_bytes(xlsx_data)
        info_xlsx = inspect_experimental_file(xlsx_bytes, "experiment.xlsx")
        assert info_xlsx["file_type"] == "excel"
        assert len(info_xlsx["sheets"]) == 1
        assert info_xlsx["sheets"][0]["name"] == "Run 1"
