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
    EXCHANGE_FUNCTIONAL = re.compile(r"Exchange Functional\s+Exchange\s*\.+\s*(?P<value>\S+)", re.IGNORECASE)
    CORRELATION_FUNCTIONAL = re.compile(r"Correlation Functional\s+Correlation\s*\.+\s*(?P<value>\S+)", re.IGNORECASE)
    DFT_METHOD_BANNER = re.compile(r"\bDFT\s+CALCULATION\b", re.IGNORECASE)
    HF_METHOD_BANNER = re.compile(r"\bHF\s+CALCULATION\b", re.IGNORECASE)
    COMPOSITE_METHOD_BANNER = re.compile(r"\bCOMPOSITE\s+CALCULATION\b", re.IGNORECASE)
    DISPERSION_CORRECTION = re.compile(r"\bDFT\s+DISPERSION\s+CORRECTION\b|(?:vdW-correction\s*:|Dispersion\s+correction\s*\.+)\s*(?P<value>D3BJ|D4|DFT-D3)", re.IGNORECASE)
    SOLVENT_NAME = re.compile(r"Solvent:\s*(?P<solvent>\S+)", re.IGNORECASE)
    CPCM_SOLVENT = re.compile(r"CPCM\s+Solvation\s+Model\s+.*\s+Solvent\s*:\s*(?P<solvent>\S+)", re.IGNORECASE)
    SMD_SOLVENT = re.compile(r"SMD\s+(?:Solvation\s+Model\s+.*\s+Solvent\s*:|solvent\s*\.+)\s*(?P<solvent>\S+)", re.IGNORECASE)
    RELATIVISTIC_ZORA = re.compile(r"\b(?:ZORA\s+APPROXIMATION|ZORA\s+IS\s+SWITCHED\s+ON)\b", re.IGNORECASE)
    RELATIVISTIC_DKH = re.compile(r"\b(?:DKH\s+APPROXIMATION|DOUGLAS-KROLL-HESS-HAMILTONIAN\s+IS\s+SWITCHED\s+ON)\b", re.IGNORECASE)
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
        rf"(?:(?:Non-thermal|Total)\s+)?Zero[ -]point (?:vibrational )?energy\s*\.*\s*(?P<value>{FLOAT})\s*Eh",
        re.IGNORECASE,
    )
    TOTAL_THERMAL_ENERGY = re.compile(
        rf"Total thermal energy\s*\.+\s*(?P<value>{FLOAT})\s*Eh",
        re.IGNORECASE,
    )
    TOTAL_THERMAL_CORRECTION = re.compile(
        rf"Thermal energy correction\s*\.+\s*(?P<value>{FLOAT})\s*Eh",
        re.IGNORECASE,
    )
    ENTHALPY_CORRECTION = re.compile(
        rf"Thermal\s+(?:Enthalpy|[Ee]nthalpy)\s+correction\s*\.+\s*(?P<value>{FLOAT})\s*Eh",
        re.IGNORECASE,
    )
    GIBBS_CORRECTION = re.compile(
        rf"Thermal(?: Gibbs)?\s+(?:free\s+)?[Ee]nergy correction\s*\.+\s*(?P<value>{FLOAT})\s*Eh",
        re.IGNORECASE,
    )
    FINAL_GIBBS = re.compile(
        rf"(?:Final|Total)\s+Gibbs\s+(?:free\s+)?(?:energy|enthalpy)\s*\.*\s*(?P<value>{FLOAT})\s*Eh",
        re.IGNORECASE,
    )
    TOTAL_ENTHALPY = re.compile(
        rf"(?:Total|Final)(?:\s+thermal)?\s+enthalpy\s*\.*\s*(?P<value>{FLOAT})\s*Eh",
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
    VIBRATIONAL_ENTROPY = re.compile(
        rf"S_vib\s*\.+\s*(?P<value>{FLOAT})\s*cal/mol-K",
        re.IGNORECASE,
    )
    ROTATIONAL_ENTROPY = re.compile(
        rf"S_rot\s*\.+\s*(?P<value>{FLOAT})\s*cal/mol-K",
        re.IGNORECASE,
    )
    TRANSLATIONAL_ENTROPY = re.compile(
        rf"S_trans\s*\.+\s*(?P<value>{FLOAT})\s*cal/mol-K",
        re.IGNORECASE,
    )
    ELECTRONIC_ENTROPY = re.compile(
        rf"S_elec\s*\.+\s*(?P<value>{FLOAT})\s*cal/mol-K",
        re.IGNORECASE,
    )
    QUASI_RRHO_BANNER = re.compile(
        r"(?P<treatment>quasi[ \-]?RRHO\s+method\s+of\s+Grimme|Grimme[ \-]?quasi[ \-]?RRHO|Standard-RRHO|modified\s+free\s+rotor)",
        re.IGNORECASE,
    )
    QUASI_RRHO_CUTOFF = re.compile(
        rf"Frequency cutoff for the RRHO\s*\.+\s*(?P<value>{FLOAT})\s*cm\*\*-1",
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
        rf"^\s*\d+\s+(?P<occ>{FLOAT})\s+(?P<eh>{FLOAT})(?:\s+(?P<ev>{FLOAT}))?(?:\s|$)",
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
    ORBITAL_SECTION = re.compile(
        r"\b(?:ORBITAL\s+ENERGIES|MOLECULAR\s+ORBITALS|MO\s+ENERGIES|MOLECULAR\s+ORBITAL\s+ENERGIES)\b",
        re.IGNORECASE,
    )
    SPIN_UP = re.compile(r"\b(?:SPIN\s+UP\s+ORBITALS|ALPHA\s+(?:MOLECULAR\s+)?ORBITALS)\b", re.IGNORECASE)
    SPIN_DOWN = re.compile(r"\b(?:SPIN\s+DOWN\s+ORBITALS|BETA\s+(?:MOLECULAR\s+)?ORBITALS)\b", re.IGNORECASE)

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
        r"|\bORCA\s+TERMINATED\s+ABNORMALLY\b"
        r"|\bTERMINATED\s+ABNORMALLY\b"
        r"|\bAn\s+error\s+has\s+occurred\b"
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
        r"\b(?:VIBRATIONAL\s+FREQUENCIES|3N-6\s+VIBRATIONAL\s+FREQUENCIES|3N-5\s+VIBRATIONAL\s+FREQUENCIES)\b",
        re.IGNORECASE,
    )
    VIB_FREQ_ROW = re.compile(
        rf"^\s*\d+:\s*(?P<cm>{FLOAT})(?:\s*cm\*\*-1|\s*cm\^-1|\s*cm-1)?(?P<imag>\s*(?:\*\*\*imaginary\s+mode\*\*\*|\(imaginary\s+mode\)|imaginary))?",
        re.IGNORECASE,
    )
    IR_SPECTRUM_SECTION = re.compile(
        r"\bIR\s+SPECTRUM\b",
        re.IGNORECASE,
    )
    IR_SPECTRUM_ROW = re.compile(
        rf"^\s*(?P<mode>\d+):\s*(?P<freq>{FLOAT})\s+(?P<t2>{FLOAT})",
        re.IGNORECASE,
    )


    HIRSHFELD_SECTION = re.compile(
        r"\bHIRSHFELD\s+(?:ANALYSIS|CHARGES|POPULATION\s+ANALYSIS)\b",
        re.IGNORECASE,
    )
    MULLIKEN_SECTION = re.compile(
        r"\bMULLIKEN\s+(?:ATOMIC\s+)?(?:CHARGES|POPULATION\s+ANALYSIS)\b",
        re.IGNORECASE,
    )
    LOEWDIN_SECTION = re.compile(
        r"\bL[OÖ]EWDIN\s+(?:ATOMIC\s+)?(?:CHARGES|POPULATION\s+ANALYSIS)\b",
        re.IGNORECASE,
    )
    CHELPG_SECTION = re.compile(
        r"\bCHELPG\s+(?:CHARGES|POPULATION\s+ANALYSIS)\b",
        re.IGNORECASE,
    )
    MAYER_SECTION = re.compile(
        r"\bMAYER\s+POPULATION(?:\s+ANALYSIS)?\b",
        re.IGNORECASE,
    )
    CHARGE_ROW = re.compile(
        rf"^\s*(?P<idx>\d+)\s+(?P<elem>[A-Za-z]{{1,3}})\s*:\s*(?P<charge>{FLOAT})",
        re.IGNORECASE,
    )
    CHARGE_ROW_TABLE = re.compile(
        rf"^\s*(?P<idx>\d+)\s+(?P<elem>[A-Za-z]{{1,3}})\s+(?P<charge>{FLOAT})(?:\s+(?P<spin>{FLOAT}))?",
        re.IGNORECASE,
    )
    MAYER_ROW = re.compile(
        rf"^\s*(?P<idx>\d+)\s+(?P<elem>[A-Za-z]{{1,3}}):?\s+(?P<na>{FLOAT})\s+(?P<za>{FLOAT})\s+(?P<qa>{FLOAT})(?:\s+(?P<va>{FLOAT}))?",
        re.IGNORECASE,
    )
    NMR_SUMMARY_SECTION = re.compile(
        r"\bCHEMICAL\s+SHIELDINGS?\b|\bNucleus\s+Element\s+Isotropic\s+Anisotropy\b",
        re.IGNORECASE,
    )
    NMR_NUCLEUS_SECTION = re.compile(
        r"^\s*Nucleus\s+\d+\s*[A-Za-z]+(?:\s*ist\s*=\s*\d+)?\s*:",
        re.IGNORECASE,
    )



