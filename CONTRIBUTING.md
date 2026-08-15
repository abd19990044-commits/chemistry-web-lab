# Contributing

Thanks for your interest in improving `orca-engine`.

## Setup

```bash
git clone https://github.com/salamhasan/orca-engine.git
cd orca-engine
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Before opening a pull request

All four must pass; CI enforces them on Linux, macOS and Windows across Python
3.10–3.13.

```bash
pytest
mypy
ruff check .
ruff format --check .
```

## Reporting a parsing bug

Parsing bugs are the most valuable reports, and the most useful ones include a
minimal excerpt. Please open an issue with:

1. The ORCA version and the relevant input keywords.
2. A short excerpt of the output around the section that parses incorrectly,
   trimmed to a few dozen lines.
3. The value `orca-engine` returned and the value it should have returned.

Full output files are usually not needed and are often large.

## Adding a fixture

Test data lives in `tests/data/` and is kept small and hand-written. A fixture
should exercise one behaviour and be readable at a glance; please do not add
multi-megabyte real outputs to the repository.

If you are fixing a bug, add the fixture and a test that fails before your fix
and passes after it. Give the test a docstring that names the defect, following
the style of the existing regression tests:

```python
def test_angstrom_block_is_preferred_over_atomic_units(self) -> None:
    """Regression: the trailing A.U. block could overwrite Angstrom values.

    ORCA prints every geometry twice. The Bohr copy must never be reported
    as Angstrom, which would scale all coordinates by 1.889726.
    """
```

This matters more here than in most projects. The failure modes this library
guards against produce plausible numbers rather than exceptions, so a test that
does not explain what it is protecting tends to be "simplified" away later.

## Scientific correctness

Changes that affect a reported number need an independent check, not just a
passing test. That can be a hand calculation in the test, agreement with a
value printed elsewhere in the same ORCA output, or a comparison against an
independent extraction of the same quantity.

If a quantity cannot be determined from the input, return `None`. Do not
substitute a default, and do not silently omit a term from a sum — report it
through `missing_references` or a consistency warning instead.

## Style

- Google-style docstrings on every public module, class and function.
- Full type annotations; `mypy --strict` must pass.
- Units belong in field names (`e_elec_eh`, `homo_ev`, `delta_g_kcal_mol`).
- Regular expressions are anchored on ORCA's exact printed labels. Loose
  keyword matching is how several of the bugs in v1.0.0 arose.

## License

Contributions are accepted under the MIT License.
