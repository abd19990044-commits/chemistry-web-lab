# orca-engine

[![CI](https://github.com/salamhasan/orca-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/salamhasan/orca-engine/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Checked with mypy](https://img.shields.io/badge/mypy-strict-blue)](https://mypy-lang.org/)

A streaming parser and thermochemistry engine for [ORCA](https://www.faccts.de/orca/)
quantum chemistry output files. It extracts energies, frontier orbitals,
geometries, and excited states from ORCA 5 and ORCA 6 outputs, and computes
reaction energies and bond dissociation energies with automatic physical
consistency checking.

The library has no runtime dependencies outside the Python standard library.

## Why another ORCA parser

Most ORCA parsing is done with ad hoc scripts that grep for a label and keep the
last match. That approach is fragile in ways that are hard to notice, because
the failures produce plausible numbers rather than errors. `orca-engine` was
written after a set of such defects was found in exactly that kind of script:

- **Frontier orbitals from different geometries.** A geometry optimization
  prints more than one orbital table. Naive "last value wins" logic can pair the
  converged HOMO with the *initial* geometry's LUMO. In our 62-file validation
  set this corrupted 12 gaps, by up to 1.01 eV.
- **No LUMO for an empty spin channel.** In a one-electron doublet the beta
  channel holds no electrons, so a guard requiring a HOMO first records no beta
  LUMO and the reported value falls back to the lowest alpha virtual. Errors of
  4.79–7.50 eV on 4 files — larger than the defect above, and found last.
- **Bohr reported as Angstrom.** ORCA prints every geometry twice, in Angstrom
  and in atomic units. Overwriting the first with the second scales all
  coordinates by 1.8897.
- **Errors masked by reruns.** Appending a rerun to an existing output leaves a
  normal-termination banner at the end, hiding the earlier failure. 7 of 69
  files in our corpus contain error terminations; 2 of those are reported as
  clean by a last-banner-wins check.
- **Silent cross-level arithmetic.** Nothing stops a script from subtracting a
  def2-SVP energy from a def2-TZVP one, or a gas-phase energy from an SMD one.

`orca-engine` treats each of these as a correctness requirement with a
regression test, not as a caveat in the documentation.

## Installation

```bash
pip install orca-engine
```

From source:

```bash
git clone https://github.com/salamhasan/orca-engine.git
cd orca-engine
pip install -e ".[dev]"
```

## Command line

Parse a directory of outputs into a JSON report:

```bash
orca-engine -d ./calculations -o results.json
```

Compute a reaction energy:

```bash
orca-engine -d ./calculations --rxn "ethene + h2 -> ethane"
```

Compute a bond dissociation energy using electronic energies plus ZPE:

```bash
orca-engine -d ./calculations --bde "toluene -> benzyl + h" --bde-kind electronic_zpe
```

Fail the run if a reaction is unbalanced or mixes levels of theory, which is
what you want inside a pipeline:

```bash
orca-engine -d ./calculations --rxn "a + b -> c" --strict-consistency
```

### Options

| Flag | Meaning |
| --- | --- |
| `-d`, `--dir` | Directory or single `.out`, `.log`, or `.zip` file. Default `.` |
| `-o`, `--output` | Report path. Defaults to `ORCA_Parsed_Data.<format>` |
| `--format` | `json` (default) or `csv` |
| `-w`, `--workers` | Worker count. Defaults to the CPU count |
| `--io-bound` | Use threads instead of processes for many small files |
| `--rxn` | Reaction equation, e.g. `"2 a + b -> c"` |
| `--bde` | Dissociation equation, e.g. `"parent -> frag + h"` |
| `--bde-kind` | `electronic`, `electronic_zpe`, `gibbs`, or `enthalpy` |
| `--strict-duplicates` | Abort when two files map to the same species name |
| `--strict-consistency` | Exit 3 when a reaction fails a consistency check |
| `-v`, `--verbose` | Debug logging |

Exit codes: `0` success, `1` nothing parsed or write failure, `2` usage error,
`3` consistency failure under `--strict-consistency`.

## Python API

```python
from pathlib import Path

from orca_engine import EnergyKind, ThermochemistryEngine, load_directory_parallel

molecules = load_directory_parallel(Path("calculations"), workers=8)

engine = ThermochemistryEngine(molecules)
result = engine.evaluate("ethene + h2 -> ethane")

print(result.delta_g_kcal_mol)
for warning in result.consistency.warnings:
    print("check failed:", warning)
```

Species are keyed by output file stem, matched case-insensitively. A file named
`ethene.out` is referred to as `ethene` in an equation.

Parsing a single stream directly:

```python
from orca_engine import OrcaParser

with open("job.out", encoding="utf-8", errors="ignore") as stream:
    jobs = OrcaParser(stream, source_name="job.out").parse()

job = jobs[-1]
print(job.homo_lumo_gap_ev, job.metadata.level_of_theory())
```

The parser consumes any `Iterator[str]`, so ZIP members, decompressed streams,
and network sources work without buffering the whole file.

## What is extracted

| Quantity | Field | Unit |
| --- | --- | --- |
| Final single point energy | `e_elec_eh` | Hartree |
| Zero-point energy | `zpe_eh` | Hartree |
| Total enthalpy | `total_enthalpy_eh` | Hartree |
| Final Gibbs free energy | `gibbs_free_energy_eh` | Hartree |
| Final entropy term (`+T·S`) | `entropy_term_eh` | Hartree |
| Total entropy correction (`−T·S`) | `entropy_correction_eh` | Hartree |
| HOMO / LUMO / gap | `homo_ev`, `lumo_ev`, `homo_lumo_gap_ev` | eV |
| Per-spin frontier levels | `alpha_homo_ev`, `beta_lumo_ev`, … | eV |
| Final geometry | `elements`, `coords`, `coords_unit` | Å |
| TDDFT transitions | `tddft_cm`, `tddft_fosc` | cm⁻¹, unitless |
| Charge, multiplicity | `metadata.charge`, `metadata.multiplicity` | — |
| Temperature, pressure | `metadata.temperature_k`, `metadata.pressure_atm` | K, atm |
| Termination | `terminated_normally`, `had_error_termination` | — |

Note that ORCA's "entropy" lines report `T·S` in Hartree, which is an energy.
The two fields are kept separate and named so that the sign convention is
explicit.

## Consistency checks

Every reaction and BDE result carries a `ConsistencyReport`:

- **Atom balance** — weighted element counts on both sides, from the parsed
  geometries.
- **Charge balance** — weighted total charge on both sides.
- **Level of theory** — all species must share `(ORCA version, basis set,
  solvation model)`.
- **Temperature** — Gibbs energies must come from a common temperature.
- **Termination** — no participating species may have an error-terminated job.

A check that cannot be evaluated (for example atom balance when no geometry was
printed) reports `None` rather than passing. Failing a check annotates the
result; it does not suppress the number, so you can still inspect it.

## Supported input

- ORCA 5.x and 6.x output files (`.out`, `.log`, any case)
- Multi-job outputs separated by `$new_job`
- ZIP archives containing ORCA outputs, parsed without extraction
- Restricted and unrestricted (spin-polarized) calculations

## Development

```bash
pip install -e ".[dev]"

pytest                       # 83 tests
mypy                         # strict mode, no errors
ruff check . && ruff format --check .

python scripts/run_mutations.py   # confirms the suite detects all 18 defects
```

## Validation data

The 69-file ORCA corpus used to validate the parser is not committed to this
repository; it is archived separately with a DOI. See `data/README.md` for the
archive reference and for how to reproduce the validation run. Small synthetic
fixtures used by the test suite live in `tests/data/` and are tracked here.

## Citation

If this software contributes to work you publish, please cite it. See
[`CITATION.cff`](CITATION.cff).

## License

MIT. See [LICENSE](LICENSE).
