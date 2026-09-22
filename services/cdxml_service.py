# -*- coding: utf-8 -*-
"""services/cdxml_service.py
===========================
Production-grade ChemDraw XML (CDXML) and canonical MDL RXN generator.

Adopts verified ChemDraw publication-grade drawing standards:
- Standard ChemDraw bond length: 14.4 pt
- Standard label and caption font size: 10 pt (Arial Unicode)
- Full bond display types: Single, Double, Triple, Aromatic, WedgeBegin,
  WedgedHashBegin, WedgeEnd, WedgedHashEnd
- Reaction layout: coefficients, '+' operators, multi-style reaction arrows
  (forward FullHead, equilibrium Equilibrium, reversible dual-headed),
  conditions over/under arrow
- Small condensed organic species (CH2=CH2, CH3-CH3, CH3COOH, etc.)
- Source-neutral CDXML documents (no proprietary tags/watermarks so ChemDraw
  treats the file as native user artwork).
"""
from __future__ import annotations

import io
import re
from fractions import Fraction
from functools import reduce
from math import gcd
from xml.etree import ElementTree as ET

from rdkit import Chem
from rdkit.Chem import AllChem, rdChemReactions, rdDepictor, rdMolDescriptors

# ─────────────────────────────────────────────────────────────
# ChemDraw Styling Constants (Points)
# ─────────────────────────────────────────────────────────────
BOND_LENGTH = 14.4
MOL_GAP = 22.0
PLUS_GAP = 14.0
ARROW_WIDTH = 72.0
PAGE_MARGIN = 24.0
BASELINE_Y = 110.0
LABEL_SIZE = 10.0
CAPTION_SIZE = 10.0
TEXT_SIZE = 10.0

_SUBSCRIPT = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")
_BOND_SPLIT_RE = re.compile(r"([=#-])")

_CONDENSED_SMALL_ORGANICS = {
    "C=C": "CH2=CH2",          # ethene
    "CC": "CH3-CH3",           # ethane
    "CO": "CH3OH",             # methanol
    "C=O": "H2C=O",            # formaldehyde
    "CCO": "CH3CH2OH",         # ethanol
    "CC=O": "CH3CHO",          # acetaldehyde
    "CC(=O)O": "CH3COOH",      # acetic acid
    "C#N": "HCN",              # hydrogen cyanide
    "CC#N": "CH3CN",           # acetonitrile
}


def _canonical_text_table(source: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for smiles, label in source.items():
        mol = Chem.MolFromSmiles(smiles)
        if mol is not None:
            out[Chem.MolToSmiles(mol)] = label
    return out


_CONDENSED_BY_CANONICAL_SMILES = _canonical_text_table(_CONDENSED_SMALL_ORGANICS)


def _condensed_for_display(smiles: str) -> str | None:
    mol = Chem.MolFromSmiles(smiles or "")
    if mol is None:
        return None
    return _CONDENSED_BY_CANONICAL_SMILES.get(Chem.MolToSmiles(mol))


def formula_for_display(smiles: str) -> str | None:
    """Return condensed text for tiny organics, or standard chemical formula."""
    condensed = _condensed_for_display(smiles)
    if condensed:
        return condensed
    mol = Chem.MolFromSmiles(smiles or "")
    if mol is None:
        return None
    carbons = sum(1 for a in mol.GetAtoms() if a.GetSymbol() == "C")
    if mol.GetRingInfo().NumRings() or carbons > 1 or mol.GetNumHeavyAtoms() > 4:
        return None
    if carbons and any(a.GetSymbol() in ("O", "N", "S") and a.GetTotalNumHs() for a in mol.GetAtoms()):
        return None
    hill = rdMolDescriptors.CalcMolFormula(mol)
    if not carbons and hill.startswith("H"):
        return None
    return hill


def _coef_text(value) -> str:
    f = Fraction(value)
    if f == 1:
        return ""
    if f.denominator == 1:
        return str(f.numerator)
    return f"{f.numerator}/{f.denominator}"


def _display_text(text: str) -> str:
    return "".join(ch.translate(_SUBSCRIPT) if ch.isdigit() else ch for ch in text)


def _prepare_mol(smiles: str) -> Chem.Mol | None:
    mol = Chem.MolFromSmiles(smiles or "")
    if mol is None:
        return None
    mol = Chem.Mol(mol)
    Chem.AssignStereochemistry(mol, cleanIt=True, force=True)
    try:
        Chem.Kekulize(mol, clearAromaticFlags=True)
    except Exception:
        pass
    rdDepictor.Compute2DCoords(mol, canonOrient=True, clearConfs=True)
    try:
        rdDepictor.StraightenDepiction(mol, minimizeRotation=True)
    except Exception:
        pass
    try:
        Chem.WedgeMolBonds(mol, mol.GetConformer())
    except Exception:
        pass
    return mol


def _mol_extent(mol: Chem.Mol) -> tuple[float, float, float, float]:
    conf = mol.GetConformer()
    xs = [conf.GetAtomPosition(i).x for i in range(mol.GetNumAtoms())]
    ys = [conf.GetAtomPosition(i).y for i in range(mol.GetNumAtoms())]
    if not xs:
        return 0.0, 1.0, 0.0, 1.0
    return min(xs), max(xs), min(ys), max(ys)


def _bond_order_value(bond: Chem.Bond) -> str:
    bt = bond.GetBondType()
    if bt == Chem.BondType.SINGLE:
        return "1"
    if bt == Chem.BondType.DOUBLE:
        return "2"
    if bt == Chem.BondType.TRIPLE:
        return "3"
    if bt == Chem.BondType.AROMATIC:
        return "1.5"
    return "1"


def _add_text(page: ET.Element, obj_id: int, x: float, y: float, text: str, size: float = TEXT_SIZE, bold: bool = False) -> int:
    t = ET.SubElement(page, "t", {"id": str(obj_id), "p": f"{x:.2f} {y:.2f}", "Justification": "Center"})
    s_attrs = {"font": "3", "size": f"{float(size):g}"}
    if bold:
        s_attrs["face"] = "1"
    s = ET.SubElement(t, "s", s_attrs)
    s.text = text
    return obj_id + 1


def _add_plain_line(page: ET.Element, obj_id: int, x1: float, y1: float, x2: float, y2: float) -> int:
    ET.SubElement(page, "graphic", {"id": str(obj_id), "GraphicType": "Line", "BoundingBox": f"{x1:.2f} {y1:.2f} {x2:.2f} {y2:.2f}"})
    return obj_id + 1


def _add_fragment(page: ET.Element, obj_id: int, mol: Chem.Mol, x0: float, center_y: float) -> tuple[int, float]:
    minx, maxx, miny, maxy = _mol_extent(mol)
    width = max((maxx - minx) * BOND_LENGTH, BOND_LENGTH)
    frag = ET.SubElement(page, "fragment", {"id": str(obj_id)})
    obj_id += 1
    conf = mol.GetConformer()
    atom_ids: dict[int, int] = {}
    for i, atom in enumerate(mol.GetAtoms()):
        p = conf.GetAtomPosition(i)
        x = x0 + (p.x - minx) * BOND_LENGTH
        y = center_y - (p.y - (miny + maxy) / 2.0) * BOND_LENGTH
        attrs = {"id": str(obj_id), "p": f"{x:.2f} {y:.2f}", "Element": str(atom.GetAtomicNum())}
        charge = atom.GetFormalCharge()
        if charge:
            attrs["Charge"] = str(charge)
        isotope = atom.GetIsotope()
        if isotope:
            attrs["Isotope"] = str(isotope)
        amap = atom.GetAtomMapNum()
        if amap:
            attrs["AtomMap"] = str(amap)
        ET.SubElement(frag, "n", attrs)
        atom_ids[i] = obj_id
        obj_id += 1
    for bond in mol.GetBonds():
        attrs = {"id": str(obj_id), "B": str(atom_ids[bond.GetBeginAtomIdx()]), "E": str(atom_ids[bond.GetEndAtomIdx()]), "Order": _bond_order_value(bond)}
        direction = bond.GetBondDir()
        if direction == Chem.BondDir.BEGINWEDGE:
            attrs["Display"] = "WedgeBegin"
        elif direction == Chem.BondDir.BEGINDASH:
            attrs["Display"] = "WedgedHashBegin"
        elif direction == Chem.BondDir.ENDUPRIGHT:
            attrs["Display"] = "WedgeEnd"
        elif direction == Chem.BondDir.ENDDOWNRIGHT:
            attrs["Display"] = "WedgedHashEnd"
        ET.SubElement(frag, "b", attrs)
        obj_id += 1
    return obj_id, width


def _new_cdxml_root(width: float, height: float) -> tuple[ET.Element, ET.Element]:
    root = ET.Element("CDXML", {
        "BoundingBox": f"0 0 {width:.0f} {height:.0f}",
        "BondLength": f"{BOND_LENGTH:g}",
        "LabelFont": "3", "LabelSize": f"{LABEL_SIZE:g}",
        "CaptionFont": "3", "CaptionSize": f"{CAPTION_SIZE:g}",
        "LineWidth": "0.6", "BoldWidth": "2", "HashSpacing": "2.5", "MarginWidth": "1.6",
    })
    colors = ET.SubElement(root, "colortable")
    ET.SubElement(colors, "color", {"r": "1", "g": "1", "b": "1"})
    ET.SubElement(colors, "color", {"r": "0", "g": "0", "b": "0"})
    fonts = ET.SubElement(root, "fonttable")
    ET.SubElement(fonts, "font", {"id": "3", "charset": "unicode", "name": "Arial"})
    page = ET.SubElement(root, "page", {"id": "1", "BoundingBox": f"0 0 {width:.0f} {height:.0f}", "Width": f"{width:.0f}", "Height": f"{height:.0f}"})
    return root, page


def generate_single_compound_cdxml(smiles: str, title: str = "Compound") -> bytes | None:
    """Generate editable ChemDraw XML (.cdxml) for a single chemical compound."""
    mol = _prepare_mol(smiles)
    if mol is None:
        return None
    minx, maxx, miny, maxy = _mol_extent(mol)
    mol_w = max((maxx - minx) * BOND_LENGTH, BOND_LENGTH)
    mol_h = max((maxy - miny) * BOND_LENGTH, BOND_LENGTH)
    width = max(240.0, mol_w + 2 * PAGE_MARGIN)
    height = max(180.0, mol_h + 2 * PAGE_MARGIN)
    root, page = _new_cdxml_root(width, height)
    x0 = (width - mol_w) / 2.0
    _add_fragment(page, 2, mol, x0, height / 2.0)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _text_width(text: str) -> float:
    return max(10.0, 5.8 * len(text))


def _split_condensed(text: str) -> list[tuple[str, str]]:
    parts = [p for p in _BOND_SPLIT_RE.split(text or "") if p]
    out: list[tuple[str, str]] = []
    for part in parts:
        out.append(("bond" if part in {"-", "=", "#"} else "group", part))
    return out


def _add_formula_or_condensed(page: ET.Element, obj_id: int, x0: float, center_y: float, label: str) -> tuple[int, float]:
    parts = _split_condensed(label)
    if not any(kind == "bond" for kind, _ in parts):
        shown = _display_text(label)
        width = _text_width(shown)
        obj_id = _add_text(page, obj_id, x0 + width / 2, center_y + 3, shown, size=10)
        return obj_id, width
    x = x0
    for kind, token in parts:
        if kind == "group":
            shown = _display_text(token)
            width = _text_width(shown)
            obj_id = _add_text(page, obj_id, x + width / 2, center_y + 3, shown, size=10)
            x += width
            continue
        bond_width = 16.0
        x1, x2 = x + 2.0, x + bond_width - 2.0
        offsets = {"-": [0.0], "=": [-1.8, 1.8], "#": [-2.5, 0.0, 2.5]}[token]
        for dy in offsets:
            obj_id = _add_plain_line(page, obj_id, x1, center_y + dy, x2, center_y + dy)
        x += bond_width
    return obj_id, x - x0


def _display_label(smiles: str, small_as_formula: bool) -> str | None:
    if not small_as_formula:
        return None
    condensed = _condensed_for_display(smiles)
    if condensed:
        return condensed
    return formula_for_display(smiles)


def generate_reaction_cdxml(
    reactant_pairs,
    product_pairs,
    *,
    arrow_top: str = "",
    arrow_bottom: str = "",
    arrow_style: str = "forward",
    title: str = "Reaction",
    small_as_formula: bool = True,
) -> bytes | None:
    """Generate editable ChemDraw XML (.cdxml) for a reaction equation.

    Supports:
    - Reactant and product chemical fragments with full bond orders and stereo
    - Stoichiometric numeral text and '+' operators
    - Reaction arrow styles: 'forward' (FullHead), 'equilibrium' (Equilibrium),
      'reversible' (dual-headed)
    - Reaction conditions over/under arrow
    - Condensed small organics (CH2=CH2, CH3COOH, etc.) with vector bond markers
    """
    def prepare_side(pairs):
        out = []
        for coef, smiles in pairs:
            label = _display_label(smiles, small_as_formula)
            if label:
                out.append((Fraction(coef), "label", label))
            else:
                mol = _prepare_mol(smiles)
                if mol is None:
                    return None
                out.append((Fraction(coef), "mol", mol))
        return out

    reactants = prepare_side(reactant_pairs)
    products = prepare_side(product_pairs)
    if not reactants or not products:
        return None

    root, page = _new_cdxml_root(800, 260)
    obj_id = 2

    def add_side(items, x_start: float) -> float:
        nonlocal obj_id
        xcur = x_start
        for idx, (coef, kind, payload) in enumerate(items):
            ctext = _coef_text(coef)
            if ctext:
                obj_id = _add_text(page, obj_id, xcur + 4, BASELINE_Y + 3, ctext, size=10)
                xcur += 14
            if kind == "label":
                obj_id, width = _add_formula_or_condensed(page, obj_id, xcur, BASELINE_Y, payload)
            else:
                obj_id, width = _add_fragment(page, obj_id, payload, xcur, BASELINE_Y)
            xcur += width
            if idx != len(items) - 1:
                xcur += PLUS_GAP
                obj_id = _add_text(page, obj_id, xcur, BASELINE_Y + 3, "+", size=11)
                xcur += PLUS_GAP
        return xcur

    x = add_side(reactants, PAGE_MARGIN) + MOL_GAP
    arrow_x1, arrow_x2, arrow_y = x, x + ARROW_WIDTH, BASELINE_Y

    # ChemDraw arrowhead convention: BoundingBox is "end_x end_y start_x start_y"
    graphic_attrs = {
        "id": str(obj_id),
        "GraphicType": "Line",
        "BoundingBox": f"{arrow_x2:.2f} {arrow_y:.2f} {arrow_x1:.2f} {arrow_y:.2f}",
        "ArrowType": "FullHead",
        "HeadSize": "750",
    }
    arrow_style_clean = (arrow_style or "forward").lower()
    if arrow_style_clean in ("equilibrium", "eq"):
        graphic_attrs["ArrowType"] = "Equilibrium"
    elif arrow_style_clean in ("reversible", "rev", "both"):
        graphic_attrs["ArrowheadHead"] = "Full"
        graphic_attrs["ArrowheadTail"] = "Full"

    ET.SubElement(page, "graphic", graphic_attrs)
    obj_id += 1

    if arrow_top:
        obj_id = _add_text(page, obj_id, (arrow_x1 + arrow_x2) / 2, arrow_y - 18, arrow_top, size=10)
    if arrow_bottom:
        obj_id = _add_text(page, obj_id, (arrow_x1 + arrow_x2) / 2, arrow_y + 20, arrow_bottom, size=10)

    x = add_side(products, arrow_x2 + MOL_GAP)
    page_width = max(420, x + PAGE_MARGIN)
    page.set("BoundingBox", f"0 0 {page_width:.0f} 220")
    page.set("Width", f"{page_width:.0f}")
    page.set("Height", "220")
    root.set("BoundingBox", page.get("BoundingBox", "0 0 800 260"))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


# ─────────────────────────────────────────────────────────────
# Canonical MDL RXN Exporter
# ─────────────────────────────────────────────────────────────
def _lcm(a: int, b: int) -> int:
    return abs(a * b) // gcd(a, b) if a and b else 0


def _canonical_2d_mol(smiles: str) -> Chem.Mol | None:
    mol = Chem.MolFromSmiles(smiles or "")
    if mol is None:
        return None
    mol = Chem.Mol(mol)
    try:
        AllChem.Compute2DCoords(mol, canonOrient=True)
        rdDepictor.StraightenDepiction(mol)
    except Exception:
        try:
            AllChem.Compute2DCoords(mol, canonOrient=True)
        except Exception:
            return None
    return mol


def _integer_scale(reactant_pairs, product_pairs):
    coeffs = [Fraction(c) for c, _ in list(reactant_pairs) + list(product_pairs)]
    den = reduce(_lcm, (c.denominator for c in coeffs), 1)
    scaled_r = [(Fraction(c) * den, s) for c, s in reactant_pairs]
    scaled_p = [(Fraction(c) * den, s) for c, s in product_pairs]
    nums = [int(c) for c, _ in scaled_r + scaled_p]
    common = reduce(gcd, (abs(n) for n in nums if n), 0) or 1
    scaled_r = [(int(c) // common, s) for c, s in scaled_r]
    scaled_p = [(int(c) // common, s) for c, s in scaled_p]
    if any(c <= 0 for c, _ in scaled_r + scaled_p):
        return None
    return scaled_r, scaled_p


def generate_chemdraw_rxn_file(
    reactant_pairs,
    product_pairs,
    filename: str = "Reaction.rxn",
    title: str = "",
) -> io.BytesIO | None:
    """Generate an MDL RXN file preserving stable canonical 2D component coordinates."""
    scaled = _integer_scale(reactant_pairs, product_pairs)
    if scaled is None:
        return None
    reactants, products = scaled

    rxn = rdChemReactions.ChemicalReaction()
    try:
        for coeff, smiles in reactants:
            mol = _canonical_2d_mol(smiles)
            if mol is None:
                return None
            for _ in range(coeff):
                rxn.AddReactantTemplate(Chem.Mol(mol))
        for coeff, smiles in products:
            mol = _canonical_2d_mol(smiles)
            if mol is None:
                return None
            for _ in range(coeff):
                rxn.AddProductTemplate(Chem.Mol(mol))

        block = rdChemReactions.ReactionToRxnBlock(rxn)
        if title:
            lines = block.splitlines()
            if len(lines) > 1:
                lines[1] = title[:80]
                block = "\n".join(lines) + ("\n" if not block.endswith("\n") else "")

        check = rdChemReactions.ReactionFromRxnBlock(block, sanitize=True, removeHs=False)
        if check is None:
            return None
    except Exception:
        return None

    out = io.BytesIO(block.encode("utf-8"))
    out.name = filename
    out.seek(0)
    return out


# ─────────────────────────────────────────────────────────────
# CDXML Validator
# ─────────────────────────────────────────────────────────────
def validate_cdxml_bytes(data: bytes) -> dict:
    """Validate that the given bytes represent a well-formed ChemDraw XML document."""
    problems: list[str] = []
    if not data:
        return {"valid": False, "problems": ["empty data"], "format": "CDXML"}
    try:
        root = ET.fromstring(data)
    except Exception as e:
        return {"valid": False, "problems": [f"XML parsing error: {e}"], "format": "CDXML"}

    if root.tag != "CDXML":
        problems.append(f"Root element is <{root.tag}> instead of <CDXML>")

    page = root.find("page")
    if page is None:
        problems.append("Missing <page> element")

    fragments = root.findall("page/fragment")
    texts = root.findall("page/t")
    graphics = root.findall("page/graphic")

    arrows = [g for g in graphics if g.get("ArrowType") or (g.get("ArrowheadHead") and g.get("ArrowheadTail"))]

    atoms_count = sum(len(f.findall("n")) for f in fragments)
    bonds_count = sum(len(f.findall("b")) for f in fragments)

    return {
        "valid": not problems,
        "problems": problems,
        "format": "ChemDraw XML (CDXML)",
        "fragments": len(fragments),
        "atoms": atoms_count,
        "bonds": bonds_count,
        "texts": len(texts),
        "arrows": len(arrows),
        "arrow_type": arrows[0].get("ArrowType", "Reversible") if arrows else None,
        "opens_in": ["ChemDraw", "ChemDraw JS", "ChemOffice", "Revvity ChemDraw Prime/Professional"],
    }
