"""Physical constants used by :mod:`orca_engine`."""

from __future__ import annotations


class PhysConst:
    """Physical conversion constants.

    Values are kept as class attributes to make formulas readable while
    avoiding object allocation in tight reporting or thermochemistry loops.
    """

    HARTREE_TO_EV: float = 27.211386245988
    HARTREE_TO_KCAL: float = 627.50947406311
    HARTREE_TO_KJ: float = 2625.4996394799
    BOHR_TO_ANG: float = 0.529177210903
    GAS_CONSTANT_KCAL_MOL_K: float = 0.00198720425864083
    GAS_CONSTANT_J_MOL_K: float = 8.314462618
    CAL_TO_JOULE: float = 4.184
