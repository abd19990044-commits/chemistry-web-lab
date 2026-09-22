"""Data models for parsed ORCA calculations and reaction thermochemistry."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
import hashlib
from typing import Any


class SpinChannel(str, Enum):
    """Orbital spin channel emitted by ORCA."""

    RESTRICTED = "restricted"
    ALPHA = "alpha"
    BETA = "beta"


class EnergyKind(str, Enum):
    """Thermochemical energy quantity used for reaction arithmetic."""

    ELECTRONIC = "electronic"
    ELECTRONIC_ZPE = "electronic_zpe"
    GIBBS = "gibbs"
    ENTHALPY = "enthalpy"
    ENTROPY = "entropy"


class CoordinateUnit(str, Enum):
    """Length unit of a parsed Cartesian coordinate block."""

    ANGSTROM = "angstrom"
    BOHR = "bohr"


def normalize_molecule_name(name: str) -> str:
    """Normalize molecule identifiers for case-insensitive matching.

    Args:
        name: User-facing molecule name or file stem.

    Returns:
        A stripped, lower-case key suitable for dictionary lookup.
    """

    return name.strip().lower()


@dataclass
class CalcMetadata:
    """Metadata describing an ORCA calculation.

    Attributes:
        orca_version: ORCA program version reported by the output header.
        basis_set: Basis set name reported by ORCA, when available.
        solvation: Solvation model reported by ORCA, when available.
        charge: Total molecular charge in units of the elementary charge.
        multiplicity: Spin multiplicity ``2S + 1``.
        temperature_k: Temperature used for the thermochemistry block, in
            kelvin. Gibbs energies are only comparable at a common temperature.
        pressure_atm: Pressure used for the thermochemistry block, in atm.
    """

    orca_version: str = "Unknown"
    method: str = "Unknown"
    basis_set: str = "Unknown"
    dispersion: str = "None"
    solvation: str = "None"
    solvent: str = "None"
    phase: str = "Gas"
    standard_state: str = "Gas Phase (1 atm)"
    relativistic: str = "None"
    charge: int | None = None
    multiplicity: int | None = None
    temperature_k: float | None = None
    pressure_atm: float | None = None
    temperature_source: str = "NOT_REPORTED"
    pressure_source: str = "NOT_REPORTED"
    quasi_rrho_treatment: str = "UNKNOWN"
    quasi_rrho_cutoff_cm: float | None = None

    def level_of_theory(self) -> tuple[str, str, str, str, str, str]:
        """Return the comparability key ``(method, basis_set, dispersion, solvation, solvent, version)``.

        Reaction energies are only physically meaningful when every species
        shares this key. :class:`~orca_engine.thermochemistry.ThermochemistryEngine`
        uses it to warn about inadvertently mixed levels of theory.
        """

        return (
            self.method,
            self.basis_set,
            self.dispersion,
            self.solvation,
            self.solvent,
            self.orca_version,
        )


@dataclass
class OrbitalEnergies:
    """Frontier orbital energies for one spin channel.

    Attributes:
        homo_ev: Highest occupied molecular orbital energy in electronvolt.
        lumo_ev: Lowest unoccupied molecular orbital energy in electronvolt.
    """

    homo_ev: float | None = None
    lumo_ev: float | None = None

    def update_from_occupation(self, occupation: float, energy_ev: float) -> None:
        """Update HOMO/LUMO values from an orbital table row.

        Encountering a further occupied orbital invalidates any previously
        assigned LUMO, so the pair always originates from the same orbital
        table even if the table is not strictly ordered.

        A LUMO is recorded even when the channel contains no occupied orbital
        at all. This is the situation for the beta channel of a one-electron
        doublet, where the lowest unoccupied level of the whole system is the
        first beta orbital. Requiring a HOMO first would leave that channel
        empty and push the reported LUMO up to the lowest alpha virtual.

        Args:
            occupation: ORCA orbital occupation number.
            energy_ev: Orbital energy in electronvolt.
        """

        if occupation > 1.0e-8:
            self.homo_ev = energy_ev
            self.lumo_ev = None
        elif self.lumo_ev is None:
            self.lumo_ev = energy_ev

    def has_data(self) -> bool:
        """Return whether this channel contains any parsed frontier value."""

        return self.homo_ev is not None or self.lumo_ev is not None


@dataclass
class JobData:
    """Parsed data for one ORCA job block.

    Multi-job ORCA outputs are represented as one :class:`JobData` per block.
    Coordinates are collected with negative slicing from tokenized coordinate
    lines, which keeps the parser robust to extra columns while preserving
    streaming behavior.

    Geometry optimizations print one orbital table, one coordinate block and
    one property section per step. Every such section replaces the previously
    stored values, so the retained data always describes a single, internally
    consistent structure: the final one.

    Attributes:
        metadata: Calculation metadata such as basis set and solvation model.
        e_elec_eh: Final single point electronic energy in Hartree.
        zpe_eh: Zero-point energy correction in Hartree.
        gibbs_free_energy_eh: Final Gibbs free energy in Hartree.
        total_enthalpy_eh: Total enthalpy in Hartree.
        entropy_term_eh: ORCA "Final entropy term", equal to ``+T*S`` in
            Hartree. This is an energy, not an entropy.
        entropy_correction_eh: ORCA "Total entropy correction", equal to
            ``-T*S`` in Hartree.
        elements: Element symbols from the final coordinate block.
        coords: Cartesian coordinates in the unit given by ``coords_unit``.
        coords_unit: Length unit of ``coords``.
        tddft_cm: TDDFT transition energies in cm^-1.
        tddft_fosc: TDDFT oscillator strengths.
        orbitals: Frontier orbital windows keyed by spin channel.
        terminated_normally: Status of the last termination banner in the
            block. Output files that were appended to across reruns can contain
            several banners; this reflects only the final one.
        termination_message: Error or termination message, when reported.
        error_messages: Every error-termination banner seen in the block, in
            order. A non-empty list with ``terminated_normally is True`` means
            an earlier attempt failed and was rerun into the same file.
    """

    metadata: CalcMetadata = field(default_factory=CalcMetadata)
    geometry_hash: str | None = None
    e_elec_eh: float | None = None
    zpe_eh: float | None = None
    gibbs_free_energy_eh: float | None = None
    total_enthalpy_eh: float | None = None
    entropy_term_eh: float | None = None
    entropy_correction_eh: float | None = None
    thermal_energy_eh: float | None = None
    thermal_correction_eh: float | None = None
    enthalpy_correction_eh: float | None = None
    gibbs_correction_eh: float | None = None
    vibrational_entropy_cal_mol_k: float | None = None
    rotational_entropy_cal_mol_k: float | None = None
    translational_entropy_cal_mol_k: float | None = None
    total_entropy_cal_mol_k: float | None = None
    elements: list[str] = field(default_factory=list)
    coords: list[tuple[float, float, float]] = field(default_factory=list)
    coords_raw: list[tuple[str, str, str]] = field(default_factory=list)
    coords_unit: CoordinateUnit | None = None
    tddft_cm: list[float] = field(default_factory=list)
    tddft_fosc: list[float] = field(default_factory=list)
    terminated_normally: bool | None = None
    termination_message: str | None = None
    error_messages: list[str] = field(default_factory=list)
    dipole_moment_debye: float | None = None
    s2_actual: float | None = None
    s2_ideal: float | None = None
    imaginary_frequencies_cm: list[float] = field(default_factory=list)
    imaginary_frequencies_count: int = 0
    vibrational_frequencies_cm: list[float] = field(default_factory=list)
    ir_frequencies_cm: list[float] = field(default_factory=list)
    ir_intensities_km_mol: list[float] = field(default_factory=list)
    ir_spectrum: list[dict[str, float | int]] = field(default_factory=list)
    stationary_point_status: str = "NO_FREQUENCY_CALCULATION"
    thermochemistry_reliability: str = "ELECTRONIC_ONLY"

    hirshfeld_charges: list[float] = field(default_factory=list)
    mulliken_charges: list[float] = field(default_factory=list)
    mayer_charges: list[float] = field(default_factory=list)
    mayer_valences: list[float] = field(default_factory=list)
    loewdin_charges: list[float] = field(default_factory=list)
    atomic_charges: dict[str, list[float]] = field(default_factory=dict)
    nmr_result: Any | None = None

    orbitals: dict[SpinChannel, OrbitalEnergies] = field(
        default_factory=lambda: {
            SpinChannel.RESTRICTED: OrbitalEnergies(),
            SpinChannel.ALPHA: OrbitalEnergies(),
            SpinChannel.BETA: OrbitalEnergies(),
        }
    )


    def compute_geometry_hash(self) -> None:
        """Compute a deterministic SHA-256 fingerprint of Cartesian coordinate table.

        Note: This is a Cartesian coordinate table hash (not a rotationally/translationally
        invariant canonical graph hash). It validates that two job steps (e.g. single-point
        and frequency calculations) share identical input coordinates in the same frame.
        """
        if not self.elements or not self.coords:
            self.geometry_hash = None
            return

        atom_strings = []
        for element, coord in zip(self.elements, self.coords):
            atom_strings.append(f"{element.upper()}:{coord[0]:.6f}:{coord[1]:.6f}:{coord[2]:.6f}")

        sorted_atoms = "".join(sorted(atom_strings))
        self.geometry_hash = hashlib.sha256(sorted_atoms.encode("utf-8")).hexdigest()

    def is_linear_geometry(self, colinear_tolerance: float = 1e-3) -> bool:
        """Return whether molecule coordinates form a strictly linear geometry."""
        real_coords = [
            coord for el, coord in zip(self.elements, self.coords)
            if not el.endswith(":") and el.upper() != "DA"
        ]
        if len(real_coords) < 3:
            return len(real_coords) == 2
        p0 = real_coords[0]
        # Find first non-coincident atom to define direction axis
        u0 = None
        for p in real_coords[1:]:
            v0 = (p[0] - p0[0], p[1] - p0[1], p[2] - p0[2])
            norm_v0 = (v0[0]**2 + v0[1]**2 + v0[2]**2)**0.5
            if norm_v0 >= 1e-6:
                u0 = (v0[0] / norm_v0, v0[1] / norm_v0, v0[2] / norm_v0)
                break
        if u0 is None:
            return False
        for pi in real_coords[1:]:
            vi = (pi[0] - p0[0], pi[1] - p0[1], pi[2] - p0[2])
            norm_vi = (vi[0]**2 + vi[1]**2 + vi[2]**2)**0.5
            if norm_vi < 1e-6:
                continue
            ui = (vi[0] / norm_vi, vi[1] / norm_vi, vi[2] / norm_vi)
            cross = (
                u0[1]*ui[2] - u0[2]*ui[1],
                u0[2]*ui[0] - u0[0]*ui[2],
                u0[0]*ui[1] - u0[1]*ui[0]
            )
            cross_mag = (cross[0]**2 + cross[1]**2 + cross[2]**2)**0.5
            dot = u0[0]*ui[0] + u0[1]*ui[1] + u0[2]*ui[2]
            if cross_mag > colinear_tolerance or abs(abs(dot) - 1.0) > colinear_tolerance:
                return False
        return True

    @property
    def expected_vibrational_modes_count(self) -> int | None:
        """Expected vibrational degrees of freedom (3N-6 for non-linear, 3N-5 for linear)."""
        real_count = len([el for el in self.elements if not el.endswith(":") and el.upper() != "DA"])
        if real_count <= 0:
            return None
        if real_count == 1:
            return 0
        if real_count == 2:
            return 1
        return (3 * real_count - 5) if self.is_linear_geometry() else (3 * real_count - 6)

    @property
    def expected_total_frequencies_count(self) -> int | None:
        """Expected total frequency records in ORCA output table (3N for N >= 2)."""
        real_count = len([el for el in self.elements if not el.endswith(":") and el.upper() != "DA"])
        if real_count <= 0:
            return None
        if real_count == 1:
            return 0
        return 3 * real_count

    @property
    def coords_xyz(self) -> list[tuple[float, float, float]]:
        """Deprecated alias for :attr:`coords`.

        Retained for backwards compatibility with code written against the
        pre-1.0 API, where the unit of this attribute was undefined.
        """

        return self.coords

    def reset_orbitals(self) -> None:
        """Clear all frontier orbital windows.

        Called when a new ``ORBITAL ENERGIES`` section begins so that HOMO and
        LUMO can never be drawn from two different optimization steps.
        """

        self.orbitals = {
            SpinChannel.RESTRICTED: OrbitalEnergies(),
            SpinChannel.ALPHA: OrbitalEnergies(),
            SpinChannel.BETA: OrbitalEnergies(),
        }

    def orbital_window(self, channel: SpinChannel) -> OrbitalEnergies:
        """Return the mutable orbital window for a spin channel.

        Args:
            channel: Restricted, alpha, or beta channel.

        Returns:
            The :class:`OrbitalEnergies` object for ``channel``.
        """

        return self.orbitals.setdefault(channel, OrbitalEnergies())

    @property
    def alpha_homo_ev(self) -> float | None:
        """Alpha HOMO energy in electronvolt, if present."""

        return self.orbitals[SpinChannel.ALPHA].homo_ev

    @property
    def alpha_lumo_ev(self) -> float | None:
        """Alpha LUMO energy in electronvolt, if present."""

        return self.orbitals[SpinChannel.ALPHA].lumo_ev

    @property
    def beta_homo_ev(self) -> float | None:
        """Beta HOMO energy in electronvolt, if present."""

        return self.orbitals[SpinChannel.BETA].homo_ev

    @property
    def beta_lumo_ev(self) -> float | None:
        """Beta LUMO energy in electronvolt, if present."""

        return self.orbitals[SpinChannel.BETA].lumo_ev

    @property
    def homo_ev(self) -> float | None:
        """Highest occupied orbital energy across spin channels.

        Restricted values are preferred. For unrestricted jobs the highest
        occupied energy across alpha and beta channels is returned, which is
        the physically meaningful frontier level.
        """

        restricted = self.orbitals[SpinChannel.RESTRICTED].homo_ev
        if restricted is not None:
            return restricted
        values = [value for value in (self.alpha_homo_ev, self.beta_homo_ev) if value is not None]
        return max(values) if values else None

    @property
    def lumo_ev(self) -> float | None:
        """Lowest unoccupied orbital energy across spin channels.

        Restricted values are preferred. For unrestricted jobs the lowest
        unoccupied energy across alpha and beta channels is returned.
        """

        restricted = self.orbitals[SpinChannel.RESTRICTED].lumo_ev
        if restricted is not None:
            return restricted
        values = [value for value in (self.alpha_lumo_ev, self.beta_lumo_ev) if value is not None]
        return min(values) if values else None

    @property
    def had_error_termination(self) -> bool:
        """Return whether any ORCA module reported an error termination.

        This stays ``True`` even when a later rerun in the same file finished
        normally, so that partially failed calculations cannot be mistaken for
        clean ones.
        """

        return bool(self.error_messages)

    @property
    def homo_lumo_gap_ev(self) -> float | None:
        """Frontier orbital gap in electronvolt, if both levels are present."""

        if self.homo_ev is None or self.lumo_ev is None:
            return None
        return self.lumo_ev - self.homo_ev

    @property
    def spin_contamination(self) -> float | None:
        """Spin contamination <S**2> - S(S+1) for unrestricted wavefunctions."""

        if self.s2_actual is None or self.s2_ideal is None:
            return None
        return self.s2_actual - self.s2_ideal

    @property
    def is_transition_state(self) -> bool:
        """Return whether structure is a transition state (exactly 1 imaginary frequency)."""

        return self.imaginary_frequencies_count == 1

    @property
    def ionization_potential_ev(self) -> float | None:
        """Koopmans' theorem vertical ionization potential in eV (IP ~ -E_HOMO)."""

        return -self.homo_ev if self.homo_ev is not None else None

    @property
    def electron_affinity_ev(self) -> float | None:
        """Koopmans' theorem vertical electron affinity in eV (EA ~ -E_LUMO)."""

        return -self.lumo_ev if self.lumo_ev is not None else None

    @property
    def chemical_hardness_ev(self) -> float | None:
        """Parr-Pearson chemical hardness eta in eV, (E_LUMO - E_HOMO) / 2."""

        if self.homo_ev is None or self.lumo_ev is None:
            return None
        return (self.lumo_ev - self.homo_ev) / 2.0

    @property
    def chemical_potential_ev(self) -> float | None:
        """Electronic chemical potential mu in eV, (E_HOMO + E_LUMO) / 2."""

        if self.homo_ev is None or self.lumo_ev is None:
            return None
        return (self.homo_ev + self.lumo_ev) / 2.0

    @property
    def electronegativity_ev(self) -> float | None:
        """Mulliken electronegativity chi in eV, -mu = -(E_HOMO + E_LUMO) / 2."""

        if self.chemical_potential_ev is None:
            return None
        return -self.chemical_potential_ev

    @property
    def chemical_softness_ev(self) -> float | None:
        """Chemical softness S = 1 / (2 * eta) = 1 / (E_LUMO - E_HOMO) in eV^-1."""

        gap = self.homo_lumo_gap_ev
        if gap is None or gap <= 0:
            return None
        return 1.0 / gap

    @property
    def electrophilicity_index_ev(self) -> float | None:
        """Parr's electrophilicity index omega = mu^2 / (2 * eta) in eV."""

        mu = self.chemical_potential_ev
        eta = self.chemical_hardness_ev
        if mu is None or eta is None or eta <= 0:
            return None
        return (mu * mu) / (2.0 * eta)

    @property
    def electrodonating_power_ev(self) -> float | None:
        """Electrodonating power omega^- (Gázquez-Cedillo-Vela index) in eV."""

        ip = self.ionization_potential_ev
        ea = self.electron_affinity_ev
        if ip is None or ea is None or (ip - ea) <= 0:
            return None
        return ((3.0 * ip + ea) ** 2) / (16.0 * (ip - ea))

    @property
    def electroaccepting_power_ev(self) -> float | None:
        """Electroaccepting power omega^+ (Gázquez-Cedillo-Vela index) in eV."""

        ip = self.ionization_potential_ev
        ea = self.electron_affinity_ev
        if ip is None or ea is None or (ip - ea) <= 0:
            return None
        return ((ip + 3.0 * ea) ** 2) / (16.0 * (ip - ea))

    @property
    def net_electrophilicity_ev(self) -> float | None:
        """Net electrophilicity Delta omega^+- = omega^+ + omega^- in eV."""

        w_plus = self.electroaccepting_power_ev
        w_minus = self.electrodonating_power_ev
        if w_plus is None or w_minus is None:
            return None
        return w_plus + w_minus

    def stoichiometry(self) -> Counter[str]:
        """Return the element count of the parsed structure.

        Counterpoise ghost centres (written ``H:`` by ORCA) and dummy centres
        (``DA``) carry no electrons and are excluded, so that atom-balance
        checking compares real atoms only.
        """

        return Counter(
            element
            for element in self.elements
            if not element.endswith(":") and element.upper() != "DA"
        )

    @property
    def ghost_atom_count(self) -> int:
        """Number of ghost or dummy centres in the parsed structure."""

        return sum(
            1 for element in self.elements if element.endswith(":") or element.upper() == "DA"
        )

    def has_orbital_data(self) -> bool:
        """Return whether any orbital channel has parsed frontier values."""

        return any(window.has_data() for window in self.orbitals.values())

    def is_empty(self) -> bool:
        """Return whether no scientifically relevant data has been parsed."""

        return not (
            self.e_elec_eh is not None
            or self.zpe_eh is not None
            or self.gibbs_free_energy_eh is not None
            or self.total_enthalpy_eh is not None
            or self.entropy_term_eh is not None
            or self.entropy_correction_eh is not None
            or self.elements
            or self.tddft_cm
            or self.has_orbital_data()
            or self.terminated_normally is not None
            or self.error_messages
            or self.dipole_moment_debye is not None
            or self.s2_actual is not None
            or self.imaginary_frequencies_count > 0
        )

    def electronic_zpe_eh(self) -> float | None:
        """Return electronic plus zero-point energy in Hartree, if complete."""

        if self.e_elec_eh is None or self.zpe_eh is None:
            return None
        return self.e_elec_eh + self.zpe_eh


@dataclass
class MoleculeData:
    """All parsed jobs associated with one molecule identifier.

    Attributes:
        name: Normalized molecule name, usually the output file stem.
        jobs: Parsed ORCA job blocks, ordered by source path then by position
            within the source.
        sources: Input paths or ZIP member paths used to build this molecule.
    """

    name: str
    jobs: list[JobData] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    @property
    def had_error_termination(self) -> bool:
        """Return whether *any* job for this species reported an error.

        Job-level flags are not sufficient. A rerun appended to the same file,
        or a compound job, places the failure in an earlier block and leaves
        the last block clean, so inspecting only the final job reproduces the
        very masking this flag exists to prevent.
        """

        return any(job.had_error_termination for job in self.jobs)

    def levels_of_theory(self) -> list[tuple[str, str, str, str, str, str, int | None, int | None]]:
        """Return every distinct level of theory across this species' jobs."""

        levels: list[tuple[str, str, str, str, str, str, int | None, int | None]] = []
        for job in self.jobs:
            level = job.metadata.level_of_theory()
            if level not in levels:
                levels.append(level)
        return levels


@dataclass(frozen=True)
class ReactionTerm:
    """One stoichiometric term in a reaction equation.

    Attributes:
        coefficient: Stoichiometric coefficient.
        molecule_name: Molecule identifier to match against parsed file stems.
        raw_text: The original token as written by the user. ``"2b"`` is
            ambiguous between "two of species b" and "one of species 2b";
            keeping the raw token lets the engine resolve it against the
            species that were actually parsed.
    """

    coefficient: float
    molecule_name: str
    raw_text: str = ""


@dataclass(frozen=True)
class Reaction:
    """Parsed chemical reaction equation.

    Attributes:
        equation: Original reaction equation string.
        reactants: Reactant terms.
        products: Product terms.
    """

    equation: str
    reactants: list[ReactionTerm]
    products: list[ReactionTerm]

    def species_names(self) -> list[str]:
        """Return all species names in equation order."""

        return [term.molecule_name for term in self.reactants + self.products]


@dataclass
class ConsistencyReport:
    """Physical consistency checks applied to a reaction.

    Attributes:
        atom_balanced: Whether both sides carry the same element counts.
        charge_balanced: Whether both sides carry the same total charge.
        atom_imbalance: Signed ``products - reactants`` element counts.
        charge_imbalance: Signed ``products - reactants`` total charge.
        mixed_levels_of_theory: Distinct ``(version, basis, solvation)`` keys
            found among the participating species. More than one entry means
            the reaction energy is not physically meaningful.
        mixed_temperatures_k: Distinct thermochemistry temperatures found.
        species_with_errors: Participating species whose calculations reported
            an error termination in any job.
        warnings: Human-readable description of every failed check.
    """

    atom_balanced: bool | None = None
    charge_balanced: bool | None = None
    atom_imbalance: dict[str, int | float] = field(default_factory=dict)
    charge_imbalance: float | None = None
    mixed_levels_of_theory: list[tuple[str, str, str, str, str, str, int | None, int | None]] = field(default_factory=list)
    pressure_balanced: bool | None = None
    mixed_temperatures_k: list[float] = field(default_factory=list)
    mixed_pressures_atm: list[float] = field(default_factory=list)
    mixed_solvations: list[str] = field(default_factory=list)
    mixed_methods: list[str] = field(default_factory=list)
    mixed_dispersions: list[str] = field(default_factory=list)
    geometry_mismatches: list[str] = field(default_factory=list)
    entropy_consistency_passed: bool | None = None
    delta_s_direct_cal_mol_k: float | None = None
    delta_s_from_hg_cal_mol_k: float | None = None
    delta_s_discrepancy_cal_mol_k: float | None = None
    stationary_point_warnings: list[str] = field(default_factory=list)
    species_with_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def element_imbalances(self) -> dict[str, int | float]:
        """Alias for atom_imbalance."""
        return self.atom_imbalance

    def is_clean(self) -> bool:
        """Return whether every check that could be evaluated passed."""

        return not self.warnings


@dataclass
class ReactionResult:
    """Reaction thermochemistry result.

    Attributes:
        reaction: Parsed reaction object.
        delta_electronic_kcal_mol: Delta electronic energy in kcal/mol.
        delta_e0_kcal_mol: Delta(E_elec + ZPE) in kcal/mol.
        delta_g_kcal_mol: Delta G in kcal/mol.
        delta_h_kcal_mol: Delta H in kcal/mol.
        missing_references: Human-readable list of missing species or energies.
        consistency: Physical consistency checks for the reaction.
    """

    reaction: Reaction
    delta_electronic_kcal_mol: float | None = None
    delta_e0_kcal_mol: float | None = None
    delta_g_kcal_mol: float | None = None
    delta_h_kcal_mol: float | None = None
    delta_entropy_cal_mol_k: float | None = None
    delta_entropy_j_mol_k: float | None = None
    delta_s_from_hg_cal_mol_k: float | None = None
    delta_s_direct_cal_mol_k: float | None = None
    delta_s_discrepancy_cal_mol_k: float | None = None
    equilibrium_constant_keq: float | None = None
    keq_status: str = "AVAILABLE"
    keq_reason: str | None = None
    reaction_type: str | None = None
    temperature_k: float | None = None
    temperature_requested_k: float | None = None
    delta_g_requested_kcal_mol: float | None = None
    keq_requested: float | None = None
    temperature_treatment_mode: str = "RIGOROUS_ORCA_THERMO"
    temperature_source: str = "NOT_REPORTED"
    pressure_atm: float | None = None
    pressure_source: str = "NOT_REPORTED"
    pressure_correction_applied: bool = False
    delta_g_pressure_corrected_kcal_mol: float | None = None
    keq_pressure_corrected: float | None = None
    keq_pressure_status: str = "AVAILABLE"
    keq_pressure_reason: str | None = None
    standard_state: str = "Gas Phase (1 atm)"
    phase: str = "Gas"
    is_exergonic: bool | None = None
    missing_references: list[str] = field(default_factory=list)
    composite_provenance: dict[str, Any] = field(default_factory=dict)
    consistency: ConsistencyReport = field(default_factory=ConsistencyReport)

    @property
    def keq_standard(self) -> float | None:
        """Standard-state (1 atm) equilibrium constant K_eq."""
        return self.equilibrium_constant_keq


@dataclass
class BDEResult:
    """Bond dissociation energy result.

    A BDE is represented as a dissociation reaction, for example
    ``parent -> fragment1 + fragment2``. Positive values indicate that the
    products are higher in energy than the parent for the selected energy kind.

    Attributes:
        reaction: Dissociation reaction used for the BDE calculation.
        energy_kind: Energy quantity used for the BDE.
        bde_kcal_mol: Bond dissociation energy in kcal/mol.
        missing_references: Missing species or energy fields.
        consistency: Physical consistency checks for the dissociation.
    """

    reaction: Reaction
    energy_kind: EnergyKind
    bde_kcal_mol: float | None = None
    missing_references: list[str] = field(default_factory=list)
    consistency: ConsistencyReport = field(default_factory=ConsistencyReport)
