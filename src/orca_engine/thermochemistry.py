"""Reaction parsing and thermochemistry calculations."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence

from orca_engine.constants import PhysConst
from orca_engine.models import (
    BDEResult,
    ConsistencyReport,
    EnergyKind,
    JobData,
    MoleculeData,
    Reaction,
    ReactionResult,
    ReactionTerm,
    normalize_molecule_name,
)
from orca_engine.regex import FLOAT

LOGGER = logging.getLogger(__name__)

#: Tolerance for treating a fractional atom or charge imbalance as zero.
BALANCE_TOLERANCE = 1.0e-6


class ReactionParseError(ValueError):
    """Raised when a reaction equation cannot be parsed."""


class ThermochemistryEngine:
    """Calculate reaction energies from parsed ORCA molecule data.

    Reaction energies are only meaningful when every participating species was
    computed at the same level of theory and the equation is stoichiometrically
    balanced. Both conditions are checked and reported alongside every result
    rather than being assumed.

    Args:
        molecules: Mapping from molecule names to parsed molecule data. Keys are
            normalized internally, so reaction equations are matched
            case-insensitively against file stems.
    """

    _ARROW = re.compile(r"\s*(?:<==>|<=>|<->|-->|->|=>|=)\s*")
    _TERM = re.compile(
        rf"^\s*(?:(?P<coeff>{FLOAT})\s*[*]\s*(?P<name_star>[A-Za-z0-9_.-]+)"
        rf"|(?P<coeff_sp>{FLOAT})\s+(?P<name_sp>[A-Za-z0-9_.-]+)"
        rf"|(?P<coeff_lead>{FLOAT})\s*(?P<name_lead>[A-Za-z_][A-Za-z0-9_.-]*)"
        rf"|(?P<bare_name>[A-Za-z0-9_.-]+))\s*$"
    )

    def __init__(self, molecules: Mapping[str, MoleculeData]) -> None:
        """Initialize the engine from a molecule mapping.

        Args:
            molecules: Mapping from molecule names to parsed molecule data.
        """

        self._molecules = {
            normalize_molecule_name(name): molecule for name, molecule in molecules.items()
        }

    @classmethod
    def parse_reaction(cls, equation: str) -> Reaction:
        """Parse a reaction equation into reactant and product terms.

        Args:
            equation: Reaction equation such as ``"1A + 2B -> 1C"``.

        Returns:
            Parsed reaction object.

        Raises:
            ReactionParseError: If the equation is malformed.
        """

        parts = cls._ARROW.split(equation.strip())
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ReactionParseError(
                "Reaction equation must contain one arrow, for example: A + 2B -> C"
            )

        reactants = cls._parse_side(parts[0], side_name="reactants")
        products = cls._parse_side(parts[1], side_name="products")
        return Reaction(equation=equation, reactants=reactants, products=products)

    def resolve_reaction(self, reaction: Reaction | str) -> Reaction:
        """Parse an equation and resolve coefficient/name ambiguity.

        A token such as ``2b`` is syntactically "two of species b", but a file
        named ``2b.out`` is common in synthetic chemistry. If the raw token
        matches a parsed species and the split name does not, the token is
        reinterpreted as a bare molecule name.

        Args:
            reaction: Parsed reaction or a reaction equation string.

        Returns:
            A reaction whose terms refer to species that exist where possible.
        """

        parsed = self.parse_reaction(reaction) if isinstance(reaction, str) else reaction
        resolved_reactants = [self._resolve_term(term) for term in parsed.reactants]
        resolved_products = [self._resolve_term(term) for term in parsed.products]
        if resolved_reactants == parsed.reactants and resolved_products == parsed.products:
            return parsed
        return Reaction(
            equation=parsed.equation,
            reactants=resolved_reactants,
            products=resolved_products,
        )

    def evaluate(self, reaction: Reaction | str) -> ReactionResult:
        """Evaluate Delta E, Delta E0, Delta G, Delta H, Delta S, and K_eq for a reaction.

        Args:
            reaction: Parsed reaction or a reaction equation string.

        Returns:
            Reaction result in kcal/mol, with a list of missing references,
            extended thermodynamic descriptors, and a physical consistency report.
        """

        parsed = self.resolve_reaction(reaction)
        result = ReactionResult(reaction=parsed)

        electronic, electronic_missing = self._calculate_delta(parsed, EnergyKind.ELECTRONIC)
        e0, e0_missing = self._calculate_delta(parsed, EnergyKind.ELECTRONIC_ZPE)
        gibbs, gibbs_missing = self._calculate_delta(parsed, EnergyKind.GIBBS)
        enthalpy, enthalpy_missing = self._calculate_delta(parsed, EnergyKind.ENTHALPY)

        result.delta_electronic_kcal_mol = electronic
        result.delta_e0_kcal_mol = e0
        result.delta_g_kcal_mol = gibbs
        result.delta_h_kcal_mol = enthalpy
        result.missing_references = sorted(
            set(electronic_missing + e0_missing + gibbs_missing + enthalpy_missing)
        )
        result.consistency = self.check_consistency(parsed)

        # Extended thermochemistry factors:
        temperatures = result.consistency.mixed_temperatures_k
        temp_k = (
            temperatures[0]
            if len(temperatures) == 1
            else 298.15
            if not temperatures
            else None
        )
        result.temperature_k = temp_k

        if gibbs is not None:
            result.is_exergonic = gibbs < 0.0
            if temp_k is not None and temp_k > 0:
                if enthalpy is not None:
                    # Delta S in cal / (mol * K)
                    result.delta_entropy_cal_mol_k = ((enthalpy - gibbs) * 1000.0) / temp_k

                # K_eq = exp(-Delta G / (R * T))
                r_gas = PhysConst.GAS_CONSTANT_KCAL_MOL_K
                exponent = -gibbs / (r_gas * temp_k)
                if exponent > 700.0:
                    result.equilibrium_constant_keq = float("inf")
                elif exponent < -700.0:
                    result.equilibrium_constant_keq = 0.0
                else:
                    import math

                    result.equilibrium_constant_keq = math.exp(exponent)

        self._log_consistency(result.consistency)
        return result

    def check_consistency(self, reaction: Reaction) -> ConsistencyReport:
        """Check atom balance, charge balance, and level-of-theory agreement.

        Args:
            reaction: Reaction to validate.

        Returns:
            A :class:`ConsistencyReport`. Checks that cannot be evaluated -- for
            example atom balance when no geometry was printed -- are left as
            ``None`` rather than being reported as passing.
        """

        report = ConsistencyReport()
        self._check_atom_balance(reaction, report)
        self._check_charge_balance(reaction, report)
        self._check_levels_of_theory(reaction, report)
        self._check_terminations(reaction, report)
        return report

    def _check_terminations(self, reaction: Reaction, report: ConsistencyReport) -> None:
        """Flag species whose calculations reported an error termination.

        Filtering on "terminated normally" is a quality-control step many users
        believe they have applied. A reaction energy drawn from a failed
        calculation must not pass silently.
        """

        failed = [
            name
            for name in dict.fromkeys(reaction.species_names())
            if (molecule := self._molecules.get(normalize_molecule_name(name))) is not None
            and molecule.had_error_termination
        ]
        report.species_with_errors = failed
        if failed:
            report.warnings.append(
                f"Calculations for {', '.join(failed)} reported an error termination."
            )

    def evaluate_bde(
        self,
        parent: str,
        fragments: Sequence[str | ReactionTerm],
        kind: EnergyKind = EnergyKind.ELECTRONIC,
    ) -> BDEResult:
        """Evaluate a bond dissociation energy from a parent and fragments.

        Args:
            parent: Parent molecule name, matched against parsed file stems.
            fragments: Product fragments. Strings imply coefficient 1.0;
                :class:`ReactionTerm` objects allow non-unity coefficients.
            kind: Energy quantity used for the BDE. ``ELECTRONIC`` gives
                D_e-like values, while ``ELECTRONIC_ZPE`` gives D_0-like values
                only when ZPE references exist for all species.

        Returns:
            BDE result in kcal/mol. Missing references are reported instead of
            silently substituting unavailable corrections.
        """

        product_terms = [
            fragment
            if isinstance(fragment, ReactionTerm)
            else ReactionTerm(coefficient=1.0, molecule_name=fragment, raw_text=fragment)
            for fragment in fragments
        ]
        reaction = Reaction(
            equation=f"{parent} -> " + " + ".join(_format_term(term) for term in product_terms),
            reactants=[ReactionTerm(coefficient=1.0, molecule_name=parent, raw_text=parent)],
            products=product_terms,
        )
        return self._build_bde_result(reaction, kind)

    def evaluate_bde_equation(
        self,
        equation: str,
        kind: EnergyKind = EnergyKind.ELECTRONIC,
    ) -> BDEResult:
        """Evaluate BDE from a dissociation equation string.

        Args:
            equation: Equation such as ``"parent -> radical + h"``.
            kind: Energy quantity used for the BDE.

        Returns:
            BDE result in kcal/mol.

        Raises:
            ReactionParseError: If the equation is malformed or has more than
                one reactant term.
        """

        reaction = self.resolve_reaction(equation)
        if len(reaction.reactants) != 1 or reaction.reactants[0].coefficient != 1.0:
            raise ReactionParseError("BDE equations must have exactly one parent reactant.")
        return self._build_bde_result(reaction, kind)

    @staticmethod
    def calculate_reaction_energy(
        rxn: Reaction,
        molecules: Mapping[str, MoleculeData],
        kind: EnergyKind = EnergyKind.ELECTRONIC_ZPE,
    ) -> float | None:
        """Calculate one reaction energy without the surrounding report.

        Args:
            rxn: Parsed reaction object.
            molecules: Mapping from molecule names to parsed data.
            kind: Energy kind to calculate.

        Returns:
            Delta energy in kcal/mol, or ``None`` when a reference is missing.
        """

        engine = ThermochemistryEngine(molecules)
        delta, missing = engine._calculate_delta(rxn, kind)
        for missing_reference in missing:
            LOGGER.warning("Missing reference for reaction energy: %s", missing_reference)
        return delta

    def _build_bde_result(self, reaction: Reaction, kind: EnergyKind) -> BDEResult:
        """Assemble a BDE result with consistency checks and logging."""

        bde, missing = self._calculate_delta(reaction, kind)
        for missing_reference in missing:
            LOGGER.warning("Missing reference for BDE: %s", missing_reference)
        consistency = self.check_consistency(reaction)
        self._log_consistency(consistency)
        return BDEResult(
            reaction=reaction,
            energy_kind=kind,
            bde_kcal_mol=bde,
            missing_references=missing,
            consistency=consistency,
        )

    @classmethod
    def _parse_side(cls, side: str, side_name: str) -> list[ReactionTerm]:
        """Parse one side of a reaction equation."""

        terms = []
        for raw_term in side.split("+"):
            text = raw_term.strip()
            if not text:
                raise ReactionParseError(f"Empty term in {side_name}: {side}")
            match = cls._TERM.match(text)
            if match is None:
                raise ReactionParseError(f"Could not parse reaction term '{text}'")

            if match.group("coeff") is not None:
                coeff_text = match.group("coeff")
                name = match.group("name_star")
            elif match.group("coeff_sp") is not None:
                coeff_text = match.group("coeff_sp")
                name = match.group("name_sp")
            elif match.group("coeff_lead") is not None:
                coeff_text = match.group("coeff_lead")
                name = match.group("name_lead")
            else:
                coeff_text = None
                name = match.group("bare_name")

            coefficient = float(coeff_text) if coeff_text else 1.0
            if coefficient <= 0:
                raise ReactionParseError(f"Coefficient must be positive in term '{text}'")

            terms.append(
                ReactionTerm(
                    coefficient=coefficient,
                    molecule_name=name,
                    raw_text=text,
                )
            )

        return terms

    def _resolve_term(self, term: ReactionTerm) -> ReactionTerm:
        """Reinterpret ``2b`` as a molecule name when species ``2b`` exists."""

        raw = normalize_molecule_name(term.raw_text)
        if not raw or term.coefficient == 1.0:
            return term
        split_name = normalize_molecule_name(term.molecule_name)
        if raw in self._molecules and split_name not in self._molecules:
            LOGGER.info(
                "Interpreting %r as the molecule name %r rather than coefficient %g times %r.",
                term.raw_text,
                raw,
                term.coefficient,
                term.molecule_name,
            )
            return ReactionTerm(coefficient=1.0, molecule_name=raw, raw_text=term.raw_text)
        return term

    def _check_atom_balance(self, reaction: Reaction, report: ConsistencyReport) -> None:
        """Compare element counts on both sides of a reaction."""

        reactant_atoms, reactant_known = self._side_stoichiometry(reaction.reactants)
        product_atoms, product_known = self._side_stoichiometry(reaction.products)
        if not (reactant_known and product_known):
            return

        deltas = {
            element: product_atoms.get(element, 0.0) - reactant_atoms.get(element, 0.0)
            for element in set(reactant_atoms) | set(product_atoms)
        }
        imbalance = {
            element: round(value)
            for element, value in deltas.items()
            if abs(value) > BALANCE_TOLERANCE
        }
        report.atom_imbalance = imbalance
        report.atom_balanced = not imbalance
        if imbalance:
            rendered = ", ".join(
                f"{element}{value:+d}" for element, value in sorted(imbalance.items())
            )
            report.warnings.append(
                f"Reaction is not atom balanced (products - reactants: {rendered})."
            )

    def _check_charge_balance(self, reaction: Reaction, report: ConsistencyReport) -> None:
        """Compare total charge on both sides of a reaction."""

        reactant_charge, reactant_known = self._side_charge(reaction.reactants)
        product_charge, product_known = self._side_charge(reaction.products)
        if not (reactant_known and product_known):
            return

        imbalance = product_charge - reactant_charge
        report.charge_imbalance = imbalance
        report.charge_balanced = abs(imbalance) <= BALANCE_TOLERANCE
        if not report.charge_balanced:
            report.warnings.append(
                f"Reaction is not charge balanced (products - reactants: {imbalance:+g})."
            )

    def _check_levels_of_theory(self, reaction: Reaction, report: ConsistencyReport) -> None:
        """Flag species computed with different methods or temperatures.

        Every job of every participating species is inspected, not just the one
        that supplies the electronic energy. Different quantities are drawn
        from different jobs by design -- ``_get_best_e0`` deliberately combines
        an electronic energy from one job with a ZPE from another -- so
        checking a single reference job would certify a reaction whose terms
        actually come from two different levels of theory.
        """

        levels: list[tuple[str, str, str]] = []
        temperatures: list[float] = []
        for name in dict.fromkeys(reaction.species_names()):
            molecule = self._molecules.get(normalize_molecule_name(name))
            if molecule is None:
                continue
            for level in molecule.levels_of_theory():
                if level not in levels:
                    levels.append(level)
            for job in molecule.jobs:
                temperature = job.metadata.temperature_k
                if temperature is not None and temperature not in temperatures:
                    temperatures.append(temperature)

        report.mixed_levels_of_theory = levels
        report.mixed_temperatures_k = temperatures
        if len(levels) > 1:
            rendered = "; ".join(
                f"ORCA {version}/{basis}/{solvation}" for version, basis, solvation in levels
            )
            report.warnings.append(
                f"Species were computed at {len(levels)} different levels of theory ({rendered}). "
                f"The reaction energy is not physically meaningful."
            )
        if len(temperatures) > 1:
            rendered_t = ", ".join(f"{value:g} K" for value in temperatures)
            report.warnings.append(
                f"Gibbs energies were computed at different temperatures ({rendered_t})."
            )

    def _side_stoichiometry(self, terms: list[ReactionTerm]) -> tuple[dict[str, float], bool]:
        """Return weighted element counts for one side and whether all are known."""

        totals: defaultdict[str, float] = defaultdict(float)
        for term in terms:
            job = self._reference_job(term.molecule_name)
            if job is None or not job.elements:
                return dict(totals), False
            for element, count in job.stoichiometry().items():
                totals[element] += term.coefficient * count
        return dict(totals), True

    def _side_charge(self, terms: list[ReactionTerm]) -> tuple[float, bool]:
        """Return the weighted total charge for one side and whether it is known."""

        total = 0.0
        for term in terms:
            job = self._reference_job(term.molecule_name)
            if job is None or job.metadata.charge is None:
                return total, False
            total += term.coefficient * job.metadata.charge
        return total, True

    def _reference_job(self, molecule_name: str) -> JobData | None:
        """Return the job used as the energy reference for a species."""

        molecule = self._molecules.get(normalize_molecule_name(molecule_name))
        if molecule is None or not molecule.jobs:
            return None
        for job in reversed(molecule.jobs):
            if job.e_elec_eh is not None:
                return job
        return molecule.jobs[-1]

    def _log_consistency(self, report: ConsistencyReport) -> None:
        """Emit one warning per failed consistency check."""

        for warning in report.warnings:
            LOGGER.warning("%s", warning)

    def _calculate_delta(
        self,
        reaction: Reaction,
        kind: EnergyKind,
    ) -> tuple[float | None, list[str]]:
        """Calculate one Delta quantity and collect missing references."""

        missing: list[str] = []
        reactant_total = self._sum_terms(reaction.reactants, kind, missing)
        product_total = self._sum_terms(reaction.products, kind, missing)
        if missing:
            return None, missing
        return (product_total - reactant_total) * PhysConst.HARTREE_TO_KCAL, []

    def _sum_terms(
        self,
        terms: list[ReactionTerm],
        kind: EnergyKind,
        missing: list[str],
    ) -> float:
        """Sum stoichiometric energies for one reaction side in Hartree."""

        total = 0.0
        for term in terms:
            energy = self._get_best_energy(term.molecule_name, kind)
            if energy is None:
                missing.append(f"{term.molecule_name}:{kind.value}")
                continue
            total += term.coefficient * energy
        return total

    def _get_best_energy(self, molecule_name: str, kind: EnergyKind) -> float | None:
        """Return the final available energy for a molecule and quantity."""

        key = normalize_molecule_name(molecule_name)
        molecule = self._molecules.get(key)
        if molecule is None or not molecule.jobs:
            LOGGER.error("No parsed ORCA data found for species '%s'", molecule_name)
            return None

        if kind is EnergyKind.ELECTRONIC:
            return self._get_best_electronic(molecule_name, molecule.jobs)
        if kind is EnergyKind.ELECTRONIC_ZPE:
            return self._get_best_e0(molecule_name, molecule.jobs)

        for job in reversed(molecule.jobs):
            if kind is EnergyKind.GIBBS and job.gibbs_free_energy_eh is not None:
                return job.gibbs_free_energy_eh
            if kind is EnergyKind.ENTHALPY and job.total_enthalpy_eh is not None:
                return job.total_enthalpy_eh

        LOGGER.debug("No %s reference energy found for species '%s'", kind.value, molecule_name)
        return None

    def _get_best_electronic(self, molecule_name: str, jobs: list[JobData]) -> float | None:
        """Return final electronic energy in Hartree."""

        for job in reversed(jobs):
            if job.e_elec_eh is not None:
                return job.e_elec_eh

        LOGGER.debug("No electronic reference energy found for species '%s'", molecule_name)
        return None

    def _get_best_e0(self, molecule_name: str, jobs: list[JobData]) -> float | None:
        """Return final electronic plus ZPE energy, allowing split job blocks.

        A frequency job is frequently run separately from the single point that
        produced the electronic energy. Both are searched from the end of the
        job list, but only within the same molecule.
        """

        electronic: float | None = None
        zpe: float | None = None
        for job in reversed(jobs):
            if electronic is None and job.e_elec_eh is not None:
                electronic = job.e_elec_eh
            if zpe is None and job.zpe_eh is not None:
                zpe = job.zpe_eh
            if electronic is not None and zpe is not None:
                return electronic + zpe

        LOGGER.debug("No electronic+ZPE reference energy found for species '%s'", molecule_name)
        return None


def _format_term(term: ReactionTerm) -> str:
    """Format a reaction term for a generated equation string."""

    if term.coefficient == 1.0:
        return term.molecule_name
    return f"{term.coefficient:g} {term.molecule_name}"
