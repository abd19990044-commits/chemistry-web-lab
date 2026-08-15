"""Reproduce Table 2 of the manuscript by mutation testing.

Each mutation restores one pre-1.0 defect or removes one guard, then runs the
test suite. A mutation that no test detects is a gap in the suite, not a
success: the defects this project guards against are silent, so a passing test
that would also pass on the broken code is measuring nothing.

The repository tree is copied to a temporary directory, so the working copy is
never modified.

Usage::

    python scripts/run_mutations.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Mutation:
    """One source edit that restores a known defect.

    Attributes:
        name: Description used in the results table.
        relative_path: Source file to edit, relative to the repository root.
        before: Exact text to replace. Must be present, or the mutation is
            reported as stale rather than silently skipped.
        after: Replacement text.
    """

    name: str
    relative_path: str
    before: str
    after: str


PARSER = "src/orca_engine/parser.py"
MODELS = "src/orca_engine/models.py"
IO = "src/orca_engine/io.py"
REGEX = "src/orca_engine/regex.py"
THERMO = "src/orca_engine/thermochemistry.py"

MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "Remove orbital-section reset",
        PARSER,
        "        if spin_channel is None:\n            self.job.reset_orbitals()\n",
        "",
    ),
    Mutation(
        "Restore permissive LUMO guard (cross-section)",
        MODELS,
        "            self.lumo_ev = None\n        elif self.lumo_ev is None:",
        "        elif self.lumo_ev is None:",
    ),
    Mutation(
        "Require a HOMO before recording a LUMO",
        MODELS,
        "elif self.lumo_ev is None:",
        "elif self.homo_ev is not None and self.lumo_ev is None:",
    ),
    Mutation(
        "Restore case-sensitive file discovery",
        IO,
        "candidate.suffix.lower() in SUPPORTED_SUFFIXES",
        "candidate.suffix in SUPPORTED_SUFFIXES",
    ),
    Mutation(
        "Discard error-message history",
        PARSER,
        "        self.job.error_messages.append(line)\n",
        "",
    ),
    Mutation(
        "Report only the last job's error flag",
        MODELS,
        "        return any(job.had_error_termination for job in self.jobs)",
        "        return self.jobs[-1].had_error_termination if self.jobs else False",
    ),
    Mutation(
        "Remove the atom-balance check",
        THERMO,
        "        self._check_atom_balance(reaction, report)\n",
        "",
    ),
    Mutation(
        "Remove the termination check",
        THERMO,
        "        self._check_terminations(reaction, report)\n",
        "",
    ),
    Mutation(
        "Remove the temperature check",
        THERMO,
        "        if len(temperatures) > 1:",
        "        if False:",
    ),
    Mutation(
        "Inspect one job per species for level of theory",
        THERMO,
        "            for level in molecule.levels_of_theory():",
        "            for level in molecule.levels_of_theory()[:1]:",
    ),
    Mutation(
        "Prefer the atomic-unit coordinate block",
        PARSER,
        "            and self.job.elements\n        ):",
        "            and False\n        ):",
    ),
    Mutation(
        "Hard-code the coordinate unit to Angstrom",
        PARSER,
        "        self.job.coords_unit = unit",
        "        self.job.coords_unit = CoordinateUnit.ANGSTROM",
    ),
    Mutation(
        "Swallow the line that terminates a section",
        PARSER,
        "        self.state = ParserState.SEARCHING\n        self._handle_searching(line)\n\n"
        "    def _handle_orbitals",
        "        self.state = ParserState.SEARCHING\n\n    def _handle_orbitals",
    ),
    Mutation(
        "Restore the loose entropy pattern",
        REGEX,
        'rf"Final entropy term\\s*\\.*\\s*(?P<value>{FLOAT})\\s*Eh"',
        'rf"(?:Final entropy term|Entropy)\\s+.*?(?P<value>{FLOAT})"',
    ),
    Mutation(
        "Restore the catch-all Error: pattern",
        REGEX,
        'r"(?:ORCA\\s+finished\\s+by\\s+error\\s+termination"',
        'r"(?:Error\\s*:\\s*.*|ORCA\\s+finished\\s+by\\s+error\\s+termination"',
    ),
    Mutation(
        "Match the SOC spectrum header as a substring",
        REGEX,
        'r"(?<!CORRECTED\\s)\\bABSORPTION',
        'r"\\bABSORPTION',
    ),
    Mutation(
        "Leave ghost centres unmatched",
        REGEX,
        'r"^(?P<symbol>[A-Za-z]{1,3})(?P<ghost>:?)$"',
        'r"^(?P<symbol>[A-Za-z]{1,3})(?P<ghost>)$"',
    ),
    Mutation(
        "Collect parallel results with as_completed",
        IO,
        "            per_file = list(executor.map(parse_path_safe, files))",
        "            from concurrent.futures import as_completed\n\n"
        "            futures = {executor.submit(parse_path_safe, p): p for p in files}\n"
        "            per_file = [future.result() for future in as_completed(futures)]",
    ),
)


def count_failures(tree: Path) -> int:
    """Run the test suite in ``tree`` and return the number of failing tests."""

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
        cwd=tree,
        capture_output=True,
        text=True,
        check=False,
    )
    tail = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    failures = 0
    for token, following in zip(tail.split(), tail.split()[1:], strict=False):
        if following.startswith(("failed", "error")) and token.isdigit():
            failures += int(token)
    if failures == 0 and result.returncode != 0:
        return 1
    return failures


def main() -> int:
    """Apply every mutation in a scratch copy and report detection.

    Returns:
        ``0`` if every mutation was detected by at least one test, else ``1``.
    """

    caches = shutil.ignore_patterns(
        ".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", "*.egg-info"
    )

    def ignore(directory: str, names: list[str]) -> set[str]:
        """Skip caches everywhere and the corpus only at the repository root.

        A blanket ``"data"`` pattern would also drop ``tests/data``, which is
        where every fixture lives.
        """

        skipped = set(caches(directory, names))
        if Path(directory).resolve() == REPO_ROOT:
            skipped.add("data")
        return skipped

    with tempfile.TemporaryDirectory() as scratch:
        tree = Path(scratch) / "orca-engine"
        shutil.copytree(REPO_ROOT, tree, ignore=ignore)

        baseline = count_failures(tree)
        if baseline:
            print(f"Baseline suite is not green ({baseline} failing). Fix that first.")
            return 1
        print(f"{'Mutation':<48} {'Tests failed':>12}")
        print("-" * 62)

        undetected: list[str] = []
        for mutation in MUTATIONS:
            target = tree / mutation.relative_path
            original = target.read_text(encoding="utf-8")
            if mutation.before not in original:
                print(f"{mutation.name:<48} {'STALE':>12}")
                undetected.append(f"{mutation.name} (pattern no longer present)")
                continue

            target.write_text(
                original.replace(mutation.before, mutation.after, 1), encoding="utf-8"
            )
            failures = count_failures(tree)
            target.write_text(original, encoding="utf-8")

            print(f"{mutation.name:<48} {failures:>12}")
            if failures == 0:
                undetected.append(mutation.name)

    print("-" * 62)
    if undetected:
        print(f"UNDETECTED ({len(undetected)}):")
        for name in undetected:
            print(f"  - {name}")
        return 1
    print(f"All {len(MUTATIONS)} mutations detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
