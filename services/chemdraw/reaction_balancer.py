from __future__ import annotations

"""Exact stoichiometric balancing for resolved reaction structures.

The balancer works from RDKit molecular graphs and conserves every element and
formal charge.  For one-dimensional systems it returns the unique smallest
positive integer solution.  For modest underdetermined systems it searches the
exact rational solution space for the smallest all-positive integer solution
without inventing any species.
"""

from fractions import Fraction
from functools import reduce
from itertools import product
from math import gcd

from rdkit import Chem


_MAX_FREE_DIM = 3
_MAX_FREE_VALUE = 24


def _lcm(a: int, b: int) -> int:
    if not a or not b:
        return 0
    return abs(a * b) // gcd(a, b)


def _species_vector(smiles: str) -> tuple[dict[str, int], int] | None:
    mol = Chem.MolFromSmiles(smiles or "")
    if mol is None:
        return None
    charge = sum(atom.GetFormalCharge() for atom in mol.GetAtoms())
    try:
        mol_h = Chem.AddHs(mol)
    except Exception:
        mol_h = mol
    counts: dict[str, int] = {}
    for atom in mol_h.GetAtoms():
        symbol = atom.GetSymbol()
        counts[symbol] = counts.get(symbol, 0) + 1
    return counts, charge


def _rref(matrix: list[list[Fraction]]) -> tuple[list[list[Fraction]], list[int]]:
    if not matrix:
        return matrix, []
    a = [row[:] for row in matrix]
    rows, cols = len(a), len(a[0])
    pivots: list[int] = []
    r = 0
    for c in range(cols):
        pivot = next((i for i in range(r, rows) if a[i][c] != 0), None)
        if pivot is None:
            continue
        a[r], a[pivot] = a[pivot], a[r]
        p = a[r][c]
        a[r] = [v / p for v in a[r]]
        for i in range(rows):
            if i == r or a[i][c] == 0:
                continue
            f = a[i][c]
            a[i] = [x - f * y for x, y in zip(a[i], a[r])]
        pivots.append(c)
        r += 1
        if r == rows:
            break
    return a, pivots


def _normalize_positive(vec: list[Fraction]) -> list[int] | None:
    if not vec or any(v <= 0 for v in vec):
        return None
    denominator_lcm = reduce(_lcm, (v.denominator for v in vec), 1)
    ints = [int(v * denominator_lcm) for v in vec]
    common = reduce(gcd, (abs(v) for v in ints if v), 0) or 1
    ints = [v // common for v in ints]
    if not all(v > 0 for v in ints):
        return None
    return ints


def _smallest_positive_null_vector(matrix: list[list[Fraction]]) -> list[Fraction] | None:
    if not matrix or not matrix[0]:
        return None
    rref, pivots = _rref(matrix)
    n = len(matrix[0])
    free = [c for c in range(n) if c not in pivots]
    if not free:
        return None

    # Unique null-space direction: exact and immediate.
    if len(free) == 1:
        f = free[0]
        x = [Fraction(0) for _ in range(n)]
        x[f] = Fraction(1)
        for row_idx in range(len(pivots) - 1, -1, -1):
            pcol = pivots[row_idx]
            row = rref[row_idx]
            x[pcol] = -sum(row[j] * x[j] for j in range(pcol + 1, n))
        if all(v < 0 for v in x):
            x = [-v for v in x]
        return x if all(v > 0 for v in x) else None

    # Multiple valid stoichiometric directions occur when several independent
    # reactants can contribute the same conserved elements.  Search only small
    # exact systems so CPU use remains bounded on the free server.
    if len(free) > _MAX_FREE_DIM:
        return None

    best: list[int] | None = None
    best_score: tuple | None = None
    for values in product(range(1, _MAX_FREE_VALUE + 1), repeat=len(free)):
        x = [Fraction(0) for _ in range(n)]
        for col, value in zip(free, values):
            x[col] = Fraction(value)
        for row_idx in range(len(pivots) - 1, -1, -1):
            pcol = pivots[row_idx]
            row = rref[row_idx]
            x[pcol] = -sum(row[j] * x[j] for j in range(pcol + 1, n))
        ints = _normalize_positive(x)
        if ints is None:
            continue
        score = (sum(ints), max(ints), tuple(ints))
        if best_score is None or score < best_score:
            best, best_score = ints, score

    if best is None:
        return None
    return [Fraction(v) for v in best]


def balance_reaction_smiles(
    reactant_smiles: list[str], product_smiles: list[str]
) -> tuple[list[int], list[int]] | None:
    """Return the smallest positive integer coefficients found, or ``None``.

    Species are fixed: the routine never invents H2O, H+, OH-, electrons, or any
    other missing species.  Formal charge is conserved alongside every element.
    """
    if not reactant_smiles or not product_smiles:
        return None
    species = reactant_smiles + product_smiles
    vectors = [_species_vector(s) for s in species]
    if any(v is None for v in vectors):
        return None

    elements = sorted({e for item in vectors if item for e in item[0]})
    rows: list[list[Fraction]] = []
    nr = len(reactant_smiles)
    for element in elements:
        row: list[Fraction] = []
        for i, item in enumerate(vectors):
            count = item[0].get(element, 0) if item else 0
            row.append(Fraction(count if i < nr else -count))
        rows.append(row)

    charge_row = [
        Fraction((item[1] if i < nr else -item[1]) if item else 0)
        for i, item in enumerate(vectors)
    ]
    if any(charge_row):
        rows.append(charge_row)

    vec = _smallest_positive_null_vector(rows)
    if vec is None:
        return None

    ints = _normalize_positive(vec)
    if ints is None:
        return None
    return ints[:nr], ints[nr:]
