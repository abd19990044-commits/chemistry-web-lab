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

    def __init__(self, file_iterator: Iterator[str], source_name: str = "<stream>") -> None:
        """Initialize the parser for one input stream.

        Args:
            file_iterator: Text iterator yielding ORCA output lines.
            source_name: Source label used in log messages.
        """

        self.iterator = file_iterator
        self.source_name = source_name
        self.jobs: list[JobData] = []
        self.job = JobData()
        self.state = ParserState.SEARCHING
        self._expecting_basis = False
        self._current_spin: SpinChannel | None = None
        self._coord_unit: CoordinateUnit | None = None
        self._state_handlers: dict[ParserState, Callable[[str], None]] = {
            ParserState.SEARCHING: self._handle_searching,
            ParserState.COORDINATES: self._handle_coordinates,
            ParserState.ORBITALS: self._handle_orbitals,
            ParserState.TDDFT: self._handle_tddft,
            ParserState.FREQUENCIES: self._handle_frequencies,
        }
        self._global_dispatch: tuple[tuple[re.Pattern[str], SearchAction], ...] = (
            (RegexLibrary.ORCA_VERSION, self._capture_orca_version),
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
            (RegexLibrary.COORD_SECTION, self._enter_coordinates),
            (RegexLibrary.ORBITAL_SECTION, self._enter_orbitals),
            (RegexLibrary.SPIN_UP, self._enter_orbitals),
            (RegexLibrary.SPIN_DOWN, self._enter_orbitals),
            (RegexLibrary.TDDFT_SECTION, self._enter_tddft),
            (RegexLibrary.VIB_FREQ_SECTION, self._enter_frequencies),
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
            self.jobs.append(self.job)
        self.job = JobData()
        self.state = ParserState.SEARCHING
        self._expecting_basis = False
        self._current_spin = None
        self._coord_unit = None

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
            return

        if RegexLibrary.ORBITAL_SECTION.search(line) or self._is_table_noise(line):
            return

        match = RegexLibrary.ORBITAL.search(line)
        if match is not None:
            occupation = float(match.group("occ"))
            energy_ev = float(match.group("ev"))
            channel = self._current_spin or SpinChannel.RESTRICTED
            self.job.orbital_window(channel).update_from_occupation(occupation, energy_ev)
            return

        self.state = ParserState.SEARCHING
        self._current_spin = None
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

        if RegexLibrary.VIB_FREQ_SECTION.search(line) or set(line) <= {"-"}:
            return

        match = RegexLibrary.VIB_FREQ_ROW.search(line)
        if match is not None:
            freq_cm = float(match.group("cm"))
            if match.group("imag") or freq_cm < 0.0:
                self.job.imaginary_frequencies_cm.append(freq_cm)
                self.job.imaginary_frequencies_count += 1
            return

        if (
            self._looks_like_major_section(line)
            or "IR SPECTRUM" in line.upper()
            or "THERMOCHEMISTRY" in line.upper()
        ):
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

    def _capture_pressure(self, match: re.Match[str], line: str) -> None:
        """Capture the thermochemistry pressure in atm."""

        del line
        self.job.metadata.pressure_atm = float(match.group("value"))

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
