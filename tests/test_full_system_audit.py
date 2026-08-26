# -*- coding: utf-8 -*-
"""Full System Audit: verifies every single subsystem and endpoint in Chemistry Lab."""
import base64
import io
import json
import os
import sys
import zipfile
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as webapp


@pytest.fixture
def client():
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        yield c


def test_audit_index_page_and_config(client):
    """1. Test that the index page loads with the complete config block."""
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Chemistry Lab" in html
    assert 'id="orca-config"' in html
    assert 'id="thermo-form"' in html
    assert 'id="thermo-reactants-list"' in html
    assert 'id="thermo-products-list"' in html
    assert 'id="engine-form"' in html
    assert 'id="reaction-form"' in html


def test_audit_molecule_drawing(client):
    """2. Test single molecule lookup and drawing."""
    resp = client.post("/api/compound", json={"query": "aspirin"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "image_png_base64" in data
    assert "image_svg_base64" in data
    assert data["formula"] == "C9H8O4"


def test_audit_reaction_drawing_and_caret_stripping(client):
    """3. Test reaction drawing, multi-species parallel resolution, and caret stripping."""
    # Test reaction with caret superscripts like NH4^+ and Fe^3+
    resp = client.post("/api/reaction", json={
        "reactants": "2 H2 + O2",
        "products": "2 H2O",
        "arrow_top": "NH4^+ catalyst",
        "arrow_bottom": "80^oC",
        "small_as_formula": True,
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["balance"]["balanced"] is True
    assert "image_png_base64" in data
    assert "image_svg_base64" in data
    assert "rxn_file_base64" in data

    # Verify caret was stripped in SVG output
    svg_text = base64.b64decode(data["image_svg_base64"]).decode("utf-8")
    assert "^" not in svg_text

    # Test long multi-component reaction (complex equation)
    import xml.etree.ElementTree as ET
    resp_long = client.post("/api/reaction", json={
        "reactants": "2 h2 + phenol + paracetamol + naproxen",
        "products": "2 h2 + phenol + paracetamol + naproxen",
        "arrow_top": "H2SO4 H2O NH4^+",
        "arrow_bottom": "80^oC",
        "small_as_formula": True,
    })
    assert resp_long.status_code == 200
    d_long = resp_long.get_json()
    assert d_long["ok"] is True
    long_svg = base64.b64decode(d_long["image_svg_base64"]).decode("utf-8")
    tree = ET.fromstring(long_svg)
    assert tree.tag.endswith("svg")



def test_audit_orca_coords_and_input_generator(client):
    """4. Test 3D coordinate lookup and ORCA input generator."""
    # Coords
    resp = client.post("/api/orca/coords", json={"query": "water"})
    assert resp.status_code == 200
    cdata = resp.get_json()
    assert cdata["ok"] is True
    assert "coords" in cdata
    assert "O" in cdata["coords"]

    # Input generator
    resp_inp = client.post("/api/orca/generate", json={
        "calc_type": "opt",
        "theory": "b3lyp",
        "basis": "def2-SVP",
        "charge": 0,
        "mult": 1,
        "cores": 4,
        "ram": 4000,
        "coords": "O 0 0 0\nH 0.7 0.7 0\nH -0.7 0.7 0",
        "solv_model": "cpcm",
        "solvent": "water",
    })
    assert resp_inp.status_code == 200
    inp_data = resp_inp.get_json()
    assert inp_data["ok"] is True
    assert "b3lyp" in inp_data["input_text"].lower()
    assert "def2-svp" in inp_data["input_text"].lower()
    assert "%pal nprocs 4 end" in inp_data["input_text"]


def test_audit_quantum_engine_parse_direct_and_archive(client):
    """5. Test calculation parsing from raw text and ZIP archive."""
    sample_text = (
        "------------------------------------------------------------------------------\n"
        "* O   R   C   A *\n"
        "------------------------------------------------------------------------------\n"
        "                               Program Version 6.0.0\n"
        "Number of atoms                             ...     3\n"
        "Total Charge                                ...     0\n"
        "Multiplicity                                ...     1\n"
        "CARTESIAN COORDINATES (ANGSTROEM)\n"
        "---------------------------------\n"
        "O      0.000000    0.000000    0.117269\n"
        "H      0.000000    0.756968   -0.469076\n"
        "H      0.000000   -0.755453   -0.469076\n"
        "FINAL SINGLE POINT ENERGY      -76.421258412356\n"
        "TOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds 234 msec\n"
        "****ORCA TERMINATED NORMALLY****\n"
    )

    # Raw text parse
    resp = client.post("/api/orca/engine/parse", json={"content": sample_text, "name": "water_test"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "latest_job" in data
    assert data["latest_job"]["e_elec_eh"] == pytest.approx(-76.421258412356)

    # Archive ZIP parse
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("calc_opt.out", sample_text)
        zf.writestr("calc_tddft.out", sample_text.replace("6.0.0", "6.0.1"))

    buf.seek(0)
    resp_zip = client.post(
        "/api/orca/engine/parse",
        data={"file": (buf, "batch_calculations.zip")},
        content_type="multipart/form-data"
    )
    assert resp_zip.status_code == 200
    zip_data = resp_zip.get_json()
    assert zip_data["ok"] is True
    assert zip_data["is_archive"] is True
    assert len(zip_data["archive_entries"]) == 2


def test_audit_thermochemistry_two_box_api(client):
    """6. Test thermochemistry evaluation with structured reactants & products."""
    water_text = (
        "Program Version 6.0.0\n"
        "FINAL SINGLE POINT ENERGY -76.400000000000\n"
        "Total Enthalpy ... -76.350000000000 Eh\n"
        "Final Gibbs free energy ... -76.370000000000 Eh\n"
        "Total entropy correction ... 0.020000000000 Eh\n"
        "CARTESIAN COORDINATES (ANGSTROEM)\n"
        "  O      0.000000    0.000000    0.117790\n"
        "  H      0.000000    0.755453   -0.471161\n"
        "  H      0.000000   -0.755453   -0.471161\n"
        "ORCA TERMINATED NORMALLY\n"
    )
    h2_text = (
        "Program Version 6.0.0\n"
        "FINAL SINGLE POINT ENERGY -1.150000000000\n"
        "Total Enthalpy ... -1.140000000000 Eh\n"
        "Final Gibbs free energy ... -1.155000000000 Eh\n"
        "CARTESIAN COORDINATES (ANGSTROEM)\n"
        "  H      0.000000    0.000000    0.370000\n"
        "  H      0.000000    0.000000   -0.370000\n"
        "ORCA TERMINATED NORMALLY\n"
    )
    o2_text = (
        "Program Version 6.0.0\n"
        "FINAL SINGLE POINT ENERGY -150.300000000000\n"
        "Total Enthalpy ... -150.280000000000 Eh\n"
        "Final Gibbs free energy ... -150.310000000000 Eh\n"
        "CARTESIAN COORDINATES (ANGSTROEM)\n"
        "  O      0.000000    0.000000    0.600000\n"
        "  O      0.000000    0.000000   -0.600000\n"
        "ORCA TERMINATED NORMALLY\n"
    )

    resp = client.post("/api/orca/engine/thermochemistry", json={
        "equation": "2 H2 + O2 -> 2 H2O",
        "reactants": [
            {"name": "H2", "coefficient": 2, "content": h2_text},
            {"name": "O2", "coefficient": 1, "content": o2_text}
        ],
        "products": [
            {"name": "H2O", "coefficient": 2, "content": water_text}
        ],
        "temperature_k": 298.15,
        "pressure_atm": 1.0,
    })
    assert resp.status_code == 200
    res = resp.get_json()
    assert res["ok"] is True
    assert "result" in res
    r = res["result"]
    assert "delta_g_kcal_mol" in r or "delta_gibbs_kcal_mol" in r


def test_audit_session_lifecycle(client):
    """7. Test ephemeral session heartbeat and leave."""
    resp = client.post("/api/session/heartbeat", json={"session_id": "test_audit_session"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["session_id"] == "test_audit_session"
    assert data["ttl_seconds"] == 1800

    resp_leave = client.post("/api/session/leave", json={"session_id": "test_audit_session"})
    assert resp_leave.status_code == 200
    assert resp_leave.get_json()["ok"] is True
