"""Unit tests for multi-series experimental spectrum parsing and UX enhancements."""

import io
import pytest
from orca_engine.experimental_spectrum import (
    ExperimentalSpectrum,
    ExperimentalSpectrumError,
    parse_experimental_text,
    parse_experimental_multi_series_text,
    parse_experimental_multi_series_excel,
)
from app import app


def test_multi_series_text_parsing_auto():
    content = """Wavelength (nm),Sample A,Sample B,Sample C
200,0.12,0.45,0.80
210,0.25,0.60,0.95
220,0.50,0.85,1.20
230,0.80,1.10,1.50
240,0.40,0.70,1.05
"""
    spectra = parse_experimental_multi_series_text(content, file_name="multi_sample.csv")
    assert len(spectra) == 3
    assert spectra[0].column_absorbance == "Sample A"
    assert spectra[1].column_absorbance == "Sample B"
    assert spectra[2].column_absorbance == "Sample C"
    assert spectra[0].points_count == 5
    assert spectra[0].raw_data[0] == (200.0, 0.12)
    assert spectra[2].raw_data[2] == (220.0, 1.20)


def test_multi_series_text_parsing_selected_columns():
    content = """Wavelength,A,B,C,D
300,0.1,0.2,0.3,0.4
350,0.5,0.6,0.7,0.8
400,0.9,1.0,1.1,1.2
"""
    spectra = parse_experimental_multi_series_text(
        content,
        file_name="selected.csv",
        wavelength_col="Wavelength",
        absorbance_cols=["B", "D"],
    )
    assert len(spectra) == 2
    assert spectra[0].column_absorbance == "B"
    assert spectra[1].column_absorbance == "D"
    assert spectra[0].raw_data[0] == (300.0, 0.2)
    assert spectra[1].raw_data[0] == (300.0, 0.4)


def test_multi_series_reject_same_column_as_x_and_y():
    content = """Wavelength,Sample A
200,0.5
250,0.8
"""
    with pytest.raises(ExperimentalSpectrumError, match="Cannot select column"):
        parse_experimental_multi_series_text(
            content,
            file_name="invalid.csv",
            wavelength_col="Wavelength",
            absorbance_cols=["Wavelength"],
        )


def test_multi_series_excel_parsing():
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "UV Data"
    ws.append(["Wavelength (nm)", "Compound 1", "Compound 2"])
    ws.append([250, 0.15, 0.35])
    ws.append([260, 0.45, 0.65])
    ws.append([270, 0.85, 1.05])
    ws.append([280, 0.30, 0.50])

    buf = io.BytesIO()
    wb.save(buf)
    file_bytes = buf.getvalue()

    spectra = parse_experimental_multi_series_excel(file_bytes, file_name="compounds.xlsx")
    assert len(spectra) == 2
    assert spectra[0].column_absorbance == "Compound 1"
    assert spectra[1].column_absorbance == "Compound 2"
    assert spectra[0].points_count == 4
    assert spectra[0].sheet_name == "UV Data"


def test_independent_x_grids_parsing():
    """Verify multiple datasets with non-identical X grids maintain exact raw coordinates without row-pairing."""
    file_a_content = """Wavelength,Sample A
200,0.1
201,0.2
203,0.5
205,0.4
"""
    file_b_content = """Wavelength,Sample C
200.0,0.15
200.5,0.25
201.5,0.55
204.0,0.40
207.0,0.30
"""
    spectra_a = parse_experimental_multi_series_text(file_a_content, file_name="file_a.csv")
    spectra_b = parse_experimental_multi_series_text(file_b_content, file_name="file_b.csv")

    assert len(spectra_a) == 1
    assert len(spectra_b) == 1
    assert [p[0] for p in spectra_a[0].raw_data] == [200.0, 201.0, 203.0, 205.0]
    assert [p[0] for p in spectra_b[0].raw_data] == [200.0, 200.5, 201.5, 204.0, 207.0]


def test_independent_ir_wavenumber_grids_parsing():
    """Verify IR spectra with differing wavenumber intervals maintain faithful X-axis coordinates."""
    ir_a_content = """Wavenumber,Film A
4000,98.2
3980,96.5
3900,90.1
"""
    ir_b_content = """Wavenumber,Film B
3998,96.0
3985,95.0
3940,91.0
3890,88.0
"""
    spectra_a = parse_experimental_multi_series_text(ir_a_content, file_name="ir_a.csv")
    spectra_b = parse_experimental_multi_series_text(ir_b_content, file_name="ir_b.csv")

    assert len(spectra_a) == 1
    assert len(spectra_b) == 1
    assert len(spectra_a[0].raw_data) == 3
    assert len(spectra_b[0].raw_data) == 4
    assert spectra_a[0].raw_data[0][0] == 3900.0
    assert spectra_a[0].raw_data[-1][0] == 4000.0
    assert spectra_b[0].raw_data[0][0] == 3890.0
    assert spectra_b[0].raw_data[-1][0] == 3998.0


def test_api_experimental_parse_multi_series():
    client = app.test_client()

    csv_data = "Wavelength,Sample1,Sample2\n200,0.1,0.5\n250,0.4,0.8\n300,0.2,0.6\n"
    res = client.post(
        "/api/orca/engine/experimental-spectrum/parse",
        data={
            "file": (io.BytesIO(csv_data.encode("utf-8")), "uv_multi.csv"),
            "file_name": "uv_multi.csv",
        },
        content_type="multipart/form-data",
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True
    assert "spectra" in data
    assert len(data["spectra"]) == 2
    assert data["series_count"] == 2
    assert data["spectra"][0]["column_absorbance"] == "Sample1"
    assert data["spectra"][1]["column_absorbance"] == "Sample2"


def test_api_ir_experimental_parse_multi_series():
    client = app.test_client()

    ir_data = "Wavenumber,Sample_IR_1,Sample_IR_2\n1000,90.5,85.2\n1500,60.2,55.1\n1700,20.4,15.3\n"
    res = client.post(
        "/api/orca/engine/ir-spectrum/parse-experimental",
        data={
            "file": (io.BytesIO(ir_data.encode("utf-8")), "ftir_multi.csv"),
            "file_name": "ftir_multi.csv",
        },
        content_type="multipart/form-data",
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True or data["success"] is True
    assert "spectra" in data
    assert len(data["spectra"]) == 2
    assert data["spectra"][0]["column_absorbance"] == "Sample_IR_1"
    assert data["spectra"][1]["column_absorbance"] == "Sample_IR_2"
