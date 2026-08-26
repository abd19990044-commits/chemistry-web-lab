"""Streaming state-machine parser for ORCA output files."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from enum import Enum

from orca_engine.models import CoordinateUnit, JobData, SpinChannel
from orca_engine.regex import RegexLibrary

LOGGER = logging.getLogger(__name__)

SearchAction = Callable[[re.Match[str], str], None]


class ParserState(str, Enum):
    """Internal parser states for line-dispatched ORCA parsing."""

    SEARCHING = "searching"
    COORDINATES = "coordinates"
    ORBITALS = "orbitals"
    TDDFT = "tddft"
    FREQUENCIES = "frequencies"
    IR_SPECTRUM = "ir_spectrum"
    CHARGES = "charges"
    NMR = "nmr"



class OrcaParser:
    """Parse ORCA output from a text iterator.

    The parser consumes an ``Iterator[str]`` and never materializes the input
    stream. This allows direct use with regular file handles, compressed ZIP
    members wrapped in :class:`io.TextIOWrapper`, sockets, or any other
    line-yielding object.

    Sectioned quantities (coordinates, orbital energies, excited states) are
    reset when their section header is encountered. A geometry optimization
    therefore leaves behind exactly one self-consistent set of values, taken
    from the final step, rather than a mixture of values from different steps.

    Args:
        file_iterator: Text iterator yielding ORCA output lines.
        source_name: Source label used in log messages.
    """

    def __init__(self, file_iterator: Iterator[str] | Sequence[str] | str, source_name: str = "<stream>") -> None:
        """Initialize the parser for one input stream.

        Args:
            file_iterator: Text iterator, sequence of lines, or raw string yielding ORCA output lines.
            source_name: Source label used in log messages.
        """

        if isinstance(file_iterator, str):
            self.iterator: Iterator[str] = iter(file_iterator.splitlines())
        else:
            self.iterator = iter(file_iterator)
        self.source_name = source_name
        self.jobs: list[JobData] = []
        self.job = JobData()
        self.state = ParserState.SEARCHING
        self._expecting_basis = False
        self._current_spin: SpinChannel | None = None
        self._pending_mo_energies: list[float] | None = None
        self._coord_unit: CoordinateUnit | None = None
        self._current_charge_type: str = "hirshfeld"
        self._nmr_lines: list[str] = []

        self._state_handlers: dict[ParserState, Callable[[str], None]] = {
            ParserState.SEARCHING: self._handle_searching,
            ParserState.COORDINATES: self._handle_coordinates,
            ParserState.ORBITALS: self._handle_orbitals,
            ParserState.TDDFT: self._handle_tddft,
            ParserState.FREQUENCIES: self._handle_frequencies,
            ParserState.IR_SPECTRUM: self._handle_ir_spectrum,
            ParserState.CHARGES: self._handle_charges,
            ParserState.NMR: self._handle_nmr,
        }

        self._global_dispatch: tuple[tuple[re.Pattern[str], SearchAction], ...] = (
            (RegexLibrary.ORCA_VERSION, self._capture_orca_version),
            (RegexLibrary.TOTAL_THERMAL_ENERGY, self._capture_total_thermal_energy),
            (RegexLibrary.TOTAL_THERMAL_CORRECTION, self._capture_total_thermal_correction),
            (RegexLibrary.ENTHALPY_CORRECTION, self._capture_enthalpy_correction),
            (RegexLibrary.GIBBS_CORRECTION, self._capture_gibbs_correction),
            (RegexLibrary.VIBRATIONAL_ENTROPY, self._capture_vibrational_entropy),
            (RegexLibrary.ROTATIONAL_ENTROPY, self._capture_rotational_entropy),
            (RegexLibrary.TRANSLATIONAL_ENTROPY, self._capture_translational_entropy),
            (RegexLibrary.QUASI_RRHO_BANNER, self._capture_quasi_rrho_banner),
            (RegexLibrary.QUASI_RRHO_CUTOFF, self._capture_quasi_rrho_cutoff),
            (RegexLibrary.SOLVATION, self._capture_solvation),
            (RegexLibrary.CHARGE, self._capture_charge),
            (RegexLibrary.MULTIPLICITY, self._capture_multiplicity),
            (RegexLibrary.TEMPERATURE, self._capture_temperature),
            (RegexLibrary.PRESSURE, self._capture_pressure),
            (RegexLibrary.ZPE, self._capture_zpe),
            (RegexLibrary.FINAL_GIBBS, self._capture_gibbs),
            (RegexLibrary.TOTAL_ENTHALPY, self._capture_enthalpy),
            (RegexLibrary.FINAL_ENTROPY_TERM, self._capture_entropy_term),
            (RegexLibrary.TOTAL_ENTROPY_CORRECTION, self._capture_entropy_correction),
            (RegexLibrary.DIPOLE_MAGNITUDE, self._capture_dipole),
            (RegexLibrary.SPIN_S2, self._capture_s2),
            (RegexLibrary.SPIN_S2_IDEAL, self._capture_s2_ideal),
            (RegexLibrary.SP_ENERGY, self._capture_sp_energy),
            (RegexLibrary.NORMAL_TERMINATION, self._capture_normal_termination),
            (RegexLibrary.ERROR_TERMINATION, self._capture_error_termination),
        )
        self._search_dispatch: tuple[tuple[re.Pattern[str], SearchAction], ...] = (
            (RegexLibrary.BASIS_MARKER, self._capture_basis_marker),
            (RegexLibrary.EXCHANGE_FUNCTIONAL, self._capture_exchange_functional),
            (RegexLibrary.CORRELATION_FUNCTIONAL, self._capture_correlation_functional),
            (RegexLibrary.DFT_METHOD_BANNER, self._capture_dft_method_banner),
            (RegexLibrary.HF_METHOD_BANNER, self._capture_hf_method_banner),
            (RegexLibrary.COMPOSITE_METHOD_BANNER, self._capture_composite_method_banner),
            (RegexLibrary.DISPERSION_CORRECTION, self._capture_dispersion_correction),
            (RegexLibrary.SOLVENT_NAME, self._capture_solvent_name),
            (RegexLibrary.CPCM_SOLVENT, self._capture_solvent_name),
            (RegexLibrary.SMD_SOLVENT, self._capture_smd_solvent),
            (RegexLibrary.RELATIVISTIC_ZORA, self._capture_relativistic_zora),
            (RegexLibrary.RELATIVISTIC_DKH, self._capture_relativistic_dkh),
            (RegexLibrary.COORD_SECTION, self._enter_coordinates),
            (RegexLibrary.ORBITAL_SECTION, self._enter_orbitals),
            (RegexLibrary.SPIN_UP, self._enter_orbitals),
            (RegexLibrary.SPIN_DOWN, self._enter_orbitals),
            (RegexLibrary.TDDFT_SECTION, self._enter_tddft),
            (RegexLibrary.VIB_FREQ_SECTION, self._enter_frequencies),
            (RegexLibrary.IR_SPECTRUM_SECTION, self._enter_ir_spectrum),
            (RegexLibrary.HIRSHFELD_SECTION, self._enter_hirshfeld_charges),
            (RegexLibrary.MULLIKEN_SECTION, self._enter_mulliken_charges),
            (RegexLibrary.LOEWDIN_SECTION, self._enter_loewdin_charges),
            (RegexLibrary.CHELPG_SECTION, self._enter_chelpg_charges),
            (RegexLibrary.MAYER_SECTION, self._enter_mayer_charges),
            (RegexLibrary.NMR_SUMMARY_SECTION, self._enter_nmr),
            (RegexLibrary.NMR_NUCLEUS_SECTION, self._enter_nmr),
        )



    def parse(self) -> list[JobData]:
        """Parse all job blocks from the stream.

        Returns:
            A list of parsed job records. Empty streams return an empty list.
        """

        for raw_line in self.iterator:
            line = raw_line.strip()
            if not line:
                continue

            if self._starts_new_job(line):
                self._finalize_job()

            self._parse_line(line)

        self._finalize_job()
        LOGGER.debug("Parsed %d job block(s) from %s", len(self.jobs), self.source_name)
        return self.jobs

    def _parse_line(self, line: str) -> None:
        """Dispatch one stripped line to the current state handler.

        Args:
            line: Non-empty ORCA output line without leading or trailing space.
        """

        self._capture_global_observables(line)
        self._state_handlers[self.state](line)

    def _capture_global_observables(self, line: str) -> None:
        """Capture scalar observables that may appear after long tables.

        Args:
            line: Non-empty ORCA output line without leading or trailing space.
        """

        for pattern, action in self._global_dispatch:
            match = pattern.search(line)
            if match is not None:
                action(match, line)

    def _starts_new_job(self, line: str) -> bool:
        """Return whether a line starts a new ORCA job block."""

        return "$new_job" in line.lower() or ("O   R   C   A" in line and not self.job.is_empty())

    def _finalize_job(self) -> None:
        """Append the current job if it contains parsed data and reset state."""

        if not self.job.is_empty():
            # Robust auto-reconstruction of total thermodynamic properties if summary line differed
            if self.job.total_enthalpy_eh is None and self.job.e_elec_eh is not None and self.job.enthalpy_correction_eh is not None:
                self.job.total_enthalpy_eh = self.job.e_elec_eh + self.job.enthalpy_correction_eh
            if self.job.gibbs_free_energy_eh is None and self.job.e_elec_eh is not None and self.job.gibbs_correction_eh is not None:
                self.job.gibbs_free_energy_eh = self.job.e_elec_eh + self.job.gibbs_correction_eh
            if self.job.entropy_term_eh is None and self.job.entropy_correction_eh is not None:
                self.job.entropy_term_eh = -self.job.entropy_correction_eh

            self.job.compute_geometry_hash()

            # Rigorous Scientific Stationary Point & Thermochemistry Reliability Classification
            # Step 1: Failed or non-normally terminated calculation
            if self.job.terminated_normally is False or (self.job.error_messages and not self.job.terminated_normally):
                self.job.stationary_point_status = "FAILED_CALCULATION"
                self.job.thermochemistry_reliability = "FAILED_CALCULATION"
            # Step 2: Missing frequency calculation (single-point / electronic structure only)
            elif not self.job.vibrational_frequencies_cm:
                self.job.stationary_point_status = "NO_FREQUENCY_CALCULATION"
                self.job.thermochemistry_reliability = "ELECTRONIC_ONLY"
            else:
                # Step 3: Check frequency completeness against total 3N modes expected in ORCA frequency output
                expected_total = self.job.expected_total_frequencies_count
                parsed_modes_count = len(self.job.vibrational_frequencies_cm)

                if expected_total is not None and expected_total > 0 and parsed_modes_count < expected_total:
                    self.job.stationary_point_status = "INCOMPLETE_FREQUENCIES"
                    self.job.thermochemistry_reliability = "INCOMPLETE_FREQUENCIES"
                else:
                    # Step 4: Normal termination + complete frequency modes -> classify by imaginary frequencies
                    if self.job.imaginary_frequencies_count == 0:
                        self.job.stationary_point_status = "LIKELY_MINIMUM"
                        self.job.thermochemistry_reliability = "HIGH"
                    elif self.job.imaginary_frequencies_count == 1:
                        self.job.stationary_point_status = "TRANSITION_STATE"
                        self.job.thermochemistry_reliability = "TRANSITION_STATE"
                    else:
                        self.job.stationary_point_status = "HIGHER_ORDER_SADDLE"
                        self.job.thermochemistry_reliability = "UNRELIABLE_FOR_MINIMUM"

            if self._nmr_lines:
                try:
                    from orca_engine.nmr import OrcaNMRParser
                    meta_dict = {
                        "method": self.job.metadata.method,
                        "basis_set": self.job.metadata.basis_set,
                        "solvent": self.job.metadata.solvent,
                        "orca_version": self.job.metadata.orca_version,
                    }
                    self.job.nmr_result = OrcaNMRParser().parse_lines(
                        self._nmr_lines,
                        coordinates=self.job.coords,
                        elements=self.job.elements,
                        metadata=meta_dict,
                    )
                except Exception as exc:
                    LOGGER.debug("Failed parsing NMR lines in OrcaParser: %s", exc)
                self._nmr_lines = []

            self.jobs.append(self.job)
        self.job = JobData()
        self.state = ParserState.SEARCHING
        self._expecting_basis = False
        self._current_spin = None
        self._coord_unit = None
        self._nmr_lines = []

    def _handle_searching(self, line: str) -> None:
        """Handle metadata, energies, and section transitions."""

        if self._expecting_basis:
            self.job.metadata.basis_set = line
            self._expecting_basis = False
            return

        for pattern, action in self._search_dispatch:
            match = pattern.search(line)
            if match is not None:
                action(match, line)
                return

    def _handle_coordinates(self, line: str) -> None:
        """Handle a Cartesian coordinate section line."""

        if self._is_table_noise(line):
            return

        coordinate = self._extract_coordinate(line)
        if coordinate is not None:
            element, xyz = coordinate
            self.job.elements.append(element)
            self.job.coords.append(xyz)
            return

        # The section is over. Re-dispatch the terminating line through the
        # SEARCHING handler so that a section header appearing immediately
        # after the table -- for example "CARTESIAN COORDINATES (A.U.)" -- is
        # acted upon rather than silently consumed.
        self.state = ParserState.SEARCHING
        self._handle_searching(line)

    def _handle_orbitals(self, line: str) -> None:
        """Handle restricted or unrestricted orbital energy table rows."""

        spin_channel = self._detect_spin_channel(line)
        if spin_channel is not None:
            self._current_spin = spin_channel
            self._pending_mo_energies = None
            return

        if RegexLibrary.ORBITAL_SECTION.search(line) or self._is_table_noise(line):
            return

        match = RegexLibrary.ORBITAL.search(line)
        if match is not None:
            occupation = float(match.group("occ"))
            if match.group("ev") is not None:
                energy_ev = float(match.group("ev"))
            else:
                energy_ev = float(match.group("eh")) * 27.211386245988
            channel = self._current_spin or SpinChannel.RESTRICTED
            self.job.orbital_window(channel).update_from_occupation(occupation, energy_ev)
            return

        # Handle MOLECULAR ORBITALS block layout (LargePrint format)
        tokens = line.strip().split()
        if tokens and all(re.match(r"^-?\d+\.\d+$", t) for t in tokens):
            floats = [float(t) for t in tokens]
            if self._pending_mo_energies is None:
                # First row of floats corresponds to orbital energies (Eh)
                self._pending_mo_energies = floats
                return
            else:
                # Second row of floats corresponds to occupations
                occs = floats
                channel = self._current_spin or SpinChannel.RESTRICTED
                for occ, eh in zip(occs, self._pending_mo_energies, strict=False):
                    energy_ev = eh * 27.211386245988
                    self.job.orbital_window(channel).update_from_occupation(occ, energy_ev)
                self._pending_mo_energies = None
                return

        if tokens and all(re.match(r"^\d+$", t) for t in tokens):
            # Line of MO index numbers (e.g. "0 1 2 3 4 5")
            self._pending_mo_energies = None
            return

        if tokens and all(set(t) == {"-"} for t in tokens):
            # Separator row (e.g. "--------- ---------")
            return

        if re.match(r"^\s*\d+\s+[A-Za-z]{1,3}\s+", line):
            # Basis function coefficient row in MOLECULAR ORBITALS block
            return

        self.state = ParserState.SEARCHING
        self._current_spin = None
        self._pending_mo_energies = None
        self._handle_searching(line)


    def _handle_tddft(self, line: str) -> None:
        """Handle TDDFT absorption spectrum rows."""

        if RegexLibrary.TDDFT_SECTION.search(line) or self._is_table_noise(line):
            return

        match = RegexLibrary.TDDFT_TRANSITION.search(line) or RegexLibrary.TDDFT.search(line)
        if match is not None:
            self.job.tddft_cm.append(float(match.group("cm")))
            self.job.tddft_fosc.append(float(match.group("fosc")))
            return

        if self.job.tddft_cm or self._looks_like_major_section(line):
            self.state = ParserState.SEARCHING
            self._handle_searching(line)

    def _handle_frequencies(self, line: str) -> None:
        """Handle vibrational frequency section lines."""

        if RegexLibrary.VIB_FREQ_SECTION.search(line) or set(line) <= {"-", "="}:
            return

        match = RegexLibrary.VIB_FREQ_ROW.search(line)
        if match is not None:
            freq_cm = float(match.group("cm"))
            self.job.vibrational_frequencies_cm.append(freq_cm)
            if match.group("imag") or freq_cm < 0.0:
                self.job.imaginary_frequencies_cm.append(freq_cm)
                self.job.imaginary_frequencies_count += 1
            return

        # Check if another section is starting
        for pattern, action in self._search_dispatch:
            m = pattern.search(line)
            if m is not None:
                self.state = ParserState.SEARCHING
                action(m, line)
                return

        if self._looks_like_major_section(line) or "THERMOCHEMISTRY" in line.upper():
            self.state = ParserState.SEARCHING
            self._handle_searching(line)

    def _enter_ir_spectrum(self, match: re.Match[str], line: str) -> None:
        """Enter IR spectrum parsing state and initialize IR spectrum lists."""

        del match, line
        self.state = ParserState.IR_SPECTRUM
        self.job.ir_frequencies_cm = []
        self.job.ir_intensities_km_mol = []
        self.job.ir_spectrum = []

    def _handle_ir_spectrum(self, line: str) -> None:
        """Handle IR spectrum section lines with frequencies and T^2 intensities."""

        if RegexLibrary.IR_SPECTRUM_SECTION.search(line) or set(line) <= {"-", "="}:
            return

        upper = line.upper()
        if "MODE" in upper and "FREQ" in upper:
            return

        match = RegexLibrary.IR_SPECTRUM_ROW.search(line)
        if match is not None:
            mode = int(match.group("mode"))
            freq_cm = float(match.group("freq"))
            t2 = float(match.group("t2"))
            self.job.ir_frequencies_cm.append(freq_cm)
            self.job.ir_intensities_km_mol.append(t2)
            self.job.ir_spectrum.append({
                "mode": mode,
                "frequency_cm": freq_cm,
                "intensity_km_mol": t2,
            })
            if not self.job.vibrational_frequencies_cm:
                self.job.vibrational_frequencies_cm.append(freq_cm)
            return

        # Check if another section is starting
        for pattern, action in self._search_dispatch:
            m = pattern.search(line)
            if m is not None:
                self.state = ParserState.SEARCHING
                action(m, line)
                return

        if self._looks_like_major_section(line) or "THERMOCHEMISTRY" in line.upper():
            self.state = ParserState.SEARCHING
            self._handle_searching(line)



    def _capture_dipole(self, match: re.Match[str], line: str) -> None:
        """Capture the total dipole moment in Debye."""

        del line
        self.job.dipole_moment_debye = float(match.group("value"))

    def _capture_s2(self, match: re.Match[str], line: str) -> None:
        """Capture the actual expectation value <S**2>."""

        del line
        self.job.s2_actual = float(match.group("value"))

    def _capture_s2_ideal(self, match: re.Match[str], line: str) -> None:
        """Capture the ideal expectation value <S**2> = S(S+1)."""

        del line
        self.job.s2_ideal = float(match.group("value"))

    def _enter_frequencies(self, match: re.Match[str], line: str) -> None:
        """Enter vibrational frequency parsing state and reset frequency arrays."""

        del match, line
        self.state = ParserState.FREQUENCIES
        self.job.imaginary_frequencies_cm = []
        self.job.imaginary_frequencies_count = 0
        self.job.vibrational_frequencies_cm = []

    def _capture_basis_marker(self, match: re.Match[str], line: str) -> None:
        """Capture basis metadata or mark the next line as the basis name."""

        del line
        basis = match.group("basis").strip()
        if basis:
            self.job.metadata.basis_set = basis
        else:
            self._expecting_basis = True

    def _capture_orca_version(self, match: re.Match[str], line: str) -> None:
        """Capture the ORCA program version from the output header."""

        del line
        self.job.metadata.orca_version = match.group("version")

    def _capture_solvation(self, match: re.Match[str], line: str) -> None:
        """Capture the solvation model from a matching line."""

        del line
        self.job.metadata.solvation = match.group("model")

    def _capture_charge(self, match: re.Match[str], line: str) -> None:
        """Capture the total molecular charge."""

        del line
        self.job.metadata.charge = int(match.group("value"))

    def _capture_multiplicity(self, match: re.Match[str], line: str) -> None:
        """Capture the spin multiplicity."""

        del line
        self.job.metadata.multiplicity = int(match.group("value"))

    def _capture_temperature(self, match: re.Match[str], line: str) -> None:
        """Capture the thermochemistry temperature in kelvin."""

        del line
        self.job.metadata.temperature_k = float(match.group("value"))
        self.job.metadata.temperature_source = "ORCA_OUTPUT"

    def _capture_pressure(self, match: re.Match[str], line: str) -> None:
        """Capture the thermochemistry pressure in atm."""

        del line
        self.job.metadata.pressure_atm = float(match.group("value"))
        self.job.metadata.pressure_source = "ORCA_OUTPUT"""


    def _capture_total_thermal_energy(self, match: re.Match[str], line: str) -> None:
        del line
        self.job.thermal_energy_eh = float(match.group("value"))

    def _capture_total_thermal_correction(self, match: re.Match[str], line: str) -> None:
        del line
        self.job.thermal_correction_eh = float(match.group("value"))

    def _capture_enthalpy_correction(self, match: re.Match[str], line: str) -> None:
        del line
        self.job.enthalpy_correction_eh = float(match.group("value"))

    def _capture_gibbs_correction(self, match: re.Match[str], line: str) -> None:
        del line
        self.job.gibbs_correction_eh = float(match.group("value"))

    def _capture_vibrational_entropy(self, match: re.Match[str], line: str) -> None:
        del line
        self.job.vibrational_entropy_cal_mol_k = float(match.group("value"))

    def _capture_rotational_entropy(self, match: re.Match[str], line: str) -> None:
        del line
        self.job.rotational_entropy_cal_mol_k = float(match.group("value"))

    def _capture_translational_entropy(self, match: re.Match[str], line: str) -> None:
        del line
        self.job.translational_entropy_cal_mol_k = float(match.group("value"))

    def _capture_quasi_rrho_banner(self, match: re.Match[str], line: str) -> None:
        del line
        treatment = match.group("treatment").upper()
        if "GRIMME" in treatment or "MODIFIED" in treatment:
            self.job.metadata.quasi_rrho_treatment = "Grimme-qRRHO"
        elif "STANDARD" in treatment:
            self.job.metadata.quasi_rrho_treatment = "Standard-RRHO"
        else:
            self.job.metadata.quasi_rrho_treatment = "UNKNOWN"

    def _capture_quasi_rrho_cutoff(self, match: re.Match[str], line: str) -> None:
        del line
        self.job.metadata.quasi_rrho_cutoff_cm = float(match.group("value"))

    def _capture_exchange_functional(self, match: re.Match[str], line: str) -> None:
        del line
        val = match.group("value")
        if self.job.metadata.method in ("Unknown", "DFT", "HF"):
            self.job.metadata.method = val
        elif val not in self.job.metadata.method:
            self.job.metadata.method = f"{self.job.metadata.method}/{val}"

    def _capture_correlation_functional(self, match: re.Match[str], line: str) -> None:
        del line
        val = match.group("value")
        if self.job.metadata.method in ("Unknown", "DFT", "HF"):
            self.job.metadata.method = val
        elif val not in self.job.metadata.method:
            self.job.metadata.method = f"{self.job.metadata.method}/{val}"

    def _capture_dft_method_banner(self, match: re.Match[str], line: str) -> None:
        del match, line
        if self.job.metadata.method == "Unknown":
            self.job.metadata.method = "DFT"

    def _capture_hf_method_banner(self, match: re.Match[str], line: str) -> None:
        del match, line
        if self.job.metadata.method == "Unknown":
            self.job.metadata.method = "HF"
            
    def _capture_composite_method_banner(self, match: re.Match[str], line: str) -> None:
        del match, line
        self.job.metadata.method = "Composite"

    def _capture_dispersion_correction(self, match: re.Match[str], line: str) -> None:
        del line
        val = match.group("value")
        if val:
            self.job.metadata.dispersion = val
        else:
            self.job.metadata.dispersion = "Present"

    def _capture_solvent_name(self, match: re.Match[str], line: str) -> None:
        del line
        self.job.metadata.solvent = match.group("solvent")
        self.job.metadata.phase = "Solution"

    def _capture_smd_solvent(self, match: re.Match[str], line: str) -> None:
        del line
        self.job.metadata.solvent = match.group("solvent")
        self.job.metadata.phase = "Solution"
        self.job.metadata.solvation = "SMD"

    def _capture_relativistic_zora(self, match: re.Match[str], line: str) -> None:
        del match, line
        self.job.metadata.relativistic = "ZORA"

    def _capture_relativistic_dkh(self, match: re.Match[str], line: str) -> None:
        del match, line
        self.job.metadata.relativistic = "DKH"

    def _capture_zpe(self, match: re.Match[str], line: str) -> None:
        """Capture zero-point energy in Hartree."""

        del line
        self.job.zpe_eh = float(match.group("value"))

    def _capture_gibbs(self, match: re.Match[str], line: str) -> None:
        """Capture final Gibbs free energy in Hartree."""

        del line
        self.job.gibbs_free_energy_eh = float(match.group("value"))

    def _capture_enthalpy(self, match: re.Match[str], line: str) -> None:
        """Capture total enthalpy in Hartree."""

        del line
        self.job.total_enthalpy_eh = float(match.group("value"))

    def _capture_entropy_term(self, match: re.Match[str], line: str) -> None:
        """Capture ORCA's final entropy term, equal to ``+T*S`` in Hartree."""

        del line
        self.job.entropy_term_eh = float(match.group("value"))

    def _capture_entropy_correction(self, match: re.Match[str], line: str) -> None:
        """Capture ORCA's total entropy correction, equal to ``-T*S``."""

        del line
        self.job.entropy_correction_eh = float(match.group("value"))

    def _capture_sp_energy(self, match: re.Match[str], line: str) -> None:
        """Capture final single point electronic energy in Hartree."""

        del line
        self.job.e_elec_eh = float(match.group("value"))

    def _capture_normal_termination(self, match: re.Match[str], line: str) -> None:
        """Capture normal ORCA termination status."""

        del match
        self.job.terminated_normally = True
        self.job.termination_message = line

    def _capture_error_termination(self, match: re.Match[str], line: str) -> None:
        """Capture ORCA error termination status.

        Every banner is retained. Files that were appended to across reruns can
        contain a failed attempt followed by a successful one, and discarding
        the earlier failures would make a partially failed calculation look
        clean.
        """

        del match
        self.job.terminated_normally = False
        self.job.termination_message = line
        self.job.error_messages.append(line)
        LOGGER.debug("Error termination in %s: %s", self.source_name, line)

    def _enter_coordinates(self, match: re.Match[str], line: str) -> None:
        """Enter coordinate parsing state and reset current coordinate arrays.

        ORCA prints the geometry twice per step, in Angstrom and in atomic
        units. The unit is recorded so that Bohr values are never reported as
        Angstrom, and Angstrom blocks are preferred when both are available.
        """

        del match
        unit = self._detect_coordinate_unit(line)
        if (
            unit is CoordinateUnit.BOHR
            and self._coord_unit is CoordinateUnit.ANGSTROM
            and self.job.elements
        ):
            # An Angstrom geometry for this step is already stored; skip the
            # redundant atomic-unit copy instead of overwriting it.
            self.state = ParserState.SEARCHING
            return

        self.state = ParserState.COORDINATES
        self._coord_unit = unit
        self.job.coords_unit = unit
        self.job.elements = []
        self.job.coords = []

    def _enter_orbitals(self, match: re.Match[str], line: str) -> None:
        """Enter orbital parsing state and capture a spin header if present.

        Restarting a restricted ``ORBITAL ENERGIES`` section discards frontier
        values from earlier optimization steps. Spin-channel sub-headers do not
        reset, because alpha and beta tables belong to the same section.
        """

        del match
        spin_channel = self._detect_spin_channel(line)
        if spin_channel is None:
            self.job.reset_orbitals()
        self.state = ParserState.ORBITALS
        self._current_spin = spin_channel
        self._pending_mo_energies = None


    def _enter_tddft(self, match: re.Match[str], line: str) -> None:
        """Enter TDDFT absorption-spectrum parsing state and reset states."""

        del match, line
        self.state = ParserState.TDDFT
        self.job.tddft_cm = []
        self.job.tddft_fosc = []

    def _extract_coordinate(self, line: str) -> tuple[str, tuple[float, float, float]] | None:
        """Extract one coordinate row using negative slicing.

        Args:
            line: Candidate coordinate line.

        Returns:
            ``(element, (x, y, z))`` if the line looks like a coordinate row;
            otherwise ``None``.
        """

        parts = line.split()
        if len(parts) < 4:
            return None

        try:
            x, y, z = (float(parts[-3]), float(parts[-2]), float(parts[-1]))
        except ValueError:
            return None

        for token in parts[:-3]:
            match = RegexLibrary.ELEMENT_SYMBOL.fullmatch(token)
            if match is not None:
                symbol = match.group("symbol").capitalize()
                if match.group("ghost"):
                    symbol = f"{symbol}:"
                return symbol, (x, y, z)

        return None

    def _detect_coordinate_unit(self, line: str) -> CoordinateUnit | None:
        """Return the length unit announced by a coordinate section header."""

        if RegexLibrary.COORD_SECTION_ANGSTROEM.search(line):
            return CoordinateUnit.ANGSTROM
        if RegexLibrary.COORD_SECTION_AU.search(line):
            return CoordinateUnit.BOHR
        return None

    def _detect_spin_channel(self, line: str) -> SpinChannel | None:
        """Return the spin channel announced by a line, if any."""

        if RegexLibrary.SPIN_UP.search(line):
            return SpinChannel.ALPHA
        if RegexLibrary.SPIN_DOWN.search(line):
            return SpinChannel.BETA
        return None

    def _is_table_noise(self, line: str) -> bool:
        """Return whether a line is table decoration rather than data."""

        upper = line.upper()
        return (
            set(line) <= {"-"}
            or upper.startswith(("NO ", "STATE"))
            or "E(EH)" in upper
            or "E(EV)" in upper
            or "CM**-1" in upper
        )

    def _looks_like_major_section(self, line: str) -> bool:
        """Return whether a line likely marks a new top-level ORCA section."""

        upper = line.upper()
        return (
            "FINAL SINGLE POINT ENERGY" in upper
            or "TOTAL RUN TIME" in upper
            or "ORCA TERMINATED" in upper
            or upper.startswith(
                (
                    "=> NOW LEAVING",
                    "CIS/TD-DFT",
                    "ORCA PROPERTY CALCULATIONS",
                )
            )
        )

    def _enter_hirshfeld_charges(self, match: re.Match[str], line: str) -> None:
        del match, line
        self.state = ParserState.CHARGES
        self._current_charge_type = "hirshfeld"
        self.job.hirshfeld_charges = []
        self.job.atomic_charges["hirshfeld"] = []

    def _enter_mulliken_charges(self, match: re.Match[str], line: str) -> None:
        del match, line
        self.state = ParserState.CHARGES
        self._current_charge_type = "mulliken"
        self.job.mulliken_charges = []
        self.job.atomic_charges["mulliken"] = []

    def _enter_loewdin_charges(self, match: re.Match[str], line: str) -> None:
        del match, line
        self.state = ParserState.CHARGES
        self._current_charge_type = "loewdin"
        self.job.loewdin_charges = []
        self.job.atomic_charges["loewdin"] = []

    def _enter_chelpg_charges(self, match: re.Match[str], line: str) -> None:
        del match, line
        self.state = ParserState.CHARGES
        self._current_charge_type = "chelpg"
        self.job.atomic_charges["chelpg"] = []

    def _enter_mayer_charges(self, match: re.Match[str], line: str) -> None:
        del match, line
        self.state = ParserState.CHARGES
        self._current_charge_type = "mayer"
        self.job.mayer_charges = []
        self.job.mayer_valences = []
        self.job.atomic_charges["mayer"] = []
        self.job.atomic_charges["mayer_valence"] = []


    def _handle_charges(self, line: str) -> None:
        if not line or set(line) <= {"-", "="}:
            return

        # Check if another section is starting
        for pattern, action in self._search_dispatch:
            m = pattern.search(line)
            if m is not None:
                self.state = ParserState.SEARCHING
                action(m, line)
                return

        upper = line.upper()
        if "SUM OF ATOMIC CHARGES" in upper or "SUM OF CHARGES" in upper or self._looks_like_major_section(line):
            self.state = ParserState.SEARCHING
            return

        # Ignore header rows
        if upper.startswith("TOTAL CHARGES") or upper.startswith("ATOM") or ("ZA" in upper and "QA" in upper):
            return

        if self._current_charge_type == "mayer":
            m_mayer = RegexLibrary.MAYER_ROW.search(line)
            if m_mayer:
                try:
                    qa = float(m_mayer.group("qa"))
                    va = float(m_mayer.group("va")) if m_mayer.group("va") is not None else 0.0
                    self.job.mayer_charges.append(qa)
                    self.job.mayer_valences.append(va)
                    if "mayer" not in self.job.atomic_charges:
                        self.job.atomic_charges["mayer"] = []
                    self.job.atomic_charges["mayer"].append(qa)
                    if "mayer_valence" not in self.job.atomic_charges:
                        self.job.atomic_charges["mayer_valence"] = []
                    self.job.atomic_charges["mayer_valence"].append(va)
                    return
                except (ValueError, TypeError):
                    pass

        m_colon = RegexLibrary.CHARGE_ROW.search(line)
        m_table = RegexLibrary.CHARGE_ROW_TABLE.search(line) if not m_colon else None
        m = m_colon or m_table
        if m:
            try:
                chg = float(m.group("charge"))
                if self._current_charge_type == "hirshfeld":
                    self.job.hirshfeld_charges.append(chg)
                elif self._current_charge_type == "mulliken":
                    self.job.mulliken_charges.append(chg)
                elif self._current_charge_type == "loewdin":
                    self.job.loewdin_charges.append(chg)
                if self._current_charge_type not in self.job.atomic_charges:
                    self.job.atomic_charges[self._current_charge_type] = []
                self.job.atomic_charges[self._current_charge_type].append(chg)
            except (ValueError, TypeError):
                pass
        elif self._is_table_noise(line):
            return
        else:
            self.state = ParserState.SEARCHING

    def _enter_nmr(self, match: re.Match[str], line: str) -> None:
        del match
        self.state = ParserState.NMR
        self._nmr_lines.append(line)

    def _handle_nmr(self, line: str) -> None:
        if not line:
            return

        # Check if another major section is starting
        for pattern, action in self._search_dispatch:
            if pattern not in (RegexLibrary.NMR_SUMMARY_SECTION, RegexLibrary.NMR_NUCLEUS_SECTION):
                m = pattern.search(line)
                if m is not None:
                    self.state = ParserState.SEARCHING
                    action(m, line)
                    return

        upper = line.upper()
        if "ORCA TERMINATION" in upper or "TIMINGS" in upper or self._looks_like_major_section(line):
            self.state = ParserState.SEARCHING
            return

        self._nmr_lines.append(line)



