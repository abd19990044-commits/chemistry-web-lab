from __future__ import annotations

"""Conservative advanced chemistry tools for the publication bot.

The module deliberately refuses ambiguous cases instead of inventing chemistry.
It provides:
- redox balancing in acidic/basic medium by enumerating only H2O/H+/OH-/e- helpers,
- conservative net-ionic reduction using explicit strong-electrolyte rules,
- conservative one-reactant/one-product atom mapping using a unique MCS,
- user-directed curved-arrow mechanism SVG rendering from atom indices.
"""

from dataclasses import dataclass
from fractions import Fraction
from functools import reduce
from itertools import product
from math import gcd
import re
from typing import Iterable

from rdkit import Chem
from rdkit.Chem import AllChem, rdFMCS, rdMolDescriptors
from rdkit.Chem.Draw import rdMolDraw2D


@dataclass(frozen=True)
class BalancedSpecies:
    coefficient: int
    label: str
    smiles: str | None = None


@dataclass(frozen=True)
class RedoxResult:
    reactants: tuple[BalancedSpecies, ...]
    products: tuple[BalancedSpecies, ...]
    medium: str


@dataclass(frozen=True)
class NetIonicResult:
    reactants: tuple[tuple[int, str], ...]
    products: tuple[tuple[int, str], ...]
    cancelled: tuple[str, ...]


def _lcm(a: int, b: int) -> int:
    if not a or not b:
        return 0
    return abs(a * b) // gcd(a, b)


def _rref(matrix: list[list[Fraction]]) -> tuple[list[list[Fraction]], list[int]]:
    if not matrix:
        return matrix, []
    a = [row[:] for row in matrix]
    rows, cols = len(a), len(a[0])
    pivots: list[int] = []
    r = 0
    for c in range(cols):
        pivot = next((i for i in range(r, rows) if a[i][c]), None)
        if pivot is None:
            continue
        a[r], a[pivot] = a[pivot], a[r]
        p = a[r][c]
        a[r] = [v / p for v in a[r]]
        for i in range(rows):
            if i == r or not a[i][c]:
                continue
            f = a[i][c]
            a[i] = [x - f * y for x, y in zip(a[i], a[r])]
        pivots.append(c)
        r += 1
        if r == rows:
            break
    return a, pivots


def _positive_null_vector(matrix: list[list[Fraction]]) -> list[int] | None:
    if not matrix or not matrix[0]:
        return None
    rr, pivots = _rref(matrix)
    n = len(matrix[0])
    free = [i for i in range(n) if i not in pivots]
    if len(free) != 1:
        return None
    x = [Fraction(0) for _ in range(n)]
    x[free[0]] = Fraction(1)
    for ri in range(len(pivots) - 1, -1, -1):
        pc = pivots[ri]
        x[pc] = -sum(rr[ri][j] * x[j] for j in range(pc + 1, n))
    if any(v == 0 for v in x):
        return None
    if all(v < 0 for v in x):
        x = [-v for v in x]
    if not all(v > 0 for v in x):
        return None
    dl = reduce(_lcm, (v.denominator for v in x), 1)
    ints = [int(v * dl) for v in x]
    common = reduce(gcd, (abs(v) for v in ints if v), 0) or 1
    ints = [v // common for v in ints]
    return ints if all(v > 0 for v in ints) else None


def species_vector(smiles: str) -> tuple[dict[str, int], int] | None:
    mol = Chem.MolFromSmiles(smiles or "")
    if mol is None:
        return None
    charge = sum(a.GetFormalCharge() for a in mol.GetAtoms())
    mh = Chem.AddHs(mol)
    counts: dict[str, int] = {}
    for atom in mh.GetAtoms():
        counts[atom.GetSymbol()] = counts.get(atom.GetSymbol(), 0) + 1
    return counts, charge


_HELPERS = {
    "H2O": ({"H": 2, "O": 1}, 0, "O"),
    "H+": ({"H": 1}, 1, "[H+]"),
    "OH-": ({"O": 1, "H": 1}, -1, "[OH-]"),
    "e-": ({}, -1, None),
}


def _balance_vectors(left: list[tuple[dict[str, int], int]], right: list[tuple[dict[str, int], int]]) -> tuple[list[int], list[int]] | None:
    vectors = left + right
    nr = len(left)
    elements = sorted({e for counts, _ in vectors for e in counts})
    rows: list[list[Fraction]] = []
    for element in elements:
        rows.append([
            Fraction((counts.get(element, 0) if i < nr else -counts.get(element, 0)))
            for i, (counts, _) in enumerate(vectors)
        ])
    charge_row = [Fraction(q if i < nr else -q) for i, (_, q) in enumerate(vectors)]
    if any(charge_row):
        rows.append(charge_row)
    vec = _positive_null_vector(rows)
    if vec is None:
        return None
    return vec[:nr], vec[nr:]


def balance_redox(reactant_smiles: list[str], product_smiles: list[str], medium: str = "acidic") -> RedoxResult | None:
    medium = medium.lower().strip()
    if medium not in {"acidic", "basic"} or not reactant_smiles or not product_smiles:
        return None
    base_left = [species_vector(s) for s in reactant_smiles]
    base_right = [species_vector(s) for s in product_smiles]
    if any(v is None for v in base_left + base_right):
        return None
    helper_names = ["H2O", "H+", "e-"] if medium == "acidic" else ["H2O", "OH-", "e-"]
    candidates: list[tuple[tuple[int, int, int], RedoxResult]] = []
    for states in product((-1, 0, 1), repeat=len(helper_names)):
        left_vec = list(base_left)
        right_vec = list(base_right)
        left_labels: list[tuple[str, str | None]] = [(Chem.MolToSmiles(Chem.MolFromSmiles(s)), s) for s in reactant_smiles]
        right_labels: list[tuple[str, str | None]] = [(Chem.MolToSmiles(Chem.MolFromSmiles(s)), s) for s in product_smiles]
        for name, state in zip(helper_names, states):
            if not state:
                continue
            counts, charge, smi = _HELPERS[name]
            if state < 0:
                left_vec.append((counts, charge)); left_labels.append((name, smi))
            else:
                right_vec.append((counts, charge)); right_labels.append((name, smi))
        solved = _balance_vectors(left_vec, right_vec)
        if solved is None:
            continue
        lc, rc = solved
        left_species = tuple(BalancedSpecies(c, label, smi) for c, (label, smi) in zip(lc, left_labels))
        right_species = tuple(BalancedSpecies(c, label, smi) for c, (label, smi) in zip(rc, right_labels))
        helper_count = sum(1 for s in states if s)
        total = sum(lc) + sum(rc)
        electron_coeff = sum(sp.coefficient for sp in left_species + right_species if sp.label == "e-")
        candidates.append(((total, helper_count, electron_coeff), RedoxResult(left_species, right_species, medium)))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    best_score = candidates[0][0]
    best = [r for score, r in candidates if score == best_score]
    return best[0] if len(best) == 1 else None


def format_redox(result: RedoxResult) -> str:
    def side(items: Iterable[BalancedSpecies]) -> str:
        return " + ".join(("" if sp.coefficient == 1 else f"{sp.coefficient} ") + sp.label for sp in items)
    return f"{side(result.reactants)} → {side(result.products)}"


_STRONG_ELECTROLYTES: dict[str, tuple[tuple[int, str], ...]] = {
    "HCl": ((1, "H+"), (1, "Cl-")), "HBr": ((1, "H+"), (1, "Br-")),
    "HI": ((1, "H+"), (1, "I-")), "HNO3": ((1, "H+"), (1, "NO3-")),
    "HClO4": ((1, "H+"), (1, "ClO4-")), "H2SO4": ((2, "H+"), (1, "SO4^2-")),
    "NaOH": ((1, "Na+"), (1, "OH-")), "KOH": ((1, "K+"), (1, "OH-")),
    "LiOH": ((1, "Li+"), (1, "OH-")), "NaCl": ((1, "Na+"), (1, "Cl-")),
    "KCl": ((1, "K+"), (1, "Cl-")), "NaNO3": ((1, "Na+"), (1, "NO3-")),
    "KNO3": ((1, "K+"), (1, "NO3-")), "Na2SO4": ((2, "Na+"), (1, "SO4^2-")),
    "K2SO4": ((2, "K+"), (1, "SO4^2-")),
}


def _ion_label_from_fragment(mol: Chem.Mol) -> str:
    formula = rdMolDescriptors.CalcMolFormula(mol)
    charge = sum(a.GetFormalCharge() for a in mol.GetAtoms())
    if charge == 0:
        return formula
    sign = "+" if charge > 0 else "-"
    mag = abs(charge)
    return formula + (sign if mag == 1 else f"^{mag}{sign}")


def _expand_electrolyte(smiles: str) -> tuple[tuple[int, str], ...]:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return ((1, smiles),)
    frags = Chem.GetMolFrags(mol, asMols=True, sanitizeFrags=True)
    if len(frags) > 1 and all(sum(a.GetFormalCharge() for a in f.GetAtoms()) != 0 for f in frags):
        counts: dict[str, int] = {}
        for f in frags:
            label = _ion_label_from_fragment(f); counts[label] = counts.get(label, 0) + 1
        return tuple(sorted(((n, label) for label, n in counts.items()), key=lambda x: x[1]))
    formula = rdMolDescriptors.CalcMolFormula(mol)
    return _STRONG_ELECTROLYTES.get(formula, ((1, formula),))


def net_ionic_equation(reactant_pairs: list[tuple[int | Fraction, str]], product_pairs: list[tuple[int | Fraction, str]]) -> NetIonicResult | None:
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
            left[label] -= n; right[label] -= n; cancelled.append(label)
    if not any(v > 0 for v in left.values()) or not any(v > 0 for v in right.values()):
        return None
    den = reduce(_lcm, [v.denominator for v in list(left.values()) + list(right.values()) if v] or [1], 1)
    li = [(int(v * den), k) for k, v in left.items() if v > 0]
    ri = [(int(v * den), k) for k, v in right.items() if v > 0]
    common = reduce(gcd, [n for n, _ in li + ri], 0) or 1
    return NetIonicResult(tuple((n // common, k) for n, k in li), tuple((n // common, k) for n, k in ri), tuple(cancelled))


def format_net_ionic(result: NetIonicResult) -> str:
    def side(items): return " + ".join(("" if n == 1 else f"{n} ") + label for n, label in items)
    return f"{side(result.reactants)} → {side(result.products)}"


def conservative_atom_map(reactant_smiles: list[str], product_smiles: list[str]) -> str | None:
    if len(reactant_smiles) != 1 or len(product_smiles) != 1:
        return None
    r, p = Chem.MolFromSmiles(reactant_smiles[0]), Chem.MolFromSmiles(product_smiles[0])
    if r is None or p is None:
        return None
    mcs = rdFMCS.FindMCS([r, p], atomCompare=rdFMCS.AtomCompare.CompareElements,
                         bondCompare=rdFMCS.BondCompare.CompareOrder, ringMatchesRingOnly=True,
                         completeRingsOnly=False, timeout=5)
    if mcs.canceled or mcs.numAtoms < 2:
        return None
    q = Chem.MolFromSmarts(mcs.smartsString)
    if q is None:
        return None
    rm, pm = r.GetSubstructMatches(q, uniquify=False, maxMatches=2), p.GetSubstructMatches(q, uniquify=False, maxMatches=2)
    if len(rm) != 1 or len(pm) != 1:
        return None
    if any(r.GetAtomWithIdx(a).GetIsotope() != p.GetAtomWithIdx(b).GetIsotope() for a, b in zip(rm[0], pm[0])):
        return None
    r2, p2 = Chem.Mol(r), Chem.Mol(p)
    next_map = 1
    for ra, pa in zip(rm[0], pm[0]):
        r2.GetAtomWithIdx(ra).SetAtomMapNum(next_map); p2.GetAtomWithIdx(pa).SetAtomMapNum(next_map); next_map += 1
    for mol in (r2, p2):
        for atom in mol.GetAtoms():
            if not atom.GetAtomMapNum(): atom.SetAtomMapNum(next_map); next_map += 1
    return f"{Chem.MolToSmiles(r2, isomericSmiles=True)}>>{Chem.MolToSmiles(p2, isomericSmiles=True)}"


_MECH_ARROW_RE = re.compile(r"^(\d+)\s*(->|~>)\s*(\d+)$")


def parse_mechanism_arrows(spec: str) -> list[tuple[int, int, int]] | None:
    arrows: list[tuple[int, int, int]] = []
    for token in [t.strip() for t in (spec or "").split(",") if t.strip()]:
        m = _MECH_ARROW_RE.fullmatch(token)
        if not m: return None
        arrows.append((int(m.group(1)), int(m.group(3)), 2 if m.group(2) == "->" else 1))
    return arrows or None


def render_mechanism_svg(smiles: str, arrows: list[tuple[int, int, int]] | None = None) -> bytes | None:
    """Render an atom-numbered molecule and optional user-directed curved arrows."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    arrows = arrows or []
    if any(a == b or a < 1 or b < 1 or a > mol.GetNumAtoms() or b > mol.GetNumAtoms() for a, b, _ in arrows):
        return None
    mol = Chem.Mol(mol); AllChem.Compute2DCoords(mol)
    for i, atom in enumerate(mol.GetAtoms(), 1): atom.SetAtomMapNum(i)
    w, h = 900, 650
    drawer = rdMolDraw2D.MolDraw2DSVG(w, h)
    opts = drawer.drawOptions(); opts.addAtomIndices = False; opts.fixedBondLength = 55; opts.bondLineWidth = 2.2
    rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol)
    coords = [drawer.GetDrawCoords(i) for i in range(mol.GetNumAtoms())]
    drawer.FinishDrawing(); raw = drawer.GetDrawingText()
    insert = ['<defs><marker id="pairArrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#141414"/></marker><marker id="fishArrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M1,1 L7,4" fill="none" stroke="#141414" stroke-width="1.5"/></marker></defs>']
    for start, end, electrons in arrows:
        p1, p2 = coords[start - 1], coords[end - 1]
        dx, dy = p2.x - p1.x, p2.y - p1.y; length = max((dx * dx + dy * dy) ** 0.5, 1.0)
        nx, ny = -dy / length, dx / length; bend = min(90.0, max(45.0, length * 0.35))
        cx, cy = (p1.x + p2.x) / 2 + nx * bend, (p1.y + p2.y) / 2 + ny * bend
        marker = "pairArrow" if electrons == 2 else "fishArrow"
        insert.append(f'<path d="M {p1.x:.1f},{p1.y:.1f} Q {cx:.1f},{cy:.1f} {p2.x:.1f},{p2.y:.1f}" fill="none" stroke="#141414" stroke-width="2.4" marker-end="url(#{marker})"/>')
    return raw.replace("</svg>", "".join(insert) + "</svg>").encode("utf-8")
