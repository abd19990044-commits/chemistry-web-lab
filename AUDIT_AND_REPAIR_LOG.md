# AUDIT AND REPAIR LOG
**ORCA Web Lab - Release Candidate Transformation Log**
**Date:** 2026-08-23
**Lead Engineer:** Principal Scientific Software Engineer, Backend Architect & Release Engineer

---

## Executive Summary

This log records every modification made to **ORCA Web Lab** during the release candidate transformation process. All modifications strictly adhere to the operating rules:
1. **Scientific Correctness & Provenance**: Never silently modify physical values; clearly separate raw output, derived values, and approximations (e.g. van 't Hoff).
2. **Architectural Preservation**: Retain existing Flask backend, Kaggle multi-session orchestrator, SQLite state machine, and RDKit chemistry core.
3. **Evidence-Based Engineering**: Every fix has an identified problem, root cause, evidence, impact, code modification, and regression test.
4. **No Runtime Monkeypatches**: All functionality is embedded directly and explicitly in application source files.

---

## Change Management Table

| Issue ID | Severity | Affected File(s) | Root Cause | Change Summary | Scientific / Technical Rationale | Test Added / Updated | Verification Result |
|---|---|---|---|---|---|---|---|
| **INIT-00** | Info | `REPOSITORY_FORENSIC_REPORT.md`, `SCIENTIFIC_FORENSIC_AUDIT.md`, `TEST_COVERAGE_AND_GAPS.md` | Phase 0–2 audit requirements | Comprehensive forensics, scientific audit, and test gap analysis. | Complete baseline provenance before code modifications. | All 249 tests audited. | Completed |
| **SCI-01** | P0 (Critical) | `orca_engine/src/orca_engine/thermochemistry.py`, `models.py` | Thermodynamic non-equivalence when evaluating $K_{\text{eq}}$ with custom temperatures without applying temperature corrections. | Separated $T_{\text{calc}}$ from $T_{\text{requested}}$. Implemented van 't Hoff extrapolation ($\Delta C_p^\circ = 0$) with explicit warnings and metadata. | Enforces thermodynamic consistency between $H^\circ(T)$, $G^\circ(T)$, and $K_{\text{eq}}(T)$. | `test_thermochemistry_scientific_audit.py::test_custom_temperature_thermodynamics` | PASSED |
| **SCI-02** | P1 (High) | `orca_engine/src/orca_engine/thermochemistry.py`, `models.py` | Missing ideal gas pressure correction $\Delta n_{\text{gas}} R T \ln(P/P_0)$ for non-1-atm reactions. | Added ideal gas standard state pressure shift when $P \neq 1.0\,\text{atm}$ and populated `pressure_correction_applied`. | Restores correct thermodynamic standard state shifts for gas-phase reactions at non-standard pressures. | `test_thermochemistry_scientific_audit.py::test_pressure_entropy_correction` | PASSED |
| **SCI-03** | P1 (High) | `orca_engine/src/orca_engine/thermochemistry.py`, `models.py` | `_check_atom_balance()` rounded fractional element deltas ($0.1$) to integer $0$, silently masking stoichiometry imbalances. | Preserved floating-point deltas in `imbalance` dictionary without integer truncation. | Ensures accurate mass conservation verification for fractional stoichiometry reactions. | `test_thermochemistry_scientific_audit.py::test_fractional_stoichiometry_atom_balance` | PASSED |
| **SCI-04** | P1 (High) | `orca_engine/src/orca_engine/models.py`, `parser.py` | Stationary point classification claimed `MINIMUM` or `TRANSITION_STATE` based solely on $N_{\text{imag}}$ without checking $3N-6 / 3N-5$ mode completeness or normal termination. | Added `expected_vibrational_modes_count`, `is_linear_geometry()`, and rigorous mode completeness checks. | Prevents unphysical classification of incomplete or unconverged calculations as stationary points. | `test_thermochemistry_scientific_audit.py::test_stationary_point_rigorous_classification` | PASSED |
| **SCI-05** | P1 (High) | `orca_engine/src/orca_engine/thermochemistry.py`, `models.py` | Multi-level composite calculations (DLPNO-CCSD(T) // DFT) lacked structured provenance tracking. | Added `ReactionResult.composite_provenance` capturing electronic level, frequency level, and coordinate table match status. | Guarantees transparent provenance for high-level composite quantum thermochemistry. | `test_thermochemistry_scientific_audit.py::test_geometry_mismatch_flagged_in_composite` | PASSED |
| **SCI-06** | P2 (Medium) | `orca_engine/src/orca_engine/models.py` | `compute_geometry_hash()` docstring lacked explicit definition of coordinate frame dependency. | Documented that hash is a Cartesian coordinate table fingerprint to verify identical frames between SP and Freq steps. | Clarifies scientific scope and prevents misuse as a rotationally invariant canonical SMILES substitute. | `test_thermochemistry.py` | PASSED |
| **SCI-07** | P1 (High) | `orca_engine/src/orca_engine/thermochemistry.py` | Entropy unit conversions between $\text{Hartree/K}$, $\text{cal}/(\text{mol}\cdot\text{K})$, and $\text{J}/(\text{mol}\cdot\text{K})$. | Verified physical constant ratios and consistency tolerance checks ($< 0.15\,\text{cal}/(\text{mol}\cdot\text{K})$). | Eliminates unit conversion discrepancies in Gibbs and entropy reporting. | `test_thermochemistry_scientific_audit.py::test_entropy_components_parsing` | PASSED |
| **SEC-01** | P1 (High) | `kaggle_runner.py`, `orca_orchestrator/kaggle_api.py` | `[sys.executable, "-m", "kaggle"]` failed on modern Kaggle CLI installations lacking `__main__.py`, causing `/health` endpoint failures. | Updated CLI resolution to resolve `shutil.which("kaggle")` with programmatic fallback to `kaggle.cli:main`. | Ensures robust cross-platform subprocess execution of Kaggle commands on both Linux and Windows. | `tests/test_production_forensic_fixes.py::test_phase0_local_development_defaults_and_unauth` | PASSED |
| **SEC-02** | P1 (High) | `sitecustomize.py`, `usercustomize.py`, `templates/index.html`, `kaggle_runner.py` | Fragile startup monkeypatches mutated `Flask.__init__` HTML responses and performed runtime string replacement on `KAGGLE_RUNNER_BODY`. | Embedded `job_runtime_fix.js` directly in `templates/index.html`, embedded heartbeat wait loop in `kaggle_runner.py`, and cleaned `sitecustomize.py` / `usercustomize.py`. | Eliminates brittle runtime monkeypatching and improves code transparency, debugging, and auditability. | Full Flask integration test suite | PASSED |
| **GIT-01** | P1 (High) | `.gitignore`, `Dockerfile`, `.github/workflows/ci.yml` | Untracked production files, missing package install in Docker, large PDFs (76.8 MB) in workspace, incomplete CI test matrix. | Added `.gitignore` patterns for large files/dumps/caches; added `pip install -e ./orca_engine` in Dockerfile; updated CI workflow to run full test discovery. | Establishes reproducible build and deployment pipelines. | `tests/test_deployment.py`, CI workflow verification | PASSED |

---
