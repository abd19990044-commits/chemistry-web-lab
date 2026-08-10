# -*- coding: utf-8 -*-
"""
chem_core.py
============
Chemistry logic shared by all tools on the site. Ported and merged from three
source bots:

  * bot3 (RDKit drawing bot)   -> resolve_compound_to_smiles, render_molecule,
                                   render_reaction_scheme, generate_mol_file,
                                   generate_rxn_file, split_compound_list
  * bot2 (ORCA input wizard)   -> PubChem property/solubility/wikipedia
                                   lookups, mol-block -> xyz conversion,
                                   generate_orca_6_input
  * bot1 (ORCA/Kaggle runner)  -> job packaging constants reused by
                                   kaggle_runner.py (kept separate, see there)

Kept intentionally synchronous (plain `requests`) since Flask on a single
Hugging Face Space instance does not need an asyncio event loop — this
mirrors the simplification bot2's own migration notes already argued for
when it moved off Cloudflare Workers.
"""
from __future__ import annotations

import io
import logging
import math
import re
import urllib.parse
from dataclasses import dataclass
from fractions import Fraction
from enum import Enum, auto
from typing import Any

import requests
from rdkit import Chem
from rdkit.Chem import AllChem, rdMolDescriptors, rdChemReactions
from rdkit.Chem.Draw import rdMolDraw2D

logger = logging.getLogger("chemistry_tools")

PUBCHEM_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
PUBCHEM_VIEW = "https://pubchem.ncbi.nlm.nih.gov/rest/pug_view"
HTTP_TIMEOUT = 15
HEADERS = {"User-Agent": "ChemistryToolsWeb/1.0 (+huggingface-space)"}

MOL_IMAGE_SIZE = (560, 420)
REACTION_SUBIMAGE_SIZE = (420, 340)
DRAWING_BOND_LINE_WIDTH = 2
DRAWING_FONT_SCALE = 0.85

_COEFFICIENT_RE = re.compile(r"^\s*\d+(\.\d+)?\s+(.*)$")
#: A bare molecular formula and nothing else -- `H2O`, `Ca(OH)2`, `NaCl`. Used
#: to decide whether digits glued to the front of a token are a stoichiometric
#: coefficient or part of a compound name.
_BARE_FORMULA_RE = re.compile(r"^(?:\([A-Za-z0-9]+\)\d*|[A-Z][a-z]?\d*)+$")
_SMILES_CHAR_RE = re.compile(r"^[A-Za-z0-9@+\-\[\]\(\)=#$:/\\%.]+$")
_SMILES_BOND_HINT_RE = re.compile(r"[=#@\[\]\(\)]")


# ─────────────────────────────────────────────────────────────
# Input classification (name / SMILES / InChI) — from bot3
# ─────────────────────────────────────────────────────────────
class InputKind(Enum):
    NAME = auto()
    SMILES = auto()
    INCHI = auto()


@dataclass(frozen=True)
class ParsedCompound:
    raw: str
    kind: InputKind


def split_compound_list(compounds_str: str) -> list[str]:
    """Split a '+'-joined list into cleaned tokens, discarding coefficients.

    Kept for callers that only want the species. Anything that draws or writes
    a reaction should use `split_compound_terms`, which keeps the coefficient
    instead of throwing it away."""
    return [term.name for term in split_compound_terms(compounds_str)]


@dataclass(frozen=True)
class ReactionTerm:
    """One `2 H2O` term of an equation: how much, and of what."""

    coefficient: Fraction
    name: str

    @property
    def pretty_coefficient(self) -> str:
        if self.coefficient == 1:
            return ""
        if self.coefficient.denominator == 1:
            return str(self.coefficient.numerator)
        return "%d/%d" % (self.coefficient.numerator, self.coefficient.denominator)


def parse_coefficient(text: str) -> tuple[Fraction, str]:
    """Splits a leading stoichiometric coefficient off a term.

    Accepts the three ways chemists write one: `2 H2O`, `2H2O`, and the
    fractional `1/2 O2` that turns up constantly in thermochemistry. A decimal
    (`0.5 O2`) is accepted and converted exactly, because `Fraction("0.5")` is
    1/2 and not the binary float that would drift when scaled later.

    The glued form has to be handled carefully. `2H2O` is two waters, but
    `1H-pyrrole` is a name, `2-butanone` is a name, and `13C-methanol` is an
    isotope label -- reading the leading digits of any of those as a coefficient
    silently changes the chemistry. So the glued form is only accepted when
    everything after the digits is a bare formula: element symbols, counts and
    parentheses, nothing else. A hyphen or a space anywhere in the remainder
    means it is part of a name, not a formula."""
    raw = re.sub(r"\s+", " ", text or "").strip()
    if not raw:
        return Fraction(1), ""

    m = re.match(r"^(\d+)\s*/\s*(\d+)\s+(.+)$", raw)          # 1/2 O2
    if m and int(m.group(2)) != 0:
        return Fraction(int(m.group(1)), int(m.group(2))), m.group(3).strip()

    m = re.match(r"^(\d+(?:\.\d+)?)\s+(.+)$", raw)             # 2 H2O / 0.5 O2
    if m:
        return Fraction(m.group(1)), m.group(2).strip()

    m = re.match(r"^(\d+)([A-Z(].*)$", raw)                     # 2H2O, but not 2-butanone
    if m and _BARE_FORMULA_RE.match(m.group(2)):
        return Fraction(int(m.group(1))), m.group(2).strip()

    return Fraction(1), raw


def split_compound_terms(compounds_str: str) -> list[ReactionTerm]:
    """Parses `2 H2 + O2` into terms, preserving each coefficient."""
    terms: list[ReactionTerm] = []
    for part in (compounds_str or "").split("+"):
        coefficient, name = parse_coefficient(part)
        if name:
            terms.append(ReactionTerm(coefficient, name))
    return terms


def format_equation(reactant_terms, product_terms) -> str:
    """`2 H2 + O2 -> 2 H2O`, as the person wrote it, for the file header and
    for anything that needs the equation as one line of text."""
    def side(terms):
        return " + ".join(
            ("%s %s" % (t.pretty_coefficient, t.name)).strip() for t in terms)
    return "%s -> %s" % (side(reactant_terms), side(product_terms))


def scale_terms_to_integers(*sides: list[ReactionTerm]) -> tuple[int, list[list[ReactionTerm]]]:
    """Multiplies every coefficient by the smallest factor that makes them all
    whole numbers, and reports that factor.

    An MDL RXN file represents `2 H2O` by containing the structure twice, so a
    fractional coefficient cannot be written at all. Multiplying a balanced
    equation through by a constant leaves it balanced and leaves every ratio
    intact, so this is a faithful transformation -- but it changes the numbers
    the person typed, so the factor is returned and disclosed rather than
    applied silently."""
    denominators = [t.coefficient.denominator for side in sides for t in side] or [1]
    factor = 1
    for d in denominators:
        factor = factor * d // math.gcd(factor, d)
    scaled = [[ReactionTerm(t.coefficient * factor, t.name) for t in side] for side in sides]
    return factor, scaled


def detect_input_kind(token: str) -> InputKind:
    stripped = token.strip()
    if stripped.upper().startswith("INCHI="):
        return InputKind.INCHI
    if " " not in stripped and _SMILES_CHAR_RE.match(stripped):
        has_digit = any(ch.isdigit() for ch in stripped)
        if _SMILES_BOND_HINT_RE.search(stripped) or has_digit:
            return InputKind.SMILES
    return InputKind.NAME


def parse_compound(token: str) -> ParsedCompound:
    return ParsedCompound(raw=token, kind=detect_input_kind(token))


def safe_filename(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in name).strip("_")
    return cleaned[:60] or "molecule"


# ─────────────────────────────────────────────────────────────
# PubChem lookups — merged from bot2 + bot3
# ─────────────────────────────────────────────────────────────
def pubchem_smiles_by_name(name: str) -> str | None:
    return _pubchem_smiles("name", name)


def pubchem_smiles_by_inchi(inchi: str) -> str | None:
    return _pubchem_smiles("inchi", inchi)


def _pubchem_smiles(namespace: str, value: str) -> str | None:
    encoded = urllib.parse.quote(value.strip(), safe="")
    url = f"{PUBCHEM_BASE}/compound/{namespace}/{encoded}/property/CanonicalSMILES/TXT"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.text.strip().splitlines()[0].strip()
    except Exception as exc:  # noqa: BLE001
        logger.info("PubChem SMILES lookup failed for %s '%s': %s", namespace, value, exc)
        return None


def resolve_compound_to_smiles(token: str) -> str | None:
    """Resolve a raw user token (name / SMILES / InChI) to canonical SMILES."""
    parsed = parse_compound(token)
    if parsed.kind is InputKind.SMILES:
        mol = Chem.MolFromSmiles(parsed.raw)
        if mol is not None:
            return Chem.MolToSmiles(mol)
        return pubchem_smiles_by_name(parsed.raw)
    if parsed.kind is InputKind.INCHI:
        return pubchem_smiles_by_inchi(parsed.raw)
    return pubchem_smiles_by_name(parsed.raw)


#: Tokens that are a valid SMILES string AND a common formula or name meaning
#: something else. `CO` is methanol as SMILES and carbon monoxide as a formula;
#: `NO` is an aminooxy fragment as SMILES and nitric oxide as a formula. Guessing
#: here would silently draw a different molecule than the one intended, so these
#: are always resolved by name and the interpretation is reported.
AMBIGUOUS_TOKENS = {
    "CO": "carbon monoxide", "NO": "nitric oxide", "CS": "carbon monosulfide",
    "NC": "cyanide", "OS": "osmium", "SN": "tin", "PS": "phosphorus sulfide",
    "CN": "cyanide", "NS": "nitrogen sulfide", "SO": "sulfur monoxide",
    "BN": "boron nitride", "SI": "silicon", "NI": "nickel", "SC": "scandium",
}


def _formula_lookup_table():
    """Inverts the conventional-formula table, so a formula can be TYPED.

    `O2`, `H2O` and `CO2` are how most people write an equation, and none of
    them survived the old path: they are not valid SMILES (`O2` is a ring-closure
    on oxygen), so they fell through to a PubChem name lookup, which needs a
    network and does not always resolve a bare formula. The table that decides
    how to WRITE a small molecule already knows every one of these; reading it
    the other way makes them work locally and instantly."""
    table = {}
    for smiles, formula in _CONVENTIONAL_FORMULAS.items():
        mol = Chem.MolFromSmiles(smiles)
        if mol is not None:
            table.setdefault(formula.upper(), Chem.MolToSmiles(mol))
    # Written forms that differ from the tabulated one but mean the same thing.
    for alias, formula in (("H2O2", "H2O2"), ("HOH", "H2O"), ("NH3", "NH3"),
                           ("CH3OH", "CH4O"), ("HO-", "OH-"), ("OH", "OH-"),
                           ("NAOH", "NAOH"), ("NACL", "NACL")):
        if formula.upper() in table:
            table.setdefault(alias.upper(), table[formula.upper()])
    return table


#: Built on first use: the table it inverts is defined further down the file,
#: next to the drawing code that is its other consumer.
_SMILES_BY_COMMON_FORMULA: dict[str, str] | None = None


def smiles_from_common_formula(token: str) -> str | None:
    """SMILES for a common molecular formula typed by hand, or None."""
    global _SMILES_BY_COMMON_FORMULA
    if _SMILES_BY_COMMON_FORMULA is None:
        _SMILES_BY_COMMON_FORMULA = _formula_lookup_table()
    return _SMILES_BY_COMMON_FORMULA.get((token or "").strip().upper())


def resolve_species(token: str) -> tuple[str | None, str]:
    """Resolves one species and says HOW it was read. Returns (smiles, note).

    The plain `resolve_compound_to_smiles` decides between "name" and "SMILES"
    from the characters alone, and that heuristic has two holes that matter when
    someone is typing an equation rather than one compound:

      * A SMILES with no digit and no bond symbol -- `CCO` for ethanol, `CCC`
        for propane -- looked like a name and went to PubChem, so it failed
        entirely without a network and was a needless round trip with one.

      * `CO` is methanol as SMILES and carbon monoxide as a formula. Both parse.
        Silently picking one draws a different molecule than the person meant,
        which is the one failure mode that produces a confidently wrong picture.

    So: known-ambiguous tokens are resolved by name and said so; otherwise a
    name lookup is tried first and a local SMILES parse is the fallback. Either
    way the caller gets a sentence it can show, because the only safe way to
    handle an ambiguity is to make the reading visible."""
    raw = (token or "").strip()
    if not raw:
        return None, ""

    if raw.upper() in AMBIGUOUS_TOKENS and raw == raw.upper():
        meaning = AMBIGUOUS_TOKENS[raw.upper()]
        smiles = pubchem_smiles_by_name(meaning)
        if smiles:
            return smiles, ("'%s' was read as the formula for %s. Write it as SMILES "
                            "(for example '%s') if you meant the structure."
                            % (raw, meaning, raw.upper()))

    # A bare formula is checked before anything else that could misread it:
    # `O2` is not valid SMILES and a name lookup for it needs a network.
    formula_smiles = smiles_from_common_formula(raw)
    if formula_smiles:
        return formula_smiles, ""

    smiles = resolve_compound_to_smiles(raw)
    if smiles:
        return smiles, ""

    mol = Chem.MolFromSmiles(raw)
    if mol is not None:
        return Chem.MolToSmiles(mol), ("'%s' was not a name PubChem knows, so it was read "
                                       "as a SMILES string." % raw)
    return None, ""


def pubchem_reachable(timeout: float = 4.0) -> bool:
    """Is PubChem reachable from this host at all?

    Only ever called on the error path, to tell "no such compound" apart from
    "this deployment cannot reach the internet". Both used to produce the same
    message, which reads as "your molecule does not exist" and sends the person
    looking in exactly the wrong place."""
    try:
        resp = requests.get(f"{PUBCHEM_BASE}/compound/name/water/cids/JSON",
                            headers=HEADERS, timeout=timeout)
        return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def fetch_pubchem_properties(name: str) -> dict[str, Any] | None:
    """CID / IUPAC name / formula / weight / canonical SMILES for a compound name."""
    encoded = urllib.parse.quote(name.strip())
    url = (
        f"{PUBCHEM_BASE}/compound/name/{encoded}/property/"
        "CanonicalSMILES,IUPACName,MolecularFormula,MolecularWeight,Title/JSON"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            return None
        props = resp.json().get("PropertyTable", {}).get("Properties", [])
        return props[0] if props else None
    except Exception as exc:  # noqa: BLE001
        logger.info("PubChem property lookup failed for '%s': %s", name, exc)
        return None


def _extract_solubility_recursive(node, results: list[str]) -> None:
    if isinstance(node, dict):
        heading = node.get("TOCHeading", "")
        if "Solubility" in heading:
            for info in node.get("Information", []):
                value = info.get("Value", {})
                for st in value.get("StringWithMarkup", []):
                    text = st.get("String")
                    if text and text not in results:
                        results.append(text)
        for value in node.values():
            _extract_solubility_recursive(value, results)
    elif isinstance(node, list):
        for item in node:
            _extract_solubility_recursive(item, results)


def fetch_solubility(cid: int) -> list[str]:
    url = f"{PUBCHEM_VIEW}/data/compound/{cid}/JSON/"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            return []
        results: list[str] = []
        _extract_solubility_recursive(resp.json(), results)
        return results[:3]
    except Exception as exc:  # noqa: BLE001
        logger.info("Solubility lookup failed for CID %s: %s", cid, exc)
        return []


def fetch_wikipedia_summary(name: str) -> str | None:
    try:
        search_name = urllib.parse.quote(name.replace(" ", "_"))
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{search_name}"
        resp = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("type") != "disambiguation":
                extract = data.get("extract", "")
                if extract:
                    match = re.search(r"^(.*?\.)(\s|$)", extract)
                    return match.group(1) if match else extract
    except Exception as exc:  # noqa: BLE001
        logger.info("Wikipedia lookup failed for '%s': %s", name, exc)
    return None


def fetch_pubchem_png(cid: int) -> bytes | None:
    try:
        url = f"{PUBCHEM_BASE}/compound/cid/{cid}/PNG?record_type=2d&image_size=large"
        resp = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
        if resp.status_code == 200 and resp.content:
            return resp.content
    except Exception as exc:  # noqa: BLE001
        logger.info("PubChem PNG download failed for CID %s: %s", cid, exc)
    return None


def fetch_pubchem_sdf(cid: int) -> str | None:
    for record_type in ("3d", "2d"):
        try:
            url = f"{PUBCHEM_BASE}/compound/cid/{cid}/SDF?record_type={record_type}"
            resp = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
            if resp.status_code == 200 and resp.text.strip():
                return resp.text
        except Exception as exc:  # noqa: BLE001
            logger.info("SDF download failed (%s) for CID %s: %s", record_type, cid, exc)
    return None


# ─────────────────────────────────────────────────────────────
# Coordinate parsing — from bot2 (mol-block / xyz -> plain xyz body)
# ─────────────────────────────────────────────────────────────
def mol_block_to_xyz(mol_block: str) -> str | None:
    try:
        lines = mol_block.splitlines()
        if len(lines) < 4:
            return None
        count_idx = atom_count = None
        for idx in range(min(6, len(lines))):
            m = re.match(r"^\s*(\d+)\s+(\d+)\s+", lines[idx])
            if m:
                atom_count = int(m.group(1))
                count_idx = idx
                break
        if atom_count is None or count_idx is None:
            return None
        atom_lines = lines[count_idx + 1: count_idx + 1 + atom_count]
        coords = []
        for line in atom_lines:
            parts = line.split()
            if len(parts) < 4:
                continue
            x, y, z, symbol = parts[0], parts[1], parts[2], parts[3]
            coords.append(f"{symbol} {x} {y} {z}")
        return "\n".join(coords) if coords else None
    except Exception as exc:  # noqa: BLE001
        logger.info("MOL->XYZ parse error: %s", exc)
        return None


def normalize_xyz_text(text: str) -> str | None:
    try:
        lines = [ln.rstrip() for ln in text.splitlines() if ln.strip() != ""]
        if not lines:
            return None
        coords = lines[2:] if (lines[0].strip().isdigit() and len(lines) >= 2) else lines
        parsed = []
        for line in coords:
            parts = line.split()
            if len(parts) >= 4:
                parsed.append(f"{parts[0]} {parts[1]} {parts[2]} {parts[3]}")
        return "\n".join(parsed) if parsed else None
    except Exception:  # noqa: BLE001
        return None


def xyz_from_uploaded_file(raw: bytes, ext: str) -> tuple[str | None, str | None]:
    """Returns (xyz_body, error)."""
    try:
        text = raw.decode("utf-8", errors="ignore")
        ext = ext.lower()
        if ext == ".xyz":
            xyz = normalize_xyz_text(text)
            return (xyz, None) if xyz else (None, "Invalid XYZ file.")
        if ext in (".sdf", ".mol"):
            xyz = mol_block_to_xyz(text)
            return (xyz, None) if xyz else (None, "Could not parse SDF/MOL file.")
        return None, "Unsupported file format."
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def xyz_from_smiles(smiles: str) -> str | None:
    """3D-embed a SMILES with RDKit (used when PubChem has no usable SDF)."""
    try:
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return None
        mol = Chem.AddHs(mol)
        params = AllChem.ETKDGv3()
        params.randomSeed = 0xC0FFEE
        if AllChem.EmbedMolecule(mol, params) != 0:
            return None
        AllChem.MMFFOptimizeMolecule(mol, maxIters=500)
        conf = mol.GetConformer()
        lines = []
        for atom in mol.GetAtoms():
            pos = conf.GetAtomPosition(atom.GetIdx())
            lines.append(f"{atom.GetSymbol()} {pos.x:.6f} {pos.y:.6f} {pos.z:.6f}")
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        logger.info("RDKit 3D embedding failed: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────
# 2D drawing — from bot3
# ─────────────────────────────────────────────────────────────
def _apply_common_draw_options(drawer) -> None:
    opts = drawer.drawOptions()
    opts.addStereoAnnotation = True
    opts.bondLineWidth = DRAWING_BOND_LINE_WIDTH
    opts.baseFontSize = DRAWING_FONT_SCALE * (opts.baseFontSize or 0.6)
    opts.padding = 0.12
    opts.legendFontSize = 18


def render_molecule_png(smiles: str, legend: str = "", size=MOL_IMAGE_SIZE) -> bytes | None:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    try:
        AllChem.Compute2DCoords(mol)
        Chem.rdDepictor.StraightenDepiction(mol)
    except Exception:  # noqa: BLE001
        pass
    drawer = rdMolDraw2D.MolDraw2DCairo(size[0], size[1])
    _apply_common_draw_options(drawer)
    rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol, legend=legend)
    drawer.FinishDrawing()
    return drawer.GetDrawingText()


# ---------------------------------------------------------------------------
# Small molecules are written, not drawn
# ---------------------------------------------------------------------------
# A skeletal drawing of O2 is two letters joined by a line: it takes the width
# of a benzene ring to say less than "O2" does. Chemists write the small
# inorganics and the one-carbon species as formulas and draw everything else,
# so that is what this does.
#
# The formula cannot be generated from the structure, because RDKit produces the
# Hill formula and chemical convention is not Hill: ammonia is NH3 and not H3N,
# sulfuric acid is H2SO4 and not H2O4S, sodium chloride is NaCl and not ClNa.
# Whether the hydrogens are written first depends on the group of the other
# element (H2O and H2S, but NH3 and CH4) and is not derivable from
# electronegativity alone. So the conventional form is tabulated, and anything
# not in the table falls back to the Hill formula only when Hill happens to be
# right for it — otherwise it is drawn as a structure, which is never wrong.
_CONVENTIONAL_FORMULAS = {
    # diatomics and elements
    "O=O": "O2", "[H][H]": "H2", "N#N": "N2", "ClCl": "Cl2", "BrBr": "Br2",
    "II": "I2", "FF": "F2", "[O][O]": "O2", "O=[O+][O-]": "O3",
    # oxides and common gases
    "O=C=O": "CO2", "[C-]#[O+]": "CO", "O=S=O": "SO2", "O=S(=O)=O": "SO3",
    "[N]=O": "NO", "[O-][N+]=O": "NO2", "[N-]=[N+]=O": "N2O",
    # hydrides
    "O": "H2O", "OO": "H2O2", "N": "NH3", "S": "H2S", "C": "CH4",
    "Cl": "HCl", "Br": "HBr", "I": "HI", "F": "HF", "P": "PH3",
    "[SiH4]": "SiH4",
    # acids, bases, salts
    "OS(=O)(=O)O": "H2SO4", "O[N+](=O)[O-]": "HNO3", "OP(=O)(O)O": "H3PO4",
    "OC(=O)O": "H2CO3", "OS(=O)O": "H2SO3", "N#C": "HCN",
    # Plain ASCII; the renderer turns digits into subscripts and a trailing
    # charge into a superscript, so the table stays readable and greppable.
    "[OH-]": "OH-", "[H+]": "H+", "[NH4+]": "NH4+",
    "[Na+].[Cl-]": "NaCl", "[Na+].[OH-]": "NaOH", "[K+].[OH-]": "KOH",
    "[Ca+2].[O-2]": "CaO", "[Na+].[Na+].[O-]C([O-])=O": "Na2CO3",
    # one-carbon species whose Hill formula IS the conventional one
    "ClC(Cl)Cl": "CHCl3", "ClC(Cl)(Cl)Cl": "CCl4",
}


def _canonical_formula_table():
    table = {}
    for smiles, formula in _CONVENTIONAL_FORMULAS.items():
        mol = Chem.MolFromSmiles(smiles)
        if mol is not None:
            table[Chem.MolToSmiles(mol)] = formula
    return table


_FORMULA_BY_CANONICAL_SMILES = _canonical_formula_table()


def formula_for_display(smiles: str) -> str | None:
    """The formula to write instead of drawing, or None to draw the structure.

    A formula is only used where it is unambiguous and conventional. Ethanol is
    drawn, never written as C2H6O: that formula is shared with dimethyl ether,
    so writing it would discard the very information the drawing exists to
    carry."""
    mol = Chem.MolFromSmiles(smiles or "")
    if mol is None:
        return None
    canonical = Chem.MolToSmiles(mol)
    if canonical in _FORMULA_BY_CANONICAL_SMILES:
        return _FORMULA_BY_CANONICAL_SMILES[canonical]

    carbons = sum(1 for a in mol.GetAtoms() if a.GetSymbol() == "C")
    if mol.GetRingInfo().NumRings() or carbons > 1 or mol.GetNumHeavyAtoms() > 4:
        return None
    # A carbon bearing O-H or N-H is written condensed (CH3OH, not CH4O), and
    # this does not generate condensed forms, so those are drawn instead.
    if carbons and any(a.GetSymbol() in ("O", "N", "S") and a.GetTotalNumHs()
                       for a in mol.GetAtoms()):
        return None
    hill = rdMolDescriptors.CalcMolFormula(mol)
    # Hill puts hydrogen first for carbon-free species, which is only sometimes
    # the convention. If it did that and the species is not in the table above,
    # there is no way to know whether it is right, so the structure is drawn.
    if not carbons and hill.startswith("H"):
        return None
    return hill


_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)


_FORMULA_TOKEN_RE = re.compile(r"([A-Za-z]+|\d+|[+-]\d*)")


def _formula_tokens(formula: str):
    """Splits `H2SO4` / `NH4+` into (text, style) with style in
    {normal, sub, sup}, so digits render as subscripts and the charge as a
    superscript — which is the difference between a formula and a string."""
    tokens = []
    parts = [p for p in _FORMULA_TOKEN_RE.split(formula or "") if p]
    for i, part in enumerate(parts):
        if part[0] in "+-":
            tokens.append((part[1:] + part[0] if len(part) > 1 else part, "sup"))
        elif part.isdigit():
            # A digit only subscripts when it counts a preceding element.
            tokens.append((part, "sub" if i and parts[i - 1][0].isalpha() else "normal"))
        else:
            tokens.append((part, "normal"))
    return tokens


def _render_formula_image(formula: str, size: int):
    """Draws `H2O` with a real subscript, on a transparent-free white tile that
    composites with the structure images beside it."""
    from PIL import Image, ImageDraw

    base_font = _layout_font(size)
    small_font = _layout_font(max(10, int(size * 0.62)))
    tokens = _formula_tokens(formula)

    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    widths, ascent = [], 0
    for text, style in tokens:
        font = small_font if style in ("sub", "sup") else base_font
        box = probe.textbbox((0, 0), text, font=font)
        widths.append(box[2] - box[0])
        ascent = max(ascent, box[3] - box[1])

    shift = max(3, int(size * 0.20))
    pad = max(4, size // 5)
    canvas = Image.new("RGB", (sum(widths) + pad * 2, ascent + shift * 2 + pad * 2),
                       (255, 255, 255))
    pen = ImageDraw.Draw(canvas)
    x, baseline = pad, pad + shift
    for (text, style), width in zip(tokens, widths):
        font = small_font if style in ("sub", "sup") else base_font
        box = pen.textbbox((0, 0), text, font=font)
        y = baseline - box[1]
        if style == "sub":
            y += ascent - (box[3] - box[1]) + shift
        elif style == "sup":
            y -= shift
        pen.text((x - box[0], y), text, fill=(20, 20, 20), font=font)
        x += width
    return canvas


def _layout_font(size: int):
    """A scalable font for the coefficients and operators.

    The container may ship without any system fonts, so every candidate can
    miss; Pillow's bundled default is scalable and is the last resort. Falling
    back to the old fixed 11px bitmap font would make a coefficient unreadable
    next to a 340px structure, which defeats the point of drawing it."""
    from PIL import ImageFont
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                       # Pillow < 10.1: not scalable
        return ImageFont.load_default()


def _trim_white(image, pad: int = 6):
    """Crops the white margin RDKit leaves around a structure, so the pieces of
    the equation sit at a natural spacing instead of drifting apart."""
    from PIL import Image, ImageChops
    background = Image.new(image.mode, image.size, (255, 255, 255))
    box = ImageChops.difference(image, background).getbbox()
    if not box:
        return image
    left, top, right, bottom = box
    return image.crop((max(0, left - pad), max(0, top - pad),
                       min(image.width, right + pad), min(image.height, bottom + pad)))


def render_reaction_png(reactant_pairs, product_pairs, sub_size=REACTION_SUBIMAGE_SIZE,
                        small_as_formula: bool = True) -> bytes | None:
    """Draws `2 H2 + O2 -> 2 H2O` with the coefficients shown as numerals.

    Each species is drawn ONCE and labelled with its coefficient, which is how a
    chemist writes an equation. That deliberately differs from the .rxn file,
    where the same stoichiometry is expressed by repeating the structure --
    the format has no numeral field, so the two representations say the same
    thing in the only way each of them can.

    Small, unambiguous species are WRITTEN rather than drawn: a skeletal O2 is
    two letters and a line taking the width of a benzene ring, and it says less
    than "O2" does. `small_as_formula=False` draws everything, for anyone who
    wants the structures throughout.

    `*_pairs` are (coefficient, smiles).
    """
    from PIL import Image, ImageDraw

    def draw_one(smiles):
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return None
        AllChem.Compute2DCoords(mol)
        drawer = rdMolDraw2D.MolDraw2DCairo(sub_size[0], sub_size[1])
        _apply_common_draw_options(drawer)
        # One bond length for the whole equation. Without this RDKit scales each
        # structure to fill its own canvas, so H2 comes out with a bond three
        # times longer than the C-C bonds beside it — the equation reads as a
        # set of unrelated drawings rather than one scheme.
        try:
            drawer.drawOptions().fixedBondLength = bond_length
        except Exception:  # noqa: BLE001
            pass
        rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol)
        drawer.FinishDrawing()
        return _trim_white(Image.open(io.BytesIO(drawer.GetDrawingText())).convert("RGB"))

    bond_length = max(24, sub_size[1] // 7)
    formula_size = max(30, sub_size[1] // 6)
    arrow_length = max(60, sub_size[0] // 5)
    arrow_head = max(14, sub_size[1] // 20)
    # Set at the same size as a written formula: in a handwritten equation the
    # 15 in "15 O2" is the same height as the O. Sizing these off the canvas
    # instead left the numerals visibly smaller than the formulas they multiply.
    coefficient_font = _layout_font(formula_size)
    operator_font = _layout_font(formula_size)

    # (kind, payload) tokens laid out left to right.
    tokens: list[tuple[str, object]] = []
    for side_index, pairs in enumerate((reactant_pairs, product_pairs)):
        if side_index:
            tokens.append(("arrow", None))
        for i, (coefficient, smiles) in enumerate(pairs):
            if i:
                tokens.append(("op", "+"))
            formula = formula_for_display(smiles) if small_as_formula else None
            image = _render_formula_image(formula, formula_size) if formula else draw_one(smiles)
            if image is None:
                return None
            label = ReactionTerm(Fraction(coefficient), "").pretty_coefficient
            if label:
                tokens.append(("coefficient", label))
            tokens.append(("mol", image))
    if not tokens:
        return None

    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    gap = max(10, sub_size[0] // 28)
    widths, heights = [], []
    for kind, payload in tokens:
        if kind == "mol":
            widths.append(payload.width); heights.append(payload.height)
        elif kind == "arrow":
            widths.append(arrow_length); heights.append(arrow_head)
        else:
            font = coefficient_font if kind == "coefficient" else operator_font
            box = probe.textbbox((0, 0), payload, font=font)
            widths.append(box[2] - box[0]); heights.append(box[3] - box[1])

    margin = gap * 2
    total_width = sum(widths) + gap * (len(tokens) - 1) + margin * 2
    total_height = max(heights) + margin * 2
    canvas = Image.new("RGB", (total_width, total_height), (255, 255, 255))
    pen = ImageDraw.Draw(canvas)

    x = margin
    for (kind, payload), width in zip(tokens, widths):
        if kind == "mol":
            canvas.paste(payload, (x, (total_height - payload.height) // 2))
        elif kind == "arrow":
            # Drawn rather than typeset. The Unicode arrow glyph is sized for
            # running text, so beside a 200px structure it reads as an
            # afterthought — and whether a given container's fallback font even
            # has the glyph is not something worth depending on.
            mid = total_height // 2
            pen.line([(x, mid), (x + arrow_length - arrow_head, mid)],
                     fill=(20, 20, 20), width=max(2, arrow_head // 7))
            pen.polygon([(x + arrow_length, mid),
                         (x + arrow_length - arrow_head, mid - arrow_head // 2),
                         (x + arrow_length - arrow_head, mid + arrow_head // 2)],
                        fill=(20, 20, 20))
        else:
            font = coefficient_font if kind == "coefficient" else operator_font
            box = pen.textbbox((0, 0), payload, font=font)
            pen.text((x - box[0], (total_height - (box[3] - box[1])) // 2 - box[1]),
                     payload, fill=(20, 20, 20), font=font)
        # A coefficient belongs to the structure after it, so it sits closer.
        x += width + (gap // 2 if kind == "coefficient" else gap)

    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()


def generate_mol_file_bytes(smiles: str) -> bytes | None:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    AllChem.Compute2DCoords(mol)
    return Chem.MolToMolBlock(mol).encode("utf-8")


def _element_counts(smiles: str) -> tuple[dict[str, int], int] | None:
    """Atom counts (explicit hydrogens included) and net charge for one species."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    counts: dict[str, int] = {}
    charge = 0
    for atom in mol.GetAtoms():
        counts[atom.GetSymbol()] = counts.get(atom.GetSymbol(), 0) + 1
        # Hydrogens are implicit in SMILES; a balance check that ignored them
        # would call CH4 -> C + 2 H2 balanced.
        h = atom.GetTotalNumHs()
        if h:
            counts["H"] = counts.get("H", 0) + h
        charge += atom.GetFormalCharge()
    return counts, charge


def check_reaction_balance(reactant_pairs, product_pairs) -> dict:
    """Is the equation balanced, given the coefficients the person supplied?

    `*_pairs` are (coefficient, smiles). Returns a verdict with the per-element
    surplus and the charge difference, so the caller can say exactly what is
    missing instead of "unbalanced".

    This is reported, never enforced: a chemist may legitimately want to draw an
    unbalanced scheme (a fragment, a mechanism step, an unspecified counter-ion).
    Drawing it without noticing is the failure -- being told is not."""
    def side_totals(pairs):
        totals: dict[str, Fraction] = {}
        charge = Fraction(0)
        for coefficient, smiles in pairs:
            parsed = _element_counts(smiles)
            if parsed is None:
                return None, None
            counts, q = parsed
            for element, n in counts.items():
                totals[element] = totals.get(element, Fraction(0)) + Fraction(coefficient) * n
            charge += Fraction(coefficient) * q
        return totals, charge

    left, left_charge = side_totals(reactant_pairs)
    right, right_charge = side_totals(product_pairs)
    if left is None or right is None:
        return {"checked": False, "balanced": False,
                "message": "The structures could not be read, so balance was not checked."}

    diffs = {}
    for element in sorted(set(left) | set(right)):
        delta = right.get(element, Fraction(0)) - left.get(element, Fraction(0))
        if delta != 0:
            diffs[element] = delta
    charge_delta = right_charge - left_charge

    def fmt(value):
        value = Fraction(value)
        return str(value.numerator) if value.denominator == 1 else str(value)

    if not diffs and charge_delta == 0:
        return {"checked": True, "balanced": True,
                "message": "The equation is balanced: every element and the total charge "
                           "match on both sides."}

    parts = []
    for element, delta in diffs.items():
        side = "product" if delta > 0 else "reactant"
        parts.append("%s %s on the %s side" % (fmt(abs(delta)), element, side))
    if charge_delta != 0:
        parts.append("a net charge difference of %s" % fmt(charge_delta))
    return {"checked": True, "balanced": False,
            # Signed product-minus-reactant counts are what the arithmetic
            # produces, but "element_surplus: {O: -1}" reads as a surplus of
            # minus one. Both forms are reported, each named for what it is.
            "element_difference": {e: fmt(d) for e, d in diffs.items()},
            "excess_side": {e: ("product" if d > 0 else "reactant")
                            for e, d in diffs.items()},
            "excess_amount": {e: fmt(abs(d)) for e, d in diffs.items()},
            "charge_difference": fmt(charge_delta),
            "message": "The equation is NOT balanced — there is an excess of "
                       + "; ".join(parts) + ". The scheme was still drawn, in case that "
                       "is what you intended."}


def validate_rxn_block(text: str) -> dict:
    """Structurally validates an MDL RXN block before it is offered as a download.

    The point is to be able to say something true about the file. ChemDraw is not
    installed here and cannot be, so "opens in ChemDraw" can only ever be an
    inference -- but "this is a well-formed MDL RXN V2000 file, and that is the
    format ChemDraw imports" IS checkable, and it is checked here: the $RXN
    header and its four lines, a counts line whose two numbers match the number
    of $MOL blocks that follow, and every one of those blocks carrying a V2000
    counts line and terminating with `M  END`.

    A malformed file is far more likely than a ChemDraw that cannot read a valid
    one, so this is the check worth making."""
    problems: list[str] = []
    lines = (text or "").splitlines()
    if not lines or lines[0].strip() != "$RXN":
        return {"valid": False, "problems": ["the file does not begin with the $RXN header"]}
    if len(lines) < 5:
        return {"valid": False, "problems": ["the $RXN header is incomplete"]}

    try:
        n_reactants = int(lines[4][0:3])
        n_products = int(lines[4][3:6])
    except (ValueError, IndexError):
        return {"valid": False,
                "problems": ["the reactant/product counts line is not in MDL fixed-column form"]}

    blocks, current = [], None
    for line in lines[5:]:
        if line.strip() == "$MOL":
            if current is not None:
                blocks.append(current)
            current = []
        elif current is not None:
            current.append(line)
    if current is not None:
        blocks.append(current)

    if len(blocks) != n_reactants + n_products:
        problems.append("the header declares %d components but the file contains %d"
                        % (n_reactants + n_products, len(blocks)))
    for i, block in enumerate(blocks, 1):
        if not any(l.rstrip().endswith(("V2000", "V3000")) for l in block[:6]):
            problems.append("component %d has no V2000/V3000 counts line" % i)
        if not any(l.strip() == "M  END" for l in block):
            problems.append("component %d is not terminated with 'M  END'" % i)

    return {
        "valid": not problems,
        "problems": problems,
        "format": "MDL RXN V2000",
        "reactants": n_reactants,
        "products": n_products,
        "opens_in": ["ChemDraw", "ChemDraw JS", "MarvinSketch", "ISIS/Draw", "Avogadro",
                     "RDKit", "Open Babel"],
    }


def generate_rxn_file_bytes(reactant_pairs, product_pairs, title: str = "") -> bytes | None:
    """Writes an MDL RXN V2000 file, repeating each species by its coefficient.

    The RXN format has no field for a stoichiometric coefficient; the convention
    every reader understands is that `2 H2O` means the structure appears twice.
    So ChemDraw will show two water molecules rather than the text "2 H2O" --
    the stoichiometry is chemically present and machine-readable, just not
    rendered as a numeral. Coefficients must therefore be whole numbers by the
    time they reach here; `scale_terms_to_integers` is what guarantees that.
    """
    def expand(pairs):
        out = []
        for coefficient, smiles in pairs:
            n = Fraction(coefficient)
            if n.denominator != 1 or n <= 0:
                return None
            out.extend([smiles] * int(n))
        return out

    left, right = expand(reactant_pairs), expand(product_pairs)
    if not left or not right:
        return None
    rxn = rdChemReactions.ReactionFromSmarts("%s>>%s" % (".".join(left), ".".join(right)),
                                             useSmiles=True)
    if rxn is None:
        return None
    rdChemReactions.Compute2DCoordsForReaction(rxn)
    block = rdChemReactions.ReactionToRxnBlock(rxn)

    if title:
        # Line 2 of the $RXN header is the reaction name; readers preserve it and
        # ChemDraw shows it, so the equation as it was typed travels with the file.
        lines = block.split("\n")
        if len(lines) > 2:
            lines[1] = title[:80]
            block = "\n".join(lines)
    return block.encode("utf-8")


# ─────────────────────────────────────────────────────────────
# ORCA 6 input generation — ported verbatim (logic) from bot2
# No silent "smart" additions: generates exactly what the user picked.
# ─────────────────────────────────────────────────────────────
CALC_TYPES = {
    "sp": "Single Point (SP)",
    "opt": "Geometry Optimization (Opt)",
    "opt freq": "Opt + Frequencies (Opt Freq)",
    "optts": "Transition State (OptTS)",
    "tddft": "TD-DFT (UV-Vis)",
    "custom": "⌨️ Custom Command Line",
}
COMPOSITE_METHODS = ["r2SCAN-3C", "B97-3C", "PBEh-3C"]
DFT_FUNCTIONALS = ["B3LYP", "CAM-B3LYP", "PBE0", "wB97M-V", "M062X", "wB97X-D4", "PBE", "BP86"]
# The correlation-fitting basis must MATCH the orbital basis. These used to
# hardcode "def2-tzvp/c", so picking def2-QZVP or cc-pVTZ produced a mismatched
# /C auxiliary basis: ORCA runs without complaining and the RI error quietly
# exceeds the method's own accuracy. AutoAux derives the right auxiliary basis
# from whatever orbital basis the person chose, which is what ORCA recommends
# when a matching /C set is not being named explicitly.
MP2_VARIANTS = ["MP2", "RI-MP2", "DLPNO-MP2"]
CCSD_VARIANTS = ["CCSD", "CCSD(T)", "DLPNO-CCSD", "DLPNO-CCSD(T)"]
#: Methods that need a /C auxiliary basis; AutoAux is added for them.
_NEEDS_AUX_C = ("RI-MP2", "DLPNO-MP2", "DLPNO-CCSD", "DLPNO-CCSD(T)")
#: X2C requires a relativistically recontracted basis. Pairing it with def2 or
#: cc-pV* is a methodological error ORCA will not warn about.
X2C_BASIS = {
    "def2-SVP": "x2c-SVPall", "def2-TZVP": "x2c-TZVPall",
    "def2-TZVPP": "x2c-TZVPPall", "def2-QZVP": "x2c-QZVPall",
    "def2-TZVPD": "x2c-TZVPall", "ma-def2-TZVP": "x2c-TZVPall",
}
RI_OPTIONS = {"none": "No RI Acceleration", "rijcosx": "RIJCOSX (Hybrids)", "rijk": "RIJK (HF/DFT)", "ri": "RI (Standard)"}
BASIS_MAP = {
    "def2": ["def2-SVP", "def2-TZVP", "def2-TZVPD", "ma-def2-TZVP", "def2-TZVPP", "def2-QZVP"],
    "dunning": ["cc-pVDZ", "cc-pVTZ", "aug-cc-pVTZ"],
    "pople": ["6-31G(d)", "6-311G(d,p)", "6-31+G(d,p)", "6-311++G(d,p)"],
}
DISPERSION_MODELS = {"none": "None", "D3": "D3", "D3bj": "D3BJ", "D4": "D4"}
SOLVATION_MODELS = {"none": "None (Gas Phase)", "cpcm": "CPCM", "smd": "SMD"}
SOLVENTS = ["Water", "Ethanol", "Methanol", "Acetone", "Chloroform", "DCM", "DMSO", "THF", "Toluene"]


def generate_orca_6_input(d: dict) -> str:
    input_text = "# ORCA 6.1 Input generated by Chemistry Lab\n"

    if d.get("custom_line"):
        input_text += f"{d['custom_line']}\n\n"
    else:
        calc_cmd = {"sp": "SP", "opt": "Opt", "opt freq": "Opt Freq", "optts": "OptTS", "tddft": "SP"}.get(
            d.get("calc_type", "sp"), "SP"
        )
        solv_str = f"{d['solv_model'].upper()}({d.get('solvent', 'Water')})" if d.get("solv_model", "none") != "none" else ""
        acc_part = f"{d['ri_type'].upper()} AutoAux" if d.get("ri_type", "none") != "none" else ""
        disp_part = d["disp"].upper() if d.get("disp", "none") != "none" else ""
        method = d.get("theory", "")
        basis = d.get("basis", "") if d.get("family") != "f_comp" else ""

        x2c, notes = "", []
        if d.get("x2c"):
            x2c = "X2C"
            swapped = X2C_BASIS.get(basis)
            if swapped:
                notes.append("# X2C needs a relativistically recontracted basis; "
                             "%s was substituted for %s." % (swapped, basis))
                basis = swapped
            elif basis:
                notes.append("# WARNING: X2C is a relativistic Hamiltonian and %s is not a "
                             "relativistically recontracted basis. Use an x2c-* or SARC set."
                             % basis)
            else:
                # A 3c composite method ships its own, non-relativistic, heavily
                # parameterised basis and its own dispersion and BSSE
                # corrections. Those parameters were fitted without a
                # relativistic Hamiltonian, so combining one with X2C is outside
                # the range the method was defined for -- and the basis cannot
                # be swapped without ceasing to be that method.
                notes.append("# WARNING: X2C was requested with a composite (3c) method. "
                             "Composite methods carry their own non-relativistic basis and "
                             "fitted corrections, so they are not defined with a relativistic "
                             "Hamiltonian. Use an explicit functional with an x2c-* or SARC "
                             "basis instead.")
        if any(method.upper().startswith(m) for m in _NEEDS_AUX_C) and "autoaux" not in acc_part.lower():
            # Derived from the chosen orbital basis rather than hardcoded.
            acc_part = (acc_part + " AutoAux").strip()

        keywords = ["!", method, basis, disp_part, acc_part, solv_str, calc_cmd, x2c]
        keywords = [k for k in keywords if str(k).strip()]
        input_text += " ".join(keywords) + "\n"
        for n in notes:
            input_text += n + "\n"
        input_text += "\n"

    input_text += f"%pal nprocs {d.get('cores', 4)} end\n"
    input_text += f"%maxcore {d.get('ram', 6000)}\n\n"

    if d.get("calc_type") == "tddft":
        input_text += f"%tddft\n   nroots {d.get('nroots', 10)}\nend\n\n"

    input_text += f"* xyz {d.get('charge', 0)} {d.get('mult', 1)}\n"
    input_text += f"{d.get('coords', '')}\n"
    input_text += "*\n"
    return input_text
