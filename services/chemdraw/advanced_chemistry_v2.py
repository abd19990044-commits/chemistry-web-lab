from __future__ import annotations

"""Charge-aware refinements for advanced chemistry tools."""

from fractions import Fraction
from functools import reduce
from math import gcd

from rdkit import Chem
from rdkit.Chem import rdMolDescriptors

from .advanced_chemistry import *  # re-export conservative redox/mapping/mechanism tools
from .advanced_chemistry import NetIonicResult, _STRONG_ELECTROLYTES, _lcm


def _fragment_counts(mol: Chem.Mol) -> dict[str, int]:
    mh = Chem.AddHs(mol)
    counts: dict[str, int] = {}
    for atom in mh.GetAtoms():
        counts[atom.GetSymbol()] = counts.get(atom.GetSymbol(), 0) + 1
    return counts


def _charge_suffix(charge: int) -> str:
    if charge == 0:
        return ""
    sign = "+" if charge > 0 else "-"
    mag = abs(charge)
    return sign if mag == 1 else f"^{mag}{sign}"


def _ion_label_from_fragment(mol: Chem.Mol) -> str:
    """Return conventional ion labels without duplicating RDKit charge text."""
    charge = sum(a.GetFormalCharge() for a in mol.GetAtoms())
    counts = _fragment_counts(mol)

    # Common aqueous ions get conventional ordering.
    if counts == {"H": 1, "O": 1} and charge == -1:
        return "OH-"
    if counts == {"N": 1, "O": 3} and charge == -1:
        return "NO3-"
    if counts == {"S": 1, "O": 4} and charge == -2:
        return "SO4^2-"
    if counts == {"Cl": 1, "O": 4} and charge == -1:
        return "ClO4-"

    # Monatomic ions.
    if len(counts) == 1 and sum(counts.values()) == 1:
        symbol = next(iter(counts))
        return symbol + _charge_suffix(charge)

    # Neutral fallback: use RDKit's formula unchanged.
    if charge == 0:
        return rdMolDescriptors.CalcMolFormula(mol)

    # Generic charged fallback: strip RDKit's terminal charge annotation first.
    formula = rdMolDescriptors.CalcMolFormula(mol)
    while formula and formula[-1] in "+-":
        formula = formula[:-1]
    # RDKit may encode magnitude before the sign, e.g. O4S-2 in some versions.
    import re
    formula = re.sub(r"[+-]\d+$", "", formula)
    return formula + _charge_suffix(charge)


def _expand_electrolyte(smiles: str) -> tuple[tuple[int, str], ...]:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return ((1, smiles),)
    frags = Chem.GetMolFrags(mol, asMols=True, sanitizeFrags=True)
    if len(frags) > 1 and all(sum(a.GetFormalCharge() for a in f.GetAtoms()) != 0 for f in frags):
        counts: dict[str, int] = {}
        for f in frags:
            label = _ion_label_from_fragment(f)
            counts[label] = counts.get(label, 0) + 1
        return tuple(sorted(((n, label) for label, n in counts.items()), key=lambda x: x[1]))
    formula = rdMolDescriptors.CalcMolFormula(mol)
    return _STRONG_ELECTROLYTES.get(formula, ((1, formula),))


def net_ionic_equation(reactant_pairs, product_pairs) -> NetIonicResult | None:
    def expand(pairs):
        out: dict[str, Fraction] = {}
        for coeff, smiles in pairs:
            for mult, label in _expand_electrolyte(smiles):
                out[label] = out.get(label, Fraction(0)) + Fraction(coeff) * mult
        return out

    left, right = expand(reactant_pairs), expand(product_pairs)
    cancelled: list[str] = []
    for label in sorted(set(left) & set(right)):
        n = min(left[label], right[label])
        if n > 0:
            left[label] -= n
            right[label] -= n
            cancelled.append(label)

    if not any(v > 0 for v in left.values()) or not any(v > 0 for v in right.values()):
        return None

    den = reduce(_lcm, [v.denominator for v in list(left.values()) + list(right.values()) if v] or [1], 1)
    li = [(int(v * den), k) for k, v in left.items() if v > 0]
    ri = [(int(v * den), k) for k, v in right.items() if v > 0]
    common = reduce(gcd, [n for n, _ in li + ri], 0) or 1
    return NetIonicResult(
        tuple((n // common, k) for n, k in li),
        tuple((n // common, k) for n, k in ri),
        tuple(cancelled),
    )


def format_net_ionic(result: NetIonicResult) -> str:
    def side(items):
        return " + ".join(("" if n == 1 else f"{n} ") + label for n, label in items)
    return f"{side(result.reactants)} → {side(result.products)}"
