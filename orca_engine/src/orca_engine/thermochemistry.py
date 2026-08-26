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
        rf"^\s*(?:(?P<coeff>{FLOAT})\s*[*]\s*(?P<name_star>['\"]?[A-Za-z0-9_.\s-]+?['\"]?)"
        rf"|(?P<coeff_sp>{FLOAT})\s+(?P<name_sp>['\"]?[A-Za-z0-9_.\s-]+?['\"]?)"
        rf"|(?P<coeff_lead>{FLOAT})\s*(?P<name_lead>[A-Za-z_]['\"]?[A-Za-z0-9_.\s-]*['\"]?)"
        rf"|(?P<bare_name>['\"]?[A-Za-z0-9_.\s-]+?['\"]?))\s*$"
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

    def evaluate(
        self,
        reaction: Reaction | str,
        custom_temperature_k: float | None = None,
        custom_pressure_atm: float | None = None,
    ) -> ReactionResult:
        """Evaluate Delta E, Delta E0, Delta G, Delta H, Delta S, and K_eq for a reaction.

        Args:
            reaction: Parsed reaction or a reaction equation string.
            custom_temperature_k: Optional requested temperature for van 't Hoff extrapolation.
            custom_pressure_atm: Optional requested pressure for ideal gas standard state shift.

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
        temp_k = temperatures[0] if len(temperatures) == 1 else None

        # Determine temperature source
        if len(temperatures) == 1:
            result.temperature_source = "ORCA_OUTPUT"
        else:
            result.temperature_source = "NOT_REPORTED"

        pressures = result.consistency.mixed_pressures_atm
        if len(pressures) == 1:
            result.pressure_source = "ORCA_OUTPUT"
        else:
            result.pressure_source = "NOT_REPORTED"

        result.temperature_k = temp_k

        # Structured composite provenance
        composite_details: dict[str, Any] = {}
        for name in dict.fromkeys(parsed.species_names()):
            molecule = self._molecules.get(normalize_molecule_name(name))
            if molecule is None:
                continue
            latest_e_job = self._get_best_electronic_job(name, molecule.jobs)
            freq_job = self._get_best_freq_job(name, molecule.jobs)
            if latest_e_job and freq_job and latest_e_job is not freq_job:
                geom_match = bool(
                    latest_e_job.geometry_hash and freq_job.geometry_hash and
                    latest_e_job.geometry_hash == freq_job.geometry_hash
                )
                composite_details[name] = {
                    "is_composite": True,
                    "electronic_level": {
                        "method": latest_e_job.metadata.method,
                        "basis_set": latest_e_job.metadata.basis_set,
                        "dispersion": latest_e_job.metadata.dispersion,
                        "solvation": latest_e_job.metadata.solvation,
                        "e_elec_eh": latest_e_job.e_elec_eh,
                        "geometry_hash": latest_e_job.geometry_hash,
                    },
                    "frequency_level": {
                        "method": freq_job.metadata.method,
                        "basis_set": freq_job.metadata.basis_set,
                        "dispersion": freq_job.metadata.dispersion,
                        "solvation": freq_job.metadata.solvation,
                        "temperature_k": freq_job.metadata.temperature_k,
                        "pressure_atm": freq_job.metadata.pressure_atm,
                        "thermal_correction_eh": freq_job.thermal_correction_eh,
                        "zpe_eh": freq_job.zpe_eh,
                        "geometry_hash": freq_job.geometry_hash,
                    },
                    "geometry_match": geom_match,
                }
        result.composite_provenance = composite_details

        if gibbs is not None:
            result.is_exergonic = gibbs < 0.0
            if temp_k is not None and temp_k > 0:
                if enthalpy is not None:
                    # Delta S in cal / (mol * K) from H-G
                    delta_s_hg = ((enthalpy - gibbs) * 1000.0) / temp_k
                    result.delta_s_from_hg_cal_mol_k = delta_s_hg
                    result.delta_entropy_cal_mol_k = delta_s_hg
                    result.delta_entropy_j_mol_k = delta_s_hg * PhysConst.CAL_TO_JOULE

                    # Direct entropy
                    direct_s, direct_missing = self._calculate_delta(parsed, EnergyKind.ENTROPY)
                    if not direct_missing and direct_s is not None:
                        direct_s_cal_mol_k = direct_s * 1000.0
                        result.delta_s_direct_cal_mol_k = direct_s_cal_mol_k
                        result.delta_s_discrepancy_cal_mol_k = abs(delta_s_hg - direct_s_cal_mol_k)

                        if result.delta_s_discrepancy_cal_mol_k > 0.15:
                            result.consistency.entropy_consistency_passed = False
                            result.consistency.warnings.append(
                                f"THERMOCHEMISTRY_INCONSISTENCY: Delta S from H-G ({delta_s_hg:.2f}) "
                                f"disagrees with direct sum ({direct_s_cal_mol_k:.2f}) by >0.15 cal/(mol*K)."
                            )
                        else:
                            result.consistency.entropy_consistency_passed = True

                # K_eq = exp(-Delta G / (R * T))
                r_gas = PhysConst.GAS_CONSTANT_KCAL_MOL_K
                exponent = -gibbs / (r_gas * temp_k)
                if exponent > 700.0:
                    result.equilibrium_constant_keq = float("inf")
                    result.keq_status = "OVERFLOW"
                    result.keq_reason = "Delta G is too large and negative, K_eq approaches infinity."
                elif exponent < -700.0:
                    result.equilibrium_constant_keq = 0.0
                    result.keq_status = "UNDERFLOW"
                    result.keq_reason = "Delta G is too large and positive, K_eq approaches zero."
                else:
                    import math
                    result.equilibrium_constant_keq = math.exp(exponent)
                    result.keq_status = "AVAILABLE"

                # Custom temperature van 't Hoff extrapolation
                if custom_temperature_k is not None and custom_temperature_k > 0:
                    result.temperature_requested_k = custom_temperature_k
                    if abs(custom_temperature_k - temp_k) < 1e-4:
                        result.delta_g_requested_kcal_mol = gibbs
                        result.keq_requested = result.equilibrium_constant_keq
                        result.temperature_treatment_mode = "RIGOROUS_ORCA_THERMO"
                    elif enthalpy is not None and result.delta_entropy_cal_mol_k is not None:
                        delta_s_kcal_mol_k = result.delta_entropy_cal_mol_k / 1000.0
                        g_req = enthalpy - custom_temperature_k * delta_s_kcal_mol_k
                        result.delta_g_requested_kcal_mol = g_req
                        result.temperature_treatment_mode = "VAN_T_HOFF_DELTA_CP_ZERO"
                        result.consistency.warnings.append(
                            f"VAN_T_HOFF_APPROXIMATION: Gibbs free energy and K_eq evaluated at requested T={custom_temperature_k:g} K "
                            f"extrapolated from ORCA calculation at T={temp_k:g} K assuming temperature-independent Delta H and Delta S (Delta Cp = 0)."
                        )
                        exponent_req = -g_req / (r_gas * custom_temperature_k)
                        if exponent_req > 700.0:
                            result.keq_requested = float("inf")
                        elif exponent_req < -700.0:
                            result.keq_requested = 0.0
                        else:
                            import math
                            result.keq_requested = math.exp(exponent_req)

                # Pressure evaluation & ideal gas correction
                p_eval = custom_pressure_atm if custom_pressure_atm is not None and custom_pressure_atm > 0 else (pressures[0] if len(pressures) == 1 else None)
                result.pressure_atm = p_eval
                if p_eval is not None and abs(p_eval - 1.0) > 1e-4:
                    n_prod_gas = sum(term.coefficient for term in parsed.products if (m := self._molecules.get(normalize_molecule_name(term.molecule_name))) and any(j.metadata.phase == "Gas" for j in m.jobs))
                    n_react_gas = sum(term.coefficient for term in parsed.reactants if (m := self._molecules.get(normalize_molecule_name(term.molecule_name))) and any(j.metadata.phase == "Gas" for j in m.jobs))
                    delta_n_gas = n_prod_gas - n_react_gas
                    if delta_n_gas != 0:
                        import math
                        delta_g_press = delta_n_gas * r_gas * temp_k * math.log(p_eval)
                        g_press_corr = gibbs + delta_g_press
                        result.delta_g_pressure_corrected_kcal_mol = g_press_corr
                        result.pressure_correction_applied = True
                        exponent_p = -g_press_corr / (r_gas * temp_k)
                        if exponent_p > 700.0:
                            result.keq_pressure_corrected = float("inf")
                            result.keq_pressure_status = "OVERFLOW"
                            result.keq_pressure_reason = "Pressure-corrected Delta G is too large and negative, K_eq(P) approaches infinity."
                        elif exponent_p < -700.0:
                            result.keq_pressure_corrected = 0.0
                            result.keq_pressure_status = "UNDERFLOW"
                            result.keq_pressure_reason = "Pressure-corrected Delta G is too large and positive, K_eq(P) approaches zero."
                        else:
                            result.keq_pressure_corrected = math.exp(exponent_p)
                            result.keq_pressure_status = "AVAILABLE"
                    else:
                        result.delta_g_pressure_corrected_kcal_mol = gibbs
                        result.keq_pressure_corrected = result.equilibrium_constant_keq
                        result.keq_pressure_status = result.keq_status
                        result.keq_pressure_reason = result.keq_reason
                        result.pressure_correction_applied = False
                else:
                    result.delta_g_pressure_corrected_kcal_mol = gibbs
                    result.keq_pressure_corrected = result.equilibrium_constant_keq
                    result.keq_pressure_status = result.keq_status
                    result.keq_pressure_reason = result.keq_reason
                    result.pressure_correction_applied = False
            else:
                result.keq_status = "NOT_REPORTED"
                result.keq_reason = "TEMPERATURE_MISSING"
                result.keq_pressure_status = "NOT_REPORTED"
                result.keq_pressure_reason = "TEMPERATURE_MISSING"
        else:
            result.keq_status = "NOT_REPORTED"
            result.keq_reason = "GIBBS_ENERGY_MISSING"
            result.keq_pressure_status = "NOT_REPORTED"
            result.keq_pressure_reason = "GIBBS_ENERGY_MISSING"

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
        self._check_stationary_points(reaction, report)
        return report

    def _check_stationary_points(self, reaction: Reaction, report: ConsistencyReport) -> None:
        """Flag participating species with non-minimum or unreliable stationary points."""
        sp_warns = []
        for name in dict.fromkeys(reaction.species_names()):
            if (molecule := self._molecules.get(normalize_molecule_name(name))) is not None:
                for job in molecule.jobs:
                    if job.stationary_point_status == "HIGHER_ORDER_SADDLE":
                        msg = f"HIGHER_ORDER_SADDLE: Species '{name}' has multiple imaginary frequencies ({job.imaginary_frequencies_count}) and is not a physical minimum."
                        sp_warns.append(msg)
                    elif job.stationary_point_status == "INCOMPLETE_FREQUENCIES":
                        msg = f"INCOMPLETE_FREQUENCIES: Species '{name}' has incomplete vibrational frequencies ({len(job.vibrational_frequencies_cm)} modes parsed)."
                        sp_warns.append(msg)
                    elif job.stationary_point_status == "FAILED_CALCULATION":
                        msg = f"FAILED_CALCULATION: Calculation for species '{name}' failed or did not terminate normally."
                        sp_warns.append(msg)
        report.stationary_point_warnings = sp_warns
        report.warnings.extend(sp_warns)

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

            if name:
                name = name.strip("'\"").strip()

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
        imbalance: dict[str, int | float] = {}
        for element, value in sorted(deltas.items()):
            if abs(value) > BALANCE_TOLERANCE:
                nearest_int = round(value)
                if abs(value - nearest_int) <= BALANCE_TOLERANCE:
                    imbalance[element] = nearest_int
                else:
                    imbalance[element] = round(value, 4)

        report.atom_imbalance = imbalance
        report.atom_balanced = not imbalance
        if imbalance:
            rendered_items = []
            for element, value in sorted(imbalance.items()):
                if isinstance(value, int):
                    rendered_items.append(f"{element}{value:+d}")
                else:
                    rendered_items.append(f"{element}{value:+g}")
            rendered = ", ".join(rendered_items)
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
        levels = []
        temperatures: list[float] = []
        pressures: list[float] = []
        solvations: list[str] = []
        methods: list[str] = []
        dispersions: list[str] = []
        
        for name in dict.fromkeys(reaction.species_names()):
            molecule = self._molecules.get(normalize_molecule_name(name))
            if molecule is None:
                continue
                
            latest_e_job = self._get_best_electronic_job(name, molecule.jobs)
            freq_job = self._get_best_freq_job(name, molecule.jobs)
            if latest_e_job and freq_job and latest_e_job is not freq_job:
                if latest_e_job.geometry_hash and freq_job.geometry_hash and latest_e_job.geometry_hash != freq_job.geometry_hash:
                    report.geometry_mismatches.append(f"{name}: GEOMETRY_MISMATCH")
                    report.warnings.append(f"Composite calculation for {name} has mismatched geometries between frequency and electronic step.")

            for level in molecule.levels_of_theory():
                if level not in levels:
                    levels.append(level)
                method, basis, disp, solv, solvent, version = level
                if solv not in solvations:
                    solvations.append(solv)
                if method not in methods:
                    methods.append(method)
                if disp not in dispersions:
                    dispersions.append(disp)
                    
            for job in molecule.jobs:
                temperature = job.metadata.temperature_k
                if temperature is not None and temperature not in temperatures:
                    temperatures.append(temperature)
                pressure = job.metadata.pressure_atm
                if pressure is not None:
                    # check if already exists with tolerance 0.01
                    if not any(abs(p - pressure) < 0.01 for p in pressures):
                        pressures.append(pressure)

        report.mixed_levels_of_theory = levels
        report.mixed_temperatures_k = temperatures
        report.mixed_pressures_atm = pressures
        report.mixed_solvations = solvations
        report.mixed_methods = methods
        report.mixed_dispersions = dispersions
        
        if len(pressures) > 1:
            report.pressure_balanced = False
            rendered_p = ", ".join(f"{value:g} atm" for value in pressures)
            report.warnings.append(f"PRESSURE_MISMATCH: Gibbs energies were computed at different pressures ({rendered_p}).")
        elif len(pressures) == 1:
            report.pressure_balanced = True
            
        if len(solvations) > 1:
            report.warnings.append("SOLVATION_MISMATCH: Reaction mixes gas-phase and solution-phase or different solvation models without explicit handling.")
            
        if len(methods) > 1:
            report.warnings.append("METHOD_MISMATCH: Species were computed using different theoretical methods.")

        if len(dispersions) > 1:
            report.warnings.append("DISPERSION_MISMATCH: Species were computed using different dispersion corrections.")

        if len(levels) > 1:
            report.warnings.append(f"Species were computed at {len(levels)} different levels of theory. The reaction energy may not be physically meaningful.")
            
        if len(temperatures) > 1:
            rendered_t = ", ".join(f"{value:g} K" for value in temperatures)
            report.warnings.append(f"Gibbs energies were computed at different temperatures ({rendered_t}).")


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
        """Return the final available energy for a molecule and quantity, supporting multi-level composite calculations."""

        key = normalize_molecule_name(molecule_name)
        molecule = self._molecules.get(key)
        if molecule is None or not molecule.jobs:
            LOGGER.error("No parsed ORCA data found for species '%s'", molecule_name)
            return None

        if kind is EnergyKind.ELECTRONIC:
            return self._get_best_electronic(molecule_name, molecule.jobs)
        if kind is EnergyKind.ELECTRONIC_ZPE:
            return self._get_best_e0(molecule_name, molecule.jobs)

        latest_electronic = self._get_best_electronic(molecule_name, molecule.jobs)
        freq_job = None
        for job in reversed(molecule.jobs):
            if (kind is EnergyKind.GIBBS and job.gibbs_free_energy_eh is not None) or \
               (kind is EnergyKind.ENTHALPY and job.total_enthalpy_eh is not None) or \
               (kind is EnergyKind.ENTROPY and job.entropy_term_eh is not None):
                freq_job = job
                break

        if freq_job is None:
            LOGGER.debug("No %s reference energy found for species '%s'", kind.value, molecule_name)
            return None

        # Multi-level composite handling: combine high-level SP electronic energy with thermal correction
        if freq_job.e_elec_eh is not None and latest_electronic is not None and abs(latest_electronic - freq_job.e_elec_eh) > 1e-6:
            if kind is EnergyKind.ENTHALPY and freq_job.total_enthalpy_eh is not None:
                h_corr = freq_job.total_enthalpy_eh - freq_job.e_elec_eh
                return latest_electronic + h_corr
            if kind is EnergyKind.GIBBS and freq_job.gibbs_free_energy_eh is not None:
                g_corr = freq_job.gibbs_free_energy_eh - freq_job.e_elec_eh
                return latest_electronic + g_corr

        if kind is EnergyKind.GIBBS and freq_job.gibbs_free_energy_eh is not None:
            return freq_job.gibbs_free_energy_eh
        if kind is EnergyKind.ENTHALPY and freq_job.total_enthalpy_eh is not None:
            return freq_job.total_enthalpy_eh
        if kind is EnergyKind.ENTROPY:
            if freq_job.entropy_term_eh is not None and freq_job.metadata.temperature_k and freq_job.metadata.temperature_k > 0:
                return freq_job.entropy_term_eh / freq_job.metadata.temperature_k
            if freq_job.entropy_correction_eh is not None and freq_job.metadata.temperature_k and freq_job.metadata.temperature_k > 0:
                return -freq_job.entropy_correction_eh / freq_job.metadata.temperature_k
            if freq_job.total_entropy_cal_mol_k is not None:
                return freq_job.total_entropy_cal_mol_k / (PhysConst.HARTREE_TO_KCAL * 1000.0)
            if freq_job.vibrational_entropy_cal_mol_k is not None:
                tot_s = (
                    (freq_job.vibrational_entropy_cal_mol_k or 0.0) +
                    (freq_job.rotational_entropy_cal_mol_k or 0.0) +
                    (freq_job.translational_entropy_cal_mol_k or 0.0) +
                    (freq_job.electronic_entropy_cal_mol_k or 0.0)
                )
                return tot_s / (PhysConst.HARTREE_TO_KCAL * 1000.0)

        return None


    def _get_best_electronic_job(self, molecule_name: str, jobs: list[JobData]) -> JobData | None:
        for job in reversed(jobs):
            if job.e_elec_eh is not None:
                return job
        return None

    def _get_best_freq_job(self, molecule_name: str, jobs: list[JobData]) -> JobData | None:
        for job in reversed(jobs):
            if job.gibbs_free_energy_eh is not None or job.total_enthalpy_eh is not None or job.entropy_term_eh is not None:
                return job
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
