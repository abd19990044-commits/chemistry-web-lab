"""
Tests for Quantum Analyzer and Reaction Thermochemistry file upload endpoints.
Verifies:
1. Single .out file upload to /api/orca/engine/parse.
2. UTF-16 encoded .out file (e.g. from Windows PowerShell redirection `orca in.inp > out.out`).
3. Zip archive upload containing multiple .out files to /api/orca/engine/parse.
4. Single .out and .zip uploads to /api/orca/engine/thermochemistry.
5. Unified reaction setup input generation via /api/v1/reactions/unified-setup.
"""
import io
import json
import os
import zipfile
import pytest
from app import app, decode_text_bytes

BENCHMARK_OUT = os.path.join(
    os.path.dirname(__file__), "..", "orca_engine", "tests", "data", "thermo.out"
)
BENCHMARK_H2O = os.path.join(
    os.path.dirname(__file__), "..", "orca_engine", "tests", "data", "rxn_h2o.out"
)
BENCHMARK_H2 = os.path.join(
    os.path.dirname(__file__), "..", "orca_engine", "tests", "data", "rxn_h2.out"
)
BENCHMARK_O = os.path.join(
    os.path.dirname(__file__), "..", "orca_engine", "tests", "data", "rxn_o.out"
)


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_decode_text_bytes_encodings():
    utf8_text = "Program Version 5.0.4\n* O   0.0  0.0  0.0\n"
    assert decode_text_bytes(utf8_text.encode("utf-8")) == utf8_text
    assert decode_text_bytes(b"\xef\xbb\xbf" + utf8_text.encode("utf-8")) == utf8_text
    assert decode_text_bytes(utf8_text.encode("utf-16")) == utf8_text
    assert "Program Version" in decode_text_bytes(utf8_text.encode("utf-16-le"))


def test_analyzer_upload_single_out(client):
    assert os.path.exists(BENCHMARK_OUT), f"Missing {BENCHMARK_OUT}"
    with open(BENCHMARK_OUT, "rb") as f:
        content = f.read()

    resp = client.post(
        "/api/orca/engine/parse",
        data={"file": (io.BytesIO(content), "thermo.out")},
        content_type="multipart/form-data"
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["is_archive"] is False
    assert data["jobs_count"] >= 1
    assert "molecule" in data
    assert "latest_job" in data
    assert "raw_text" in data
    assert len(data["raw_text"]) > 0


def test_analyzer_upload_utf16_out(client):
    with open(BENCHMARK_OUT, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    utf16_bytes = text.encode("utf-16")
    resp = client.post(
        "/api/orca/engine/parse",
        data={"file": (io.BytesIO(utf16_bytes), "powershell_output.out")},
        content_type="multipart/form-data"
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["jobs_count"] >= 1


def test_analyzer_upload_zip_archive(client):
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        with open(BENCHMARK_H2O, "rb") as f:
            zf.writestr("h2o_opt.out", f.read())
        with open(BENCHMARK_H2, "rb") as f:
            zf.writestr("h2_opt.out", f.read())

    zip_buf.seek(0)
    resp = client.post(
        "/api/orca/engine/parse",
        data={"file": (zip_buf, "reaction_calcs.zip")},
        content_type="multipart/form-data"
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["is_archive"] is True
    assert len(data["archive_entries"]) == 2

    for entry in data["archive_entries"]:
        assert "filename" in entry
        assert "basename" in entry
        assert "raw_text" in entry
        assert len(entry["raw_text"]) > 0
        assert "molecule" in entry
        assert "latest_job" in entry
        assert entry["jobs_count"] >= 1

    assert data["selected_file"] in ("h2o_opt.out", "h2_opt.out")
    assert "latest_job" in data


def test_thermochemistry_upload_zip_archive(client):
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        with open(BENCHMARK_H2, "rb") as f:
            zf.writestr("H2.out", f.read())
        with open(BENCHMARK_O, "rb") as f:
            zf.writestr("O.out", f.read())
        with open(BENCHMARK_H2O, "rb") as f:
            zf.writestr("H2O.out", f.read())

    zip_buf.seek(0)
    resp = client.post(
        "/api/orca/engine/thermochemistry",
        data={
            "equation": "H2 + O -> H2O",
            "archive": (zip_buf, "reaction_calcs.zip")
        },
        content_type="multipart/form-data"
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "result" in data
    res = data["result"]
    assert "delta_gibbs_kcal_mol" in res or "delta_g_kcal_mol" in res


def test_thermochemistry_upload_structured_json(client):
    with open(BENCHMARK_H2, "r", encoding="utf-8") as f:
        h2_txt = f.read()
    with open(BENCHMARK_O, "r", encoding="utf-8") as f:
        o_txt = f.read()
    with open(BENCHMARK_H2O, "r", encoding="utf-8") as f:
        h2o_txt = f.read()

    resp = client.post(
        "/api/orca/engine/thermochemistry",
        json={
            "equation": "H2 + O -> H2O",
            "reactants": [
                {"name": "H2", "coefficient": 1, "content": h2_txt},
                {"name": "O", "coefficient": 1, "content": o_txt},
            ],
            "products": [
                {"name": "H2O", "coefficient": 1, "content": h2o_txt}
            ]
        }
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "result" in data


def test_unified_reaction_setup_endpoint(client):
    payload = {
        "equation": "2 H2 + O2 -> 2 H2O",
        "reactants": [
            {"name": "H2", "coefficient": 2, "charge": 0, "multiplicity": 1, "coords": "H 0.0 0.0 0.0\nH 0.0 0.0 0.74"},
            {"name": "O2", "coefficient": 1, "charge": 0, "multiplicity": 3, "coords": "O 0.0 0.0 0.0\nO 0.0 0.0 1.21"}
        ],
        "products": [
            {"name": "H2O", "coefficient": 2, "charge": 0, "multiplicity": 1, "coords": "O 0.0 0.0 0.0\nH 0.75 0.0 0.58\nH -0.75 0.0 0.58"}
        ],
        "calc_type": "opt_freq",
        "method": "B3LYP",
        "basis_set": "def2-SVP",
        "solvation": "Water",
        "dispersion": "D3BJ",
        "nprocs": 4,
        "maxcore": 2000
    }
    resp = client.post("/api/v1/reactions/unified-setup", json=payload)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert len(data["species"]) == 3
    for sp in data["species"]:
        assert len(sp["stages"]) >= 1
        input_deck = sp["stages"][0]["input_text"]
        assert "B3LYP" in input_deck
        assert "def2-SVP" in input_deck
        assert "CPCM(Water)" in input_deck or "CPCM" in input_deck
        assert "D3BJ" in input_deck
        assert "OPT" in input_deck.upper()
