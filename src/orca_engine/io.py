"""Input discovery, streaming file parsing, and parallel loading."""

from __future__ import annotations

import io
import logging
import zipfile
from collections import defaultdict
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

from orca_engine.models import MoleculeData, normalize_molecule_name
from orca_engine.parser import OrcaParser

LOGGER = logging.getLogger(__name__)
SUPPORTED_SUFFIXES = frozenset({".out", ".log", ".zip"})
ARCHIVE_MEMBER_SUFFIXES = frozenset({".out", ".log"})


def discover_orca_files(path: Path) -> list[Path]:
    """Discover supported ORCA output files below a path.

    Suffix matching is case-insensitive and de-duplicated. ORCA writes both
    ``.out`` and ``.OUT`` depending on how it is invoked, and a case-sensitive
    glob would silently return different file sets on Linux and on Windows,
    making a study impossible to reproduce across platforms.

    Args:
        path: Directory to scan recursively, or one supported file.

    Returns:
        Sorted, de-duplicated list of matching paths.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ValueError: If ``path`` is a file with an unsupported suffix.
    """

    if not path.exists():
        raise FileNotFoundError(f"Input path does not exist: {path}")

    if path.is_file():
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise ValueError(f"Unsupported input file suffix: {path.suffix}")
        return [path]

    unique: dict[Path, None] = {}
    for candidate in path.rglob("*"):
        if candidate.is_file() and candidate.suffix.lower() in SUPPORTED_SUFFIXES:
            unique[candidate.resolve()] = None
    return sorted(unique)


def parse_path_safe(path: Path) -> list[MoleculeData]:
    """Parse one file while isolating I/O and parser failures.

    Args:
        path: A `.out`, `.log`, or `.zip` path.

    Returns:
        Parsed molecule records. ZIP files may produce multiple molecules, one
        per ORCA output member.
    """

    try:
        suffix = path.suffix.lower()
        if suffix == ".zip":
            return _parse_zip(path)
        if suffix in ARCHIVE_MEMBER_SUFFIXES:
            molecule = _parse_regular_file(path)
            return [molecule] if molecule is not None else []
        LOGGER.warning("Skipping unsupported file: %s", path)
    except (OSError, zipfile.BadZipFile, UnicodeError) as exc:
        LOGGER.error("Failed to read %s: %s", path, exc)
    except Exception:  # pragma: no cover - defensive boundary
        LOGGER.exception("Unexpected parser failure for %s", path)
    return []


def load_directory_parallel(
    path: Path,
    workers: int,
    io_bound: bool = False,
    strict_duplicates: bool = False,
) -> dict[str, MoleculeData]:
    """Load all ORCA outputs under a path with configurable concurrency.

    Results are assembled in sorted input-path order regardless of the order in
    which workers finish, so repeated runs over the same directory always
    produce byte-identical reports.

    Args:
        path: Directory or supported file to parse.
        workers: Maximum number of worker threads or processes.
        io_bound: Use :class:`ThreadPoolExecutor` instead of
            :class:`ProcessPoolExecutor` when many small files make I/O the
            bottleneck.
        strict_duplicates: Raise instead of warning when two different sources
            map to the same molecule key.

    Returns:
        Mapping from normalized molecule name to parsed molecule data.

    Raises:
        DuplicateSpeciesError: If ``strict_duplicates`` is set and two distinct
            sources collapse onto one molecule key.
    """

    files = discover_orca_files(path)
    if not files:
        LOGGER.warning("No ORCA output files found under %s", path)
        return {}

    safe_workers = max(1, workers)
    LOGGER.info("Parsing %d file(s) with %d worker(s)", len(files), safe_workers)

    if safe_workers == 1:
        per_file = [parse_path_safe(file_path) for file_path in files]
    else:
        executor: Executor = (
            ThreadPoolExecutor(max_workers=safe_workers)
            if io_bound
            else ProcessPoolExecutor(max_workers=safe_workers)
        )
        with executor:
            # ``Executor.map`` preserves input order, unlike ``as_completed``.
            per_file = list(executor.map(parse_path_safe, files))

    molecules = [molecule for group in per_file for molecule in group]
    return _merge_molecules(molecules, strict=strict_duplicates)


class DuplicateSpeciesError(ValueError):
    """Raised when distinct sources collapse onto one molecule identifier."""


def _parse_regular_file(path: Path) -> MoleculeData | None:
    """Parse one regular ORCA output file as a streaming text iterator."""

    with path.open("r", encoding="utf-8", errors="ignore") as stream:
        jobs = OrcaParser(stream, source_name=str(path)).parse()
    if not jobs:
        return None
    return MoleculeData(
        name=normalize_molecule_name(path.stem),
        jobs=jobs,
        sources=[str(path)],
    )


def _parse_zip(path: Path) -> list[MoleculeData]:
    """Parse all supported ORCA output members inside a ZIP archive."""

    molecules: list[MoleculeData] = []
    with zipfile.ZipFile(path, "r") as archive:
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            member_name = info.filename
            if info.is_dir() or "__MACOSX" in member_name:
                continue
            member_path = Path(member_name)
            if member_path.suffix.lower() not in ARCHIVE_MEMBER_SUFFIXES:
                continue

            source_name = f"{path}!{member_name}"
            try:
                with archive.open(info, "r") as raw_stream:
                    text_stream = io.TextIOWrapper(raw_stream, encoding="utf-8", errors="ignore")
                    jobs = OrcaParser(text_stream, source_name=source_name).parse()
            except (OSError, UnicodeError, ValueError) as exc:
                LOGGER.error("Failed to parse ZIP member %s: %s", source_name, exc)
                continue

            if jobs:
                molecules.append(
                    MoleculeData(
                        name=normalize_molecule_name(member_path.stem),
                        jobs=jobs,
                        sources=[source_name],
                    )
                )
    return molecules


def _merge_molecules(
    molecules: list[MoleculeData],
    strict: bool = False,
) -> dict[str, MoleculeData]:
    """Merge molecule records sharing the same normalized name.

    Species are keyed by file stem, so ``orca5/nap.out`` and
    ``orca6.1/nap.out`` collapse onto the same key even though they may differ
    in basis set, solvation model, or program version. Silently merging them
    would let a reaction draw its reference energies from two different levels
    of theory, so every collision is reported.

    Args:
        molecules: Parsed molecule records in deterministic source order.
        strict: Raise :class:`DuplicateSpeciesError` instead of warning.

    Returns:
        Mapping from normalized molecule name to merged molecule data.

    Raises:
        DuplicateSpeciesError: If ``strict`` is set and a collision occurs.
    """

    merged: dict[str, MoleculeData] = {}
    for molecule in molecules:
        key = normalize_molecule_name(molecule.name)
        if key not in merged:
            merged[key] = MoleculeData(name=key)
        merged[key].jobs.extend(molecule.jobs)
        merged[key].sources.extend(molecule.sources)

    collisions = {key: value.sources for key, value in merged.items() if len(value.sources) > 1}
    for key, sources in collisions.items():
        message = (
            f"Molecule identifier {key!r} maps to {len(sources)} sources: "
            f"{', '.join(sources)}. Energies will be taken from the last source "
            f"in sorted order. Rename the files or parse the directories "
            f"separately if these are different species or different levels of theory."
        )
        if strict:
            raise DuplicateSpeciesError(message)
        LOGGER.warning("%s", message)

    _warn_on_mixed_levels(merged)
    return dict(sorted(merged.items()))


def _warn_on_mixed_levels(merged: dict[str, MoleculeData]) -> None:
    """Warn when one molecule key carries several distinct levels of theory."""

    for key, molecule in merged.items():
        levels = defaultdict(list)
        for job in molecule.jobs:
            levels[job.metadata.level_of_theory()].append(job)
        if len(levels) > 1:
            rendered = "; ".join(
                f"ORCA {version}/{basis}/{solvation}" for version, basis, solvation in levels
            )
            LOGGER.warning(
                "Molecule %r contains %d distinct levels of theory (%s).",
                key,
                len(levels),
                rendered,
            )
