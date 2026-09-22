# -*- coding: utf-8 -*-
"""tests/test_cdxml_export.py
=============================
Comprehensive unit and integration test suite for ChemDraw XML (CDXML) export
and reaction drawing enhancements.
"""
from __future__ import annotations

import base64
from xml.etree import ElementTree as ET

import pytest
from app import app
from services import cdxml_service


def _get_arrow(root: ET.Element):
    for g in root.findall("page/graphic"):
        if g.get("ArrowType") or (g.get("ArrowheadHead") and g.get("ArrowheadTail")):
            return g
    raise AssertionError("No reaction arrow graphic found in CDXML page")


# ─────────────────────────────────────────────────────────────
# 1. Single Compound CDXML Tests
# ─────────────────────────────────────────────────────────────
def test_single_compound_cdxml_aspirin():
    data = cdxml_service.generate_single_compound_cdxml("CC(=O)OC1=CC=CC=C1C(=O)O", "Aspirin")
    assert data is not None
    assert data.startswith(b"<?xml")

    root = ET.fromstring(data)
    assert root.tag == "CDXML"
    assert root.get("BondLength") == "14.4"
    assert root.get("LabelFont") == "3"

    page = root.find("page")
    assert page is not None

    fragments = page.findall("fragment")
    assert len(fragments) == 1
    frag = fragments[0]

    nodes = frag.findall("n")
    bonds = frag.findall("b")
    assert len(nodes) == 13  # 9 carbons, 4 oxygens
    assert len(bonds) == 13

    # Check that atoms have coordinates and element numbers
    for n in nodes:
        assert "p" in n.attrib
        assert "Element" in n.attrib
        assert int(n.attrib["Element"]) in (6, 8)

    # Check validation helper
    report = cdxml_service.validate_cdxml_bytes(data)
    assert report["valid"] is True
    assert report["fragments"] == 1
    assert report["atoms"] == 13
    assert report["bonds"] == 13


def test_single_compound_cdxml_stereochemistry():
    # L-Alanine has chiral center with wedge
    smiles = "C[C@H](N)C(=O)O"
    data = cdxml_service.generate_single_compound_cdxml(smiles, "L-Alanine")
    assert data is not None

    root = ET.fromstring(data)
    displays = [b.get("Display") for b in root.findall("page/fragment/b") if b.get("Display")]
    assert any(d in {"WedgeBegin", "WedgedHashBegin", "WedgeEnd", "WedgedHashEnd"} for d in displays)


def test_single_compound_cdxml_charges_and_isotopes():
    # Acetate anion with carbon-13: [13CH3]C(=O)[O-]
    smiles = "[13CH3]C(=O)[O-]"
    data = cdxml_service.generate_single_compound_cdxml(smiles, "13C-Acetate")
    assert data is not None

    root = ET.fromstring(data)
    nodes = root.findall("page/fragment/n")
    has_charge = any(n.get("Charge") == "-1" for n in nodes)
    has_isotope = any(n.get("Isotope") == "13" for n in nodes)
    assert has_charge
    assert has_isotope


# ─────────────────────────────────────────────────────────────
# 2. Reaction Scheme CDXML Tests
# ─────────────────────────────────────────────────────────────
def test_reaction_cdxml_forward_arrow_and_conditions():
    data = cdxml_service.generate_reaction_cdxml(
        [(2, "[H][H]"), (1, "O=O")],
        [(2, "O")],
        arrow_top="Pd/C",
        arrow_bottom="25 °C, 1 atm",
        arrow_style="forward",
        title="2 H2 + O2 -> 2 H2O",
        small_as_formula=False,
    )
    assert data is not None
    root = ET.fromstring(data)
    assert root.tag == "CDXML"

    page = root.find("page")
    assert page is not None

    # Fragments for H2, O2, and H2O
    fragments = page.findall("fragment")
    assert len(fragments) == 3

    arrow = _get_arrow(root)
    assert arrow.get("ArrowType") == "FullHead"

    # In ChemDraw coordinate convention, arrow BoundingBox is "end_x end_y start_x start_y"
    coords = [float(v) for v in arrow.get("BoundingBox").split()]
    assert coords[0] > coords[2]  # end_x > start_x (points right)

    # Check texts: numerals '2', conditions, and '+'
    texts = ["".join(t.itertext()) for t in page.findall("t")]
    assert "2" in texts
    assert "+" in texts
    assert "Pd/C" in texts
    assert "25 °C, 1 atm" in texts


def test_reaction_cdxml_equilibrium_arrow():
    data = cdxml_service.generate_reaction_cdxml(
        [(1, "CC(=O)O"), (1, "CCO")],
        [(1, "CC(=O)OCC"), (1, "O")],
        arrow_top="H2SO4",
        arrow_style="equilibrium",
    )
    assert data is not None
    root = ET.fromstring(data)
    arrow = _get_arrow(root)
    assert arrow.get("ArrowType") == "Equilibrium"


def test_reaction_cdxml_reversible_arrow():
    data = cdxml_service.generate_reaction_cdxml(
        [(1, "c1ccccc1")],
        [(1, "c1ccccc1")],
        arrow_style="reversible",
    )
    assert data is not None
    root = ET.fromstring(data)
    arrow = _get_arrow(root)
    assert arrow.get("ArrowheadHead") == "Full"
    assert arrow.get("ArrowheadTail") == "Full"


def test_reaction_cdxml_condensed_small_organics():
    # Hydrogenation of ethylene to ethane
    data = cdxml_service.generate_reaction_cdxml(
        [(1, "C=C"), (1, "[H][H]")],
        [(1, "CC")],
        arrow_top="Ni",
        arrow_bottom="150 °C",
        small_as_formula=True,
    )
    assert data is not None
    root = ET.fromstring(data)
    all_texts = " ".join("".join(t.itertext()) for t in root.findall("page/t"))
    assert "CH₂" in all_texts
    assert "H₂" in all_texts
    assert "CH₃" in all_texts

    # Vector bond lines for CH2=CH2 and CH3-CH3
    plain_lines = [g for g in root.findall("page/graphic") if not g.get("ArrowType")]
    assert len(plain_lines) >= 3


def test_cdxml_source_neutrality():
    data = cdxml_service.generate_reaction_cdxml(
        [(1, "C=C"), (1, "[H][H]")],
        [(1, "CC")],
        arrow_top="Ni",
        arrow_bottom="150 °C",
    )
    assert data is not None
    text = data.decode("utf-8")
    root = ET.fromstring(data)

    # No generator watermarks
    assert "CreationProgram" not in root.attrib
    assert "Creator" not in root.attrib
    assert "Producer" not in root.attrib
    assert "<!--" not in text
    assert "generated by" not in text.lower()


# ─────────────────────────────────────────────────────────────
# 3. Web API Endpoints Tests
# ─────────────────────────────────────────────────────────────
def test_api_compound_includes_cdxml():
    client = app.test_client()
    resp = client.post("/api/compound", json={"query": "Aspirin"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "cdxml_file_base64" in data
    assert data["cdxml_file_base64"]

    cdxml_bytes = base64.b64decode(data["cdxml_file_base64"])
    report = cdxml_service.validate_cdxml_bytes(cdxml_bytes)
    assert report["valid"] is True
    assert report["atoms"] == 13


def test_api_reaction_includes_cdxml_and_respects_arrow_style():
    client = app.test_client()
    resp = client.post("/api/reaction", json={
        "reactants": "2 H2 + O2",
        "products": "2 H2O",
        "arrow_top": "Pd/C",
        "arrow_bottom": "25 °C",
        "arrow_style": "equilibrium",
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "cdxml_file_base64" in data
    assert data["cdxml_file_base64"]

    cdxml_bytes = base64.b64decode(data["cdxml_file_base64"])
    report = cdxml_service.validate_cdxml_bytes(cdxml_bytes)
    assert report["valid"] is True
    assert report["arrow_type"] == "Equilibrium"
    assert data["cdxml_report"]["valid"] is True


def test_api_v1_export_compound_cdxml_attachment():
    client = app.test_client()
    resp = client.post("/api/v1/compound/export-cdxml", json={"query": "ethanol"})
    assert resp.status_code == 200
    assert "chemical/x-cdxml" in resp.content_type
    assert 'attachment; filename="ethanol.cdxml"' in resp.headers.get("Content-Disposition", "")
    assert resp.data.startswith(b"<?xml")
    root = ET.fromstring(resp.data)
    assert root.tag == "CDXML"


def test_api_v1_export_reaction_cdxml_attachment():
    client = app.test_client()
    resp = client.post("/api/v1/reaction/export-cdxml", json={
        "reactants": "benzene + Br2",
        "products": "bromobenzene + HBr",
        "arrow_top": "FeBr3",
        "arrow_style": "forward",
    })
    assert resp.status_code == 200
    assert "chemical/x-cdxml" in resp.content_type
    assert 'attachment; filename="reaction.cdxml"' in resp.headers.get("Content-Disposition", "")
    report = cdxml_service.validate_cdxml_bytes(resp.data)
    assert report["valid"] is True


def test_api_v1_export_reaction_rxn_attachment():
    client = app.test_client()
    resp = client.post("/api/v1/reaction/export-rxn", json={
        "reactants": "2 H2 + O2",
        "products": "2 H2O",
    })
    assert resp.status_code == 200
    assert "chemical/x-mdl-rxnfile" in resp.content_type
    assert 'attachment; filename="reaction.rxn"' in resp.headers.get("Content-Disposition", "")
    assert b"$RXN" in resp.data
