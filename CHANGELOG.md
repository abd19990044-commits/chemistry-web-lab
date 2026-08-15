# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-07-29

First public release. This version corrects several defects in the pre-release
code that produced plausible but incorrect numbers rather than errors. Anyone
who used the pre-release parser should re-derive their results.

### Fixed

- **Frontier orbitals could come from different geometry optimization steps.**
  Each `ORBITAL ENERGIES` section now resets the frontier window, and a further
  occupied orbital invalidates a previously assigned LUMO. Previously the HOMO
  came from the final section while the LUMO was retained from the first, which
  corrupted 12 of 62 HOMO–LUMO gaps in the validation set, by up to 1.01 eV.
- **No LUMO was recorded for a spin channel holding no electrons.** In an
  open-shell system such as a one-electron doublet, the beta channel has no
  occupied orbital, so a guard requiring a HOMO first left it empty and the
  reported LUMO fell back to the lowest *alpha* virtual. This affected 4 of 62
  files in the validation set with gap errors of 4.79 to 7.50 eV — larger than
  any other defect fixed in this release.
- **`.OUT` files were invisible on Linux and macOS.** File discovery is now
  case-insensitive and de-duplicated. The previous case-sensitive glob returned
  different file sets on different platforms, so the same directory produced
  different results depending on the operating system.
- **Atomic-unit coordinates could be reported as Angstrom.** The Angstrom and
  A.U. coordinate blocks are now distinguished explicitly and the unit is
  recorded in `coords_unit`. Previously the correct behaviour depended on an
  incidental state-machine transition.
- **Error terminations were masked by a later rerun.** All error banners are now
  retained in `error_messages` and surfaced through `had_error_termination`, and
  the flag is aggregated across job blocks by `MoleculeData.had_error_termination`
  so that a compound job cannot hide a failure in an earlier block. Seven of 69
  files in the validation corpus contain error terminations; two of those end
  with a normal-termination banner and were reported as clean runs.
- **Calculations that failed before the SCF vanished from the report.** ORCA's
  `INPUT ERROR` and `aborting the run` banners are now recognised, so such a
  file is reported as a failed calculation rather than silently dropped.
- **Spin-orbit-corrected absorption spectra were merged with uncorrected ones.**
  ORCA's SOC header contains the plain header as a substring; the excited-state
  pattern now excludes it.
- **Counterpoise ghost centres truncated the geometry.** ORCA writes ghost
  centres as `H:`, which failed the element pattern and ended the coordinate
  section, dropping every real atom that followed. Ghosts are now parsed and
  excluded from `stoichiometry()`.
- **The entropy field conflated `+T·S` and `−T·S`.** ORCA prints both "Final
  entropy term" and "Total entropy correction"; whichever appeared last won, so
  the sign of the stored value depended on print order. These are now separate
  fields, `entropy_term_eh` and `entropy_correction_eh`, both documented as
  energies in Hartree rather than entropies.
- **The entropy pattern matched explanatory prose.** The loose `Entropy`
  alternative matched the sentence "out the resulting rotational entropy values
  for sn=1,12:" and captured 1.0 as an entropy. Patterns are now anchored on
  ORCA's exact labels.
- **The error-termination pattern matched diagnostic text in successful runs.**
  The former catch-all `Error\s*:` alternative is removed.
- **Results depended on worker completion order.** Parallel loading now uses
  `Executor.map`, which preserves input order, instead of `as_completed`.
  Repeated runs over a directory now produce byte-identical reports.
- **A file named `2b.out` was unreachable from a reaction equation.** Tokens
  that parse as a coefficient followed by a name are now resolved against the
  species that were actually loaded.

### Added

- Physical consistency checking on every reaction and BDE result: atom balance,
  charge balance, level-of-theory agreement, temperature agreement, and error
  termination. Checks that cannot be evaluated report `None` rather than
  passing. Level-of-theory agreement inspects every job of every participating
  species, not only the job supplying the electronic energy, because different
  quantities are deliberately drawn from different jobs.
- Capture of molecular charge, spin multiplicity, temperature, and pressure.
- `homo_lumo_gap_ev`, `stoichiometry()`, and an empirical formula in reports.
- Warnings when two source files map to the same species identifier, with
  `--strict-duplicates` to make it fatal, and `--strict-consistency` to make a
  failed reaction check a non-zero exit.
- `CoordinateUnit` and `ConsistencyReport` to the public API.
- A test suite of 83 tests, each regression test naming the defect it pins,
  plus `scripts/run_mutations.py`, which restores each defect in a scratch
  copy and confirms the suite detects all 18.
- Continuous integration across Linux, macOS and Windows on Python 3.10–3.13.

### Changed

- The package moved to a `src/` layout and is installable, with an
  `orca-engine` console script.
- `JobData.coords_xyz` is now a deprecated alias for `JobData.coords`, which is
  accompanied by an explicit `coords_unit`.
- `entropy_value` and `entropy_unit` are replaced by `entropy_term_eh` and
  `entropy_correction_eh`.
- Missing reference energies are logged at debug rather than error level; the
  caller receives them in `missing_references`.
