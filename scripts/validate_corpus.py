"""Validate the parser against an independent extraction of the same corpus.

This script reproduces the validation reported in the manuscript. It parses
every ORCA output under a directory with :mod:`orca_engine` and, separately,
with a deliberately simple reference implementation written from ORCA's printed
table layout alone. Disagreement between the two is reported per file.

The reference implementation is intentionally independent of the library: it
re-reads the file, keeps only the last ``ORBITAL ENERGIES`` section, and tracks
alpha and beta channels separately. It is slow and holds the file in memory,
which is exactly why it is not the production code path.

Usage::

    python scripts/validate_corpus.py data/orcafile
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orca_engine.io import discover_orca_files, load_directory_parallel
from orca_engine.parser import OrcaParser

# Deliberately defined here rather than imported from orca_engine, so that a
# mistake in the library's patterns cannot propagate into its own validation.
NUMBER = r"[-+]?\d+\.\d+"
ORBITAL_ROW = re.compile(rf"^\s*(\d+)\s+(\d+\.\d+)\s+({NUMBER})\s+({NUMBER})\s*$")
ORBITAL_HEADER = re.compile(r"ORBITAL ENERGIES")
SPIN_HEADER = re.compile(r"SPIN (UP|DOWN) ORBITALS")

TOLERANCE_EV = 1.0e-4


def reference_frontier_levels(path: Path) -> tuple[float | None, float | None]:
    """Extract HOMO and LUMO by a different algorithm from the library's.

    The library walks the orbital table as a state machine and assigns HOMO and
    LUMO positionally. This function instead collects every ``(occupation,
    energy)`` pair in the final orbital section into sets and takes extrema:
    the HOMO is the maximum energy among occupied orbitals and the LUMO the
    minimum energy among unoccupied ones. It therefore does not depend on the
    table being ordered, on any guard about which value was assigned first, or
    on any pattern defined in :mod:`orca_engine`.

    An earlier version of this function shared the library's sequential guard
    and consequently reproduced one of its bugs. Using an independent algorithm
    is the point of the exercise.

    Args:
        path: ORCA output file.

    Returns:
        ``(homo_ev, lumo_ev)``, either of which may be ``None``.
    """

    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    starts = [index for index, line in enumerate(lines) if ORBITAL_HEADER.search(line)]
    if not starts:
        return None, None

    occupied: list[float] = []
    virtual: list[float] = []
    seen_indices: set[tuple[str, int]] = set()
    channel = "restricted"

    for line in lines[starts[-1] :]:
        spin = SPIN_HEADER.search(line)
        if spin is not None:
            channel = spin.group(1).lower()
            continue

        row = ORBITAL_ROW.match(line)
        if row is None:
            continue

        index = int(row.group(1))
        # Orbital indices restart at 0 in each channel; a repeat within the
        # same channel means a new table has begun, which cannot happen inside
        # the final section.
        if (channel, index) in seen_indices:
            break
        seen_indices.add((channel, index))

        occupation = float(row.group(2))
        energy_ev = float(row.group(4))
        (occupied if occupation > 0.0 else virtual).append(energy_ev)

    return (max(occupied) if occupied else None, min(virtual) if virtual else None)


def main(argv: list[str] | None = None) -> int:
    """Run the corpus validation and print a summary.

    Args:
        argv: Optional argument vector.

    Returns:
        ``0`` when every comparison agrees, ``1`` otherwise.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path, help="Directory of ORCA output files.")
    args = parser.parse_args(argv)

    files = discover_orca_files(args.corpus)
    print(f"Discovered {len(files)} ORCA output file(s) under {args.corpus}")

    compared = 0
    disagreements: list[tuple[str, float]] = []
    with_errors: list[tuple[str, int, bool]] = []
    unparsed: list[str] = []

    for path in files:
        reference = reference_frontier_levels(path)
        with path.open("r", encoding="utf-8", errors="ignore") as stream:
            jobs = OrcaParser(stream, source_name=str(path)).parse()
        if not jobs:
            unparsed.append(path.name)
            continue

        # Aggregate over every job. Inspecting only ``jobs[-1]`` reproduces the
        # masking this check exists to detect.
        error_count = sum(len(job.error_messages) for job in jobs)
        if error_count:
            masked = any(job.terminated_normally for job in jobs)
            with_errors.append((path.name, error_count, masked))

        job = jobs[-1]
        if reference[0] is None or reference[1] is None:
            continue
        if job.homo_ev is None or job.lumo_ev is None:
            continue

        compared += 1
        parsed_gap = job.lumo_ev - job.homo_ev
        reference_gap = reference[1] - reference[0]
        if abs(parsed_gap - reference_gap) > TOLERANCE_EV:
            disagreements.append((path.name, parsed_gap - reference_gap))

    print(f"\nHOMO-LUMO gap: {compared - len(disagreements)}/{compared} agree with the reference")
    for name, delta in disagreements:
        print(f"  DISAGREEMENT  {name}: {delta:+.4f} eV")

    masked_files = [item for item in with_errors if item[2]]
    print(f"\nFiles containing an error termination: {len(with_errors)}")
    print(f"  of which end with a normal-termination banner (masked): {len(masked_files)}")
    for name, count, is_masked in with_errors:
        print(f"  {name}: {count} error banner(s){'  [MASKED]' if is_masked else ''}")

    print(f"\nFiles yielding no parsable job: {len(unparsed)}")
    for name in unparsed:
        print(f"  {name}")

    molecules = load_directory_parallel(args.corpus, workers=1)
    collisions = {
        name: molecule.sources for name, molecule in molecules.items() if len(molecule.sources) > 1
    }
    print(f"\nColliding species identifiers: {len(collisions)}")
    for name, sources in collisions.items():
        print(f"  {name}: {len(sources)} sources")

    levels: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for name, molecule in molecules.items():
        for job in molecule.jobs:
            levels[name].add(job.metadata.level_of_theory())
    mixed = {name for name, values in levels.items() if len(values) > 1}
    print(f"Species spanning more than one level of theory: {len(mixed)}")

    return 1 if disagreements else 0


if __name__ == "__main__":
    raise SystemExit(main())
