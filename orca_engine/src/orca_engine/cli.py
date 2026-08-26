"""Command line interface for :mod:`orca_engine`."""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Sequence
from pathlib import Path

from orca_engine.io import load_directory_parallel
from orca_engine.models import BDEResult, EnergyKind, ReactionResult
from orca_engine.reporting import build_report, write_report
from orca_engine.thermochemistry import ReactionParseError, ThermochemistryEngine

LOGGER = logging.getLogger("orca_engine")


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the command line argument parser.

    Returns:
        Configured :class:`argparse.ArgumentParser`.
    """

    parser = argparse.ArgumentParser(
        prog="orca-engine",
        description="Streaming ORCA parser and thermochemistry engine.",
    )
    parser.add_argument(
        "-d",
        "--dir",
        default=".",
        help="Target directory or single ORCA output file.",
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=os.cpu_count() or 4,
        help="Number of worker processes or threads.",
    )
    parser.add_argument(
        "--io-bound",
        action="store_true",
        help="Use ThreadPoolExecutor for many small I/O-bound files.",
    )
    parser.add_argument(
        "--strict-duplicates",
        action="store_true",
        help=(
            "Abort instead of warning when two source files map to the same molecule identifier."
        ),
    )
    parser.add_argument(
        "--strict-consistency",
        action="store_true",
        help=(
            "Exit non-zero when a reaction fails an atom-balance, "
            "charge-balance, or level-of-theory check."
        ),
    )
    parser.add_argument(
        "--rxn",
        help='Reaction equation, for example: "1A + 2B -> 1C".',
    )
    parser.add_argument(
        "--bde",
        help='Bond dissociation equation, for example: "Parent -> Frag + H".',
    )
    parser.add_argument(
        "--bde-kind",
        choices=tuple(kind.value for kind in EnergyKind),
        default=EnergyKind.ELECTRONIC.value,
        help="Energy quantity used for --bde.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "csv"),
        default="json",
        help="Output report format.",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output report path. Defaults to ORCA_Parsed_Data.<format>.",
    )
    parser.add_argument(
        "-g",
        "--gui",
        action="store_true",
        help="Launch the interactive 3D Molecular, HOMO/LUMO, and UV-Vis Web Studio.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="TCP port for the interactive Web Studio (default: 8080).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


def configure_logging(verbose: bool = False) -> None:
    """Configure package logging for CLI execution.

    Args:
        verbose: Emit debug logs when true; otherwise emit info logs.
    """

    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line interface.

    Args:
        argv: Optional argument vector for tests or embedded invocation.

    Returns:
        Process-style exit code.
    """

    parser = build_arg_parser()
    args = parser.parse_args(argv)
    configure_logging(verbose=args.verbose)

    if args.gui:
        from orca_engine.adapters.web_adapter import run_gui_server

        target_dir = Path(args.dir) if args.dir and Path(args.dir).exists() else None
        run_gui_server(port=args.port, data_dir=target_dir)
        return 0

    target = Path(args.dir)
    try:
        molecules = load_directory_parallel(
            target,
            workers=args.workers,
            io_bound=args.io_bound,
            strict_duplicates=args.strict_duplicates,
        )
    except (FileNotFoundError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 2

    if not molecules:
        LOGGER.error("No valid ORCA outputs were parsed.")
        return 1

    engine = ThermochemistryEngine(molecules)
    reaction_result = None
    if args.rxn:
        try:
            reaction_result = engine.evaluate(args.rxn)
        except ReactionParseError as exc:
            LOGGER.error("Could not parse reaction equation: %s", exc)
            return 2
        _log_reaction_result(reaction_result)

    bde_result = None
    if args.bde:
        try:
            bde_result = engine.evaluate_bde_equation(args.bde, kind=EnergyKind(args.bde_kind))
        except ReactionParseError as exc:
            LOGGER.error("Could not parse BDE equation: %s", exc)
            return 2
        _log_bde_result(bde_result)

    report = build_report(molecules, reaction_result=reaction_result, bde_result=bde_result)
    output_path = _resolve_output_path(target, args.output, args.format)
    try:
        write_report(report, output_path, args.format)
    except OSError as exc:
        LOGGER.error("Failed to write report %s: %s", output_path, exc)
        return 1

    LOGGER.info("Parsing complete. Data exported to %s", output_path)

    if args.strict_consistency and _has_consistency_failure(reaction_result, bde_result):
        LOGGER.error("Consistency checks failed and --strict-consistency is set.")
        return 3
    return 0


def _has_consistency_failure(
    reaction_result: ReactionResult | None,
    bde_result: BDEResult | None,
) -> bool:
    """Return whether any evaluated result failed a consistency check."""

    return any(
        result is not None and not result.consistency.is_clean()
        for result in (reaction_result, bde_result)
    )


def _resolve_output_path(target: Path, output: str | None, output_format: str) -> Path:
    """Resolve the final report path."""

    if output:
        return Path(output)
    output_dir = target if target.is_dir() else target.parent
    return output_dir / f"ORCA_Parsed_Data.{output_format}"


def _log_reaction_result(result: ReactionResult) -> None:
    """Log reaction thermochemistry in a concise human-readable form."""

    LOGGER.info("Reaction: %s", result.reaction.equation)
    LOGGER.info("Delta E_elec: %s kcal/mol", _fmt_energy(result.delta_electronic_kcal_mol))
    LOGGER.info("Delta(E_elec+ZPE): %s kcal/mol", _fmt_energy(result.delta_e0_kcal_mol))
    LOGGER.info("Delta G: %s kcal/mol", _fmt_energy(result.delta_g_kcal_mol))
    LOGGER.info("Delta H: %s kcal/mol", _fmt_energy(result.delta_h_kcal_mol))
    if result.delta_entropy_cal_mol_k is not None:
        LOGGER.info("Delta S: %.6f cal/(mol*K)", result.delta_entropy_cal_mol_k)
    if result.equilibrium_constant_keq is not None:
        LOGGER.info("K_eq: %.4e", result.equilibrium_constant_keq)
    if result.missing_references:
        LOGGER.warning("Missing reaction references: %s", ", ".join(result.missing_references))
    if result.consistency.is_clean():
        LOGGER.info("Consistency checks passed.")


def _log_bde_result(result: BDEResult) -> None:
    """Log a BDE result in a concise human-readable form."""

    LOGGER.info("BDE: %s", result.reaction.equation)
    LOGGER.info("BDE kind: %s", result.energy_kind.value)
    LOGGER.info("BDE: %s kcal/mol", _fmt_energy(result.bde_kcal_mol))
    if result.missing_references:
        LOGGER.warning("Missing BDE references: %s", ", ".join(result.missing_references))


def _fmt_energy(value: float | None) -> str:
    """Format an optional reaction energy for logging."""

    return "not available" if value is None else f"{value:.6f}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
