"""Regular expressions used by the ORCA streaming parser."""

from __future__ import annotations

import re

FLOAT = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
INT = r"[-+]?\d+"


class RegexLibrary:
    """Compiled regular expressions for robust ORCA text parsing.

    Patterns are deliberately anchored on the literal labels emitted by ORCA
    rather than on loose keywords. Loose keyword matching was found to produce
    silent mis-assignments, for example capturing a sentence that merely
    mentions the word "entropy" as an entropy value.
    """

    ORCA_VERSION = re.compile(
        r"Program Version\s+(?P<version>\d+(?:\.\d+)*)",
        re.IGNORECASE,
    )
    BASIS_MARKER = re.compile(
        r"Your calculation utilizes the basis:\s*(?P<basis>.*)$",
        re.IGNORECASE,
    )
    SOLVATION = re.compile(
        r"utilizes the\s+(?P<model>\w+)\s+solvation module",
        re.IGNORECASE,
    )
    CHARGE = re.compile(
        rf"Total Charge\s+Charge\s*\.+\s*(?P<value>{INT})",
        re.IGNORECASE,
    )
    MULTIPLICITY = re.compile(
        r"Multiplicity\s+Mult\s*\.+\s*(?P<value>\d+)",
        re.IGNORECASE,
    )
    SP_ENERGY = re.compile(
        rf"FINAL SINGLE POINT ENERGY\s+(?P<value>{FLOAT})",
        re.IGNORECASE,
    )
    ZPE = re.compile(
        rf"Zero point energy\s*\.*\s*(?P<value>{FLOAT})\s*Eh",
        re.IGNORECASE,
    )
    FINAL_GIBBS = re.compile(
        rf"Final Gibbs free energy\s*\.*\s*(?P<value>{FLOAT})\s*Eh",
        re.IGNORECASE,
    )
    TOTAL_ENTHALPY = re.compile(
        rf"Total enthalpy\s*\.*\s*(?P<value>{FLOAT})\s*Eh",
        re.IGNORECASE,
    )
    # ORCA prints "Final entropy term" as +T*S and "Total entropy correction"
    # as -T*S. Both are energies in Hartree, not entropies; they are captured
    # into separate fields so that the sign convention is never ambiguous.
    FINAL_ENTROPY_TERM = re.compile(
        rf"Final entropy term\s*\.*\s*(?P<value>{FLOAT})\s*Eh",
        re.IGNORECASE,
    )
    TOTAL_ENTROPY_CORRECTION = re.compile(
        rf"Total entropy correction\s*\.*\s*(?P<value>{FLOAT})\s*Eh",
        re.IGNORECASE,
    )
    TEMPERATURE = re.compile(
        rf"^\s*Temperature\s*\.+\s*(?P<value>{FLOAT})\s*K\b",
        re.IGNORECASE,
    )
    PRESSURE = re.compile(
        rf"^\s*Pressure\s*\.+\s*(?P<value>{FLOAT})\s*atm\b",
        re.IGNORECASE,
    )
    ORBITAL = re.compile(
        rf"^\s*\d+\s+(?P<occ>{FLOAT})\s+(?P<eh>{FLOAT})\s+(?P<ev>{FLOAT})(?:\s|$)",
        re.IGNORECASE,
    )
    TDDFT = re.compile(
        rf"^\s*\d+\s+(?P<cm>{FLOAT})\s+(?P<nm>{FLOAT})\s+(?P<fosc>{FLOAT})(?:\s|$)",
        re.IGNORECASE,
    )
    TDDFT_TRANSITION = re.compile(
        rf"^\s*\S+\s+->\s+\S+\s+"
        rf"(?P<ev>{FLOAT})\s+(?P<cm>{FLOAT})\s+(?P<nm>{FLOAT})\s+(?P<fosc>{FLOAT})(?:\s|$)",
        re.IGNORECASE,
    )
    # ORCA prints the geometry twice per step: once in Angstrom and once in
    # atomic units (Bohr). The two blocks must be distinguished explicitly,
    # otherwise Bohr values are silently reported as Angstrom.
    COORD_SECTION_ANGSTROEM = re.compile(
        r"\bCARTESIAN\s+COORDINATES\s*\(\s*ANGSTROEM\s*\)", re.IGNORECASE
    )
    COORD_SECTION_AU = re.compile(r"\bCARTESIAN\s+COORDINATES\s*\(\s*A\.U\.\s*\)", re.IGNORECASE)
    COORD_SECTION = re.compile(r"\bCARTESIAN\s+COORDINATES\b", re.IGNORECASE)
    ORBITAL_SECTION = re.compile(r"\bORBITAL\s+ENERGIES\b", re.IGNORECASE)
    SPIN_UP = re.compile(r"\bSPIN\s+UP\s+ORBITALS\b", re.IGNORECASE)
    SPIN_DOWN = re.compile(r"\bSPIN\s+DOWN\s+ORBITALS\b", re.IGNORECASE)
    # ORCA prints a spin-orbit-corrected spectrum under a header that contains
    # the plain header as a substring. Matching it would concatenate two
    # physically different spectra into one array, so it is excluded here and
    # captured separately.
    TDDFT_SECTION = re.compile(
        r"(?<!CORRECTED\s)\bABSORPTION\s+SPECTRUM\s+VIA\s+TRANSITION\s+"
        r"ELECTRIC\s+DIPOLE\s+MOMENTS\b",
        re.IGNORECASE,
    )
    TDDFT_SECTION_SOC = re.compile(
        r"\bSOC\s+CORRECTED\s+ABSORPTION\s+SPECTRUM\s+VIA\s+TRANSITION\s+"
        r"ELECTRIC\s+DIPOLE\s+MOMENTS\b",
        re.IGNORECASE,
    )
    NORMAL_TERMINATION = re.compile(r"ORCA\s+TERMINATED\s+NORMALLY", re.IGNORECASE)
    # Fatal-termination banners only. An earlier catch-all ``Error\s*:``
    # alternative matched diagnostic text inside successful runs and flagged
    # them as failures, so each accepted form is spelled out.
    ERROR_TERMINATION = re.compile(
        r"(?:ORCA\s+finished\s+by\s+error\s+termination"
        r"|\bINPUT\s+ERROR\b"
        r"|\bABORTING\s+THE\s+RUN\b"
        r"|\bUNRECOGNIZED\s+OR\s+DUPLICATED\s+KEYWORD)",
        re.IGNORECASE,
    )
    # Element symbols in ORCA geometries. Counterpoise ghost centres are
    # written with a trailing colon (``H:``); dummy centres are written ``DA``.
    ELEMENT_SYMBOL = re.compile(r"^(?P<symbol>[A-Za-z]{1,3})(?P<ghost>:?)$")
    OPTIMIZATION_NOT_CONVERGED = re.compile(
        r"The optimization did not converge",
        re.IGNORECASE,
    )
    DIPOLE_MAGNITUDE = re.compile(
        rf"(?:Total\s+Dipole\s+Moment\s*:\s*|Magnitude\s*\(\s*Debye\s*\)\s*:\s*)(?P<value>{FLOAT})",
        re.IGNORECASE,
    )
    SPIN_S2 = re.compile(
        rf"(?<!Ideal\s)<\s*S\*\*2\s*>\s*:\s*(?P<value>{FLOAT})",
        re.IGNORECASE,
    )
    SPIN_S2_IDEAL = re.compile(
        rf"Ideal\s*<\s*S\*\*2\s*>\s*:\s*(?P<value>{FLOAT})",
        re.IGNORECASE,
    )
    VIB_FREQ_SECTION = re.compile(
        r"\bVIBRATIONAL\s+FREQUENCIES\b",
        re.IGNORECASE,
    )
    VIB_FREQ_ROW = re.compile(
        rf"^\s*\d+:\s*(?P<cm>{FLOAT})\s*cm\*\*-1(?P<imag>\s*\*\*\*imaginary\s+mode\*\*\*)?",
        re.IGNORECASE,
    )
