"""Streaming ORCA output parser and thermochemistry engine."""

from orca_engine.constants import PhysConst
from orca_engine.io import (
    DuplicateSpeciesError,
    discover_orca_files,
    load_directory_parallel,
    parse_path_safe,
)
from orca_engine.models import (
    BDEResult,
    CalcMetadata,
    ConsistencyReport,
    CoordinateUnit,
    EnergyKind,
    JobData,
    MoleculeData,
    OrbitalEnergies,
    Reaction,
    ReactionResult,
    ReactionTerm,
    SpinChannel,
)
from orca_engine.parser import OrcaParser
from orca_engine.thermochemistry import ReactionParseError, ThermochemistryEngine

__version__ = "1.0.0"

__all__ = [
    "BDEResult",
    "CalcMetadata",
    "ConsistencyReport",
    "CoordinateUnit",
    "DuplicateSpeciesError",
    "EnergyKind",
    "JobData",
    "MoleculeData",
    "OrbitalEnergies",
    "OrcaParser",
    "PhysConst",
    "Reaction",
    "ReactionParseError",
    "ReactionResult",
    "ReactionTerm",
    "SpinChannel",
    "ThermochemistryEngine",
    "__version__",
    "discover_orca_files",
    "load_directory_parallel",
    "parse_path_safe",
]
