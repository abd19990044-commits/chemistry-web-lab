# Validation data

This directory holds the ORCA output corpus used to validate the parser. It is
**not tracked in git** (see `.gitignore`); the archive is deposited separately
so that the repository stays small and the data set gets its own persistent
identifier.

## Contents

| Subset | Files | ORCA version |
| --- | --- | --- |
| `orcafile/orca5/` | 55 | 5.0.2 |
| `orcafile/orca6.1/` | 14 | 6.1.0 |

The corpus covers geometry optimizations, frequency jobs, single points, TDDFT
absorption spectra, restricted and unrestricted calculations, gas-phase and SMD
solvated runs, and several runs that terminated with errors. Total size is
approximately 24 MB.

## Obtaining the data

Deposit the `orcafile/` directory on Zenodo and record the DOI here before
submitting the manuscript:

    DOI: 10.5281/zenodo.XXXXXXX

Then place the extracted archive at `data/orcafile/`.

## Reproducing the validation

With the corpus in place, from the repository root:

```bash
python scripts/validate_corpus.py data/orcafile
```

The script re-parses every file, extracts HOMO and LUMO with an independent
spin-aware reference implementation, and reports any disagreement. It also
counts files whose error terminations are masked by a later normal termination
banner, and species identifiers that collide across subdirectories.
