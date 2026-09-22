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
Hugging Face Space instance does not need an asyncio event loop - this
mirrors the simplification bot2's own migration notes already argued for
when it moved off Cloudflare Workers.
"""
from __future__ import annotations

import concurrent.futures
import io
import json
import logging
import math
import os
import re
import threading
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

# Concurrency and resource limits for UFF builder
UFF_MAX_CONCURRENCY = int(os.environ.get("UFF_MAX_CONCURRENCY", "4"))
UFF_MAX_ATOMS = int(os.environ.get("UFF_MAX_ATOMS", "350"))
UFF_TIMEOUT_SECONDS = float(os.environ.get("UFF_TIMEOUT_SECONDS", "8.0"))

_uff_semaphore = threading.BoundedSemaphore(UFF_MAX_CONCURRENCY)
_uff_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=UFF_MAX_CONCURRENCY,
    thread_name_prefix="uff_worker"
)

PUBCHEM_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
PUBCHEM_VIEW = "https://pubchem.ncbi.nlm.nih.gov/rest/pug_view"
HTTP_TIMEOUT = 15
HEADERS = {"User-Agent": "ChemistryToolsWeb/1.0 (+huggingface-space)"}

MOL_IMAGE_SIZE = (3600, 2700)
REACTION_SUBIMAGE_SIZE = (2520, 2040)
RENDER_DPI = 600
DRAWING_BOND_LINE_WIDTH = 4
DRAWING_FONT_SCALE = 0.85

_COEFFICIENT_RE = re.compile(r"^\s*\d+(\.\d+)?\s+(.*)$")
#: A bare molecular formula and nothing else -- `H2O`, `Ca(OH)2`, `NaCl`. Used
#: to decide whether digits glued to the front of a token are a stoichiometric
#: coefficient or part of a compound name.
_BARE_FORMULA_RE = re.compile(r"^(?:\([A-Za-z0-9]+\)\d*|[A-Z][a-z]?\d*)+$")
_SMILES_CHAR_RE = re.compile(r"^[A-Za-z0-9@+\-\[\]\(\)=#$:/\\%.]+$")
_SMILES_BOND_HINT_RE = re.compile(r"[=#@\[\]\(\)]")


# ─────────────────────────────────────────────────────────────
# Input classification (name / SMILES / InChI) - from bot3
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
        # Also check if it parses as a valid multi-atom SMILES in RDKit
        # (e.g. CCO, CCC, c1ccccc1, NC=O)
        mol = Chem.MolFromSmiles(stripped)
        if mol is not None and mol.GetNumAtoms() > 1:
            return InputKind.SMILES
    return InputKind.NAME


def parse_compound(token: str) -> ParsedCompound:
    return ParsedCompound(raw=token, kind=detect_input_kind(token))


def safe_filename(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in name).strip("_")
    return cleaned[:60] or "molecule"


NIH_CIR_BASE = "https://cactus.nci.nih.gov/chemical/structure"
OPSIN_BASE = "https://opsin.ch.cam.ac.uk/opsin"

_IUPAC_HINT_RE = re.compile(
    r"(\b\d+,\d+|\b\d+-[a-zA-Z]|\([0-9a-zA-Z,\-±\+ ]+\)|-(?:yl|oic|oate|amide|amine|ol|one|al|ene|yne|ane)\b|"
    r"\b(?:methyl|ethyl|propyl|butyl|pentyl|hexyl|phenyl|benzyl|acetyl|hydroxy|chloro|bromo|fluoro|iodo|"
    r"nitro|amino|cyclo|oxazo|pyridi|purin|pyrimidin|carboxyl|benzo)\b)",
    re.IGNORECASE
)


def _resolve_fallback_smiles(name: str) -> str | None:
    """Resolve chemical name via OPSIN (IUPAC name parser) or NIH Chemical Identifier Resolver (CIR)."""
    clean = (name or "").strip()
    if not clean:
        return None
    enc = urllib.parse.quote(clean, safe="")

    # 1. Try OPSIN (IUPAC systematic nomenclature parser)
    try:
        url = f"{OPSIN_BASE}/{enc}.json"
        resp = requests.get(url, headers=HEADERS, timeout=6.0)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("smiles"):
                mol = Chem.MolFromSmiles(data["smiles"])
                if mol is not None:
                    return Chem.MolToSmiles(mol)
    except Exception as exc:
        logger.debug("OPSIN lookup failed for '%s': %s", clean, exc)

    # 2. Try NIH CADD Chemical Identifier Resolver
    try:
        url = f"{NIH_CIR_BASE}/{enc}/smiles"
        resp = requests.get(url, headers=HEADERS, timeout=6.0)
        if resp.status_code == 200 and resp.text.strip():
            sm = resp.text.strip()
            mol = Chem.MolFromSmiles(sm)
            if mol is not None:
                return Chem.MolToSmiles(mol)
    except Exception as exc:
        logger.debug("NIH CIR SMILES lookup failed for '%s': %s", clean, exc)

    return None


def pubchem_smiles_by_name(name: str) -> str | None:
    formula_smiles = smiles_from_common_formula(name)
    if formula_smiles:
        return formula_smiles

    clean_name = (name or "").strip()
    # If the token exhibits IUPAC systematic nomenclature characteristics, try OPSIN first for 100% precision
    if _IUPAC_HINT_RE.search(clean_name):
        fallback = _resolve_fallback_smiles(clean_name)
        if fallback:
            return fallback

    res = _pubchem_smiles("name", clean_name)
    if res:
        return res
    return _resolve_fallback_smiles(clean_name)


def pubchem_smiles_by_inchi(inchi: str) -> str | None:
    res = _pubchem_smiles("inchi", inchi)
    if res:
        return res
    return _resolve_fallback_smiles(inchi)


def _pubchem_smiles(namespace: str, value: str) -> str | None:
    encoded = urllib.parse.quote(value.strip(), safe="")
    url = f"{PUBCHEM_BASE}/compound/{namespace}/{encoded}/property/CanonicalSMILES/TXT"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=6.0)
        resp.raise_for_status()
        return resp.text.strip().splitlines()[0].strip()
    except Exception as exc:  # noqa: BLE001
        logger.info("PubChem SMILES lookup failed for %s '%s': %s", namespace, value, exc)
        return None


def resolve_compound_to_smiles(token: str) -> str | None:
    """Resolve a raw user token (name / SMILES / InChI / formula) to canonical SMILES."""
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


def is_iupac_name(name: str) -> bool:
    """Detect whether a chemical name exhibits IUPAC systematic locants or affixes."""
    if not name or not isinstance(name, str):
        return False
    return bool(_IUPAC_HINT_RE.search(name.strip()))


def _formula_lookup_table():
    """Inverts the conventional-formula table, so a formula or common chemical name can be TYPED locally."""
    table = {}
    for smiles, formula in _CONVENTIONAL_FORMULAS.items():
        mol = Chem.MolFromSmiles(smiles)
        if mol is not None:
            table.setdefault(formula.upper(), Chem.MolToSmiles(mol))

    # Extended common names, IUPAC aliases, and formulas
    common_aliases = {
        # Common Bioactive & Medicinal Compounds
        "CAFFEINE": "Cn1cnc2c1c(=O)n(C)c(=O)n2C",
        "ASPIRIN": "CC(=O)Oc1ccccc1C(=O)O", "2-(ACETYLOXY)BENZOIC ACID": "CC(=O)Oc1ccccc1C(=O)O", "ACETYLSALICYLIC ACID": "CC(=O)Oc1ccccc1C(=O)O",
        "PARACETAMOL": "CC(=O)Nc1ccc(O)cc1", "ACETAMINOPHEN": "CC(=O)Nc1ccc(O)cc1", "4-ACETAMIDOPHENOL": "CC(=O)Nc1ccc(O)cc1",
        "NAPROXEN": "CC(c1ccc2cc(OC)ccc2c1)C(=O)O",
        "IBUPROFEN": "CC(C)Cc1ccc(C(C)C(=O)O)cc1",
        "UREA": "NC(=O)N", "CH4N2O": "NC(=O)N",
        # Water & gases
        "WATER": "O", "H2O": "O", "HOH": "O", "OXYGEN": "O=O", "O2": "O=O",
        "HYDROGEN": "[H][H]", "H2": "[H][H]", "NITROGEN": "N#N", "N2": "N#N",
        "CHLORINE": "ClCl", "CL2": "ClCl", "BROMINE": "BrBr", "BR2": "BrBr",
        "IODINE": "II", "I2": "II", "FLUORINE": "FF", "F2": "FF",
        "CARBON DIOXIDE": "O=C=O", "CO2": "O=C=O", "CARBON MONOXIDE": "[C-]#[O+]", "CO": "[C-]#[O+]",
        "METHANE": "C", "CH4": "C", "AMMONIA": "N", "NH3": "N", "OZONE": "O=[O+][O-]", "O3": "O=[O+][O-]",
        "NITRIC OXIDE": "[N]=O", "NO": "[N]=O", "NITROGEN DIOXIDE": "[O-][N+]=O", "NO2": "[O-][N+]=O",
        "NITROUS OXIDE": "[N-]=[N+]=O", "N2O": "[N-]=[N+]=O", "SULFUR DIOXIDE": "O=S=O", "SO2": "O=S=O",
        "SULFUR TRIOXIDE": "O=S(=O)=O", "SO3": "O=S(=O)=O", "HYDROGEN SULFIDE": "S", "H2S": "S",
        # Acids & bases
        "HYDROCHLORIC ACID": "Cl", "HCL": "Cl", "HYDROBROMIC ACID": "Br", "HBR": "Br",
        "HYDROIODIC ACID": "I", "HI": "I", "HYDROFLUORIC ACID": "F", "HF": "F",
        "SULFURIC ACID": "OS(=O)(=O)O", "H2SO4": "OS(=O)(=O)O",
        "NITRIC ACID": "O[N+](=O)[O-]", "HNO3": "O[N+](=O)[O-]",
        "PHOSPHORIC ACID": "OP(=O)(O)O", "H3PO4": "OP(=O)(O)O",
        "CARBONIC ACID": "OC(=O)O", "H2CO3": "OC(=O)O",
        "ACETIC ACID": "CC(=O)O", "CH3COOH": "CC(=O)O", "ACOH": "CC(=O)O",
        "FORMIC ACID": "C(=O)O", "HCOOH": "C(=O)O",
        "SODIUM HYDROXIDE": "[Na+].[OH-]", "NAOH": "[Na+].[OH-]",
        "POTASSIUM HYDROXIDE": "[K+].[OH-]", "KOH": "[K+].[OH-]",
        "LITHIUM HYDROXIDE": "[Li+].[OH-]", "LIOH": "[Li+].[OH-]",
        "CALCIUM OXIDE": "[Ca+2].[O-2]", "CAO": "[Ca+2].[O-2]",
        "SODIUM CHLORIDE": "[Na+].[Cl-]", "NACL": "[Na+].[Cl-]",
        "POTASSIUM CHLORIDE": "[K+].[Cl-]", "KCL": "[K+].[Cl-]",
        "SODIUM CARBONATE": "[Na+].[Na+].[O-]C([O-])=O", "NA2CO3": "[Na+].[Na+].[O-]C([O-])=O",
        "SODIUM BICARBONATE": "[Na+].[O-]C(=O)O", "NAHCO3": "[Na+].[O-]C(=O)O",
        "CALCIUM CARBONATE": "[Ca+2].[O-]C([O-])=O", "CACO3": "[Ca+2].[O-]C([O-])=O",
        # Solvents & Reagents
        "METHANOL": "CO", "CH3OH": "CO", "MEOH": "CO",
        "ETHANOL": "CCO", "C2H5OH": "CCO", "ETOH": "CCO",
        "PROPANOL": "CCCO", "1-PROPANOL": "CCCO", "ISOPROPANOL": "CC(C)O", "IPA": "CC(C)O", "2-PROPANOL": "CC(C)O",
        "ACETONE": "CC(=O)C", "CH3COCH3": "CC(=O)C",
        "DIETHYL ETHER": "CCOCC", "ETHER": "CCOCC", "ET2O": "CCOCC",
        "TETRAHYDROFURAN": "C1CCOC1", "THF": "C1CCOC1",
        "DIMETHYLFORMAMIDE": "CN(C)C=O", "DMF": "CN(C)C=O",
        "DIMETHYLSULFOXIDE": "CS(=O)C", "DMSO": "CS(=O)C",
        "DICHLOROMETHANE": "ClCCl", "DCM": "ClCCl", "CH2CL2": "ClCCl", "METHYLENE CHLORIDE": "ClCCl",
        "CHLOROFORM": "ClC(Cl)Cl", "CHCL3": "ClC(Cl)Cl",
        "CARBON TETRACHLORIDE": "ClC(Cl)(Cl)Cl", "CCL4": "ClC(Cl)(Cl)Cl",
        "ETHYL ACETATE": "CCOC(=O)C", "ETOAC": "CCOC(=O)C",
        "BENZENE": "c1ccccc1", "C6H6": "c1ccccc1", "PHH": "c1ccccc1",
        "TOLUENE": "Cc1ccccc1", "C7H8": "Cc1ccccc1", "PHME": "Cc1ccccc1",
        "PHENOL": "Oc1ccccc1", "PHOH": "Oc1ccccc1",
        "ANILINE": "Nc1ccccc1", "PHNH2": "Nc1ccccc1",
        "ETHENE": "C=C", "ETHYLENE": "C=C", "C2H4": "C=C",
        "ETHYNE": "C#C", "ACETYLENE": "C#C", "C2H2": "C#C",
        "ETHANE": "CC", "C2H6": "CC", "PROPANE": "CCC", "C3H8": "CCC",
        "BUTANE": "CCCC", "C4H10": "CCCC",
        # Ions
        "OH-": "[OH-]", "HO-": "[OH-]", "HYDROXIDE": "[OH-]",
        "H+": "[H+]", "PROTON": "[H+]", "H3O+": "[OH3+]", "HYDRONIUM": "[OH3+]",
        "NH4+": "[NH4+]", "AMMONIUM": "[NH4+]",
        "CL-": "[Cl-]", "CHLORIDE": "[Cl-]",
        "BR-": "[Br-]", "BROMIDE": "[Br-]",
        "I-": "[I-]", "IODIDE": "[I-]",
        "NO3-": "[O-][N+](=O)[O-]", "NITRATE": "[O-][N+](=O)[O-]",
        "SO4^2-": "[O-]S(=O)(=O)[O-]", "SO4-2": "[O-]S(=O)(=O)[O-]", "SULFATE": "[O-]S(=O)(=O)[O-]",
    }
    for alias, smiles in common_aliases.items():
        mol = Chem.MolFromSmiles(smiles)
        if mol is not None:
            table[alias.upper()] = Chem.MolToSmiles(mol)
    return table


_SMILES_BY_COMMON_FORMULA: dict[str, str] | None = None


def smiles_from_common_formula(token: str) -> str | None:
    global _SMILES_BY_COMMON_FORMULA
    if _SMILES_BY_COMMON_FORMULA is None:
        _SMILES_BY_COMMON_FORMULA = _formula_lookup_table()
    return _SMILES_BY_COMMON_FORMULA.get((token or "").strip().upper())


def resolve_species(token: str) -> tuple[str | None, str]:
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
    try:
        resp = requests.get(f"{PUBCHEM_BASE}/compound/name/water/cids/JSON",
                            headers=HEADERS, timeout=timeout)
        if resp.status_code == 200:
            return True
    except Exception:
        pass
    # If PubChem is busy or rate-limited, check NIH CIR resolver
    try:
        resp = requests.get(f"{NIH_CIR_BASE}/water/smiles",
                            headers=HEADERS, timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False


def fetch_iupac_name(query: str, smiles: str | None = None) -> str:
    """Fetch authoritative IUPAC systematic name from PubChem or NIH CIR."""
    clean = (query or "").strip()
    if is_iupac_name(clean):
        return clean
    for q in (clean, smiles):
        if not q:
            continue
        try:
            enc = urllib.parse.quote(q.strip())
            resp = requests.get(f"{NIH_CIR_BASE}/{enc}/iupac_name", headers=HEADERS, timeout=4.0)
            if resp.status_code == 200 and resp.text.strip():
                text = resp.text.strip()
                if not text.startswith("<h1>") and "HTML" not in text and "<" not in text and "\n" not in text:
                    return text
        except Exception:
            pass
    return ""


def fetch_pubchem_properties(name: str) -> dict[str, Any] | None:
    encoded = urllib.parse.quote(name.strip())
    url = (
        f"{PUBCHEM_BASE}/compound/name/{encoded}/property/"
        "CanonicalSMILES,IUPACName,MolecularFormula,MolecularWeight,Title/JSON"
    )
    props_dict: dict[str, Any] | None = None
    try:
        resp = requests.get(url, headers=HEADERS, timeout=6.0)
        if resp.status_code == 200:
            props = resp.json().get("PropertyTable", {}).get("Properties", [])
            if props:
                props_dict = props[0]
    except Exception as exc:
        logger.info("PubChem property lookup failed for '%s': %s", name, exc)

    # If name lookup failed, and it might be a SMILES, try PubChem compound/smiles endpoint
    if not props_dict:
        try:
            smiles_enc = urllib.parse.quote(name.strip())
            url_smiles = (
                f"{PUBCHEM_BASE}/compound/smiles/{smiles_enc}/property/"
                "CanonicalSMILES,IUPACName,MolecularFormula,MolecularWeight,Title/JSON"
            )
            resp = requests.get(url_smiles, headers=HEADERS, timeout=6.0)
            if resp.status_code == 200:
                props = resp.json().get("PropertyTable", {}).get("Properties", [])
                if props:
                    props_dict = props[0]
        except Exception:
            pass

    # If props was found, ensure IUPACName is populated if missing
    if props_dict:
        if not props_dict.get("IUPACName"):
            iupac = fetch_iupac_name(name, props_dict.get("CanonicalSMILES"))
            if iupac:
                props_dict["IUPACName"] = iupac
        return props_dict

    # Fallback: synthesize properties from resolved SMILES + RDKit
    try:
        smiles = resolve_compound_to_smiles(name)
        if smiles:
            mol = Chem.MolFromSmiles(smiles)
            if mol is not None:
                from rdkit.Chem import Descriptors
                formula = rdMolDescriptors.CalcMolFormula(mol)
                mw = round(Descriptors.MolWt(mol), 3)
                can_smiles = Chem.MolToSmiles(mol)
                iupac = fetch_iupac_name(name, can_smiles) or (name if is_iupac_name(name) else "")
                return {
                    "CanonicalSMILES": can_smiles,
                    "IUPACName": iupac,
                    "MolecularFormula": formula,
                    "MolecularWeight": str(mw),
                    "Title": name.capitalize(),
                }
    except Exception as exc:
        logger.info("Fallback property calculation failed for '%s': %s", name, exc)

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
    except Exception as exc:
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
    except Exception as exc:
        logger.info("Wikipedia lookup failed for '%s': %s", name, exc)
    return None


def fetch_pubchem_png(cid: int) -> bytes | None:
    try:
        url = f"{PUBCHEM_BASE}/compound/cid/{cid}/PNG?record_type=2d&image_size=large"
        resp = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
        if resp.status_code == 200 and resp.content:
            return resp.content
    except Exception as exc:
        logger.info("PubChem PNG download failed for CID %s: %s", cid, exc)
    return None


def fetch_pubchem_sdf(cid: int | None, name: str = "") -> str | None:
    if cid:
        for record_type in ("3d", "2d"):
            try:
                url = f"{PUBCHEM_BASE}/compound/cid/{cid}/SDF?record_type={record_type}"
                resp = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
                if resp.status_code == 200 and resp.text.strip():
                    return resp.text
            except Exception as exc:
                logger.info("SDF download failed (%s) for CID %s: %s", record_type, cid, exc)
    if name:
        try:
            enc = urllib.parse.quote(name.strip())
            url = f"{NIH_CIR_BASE}/{enc}/sdf?get3d=true"
            resp = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
            if resp.status_code == 200 and resp.text.strip():
                return resp.text
        except Exception as exc:
            logger.info("NIH CIR 3D SDF download failed for '%s': %s", name, exc)
    return None


# ─────────────────────────────────────────────────────────────
# Coordinate parsing - from bot2 (mol-block / xyz -> plain xyz body)
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
    except Exception as exc:
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
    except Exception:
        return None


def xyz_from_uploaded_file(raw: bytes, ext: str) -> tuple[str | None, str | None]:
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
    except Exception as exc:
        return None, str(exc)


def xyz_from_smiles(smiles: str) -> str | None:
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
    except Exception as exc:
        logger.info("RDKit 3D embedding failed: %s", exc)
        return None


VDW_RADII: dict[str, float] = {
    "H": 1.20, "He": 1.40, "Li": 1.82, "Be": 1.53, "B": 1.92, "C": 1.70,
    "N": 1.55, "O": 1.52, "F": 1.47, "Ne": 1.54, "Na": 2.27, "Mg": 1.73,
    "Al": 1.84, "Si": 2.10, "P": 1.80, "S": 1.80, "Cl": 1.75, "Ar": 1.88,
    "K": 2.75, "Ca": 2.31, "Br": 1.85, "I": 1.98,
}


def calculate_safe_collision_free_offset(
    existing_atoms: list[dict[str, Any]],
    new_atoms: list[dict[str, Any]],
    initial_offset: float = 5.0,
    min_clearance: float = 2.8
) -> float:
    """Calculate an offset along X such that no two atoms across fragments are closer than min_clearance."""
    if not existing_atoms or not new_atoms:
        return 0.0

    max_x1 = max(a["x"] for a in existing_atoms)
    min_x2 = min(a["x"] for a in new_atoms)
    offset_x = (max_x1 - min_x2) + initial_offset

    for _ in range(20):
        collision = False
        for a1 in existing_atoms:
            for a2 in new_atoms:
                dx = (a2["x"] + offset_x) - a1["x"]
                dy = a2["y"] - a1["y"]
                dz = a2["z"] - a1["z"]
                dist = (dx * dx + dy * dy + dz * dz) ** 0.5
                r1 = VDW_RADII.get(a1["elem"], 1.70)
                r2 = VDW_RADII.get(a2["elem"], 1.70)
                safe_thresh = max(min_clearance, (r1 + r2) * 0.75)
                if dist < safe_thresh:
                    collision = True
                    offset_x += 1.0
                    break
            if collision:
                break
        if not collision:
            break

    return offset_x


def _run_uff_worker(
    atoms_data: list[tuple[str, float, float, float]],
    max_iters: int,
    charge: int = 0
) -> tuple[str | None, str | None]:
    n_atoms = len(atoms_data)
    if n_atoms == 0:
        return None, "No coordinates provided."

    # 1. Construct RDKit molecule from 3D XYZ representation
    xyz_lines = [f"{n_atoms}", "ORCA Web Lab Builder Model"]
    for elem, x, y, z in atoms_data:
        xyz_lines.append(f"{elem} {x:.6f} {y:.6f} {z:.6f}")
    xyz_block = "\n".join(xyz_lines)

    raw_mol = Chem.MolFromXYZBlock(xyz_block)
    if raw_mol is None:
        return None, "Failed to parse 3D coordinates into molecular structure."

    mol = Chem.Mol(raw_mol)
    bonds_perceived = False

    # 2. Chemical perception via rdDetermineBonds (perceives hybridization, valence, and bond orders)
    try:
        from rdkit.Chem import rdDetermineBonds
        try:
            rdDetermineBonds.DetermineBonds(mol, charge=charge)
            bonds_perceived = True
        except Exception:
            rdDetermineBonds.DetermineConnectivity(mol)
            try:
                Chem.SanitizeMol(mol, sanitizeOps=Chem.SANITIZE_ALL ^ Chem.SANITIZE_PROPERTIES)
                bonds_perceived = True
            except Exception:
                pass
    except Exception:
        pass

    # 3. Fallback: manual covalent bond threshold + sanitization if rdDetermineBonds could not form bonds
    if not bonds_perceived or (n_atoms > 1 and mol.GetNumBonds() == 0):
        rw = Chem.RWMol(raw_mol)
        pt = Chem.GetPeriodicTable()
        for i in range(n_atoms):
            elem_i, xi, yi, zi = atoms_data[i]
            r_i = pt.GetRcovalent(elem_i) or 1.0
            for j in range(i + 1, n_atoms):
                elem_j, xj, yj, zj = atoms_data[j]
                r_j = pt.GetRcovalent(elem_j) or 1.0
                dist = math.hypot(xi - xj, yi - yj, zi - zj)
                if 0.4 < dist < (r_i + r_j) * 1.30:
                    rw.AddBond(i, j, Chem.BondType.SINGLE)
        mol = rw.GetMol()
        try:
            Chem.SanitizeMol(mol, sanitizeOps=Chem.SANITIZE_ALL ^ Chem.SANITIZE_PROPERTIES)
        except Exception:
            try:
                mol.UpdatePropertyCache(strict=False)
            except Exception:
                pass

    # 4. Verify UFF parameter availability for all atoms in the structure
    if not AllChem.UFFHasAllMoleculeParams(mol):
        unsupported = [f"{a.GetSymbol()}{a.GetIdx() + 1}" for a in mol.GetAtoms()]
        return None, f"UFF force field parameters are not available for some atoms in this structure ({', '.join(unsupported[:4])}). Original geometry preserved."

    # 5. Perform UFF optimization
    try:
        AllChem.UFFOptimizeMolecule(mol, maxIters=max_iters)
    except Exception as exc:
        return None, f"UFF force field optimization error: {exc}. Original geometry preserved."

    # 6. Extract relaxed coordinates
    opt_conf = mol.GetConformer()
    out_lines = []
    for atom in mol.GetAtoms():
        pos = opt_conf.GetAtomPosition(atom.GetIdx())
        out_lines.append(f"{atom.GetSymbol():<3} {pos.x:12.6f} {pos.y:12.6f} {pos.z:12.6f}")

    return "\n".join(out_lines), None


def clean_3d_coordinates_uff(
    xyz_text: str,
    max_iters: int = 200,
    max_atoms: int = UFF_MAX_ATOMS,
    timeout_sec: float = UFF_TIMEOUT_SECONDS,
    charge: int = 0
) -> tuple[str | None, str | None]:
    """Optimize/relax 3D coordinates using RDKit's Universal Force Field (UFF) with concurrency control.
    Returns (opt_coords, err_message).
    """
    try:
        lines = [line.strip() for line in xyz_text.strip().splitlines() if line.strip()]
        if not lines:
            return None, "No coordinates provided."
        if lines[0].isdigit() and len(lines) > 2:
            lines = lines[2:]

        atoms_data = []
        for line in lines:
            parts = line.split()
            if len(parts) >= 4:
                elem = parts[0].capitalize()
                try:
                    x, y, z = float(parts[1]), float(parts[2]), float(parts[3])
                    atoms_data.append((elem, x, y, z))
                except ValueError:
                    continue

        if not atoms_data:
            return None, "No valid atomic coordinates parsed."

        if len(atoms_data) > max_atoms:
            return None, f"Structure size ({len(atoms_data)} atoms) exceeds UFF relaxation limit ({max_atoms} atoms). Original geometry preserved."

        # Acquire concurrency semaphore with non-blocking try or bounded timeout
        acquired = _uff_semaphore.acquire(timeout=2.0)
        if not acquired:
            return None, "Server is currently handling maximum concurrent UFF relaxations. Original geometry preserved."

        try:
            future = _uff_executor.submit(_run_uff_worker, atoms_data, max_iters, charge)
            opt_coords, err = future.result(timeout=timeout_sec)
            return opt_coords, err
        except concurrent.futures.TimeoutError:
            return None, f"UFF force field relaxation timed out after {timeout_sec:.1f}s. Original geometry preserved."
        finally:
            _uff_semaphore.release()

    except Exception as exc:
        logger.info("UFF relaxation failed: %s", exc)
        return None, f"Geometry relaxation failed: {exc}. Original geometry preserved."


# ─────────────────────────────────────────────────────────────
# 2D drawing - from bot3
# ─────────────────────────────────────────────────────────────
def _apply_common_draw_options(drawer) -> None:
    opts = drawer.drawOptions()
    opts.addStereoAnnotation = True
    opts.bondLineWidth = DRAWING_BOND_LINE_WIDTH
    opts.baseFontSize = DRAWING_FONT_SCALE * (opts.baseFontSize or 0.6)
    opts.padding = 0.12
    opts.legendFontSize = 48


def _png_with_dpi(raw: bytes, dpi: int = RENDER_DPI) -> bytes:
    """Normalize a PNG and embed a real 600-DPI print resolution tag.

    RDKit/Cairo produces excellent antialiased line art, but the PNG it returns
    does not reliably carry a publication-resolution metadata tag. Re-saving
    without resampling preserves every generated pixel while adding the DPI
    metadata expected by graphics software and journal production workflows.
    """
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        out = io.BytesIO()
        image.save(out, format="PNG", dpi=(dpi, dpi), compress_level=6)
        return out.getvalue()


def render_molecule_png(smiles: str, legend: str = "", size=MOL_IMAGE_SIZE) -> bytes | None:
    """Render publication-quality molecular line art at 600 DPI.

    The output canvas is 1800x1350 px and carries a 600-DPI PNG resolution tag.
    Bond and label scales are increased by the same factor as the canvas so the
    molecule keeps the corrected visual proportions while retaining sharp lines
    and text when placed in a manuscript.
    """
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    try:
        AllChem.Compute2DCoords(mol)
        Chem.rdDepictor.StraightenDepiction(mol)
    except Exception:
        pass

    drawer = rdMolDraw2D.MolDraw2DCairo(size[0], size[1])
    _apply_common_draw_options(drawer)
    opts = drawer.drawOptions()
    try:
        opts.fixedBondLength = 90.0
        opts.fixedFontSize = 60.0
        opts.minFontSize = 48.0
        opts.maxFontSize = 66.0
        opts.padding = 0.05
    except Exception:
        pass
    try:
        drawer.SetFontSize(60)
    except Exception:
        pass

    rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol, legend=legend)
    drawer.FinishDrawing()
    return _png_with_dpi(drawer.GetDrawingText())



def render_molecule_svg(smiles: str, legend: str = "", size=MOL_IMAGE_SIZE) -> bytes | None:
    """Return a genuine vector SVG molecular drawing for publication."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    try:
        AllChem.Compute2DCoords(mol)
        Chem.rdDepictor.StraightenDepiction(mol)
    except Exception:
        pass
    drawer = rdMolDraw2D.MolDraw2DSVG(size[0], size[1])
    _apply_common_draw_options(drawer)
    opts = drawer.drawOptions()
    try:
        opts.fixedBondLength = 90.0
        opts.fixedFontSize = 60.0
        opts.minFontSize = 48.0
        opts.maxFontSize = 66.0
        opts.padding = 0.05
    except Exception:
        pass
    try:
        drawer.SetFontSize(60)
    except Exception:
        pass
    rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol, legend=legend)
    drawer.FinishDrawing()
    return drawer.GetDrawingText().encode("utf-8")


def _molecule_svg_fragment(smiles: str, bond_length: float = 48.0) -> tuple[str, int, int] | None:
    """Render a molecule into a tight, publication-grade SVG fragment with exact dimensions."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    try:
        AllChem.Compute2DCoords(mol)
        Chem.rdDepictor.StraightenDepiction(mol)
    except Exception:
        pass

    # Compute bounding box from 2D coordinates
    try:
        conf = mol.GetConformer()
        if mol.GetNumAtoms() > 0:
            xs = [conf.GetAtomPosition(i).x for i in range(mol.GetNumAtoms())]
            ys = [conf.GetAtomPosition(i).y for i in range(mol.GetNumAtoms())]
            span_x = max(xs) - min(xs)
            span_y = max(ys) - min(ys)
        else:
            span_x, span_y = 1.0, 1.0
    except Exception:
        span_x, span_y = 3.0, 2.5

    # ACS 1996 standard bond length in 2D depiction: ~1.5 units = bond_length pixels
    scale = bond_length / 1.5
    mol_w = max(130, int(span_x * scale + 50))
    mol_h = max(110, int(span_y * scale + 40))

    drawer = rdMolDraw2D.MolDraw2DSVG(mol_w, mol_h)
    _apply_common_draw_options(drawer)
    opts = drawer.drawOptions()
    try:
        opts.fixedBondLength = float(bond_length)
        opts.fixedFontSize = 32
        opts.minFontSize = 24
        opts.maxFontSize = 40
        opts.bondLineWidth = 2.4
        opts.padding = 0.08
    except Exception:
        pass
    rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol)
    drawer.FinishDrawing()
    raw = drawer.GetDrawingText()
    svg_tag_start = raw.find("<svg")
    if svg_tag_start == -1:
        return None
    start = raw.find(">", svg_tag_start) + 1
    end = raw.rfind("</svg>")
    fragment = raw[start:end] if start > 0 and end > start else None
    if fragment is None:
        return None
    return fragment, mol_w, mol_h


def _svg_formula_markup(formula: str, x: int, y: int, font_size: int = 44) -> str:
    parts = [f'<text x="{x}" y="{y}" font-family="Arial,Helvetica,sans-serif" font-size="{font_size}" font-weight="500" fill="#141414">']
    for text, style in _formula_tokens(formula):
        if style == "sub":
            parts.append(f'<tspan font-size="{int(font_size * 0.7)}" baseline-shift="sub">{text}</tspan>')
        elif style == "sup":
            parts.append(f'<tspan font-size="{int(font_size * 0.7)}" baseline-shift="super">{text}</tspan>')
        else:
            parts.append(text)
    parts.append("</text>")
    return "".join(parts)


def render_reaction_svg(reactant_pairs, product_pairs, small_as_formula: bool = True,
                        arrow_top: str = "", arrow_bottom: str = "",
                        arrow_style: str = "forward") -> bytes | None:
    """Return a genuine vector SVG reaction scheme formatted to Q1 publication standards."""
    tokens: list[tuple[str, object]] = []
    for side_index, pairs in enumerate((reactant_pairs, product_pairs)):
        if side_index:
            tokens.append(("arrow", None))
        for i, (coefficient, smiles) in enumerate(pairs):
            if i:
                tokens.append(("op", "+"))
            label = ReactionTerm(Fraction(coefficient), "").pretty_coefficient
            if label:
                tokens.append(("coefficient", label))
            formula = formula_for_display(smiles) if small_as_formula else None
            if formula:
                tokens.append(("formula", formula))
            else:
                mol_res = _molecule_svg_fragment(smiles)
                if mol_res is None:
                    return None
                tokens.append(("mol", mol_res))
    if not tokens:
        return None

    gap = 24
    arrow_label_size = 26
    arrow_label_sup_size = 18

    def svg_annotation_width(text: str) -> int:
        total = 0
        for chunk, style in _arrow_annotation_parts(text):
            is_sup = (style == "sup")
            is_sub = (style == "sub")
            total += len(chunk) * (11 if (is_sup or is_sub) else 15)
        return total

    widths, heights = [], []
    for kind, payload in tokens:
        if kind == "mol":
            frag, w, h = payload
            widths.append(w)
            heights.append(h)
        elif kind == "formula":
            widths.append(max(60, 24 * len(payload) + 16))
            heights.append(50)
        elif kind == "coefficient":
            widths.append(max(30, 24 * len(payload)))
            heights.append(50)
        elif kind == "op":
            widths.append(32)
            heights.append(50)
        else:
            arrow_needed = max(130, svg_annotation_width(arrow_top) + 36,
                               svg_annotation_width(arrow_bottom) + 36)
            widths.append(arrow_needed)
            heights.append(30)

    margin = 32
    total_width = sum(widths) + gap * (len(tokens) - 1) + margin * 2
    label_extra = 50 if (arrow_top or arrow_bottom) else 0
    total_height = max(max(heights) + 50, 160 + label_extra)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_width}" height="{total_height}" viewBox="0 0 {total_width} {total_height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<style>text{font-family:Arial,Helvetica,sans-serif;fill:#141414}</style>',
    ]
    x = margin
    center = total_height // 2

    def escape_xml(value: str) -> str:
        return (value.replace("&", "&amp;").replace("<", "&lt;")
                     .replace(">", "&gt;").replace('"', "&quot;"))

    def svg_annotation(text: str, center_x: int, baseline_y: int) -> str:
        if not text:
            return ""
        parts = _arrow_annotation_parts(text)
        pieces = []
        for chunk, style in parts:
            escaped = escape_xml(chunk)
            if style == "sub":
                pieces.append(f'<tspan font-size="{arrow_label_sup_size}" baseline-shift="sub">{escaped}</tspan>')
            elif style == "sup":
                pieces.append(f'<tspan font-size="{arrow_label_sup_size}" baseline-shift="super">{escaped}</tspan>')
            else:
                pieces.append(escaped)
        return (
            f'<text x="{center_x}" y="{baseline_y}" '
            f'font-family="Arial,Helvetica,sans-serif" font-size="{arrow_label_size}" '
            f'font-weight="500" fill="#141414" text-anchor="middle">{"".join(pieces)}</text>'
        )

    for i, (kind, payload) in enumerate(tokens):
        if kind == "mol":
            frag, w, h = payload
            out.append(f'<g transform="translate({x},{center - h // 2})">{frag}</g>')
        elif kind == "formula":
            out.append(_svg_formula_markup(payload, x + 8, center + 14, 44))
        elif kind == "coefficient":
            out.append(f'<text x="{x}" y="{center + 14}" font-size="44" font-weight="600">{escape_xml(payload)}</text>')
        elif kind == "op":
            out.append(f'<text x="{x + 4}" y="{center + 13}" font-size="40" font-weight="500">+</text>')
        else:
            y = center
            arrow_width = widths[i]
            arrow_end = x + arrow_width - 10
            head_x = arrow_end - 16
            style = (arrow_style or "forward").lower()
            if style in ("equilibrium", "eq"):
                off = 5
                out.append(f'<line x1="{x}" y1="{y - off}" x2="{head_x}" y2="{y - off}" stroke="#141414" stroke-width="2.5"/>')
                out.append(f'<path d="M {arrow_end} {y - off} L {head_x} {y - off - 6} L {head_x} {y - off + 6} Z" fill="#141414"/>')
                out.append(f'<line x1="{x + 16}" y1="{y + off}" x2="{arrow_end}" y2="{y + off}" stroke="#141414" stroke-width="2.5"/>')
                out.append(f'<path d="M {x} {y + off} L {x + 16} {y + off - 6} L {x + 16} {y + off + 6} Z" fill="#141414"/>')
            elif style in ("reversible", "rev", "both"):
                out.append(f'<line x1="{x + 16}" y1="{y}" x2="{head_x}" y2="{y}" stroke="#141414" stroke-width="2.5"/>')
                out.append(f'<path d="M {arrow_end} {y} L {head_x} {y - 8} L {head_x} {y + 8} Z" fill="#141414"/>')
                out.append(f'<path d="M {x} {y} L {x + 16} {y - 8} L {x + 16} {y + 8} Z" fill="#141414"/>')
            else:
                out.append(f'<line x1="{x}" y1="{y}" x2="{head_x}" y2="{y}" stroke="#141414" stroke-width="3"/>')
                out.append(f'<path d="M {arrow_end} {y} L {head_x} {y - 8} L {head_x} {y + 8} Z" fill="#141414"/>')
            arrow_center = (x + arrow_end) / 2
            if arrow_top:
                out.append(svg_annotation(arrow_top, int(arrow_center), y - 18))
            if arrow_bottom:
                out.append(svg_annotation(arrow_bottom, int(arrow_center), y + 36))
        x += widths[i] + gap
    out.append("</svg>")
    return "".join(out).encode("utf-8")


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
# right for it - otherwise it is drawn as a structure, which is never wrong.
_CONVENTIONAL_FORMULAS = {
    "O=O": "O2", "[H][H]": "H2", "N#N": "N2", "ClCl": "Cl2", "BrBr": "Br2",
    "II": "I2", "FF": "F2", "[O][O]": "O2", "O=[O+][O-]": "O3",
    "O=C=O": "CO2", "[C-]#[O+]": "CO", "O=S=O": "SO2", "O=S(=O)=O": "SO3",
    "[N]=O": "NO", "[O-][N+]=O": "NO2", "[N-]=[N+]=O": "N2O",
    "O": "H2O", "OO": "H2O2", "N": "NH3", "S": "H2S", "C": "CH4",
    "Cl": "HCl", "Br": "HBr", "I": "HI", "F": "HF", "P": "PH3",
    "[SiH4]": "SiH4",
    "OS(=O)(=O)O": "H2SO4", "O[N+](=O)[O-]": "HNO3", "OP(=O)(O)O": "H3PO4",
    "OC(=O)O": "H2CO3", "OS(=O)O": "H2SO3", "N#C": "HCN",
    "[OH-]": "OH-", "[H+]": "H+", "[NH4+]": "NH4+",
    "[Na+].[Cl-]": "NaCl", "[Na+].[OH-]": "NaOH", "[K+].[OH-]": "KOH",
    "[Ca+2].[O-2]": "CaO", "[Na+].[Na+].[O-]C([O-])=O": "Na2CO3",
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
    mol = Chem.MolFromSmiles(smiles or "")
    if mol is None:
        return None
    canonical = Chem.MolToSmiles(mol)
    if canonical in _FORMULA_BY_CANONICAL_SMILES:
        return _FORMULA_BY_CANONICAL_SMILES[canonical]
    carbons = sum(1 for a in mol.GetAtoms() if a.GetSymbol() == "C")
    if mol.GetRingInfo().NumRings() or carbons > 1 or mol.GetNumHeavyAtoms() > 4:
        return None
    if carbons and any(a.GetSymbol() in ("O", "N", "S") and a.GetTotalNumHs()
                       for a in mol.GetAtoms()):
        return None
    hill = rdMolDescriptors.CalcMolFormula(mol)
    if not carbons and hill.startswith("H"):
        return None
    return hill


_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)

_FORMULA_TOKEN_RE = re.compile(r"(\^[^\s\^]+|[A-Za-z]+|\d+|[+-]\d*|\s+|[^A-Za-z\d\s\^+-]+)")


def _formula_tokens(formula: str) -> list[tuple[str, str]]:
    """Tokenize a chemical formula or arrow annotation into (text, style) chunks.
    style is one of 'normal', 'sub', 'sup'.
    A caret (^) immediately raises following characters to superscript and is removed completely.
    E.g. 'NH4^+' -> [('NH', 'normal'), ('4', 'sub'), ('+', 'sup')]
         'Fe^3+' -> [('Fe', 'normal'), ('3+', 'sup')]
         '80^oC' -> [('80', 'normal'), ('oC', 'sup')]
    """
    if not formula:
        return []
    tokens = []
    raw_parts = [p for p in _FORMULA_TOKEN_RE.findall(formula) if p]
    for i, part in enumerate(raw_parts):
        if part.startswith("^"):
            sup_content = part[1:]
            if sup_content:
                tokens.append((sup_content, "sup"))
        elif part[0] in "+-":
            tokens.append((part[1:] + part[0] if len(part) > 1 else part, "sup"))
        elif part.isdigit():
            tokens.append((part, "sub" if i and raw_parts[i - 1][0].isalpha() else "normal"))
        else:
            tokens.append((part, "normal"))
    return tokens


def _render_formula_image(formula: str, size: int):
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
    canvas = Image.new("RGB", (sum(widths) + pad * 2, ascent + shift * 2 + pad * 2), (255, 255, 255))
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
    from PIL import ImageFont
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _trim_white(image, pad: int = 6):
    from PIL import Image, ImageChops
    background = Image.new(image.mode, image.size, (255, 255, 255))
    box = ImageChops.difference(image, background).getbbox()
    if not box:
        return image
    left, top, right, bottom = box
    return image.crop((max(0, left - pad), max(0, top - pad),
                       min(image.width, right + pad), min(image.height, bottom + pad)))


def _arrow_annotation_parts(text: str) -> list[tuple[str, str]]:
    """Return (text, style) chunks for an arrow annotation with proper sub/sup."""
    return _formula_tokens(text)


def _arrow_annotation_width(draw, text: str, font, sup_font) -> int:
    """Measure an arrow annotation using the same fonts used to draw it."""
    width = 0
    for chunk, style in _arrow_annotation_parts(text):
        if not chunk:
            continue
        current_font = sup_font if style in ("sub", "sup") else font
        box = draw.textbbox((0, 0), chunk, font=current_font)
        width += max(0, box[2] - box[0])
    return width


def _arrow_annotation_height(font, sup_font) -> tuple[int, int]:
    """Return approximate normal and superscript line heights in pixels."""
    normal = int(getattr(font, "size", 36))
    superscript = int(getattr(sup_font, "size", max(20, normal * 0.68)))
    return normal, superscript


def _draw_arrow_label(draw, center_x, baseline_y, text, font, sup_font, *, fill=(20, 20, 20)):
    """Draw an annotation centered around ``center_x`` with chemical subscripts and superscripts."""
    parts = _arrow_annotation_parts(text)
    if not parts:
        return

    widths = []
    for chunk, style in parts:
        current_font = sup_font if style in ("sub", "sup") else font
        box = draw.textbbox((0, 0), chunk, font=current_font)
        widths.append(max(0, box[2] - box[0]))

    total_width = sum(widths)
    x = center_x - total_width // 2
    normal_size, _ = _arrow_annotation_height(font, sup_font)

    for (chunk, style), width in zip(parts, widths):
        current_font = sup_font if style in ("sub", "sup") else font
        box = draw.textbbox((0, 0), chunk, font=current_font)
        y = baseline_y - box[3]
        if style == "sup":
            y -= max(4, normal_size // 3)
        elif style == "sub":
            y += max(3, normal_size // 4)
        draw.text((x, y), chunk, fill=fill, font=current_font)
        x += width

def render_reaction_png(reactant_pairs, product_pairs, sub_size=REACTION_SUBIMAGE_SIZE,
                        small_as_formula: bool = True, arrow_top: str = "",
                        arrow_bottom: str = "",
                        arrow_style: str = "forward") -> bytes | None:
    from PIL import Image, ImageDraw
    def draw_one(smiles):
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return None
        AllChem.Compute2DCoords(mol)
        drawer = rdMolDraw2D.MolDraw2DCairo(sub_size[0], sub_size[1])
        _apply_common_draw_options(drawer)
        try:
            opts = drawer.drawOptions()
            # Match the large-pixel reaction canvas to a readable RDKit scale.
            # The old renderer fixed only the bond length, leaving atom/group
            # labels to RDKit's automatic font scaling.
            opts.fixedBondLength = 72.0
            opts.fixedFontSize = 54.0
            opts.minFontSize = 42.0
            opts.maxFontSize = 64.0
            opts.bondLineWidth = 3.0
        except Exception:
            pass
        rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol)
        drawer.FinishDrawing()
        return _trim_white(Image.open(io.BytesIO(drawer.GetDrawingText())).convert("RGB"))
    bond_length = max(72, sub_size[1] // 7)
    formula_size = max(90, sub_size[1] // 6)

    # Keep molecular RDKit scaling unchanged. Size '+' and the arrow
    # independently so they do not grow with the high-resolution canvas.
    operator_size = max(48, int(formula_size * 0.62))
    arrow_length = max(300, int(sub_size[0] * 0.16))
    arrow_head = max(18, int(operator_size * 0.55))
    coefficient_font = _layout_font(operator_size)
    operator_font = _layout_font(operator_size)
    arrow_label_font = _layout_font(max(34, int(formula_size * 0.42)))
    arrow_label_sup_font = _layout_font(max(22, int(formula_size * 0.28)))
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
    top_width = _arrow_annotation_width(probe, arrow_top, arrow_label_font, arrow_label_sup_font)
    bottom_width = _arrow_annotation_width(probe, arrow_bottom, arrow_label_font, arrow_label_sup_font)
    desired_arrow_length = max(arrow_length, max(top_width, bottom_width) + 120)
    arrow_length = min(desired_arrow_length, max(300, int(sub_size[0] * 0.60)))
    arrow_head = max(18, min(int(arrow_length * 0.12), 64))
    widths = [arrow_length if kind == "arrow" else width for (kind, _), width in zip(tokens, widths)]

    # Leave real vertical room for arrow annotations so text is never clipped
    # by the reaction canvas. This does not change the molecular scale.
    normal_label_h, sup_label_h = _arrow_annotation_height(arrow_label_font, arrow_label_sup_font)
    label_room = ((normal_label_h + sup_label_h + 32) * int(bool(arrow_top)) + (normal_label_h + sup_label_h + 32) * int(bool(arrow_bottom)))
    top_width = _arrow_annotation_width(probe, arrow_top, arrow_label_font, arrow_label_sup_font)
    bottom_width = _arrow_annotation_width(probe, arrow_bottom, arrow_label_font, arrow_label_sup_font)
    desired_arrow_length = max(90, max(top_width, bottom_width) + 80)
    arrow_length = min(desired_arrow_length, max(90, sub_size[0] // 2))
    arrow_head = max(18, min(int(arrow_length * 0.12), 64))
    widths = [arrow_length if kind == "arrow" else width for (kind, _), width in zip(tokens, widths)]
    margin = gap * 2
    total_width = sum(widths) + gap * (len(tokens) - 1) + margin * 2
    total_height = max(max(heights) + margin * 2,
                       max(1, max(heights)) + label_room + margin * 2)
    canvas = Image.new("RGB", (total_width, total_height), (255, 255, 255))
    pen = ImageDraw.Draw(canvas)
    x = margin
    for (kind, payload), width in zip(tokens, widths):
        if kind == "mol":
            canvas.paste(payload, (x, (total_height - payload.height) // 2))
        elif kind == "arrow":
            mid = total_height // 2
            style = (arrow_style or "forward").lower()
            if style in ("equilibrium", "eq"):
                off = max(4, arrow_head // 3)
                pen.line([(x, mid - off), (x + arrow_length - arrow_head, mid - off)], fill=(20, 20, 20), width=max(2, arrow_head // 7))
                pen.polygon([(x + arrow_length, mid - off),
                             (x + arrow_length - arrow_head, mid - off - arrow_head // 2),
                             (x + arrow_length - arrow_head, mid - off + arrow_head // 2)], fill=(20, 20, 20))
                pen.line([(x + arrow_head, mid + off), (x + arrow_length, mid + off)], fill=(20, 20, 20), width=max(2, arrow_head // 7))
                pen.polygon([(x, mid + off),
                             (x + arrow_head, mid + off - arrow_head // 2),
                             (x + arrow_head, mid + off + arrow_head // 2)], fill=(20, 20, 20))
            elif style in ("reversible", "rev", "both"):
                pen.line([(x + arrow_head, mid), (x + arrow_length - arrow_head, mid)], fill=(20, 20, 20), width=max(2, arrow_head // 7))
                pen.polygon([(x + arrow_length, mid),
                             (x + arrow_length - arrow_head, mid - arrow_head // 2),
                             (x + arrow_length - arrow_head, mid + arrow_head // 2)], fill=(20, 20, 20))
                pen.polygon([(x, mid),
                             (x + arrow_head, mid - arrow_head // 2),
                             (x + arrow_head, mid + arrow_head // 2)], fill=(20, 20, 20))
            else:
                pen.line([(x, mid), (x + arrow_length - arrow_head, mid)], fill=(20, 20, 20), width=max(2, arrow_head // 7))
                pen.polygon([(x + arrow_length, mid),
                             (x + arrow_length - arrow_head, mid - arrow_head // 2),
                             (x + arrow_length - arrow_head, mid + arrow_head // 2)], fill=(20, 20, 20))
            if arrow_top:
                _draw_arrow_label(pen, x + arrow_length // 2, mid - 28, arrow_top, arrow_label_font, arrow_label_sup_font)
            if arrow_bottom:
                _draw_arrow_label(pen, x + arrow_length // 2, mid + arrow_label_font.size + 26, arrow_bottom, arrow_label_font, arrow_label_sup_font)
        else:
            font = coefficient_font if kind == "coefficient" else operator_font
            box = pen.textbbox((0, 0), payload, font=font)
            pen.text((x - box[0], (total_height - (box[3] - box[1])) // 2 - box[1]), payload, fill=(20, 20, 20), font=font)
        x += width + (gap // 2 if kind == "coefficient" else gap)
    max_display_width = 7200
    if canvas.width > max_display_width:
        scale = max_display_width / canvas.width
        target = (max_display_width, max(1, round(canvas.height * scale)))
        canvas = canvas.resize(target, Image.Resampling.LANCZOS)
    out = io.BytesIO()
    canvas.save(out, format="PNG", dpi=(RENDER_DPI, RENDER_DPI), compress_level=6)
    return out.getvalue()


def generate_mol_file_bytes(smiles: str) -> bytes | None:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    AllChem.Compute2DCoords(mol)
    return Chem.MolToMolBlock(mol).encode("utf-8")


def _element_counts(smiles: str) -> tuple[dict[str, int], int] | None:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    counts: dict[str, int] = {}
    charge = 0
    for atom in mol.GetAtoms():
        counts[atom.GetSymbol()] = counts.get(atom.GetSymbol(), 0) + 1
        h = atom.GetTotalNumHs()
        if h:
            counts["H"] = counts.get("H", 0) + h
        charge += atom.GetFormalCharge()
    return counts, charge


def check_reaction_balance(reactant_pairs, product_pairs) -> dict:
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
                "message": "The equation is balanced: every element and the total charge match on both sides."}
    parts = []
    for element, delta in diffs.items():
        side = "product" if delta > 0 else "reactant"
        parts.append("%s %s on the %s side" % (fmt(abs(delta)), element, side))
    if charge_delta != 0:
        parts.append("a net charge difference of %s" % fmt(charge_delta))
    return {"checked": True, "balanced": False,
            "element_difference": {e: fmt(d) for e, d in diffs.items()},
            "excess_side": {e: ("product" if d > 0 else "reactant") for e, d in diffs.items()},
            "excess_amount": {e: fmt(abs(d)) for e, d in diffs.items()},
            "charge_difference": fmt(charge_delta),
            "message": "The equation is NOT balanced - there is an excess of " + "; ".join(parts) + ". The scheme was still drawn, in case that is what you intended."}


def validate_rxn_block(text: str) -> dict:
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
        return {"valid": False, "problems": ["the reactant/product counts line is not in MDL fixed-column form"]}
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
        problems.append("the header declares %d components but the file contains %d" % (n_reactants + n_products, len(blocks)))
    for i, block in enumerate(blocks, 1):
        if not any(l.rstrip().endswith(("V2000", "V3000")) for l in block[:6]):
            problems.append("component %d has no V2000/V3000 counts line" % i)
        if not any(l.strip() == "M  END" for l in block):
            problems.append("component %d is not terminated with 'M  END'" % i)
    return {"valid": not problems, "problems": problems, "format": "MDL RXN V2000", "reactants": n_reactants, "products": n_products,
            "opens_in": ["ChemDraw", "ChemDraw JS", "MarvinSketch", "ISIS/Draw", "Avogadro", "RDKit", "Open Babel"]}


def generate_rxn_file_bytes(reactant_pairs, product_pairs, title: str = "") -> bytes | None:
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
    rxn = rdChemReactions.ReactionFromSmarts("%s>>%s" % (".".join(left), ".".join(right)), useSmiles=True)
    if rxn is None:
        return None
    rdChemReactions.Compute2DCoordsForReaction(rxn)
    block = rdChemReactions.ReactionToRxnBlock(rxn)
    if title:
        lines = block.split("\n")
        if len(lines) > 2:
            lines[1] = title[:80]
            block = "\n".join(lines)
    return block.encode("utf-8")


# ─────────────────────────────────────────────────────────────
# ORCA 6 input generation - ported verbatim (logic) from bot2
# ─────────────────────────────────────────────────────────────
# ORCA 6.1 input generation - Enhanced for ORCA 6.1
# No silent "smart" additions: generates exactly what the user picked.
# ─────────────────────────────────────────────────────────────
CALC_TYPES = {
    "sp": "Single Point Energy (SP)",
    "opt": "Geometry Optimization (Opt)",
    "freq": "Analytical Frequencies / IR (Freq)",
    "numfreq": "Numerical Frequencies / IR (NumFreq)",
    "opt freq": "Opt + Frequencies / IR (Opt Freq)",
    "optts": "Transition State Optimization (OptTS)",
    "optts freq": "OptTS + Frequencies (OptTS Freq)",
    "tddft": "TD-DFT (UV-Vis Excited States)",
    "nmr": "NMR Chemical Shifts (EPRNMR)",
    "custom": "⌨️ Custom Command Line",
}
SCF_CONVERGENCE = {
    "none": "Default SCF",
    "tightscf": "TightSCF (Recommended for Freq/Opt)",
    "verytightscf": "VeryTightSCF (High Precision)",
    "normalscf": "NormalSCF",
    "loosescf": "LooseSCF (Quick Preliminary)",
    "slowconv": "SlowConv (Hard to Converge)",
}
COMPOSITE_METHODS = ["r2SCAN-3c", "B97-3c", "PBEh-3c", "HF-3c", "wB97X-3c", "B3LYP-3c"]
DFT_FUNCTIONALS = [
    "B3LYP", "CAM-B3LYP", "PBE0", "wB97M-V", "wB97X-D4", "wB97X-V", "wB97X-D3",
    "M062X", "M06", "PBE", "BP86", "r2SCAN", "TPSS", "PW6B95", "B2PLYP", "DSD-PBEP86"
]
BUILTIN_DISPERSION_FUNCTIONALS = {
    "wb97m-v", "wb97x-v", "wb97x-d4", "wb97x-d3", "wb97x-d3bj", "wb97x-d4rev", "b97-d3", "b97-d"
}
MP2_VARIANTS = ["MP2", "RI-MP2", "DLPNO-MP2"]
CCSD_VARIANTS = ["CCSD", "CCSD(T)", "DLPNO-CCSD", "DLPNO-CCSD(T)"]
_NEEDS_AUX_C = ("RI-MP2", "DLPNO-MP2", "DLPNO-CCSD", "DLPNO-CCSD(T)", "B2PLYP", "DSD-PBEP86")
X2C_BASIS = {
    "def2-SVP": "x2c-SVPall", "def2-SVPD": "x2c-SVPall",
    "def2-TZVP": "x2c-TZVPall", "def2-TZVPD": "x2c-TZVPall",
    "def2-TZVPP": "x2c-TZVPPall", "def2-TZVPPD": "x2c-TZVPPall",
    "def2-QZVP": "x2c-QZVPall", "def2-QZVPP": "x2c-QZVPall",
    "ma-def2-SVP": "x2c-SVPall", "ma-def2-TZVP": "x2c-TZVPall",
}
RI_OPTIONS = {
    "none": "No RI Acceleration",
    "rijcosx": "RIJCOSX (Fast Hybrids / RI-J + COSX)",
    "rijk": "RIJK (Exact Coulomb & Exchange)",
    "ri": "RI (Standard Pure DFT/GGA)",
}
BASIS_MAP = {
    "def2": [
        "def2-SVP", "def2-SVPD", "def2-TZVP", "def2-TZVPD", "def2-TZVPP",
        "def2-TZVPPD", "def2-QZVP", "def2-QZVPP", "ma-def2-SVP", "ma-def2-TZVP"
    ],
    "dunning": [
        "cc-pVDZ", "aug-cc-pVDZ", "cc-pVTZ", "aug-cc-pVTZ", "cc-pVQZ", "aug-cc-pVQZ"
    ],
    "pople": [
        "6-31G(d)", "6-31G(d,p)", "6-31+G(d,p)", "6-311G(d,p)", "6-311+G(d,p)",
        "6-311++G(d,p)", "6-311++G(2df,2pd)"
    ],
    "pcseg": [
        "pcseg-1", "pcseg-2", "pcseg-3", "aug-pcseg-1", "aug-pcseg-2"
    ],
    "x2c": [
        "x2c-SVPall", "x2c-TZVPall", "x2c-TZVPPall", "x2c-QZVPall", "ZORA-def2-TZVP", "SARC-ZORA-TZVP"
    ],
    "property": [
        "IGLO-III", "pcJ-2", "EPR-II", "EPR-III"
    ],
}
DISPERSION_MODELS = {"none": "None", "D3": "D3", "D3bj": "D3BJ", "D4": "D4"}
SOLVATION_MODELS = {"none": "None (Gas Phase)", "cpcm": "CPCM", "smd": "SMD"}
SOLVENTS = [
    "Water", "Acetonitrile", "Methanol", "Ethanol", "Acetone", "DCM", "Chloroform",
    "THF", "DMSO", "DMF", "Toluene", "Benzene", "Hexane", "Cyclohexane",
    "DiethylEther", "1,4-Dioxane", "CarbonTetrachloride", "EthylAcetate", "Pyridine"
]


def generate_orca_6_input(d: dict) -> str:
    input_text = "# ORCA 6.1.0 Input generated by ORCA Web Lab\n"
    if d.get("custom_line"):
        input_text += f"{d['custom_line']}\n\n"
    else:
        raw_calc = str(d.get("calc_type") or "sp").strip()
        calc_norm = raw_calc.lower().replace("_", " ")
        calc_cmd_map = {
            "sp": "SP",
            "opt": "Opt",
            "freq": "Freq",
            "numfreq": "NumFreq",
            "opt freq": "Opt Freq",
            "optts": "OptTS",
            "optts freq": "OptTS Freq",
            "tddft": "",
            "nmr": "NMR",
            "custom orca": "",
            "imported": "",
        }
        if calc_norm not in calc_cmd_map:
            raise ValueError(f"Unknown or unsupported calculation type: {raw_calc!r}")
        calc_cmd = calc_cmd_map[calc_norm]

        method = d.get("theory", "").strip()
        is_composite = (
            d.get("family") == "f_comp"
            or any(m.lower() == method.lower() for m in COMPOSITE_METHODS)
            or "-3c" in method.lower()
            or "-2c" in method.lower()
        )

        # Composite methods carry their own tailored basis sets (Manual §3.7)
        basis = "" if is_composite else d.get("basis", "").strip()

        # Dispersion Handling (Manual §3.5)
        # Suppress dispersion for composite methods and functionals with built-in dispersion
        has_builtin_disp = (
            is_composite
            or method.lower() in BUILTIN_DISPERSION_FUNCTIONALS
            or method.lower().endswith("-v")
        )
        disp_val = d.get("disp", "none")
        disp_part = ""
        if not has_builtin_disp and disp_val != "none":
            disp_map = {"d3": "D3", "d3bj": "D3BJ", "d3zero": "D3ZERO", "d4": "D4"}
            disp_part = disp_map.get(disp_val.lower(), disp_val.upper())

        # RI Acceleration Handling (Manual §2.7.4 & §3.2)
        # Use clean RI keywords; def2 basis sets automatically use def2/J and def2/JK
        ri_type_val = d.get("ri_type", "none").lower()
        acc_part = {
            "rijcosx": "RIJCOSX",
            "rijk": "RIJK",
            "ri": "RI",
        }.get(ri_type_val, "")

        if d.get("autoaux"):
            acc_part = (acc_part + " AutoAux").strip()

        # Solvation Handling (Manual §2.13)
        solv_str = ""
        if d.get("solv_model", "none") != "none":
            solv_model = d["solv_model"].upper()
            solvent = d.get("solvent", "Water")
            solv_str = f"{solv_model}({solvent})"

        # SCF convergence
        scf_conv = d.get("scf_conv", "none")
        scf_part = {
            "tightscf": "TightSCF",
            "verytightscf": "VeryTightSCF",
            "normalscf": "NormalSCF",
            "loosescf": "LooseSCF",
            "slowconv": "SlowConv",
        }.get(scf_conv.lower(), "") if scf_conv != "none" else ""

        # LargePrint option
        largeprint_part = "LargePrint" if d.get("largeprint") else ""

        # Relativistic X2C handling (Manual §2.12)
        x2c, notes = "", []
        if d.get("x2c"):
            x2c = "X2C"
            swapped = X2C_BASIS.get(basis)
            if swapped:
                notes.append("# X2C needs a relativistically recontracted basis; %s was substituted for %s." % (swapped, basis))
                basis = swapped
            elif basis and not basis.lower().startswith(("x2c-", "zora-", "sarc-", "ano-rcc", "dhf-")):
                notes.append("# WARNING: X2C is a relativistic Hamiltonian and %s is not a relativistically recontracted basis. Use an x2c-* or SARC set." % basis)
            elif is_composite:
                notes.append("# WARNING: X2C was requested with a composite (3c) method. Composite methods carry their own non-relativistic basis and fitted corrections, so they are not defined with a relativistic Hamiltonian. Use an explicit functional with an x2c-* or SARC basis instead.")

        keywords = ["!", method, basis, disp_part, acc_part, scf_part, solv_str, calc_cmd, x2c, largeprint_part]
        keywords = [k for k in keywords if str(k).strip()]
        input_text += " ".join(keywords) + "\n"
        for n in notes:
            input_text += n + "\n"
        input_text += "\n"

    # Parallel and memory configuration (Manual §2.4 & §2.5)
    input_text += f"%pal nprocs {d.get('cores', 4)} end\n"
    input_text += f"%maxcore {d.get('ram', 6000)}\n\n"

    # Calculation-specific blocks (Manual §5.6, §5.21, §4.1)
    if d.get("calc_type") == "tddft":
        input_text += f"%tddft\n   nroots {d.get('nroots', 10)}\nend\n\n"
    elif d.get("calc_type") == "nmr":
        input_text += "%eprnmr\n   Nuclei = all H { shift }\n   Nuclei = all C { shift }\nend\n\n"
    elif "freq" in str(d.get("calc_type", "")).lower() and d.get("calc_type") != "custom":
        temp = d.get("temp", 298.15)
        pressure = d.get("pressure", 1.0)
        input_text += f"%freq\n   Temp {temp}\n   Pressure {pressure}\nend\n\n"

    input_text += f"* xyz {d.get('charge', 0)} {d.get('mult', 1)}\n"
    input_text += f"{d.get('coords', '')}\n"
    input_text += "*\n"
    return input_text


# ─────────────────────────────────────────────────────────────
# ChemDraw XML (CDXML) & Enhanced RXN Exporters
# ─────────────────────────────────────────────────────────────
from services.cdxml_service import (  # noqa: E402
    generate_single_compound_cdxml,
    generate_reaction_cdxml,
    generate_chemdraw_rxn_file,
    validate_cdxml_bytes,
)


