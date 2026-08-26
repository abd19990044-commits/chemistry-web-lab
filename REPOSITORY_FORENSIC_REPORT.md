# REPOSITORY FORENSIC REPORT
**ORCA Web Lab - Production Release Candidate Forensic Audit**
**Date:** 2026-08-23
**Auditor:** Principal Scientific Software Engineer & Release Architect
**Target Release:** Production Release Candidate (Clean, Reproducible, Scientifically Defensible)

---

## 1. Executive Summary

This forensic audit evaluates the entirety of the **ORCA Web Lab** repository. The system is a hybrid web and computational chemistry platform providing:
1. **2D Chemical Structure & Reaction Scheme Visualization & Publishing** (powered by RDKit).
2. **ORCA Input Generator & PubChem/Solubility Data Aggregator** (supporting ORCA 5/6, DFT, Composite, TD-DFT, Solvation, Relativistic methods).
3. **Quantum Chemistry Engine** (`orca_engine`): Streaming output parser, thermochemistry calculator, TD-DFT / IR / UV-Vis spectral convolution, multi-spectrum overlay with experimental data, and single-point composite arithmetic.
4. **Fault-Tolerant Kaggle Orchestrator** (`orca_orchestrator` & legacy `kaggle_runner`): Long-running job management across 12-hour session windows with automated checkpointing, state recovery, and watchdog supervision.

While the core functionality and domain logic are exceptionally rich, the repository currently suffers from severe **environmental fragility, startup monkeypatches, untracked production files, duplicate legacy wrappers, scientific precision hazards, and gaps in CI/testing automation**.

---

## 2. Environment & System Specifications

| Component | Audit Observation | Production Target / Standard | Risk Level |
|---|---|---|---|
| **Python Runtime** | Host has Python 3.14.5 and 3.12.10; Dockerfile specifies `python:3.11-slim`. | Python 3.10 - 3.12 (Standard scientific C-extensions support). | **Medium** (Python 3.14 compatibility edge cases in dependencies). |
| **Host OS** | Windows 10/11 (`win32`), PowerShell environment. | Linux container (`Debian 12 / Ubuntu 22.04` on Hugging Face Spaces). | **Low** (Paths must remain cross-platform). |
| **Local Dependencies** | `orca-engine 1.0.0` was installed in editable mode pointing to `G:\orca_engine` instead of local `./orca_engine`. | Local package directory `./orca_engine/src` or canonical editable install. | **High** (Risk of desynchronized execution). |
| **Container Base** | `Dockerfile` builds `python:3.11-slim`, installs system Cairo libs (`libxrender1`, `libxext6`, `libsm6`, `libexpat1`). | Validated for Hugging Face Docker Space, single worker Gunicorn (`8 threads`, `900s timeout`). | **Low** (Configuration matches design). |
| **Repository Size** | Total size: **121.7 MB**. Major space taken by `orca_manual_6_1_0.pdf` (**76.8 MB**) and `orca_all_code.txt` (**2.2 MB**). | Core code is ~5 MB; large reference manuals and code dumps should not bloat git release. | **Medium** (Release hygiene & clone speed). |

---

## 3. Runtime Architecture & Dependency Graph

```mermaid
graph TD
    User([Browser Client]) -->|HTTP / HTML / Static| App[app.py : Flask Application]
    
    subgraph Web & API Surface [app.py]
        Auth[/api/auth/*]
        Explorer[/api/compound]
        ReactionDrawing[/api/reaction]
        Wizard[/api/orca/generate]
        Spectrum[/api/spectrum/*]
        OrchAPI[/api/orca/*]
        LegacyAPI[/api/kaggle/*]
    end
    
    App --> ChemCore[chem_core/ & chem_core.py]
    App --> Engine[orca_engine/src/orca_engine]
    App --> Orchestrator[orca_orchestrator/]
    App --> LegacyRunner[kaggle_runner.py]

    subgraph Chemistry Module [chem_core]
        ChemCoreInit[chem_core/__init__.py] --> Drawing[chem_core/drawing.py : RDKit Cairo/SVG]
        ChemCoreInit --> ChemCoreLegacy[chem_core.py : PubChem, 3D UFF, XYZ, Rxn]
    end

    subgraph Quantum Engine [orca_engine]
        Parser[parser.py : Streaming State Machine]
        Thermo[thermochemistry.py : Delta E/H/G/S, Keq, BDE]
        ExpSpec[experimental_spectrum.py : CSV/XLSX UV-Vis Reference]
        WebAdapter[adapters/web_adapter.py : Spectrum Convolution & 3D Viewer]
        Models[models.py : JobData, MoleculeData, ConsistencyReport]
    end

    subgraph Execution & Orchestration [orca_orchestrator]
        Service[service.py : OrchestratorService]
        Store[store.py : SQLite / State Files]
        Reconciler[reconciler.py : Kernel Lifecycle & Restarts]
        KaggleClient[kaggle_api.py : Kaggle CLI Subprocess Wrapper]
        RunnerBuilder[runner/builder.py & kernel_runner.py]
    end
```

---

## 4. Canonical Modules vs. Legacy / Duplicate Wrappers

| Module Path | Canonical Status | Role & Relationship | Action Needed |
|---|---|---|---|
| `app.py` | **Canonical** | Main Flask application, routing, static handler, error handlers. | Retain; harden limits and remove traceback leakage. |
| `chem_core.py` (1,746 lines) | **Canonical Core** | PubChem client, UFF 3D optimization, RXN/MOL generation, formula parsing. | Retain; integrate with package structure cleanly. |
| `chem_core/` (`__init__.py`, `drawing.py`) | **Canonical Package Wrapper** | Overrides legacy drawing with 600 DPI publication-grade RDKit Cairo/SVG renderer. | Retain; ensure clean import without dynamic import hack. |
| `kaggle_runner.py` (3,678 lines) | **Legacy / Fallback** | Historical single-file Kaggle runner and execution orchestrator. | Retain for backward compatibility with `/api/kaggle/*`. |
| `kaggle_runner/__init__.py` (38 lines) | **Compatibility Shim** | Dynamic import wrapper injecting `sys` into `kaggle_runner.py`. | Normalize `kaggle_runner.py` directly; remove fragile shim. |
| `orca_orchestrator/` (20 files) | **Canonical Orchestrator** | Modern SQLite-backed, transactional, multi-window runner for Kaggle ORCA jobs. | Retain as primary execution engine. |
| `orca_engine/src/orca_engine/` | **Canonical Engine** | Standalone quantum chemistry parser and thermochemistry package. | Retain and fix scientific calculation issues. |
| `sitecustomize.py` | **Fragile Monkeypatch** | Injects `sys.path`, intercepts Flask init to modify HTML responses (`job_runtime_fix.js`), patches `chem_core` drawing. | **Phase 5 Target**: Remove monkeypatch; integrate fixes directly into `app.py` and `chem_core`. |
| `usercustomize.py` | **Fragile Monkeypatch** | String-replaces internal code inside `kaggle_runner.KAGGLE_RUNNER_BODY`. | **Phase 5 Target**: Remove monkeypatch; bake heartbeat logic directly into `kaggle_runner.py`. |
| `merg.py` | **Artifact / Scratch** | Utility script used to create `orca_all_code.txt`. | Move to scripts or ignore. Do not track in production release. |
| `orca_all_code.txt` (2.2 MB) | **Artifact** | Monolithic text dump of entire codebase. | Remove from release distribution. |
| `orca_manual_6_1_0.pdf` (76.8 MB) | **Reference Artifact** | ORCA 6.1.0 official manual used for reverse-engineering keywords. | Keep in gitignore or documentation archive, exclude from slim releases. |
| `scratch_*.txt`, `manual_exact_specs.txt` | **Scratch / Forensic Data** | Data extractions from manual analysis. | Keep outside production distribution. |
| `.openclaw`, `.autoclaw`, `.agents` | **Agent Runtime Dumps** | Leftover session state from tooling. | Exclude via `.gitignore`. |

---

## 5. Git & Release Hygiene Risks

1. **Untracked Critical Production Code**:
   - `orca_engine/src/orca_engine/experimental_spectrum.py`: Contains full implementation of experimental UV-Vis parsing, multi-spectrum overlay, and CSV/Excel ingestion. If cloned without untracked files, `orca_engine` and `app.py` fail on import!
   - `orca_engine/tests/test_thermochemistry_scientific_audit.py`: Untracked scientific test suite.
   - `tests/conftest.py`: Untracked root pytest fixtures for test isolation.
   - Multiple active test suites: `test_dual_workflow_and_queue.py`, `test_experimental_spectrum.py`, `test_ir_spectrum.py`, `test_orca_builder.py`, `test_orca_input_generator.py`, `test_orca_runtime_smoke.py`, `test_production_forensic_fixes.py`, `test_pubchem_kaggle_thermo_fixes.py`, `test_scientific_benchmarks.py`, `test_security_hardening.py`, `test_thermochemistry_dual_slot.py`.
   - `pytest.ini`: Untracked root test configuration.
   - `static/js/workers/archive-worker.js`: Untracked web worker script for client-side archive decompression.
   - `static/docs/ChemistryLab_User_Manual.docx`: Untracked documentation download.
2. **CI Configuration Disconnect**:
   - `.github/workflows/ci.yml` only runs a tiny subset of 7 test files (`test_orca_engine_api.py`, `test_web_routes.py`, `test_drawing.py`, `test_frontend.py`, `test_production_wiring.py`, `test_account_control.py`, `test_reaction.py`).
   - 17 other test suites are completely skipped in CI!
3. **Dirty Git Working Directory**:
   - 50+ modified files pending commit, mixing feature implementations, bug fixes, and manual extractions.

---

## 6. Scientific Engine Forensic Summary

Line-by-line inspection of `orca_engine/src/orca_engine/` revealed the following critical items:

### A. Atom Balance Fractional Stoichiometry (Severity: P1)
- In `thermochemistry.py` `_check_atom_balance()`:
  ```python
  imbalance = {element: round(value) for element, value in deltas.items() if abs(value) > BALANCE_TOLERANCE}
  ```
  `round(value)` converts fractional differences (e.g. `0.1` mol from fractional coefficients) into `0`, yielding dictionary entries with zero imbalance or incorrect integer truncation. Must use floating-point tolerances and honest rounding.

### B. Custom Pressure Semantics (Severity: P1)
- Pressure is read from ORCA metadata (`pressure_atm`), but custom pressures do not calculate the translational entropy / ideal gas standard state correction ($\Delta G(P) = \Delta G(P_0) + R T \ln(P/P_0)$) or are simply reported as metadata. Must either calculate the exact ideal gas pressure correction to $G$ or document strictly as metadata without misleading thermodynamics claims.

### C. Temperature Handling & Gibbs Energy Evaluation (Severity: P0)
- In `evaluate()`:
  $K_{\text{eq}} = \exp(-\Delta G / (R \cdot T_{\text{reported}}))$ uses the temperature reported in the ORCA calculation. If a user queries or specifies a different temperature, $\Delta G(T)$ cannot simply use $\Delta G(T_0)$ without recomputing the thermal corrections (or applying van 't Hoff approximation with explicit assumptions). The code must distinguish between $T_{\text{calc}}$ and $T_{\text{custom}}$.

### D. Geometry Hash & Provenance (Severity: P2)
- `JobData.compute_geometry_hash()` sorts strings of formatted coordinates:
  ```python
  sorted_atoms = "".join(sorted(atom_strings))
  self.geometry_hash = hashlib.sha256(sorted_atoms.encode("utf-8")).hexdigest()
  ```
  This is a coordinate multiset hash rather than a canonical molecular structure hash (it is coordinate-system dependent and not rotation/translation invariant). While sufficient for detecting identical coordinate tables between SP and Freq steps of the same calculation, it must be documented explicitly as a **Coordinate Table Hash** to avoid overclaiming.

### E. Stationary Point Classification (Severity: P1)
- `stationary_point_status` classifies minima and transition states based on `imaginary_frequencies_count == 0` or `1`. However, it must verify the expected vibrational mode count ($3N-6$ for non-linear, $3N-5$ for linear molecules) and ensure the calculation completed normally without truncated frequency blocks.

---

## 7. Security & Resource Hardening Audit

1. **Archive Ingestion (Zip / Tar / RAR Slip & Bombs)**:
   - In `app.py` and `orca_artifacts.py`: Ensure strict path normalization preventing directory traversal (`../`) during archive extraction.
   - Decompression bombs: Need explicit caps on total extracted file size (e.g., max 2 GB decompressed) and total file count (max 10,000 files).
2. **Operational Endpoints Protection**:
   - `/api/orca/sweep` and internal admin/debug endpoints: Must not expose sensitive system paths or execute arbitrary unauthenticated sweeps.
3. **Subprocess Execution & Command Injection**:
   - `kaggle_runner.py` and `orca_orchestrator/kaggle_api.py` execute the `kaggle` CLI. All arguments must be passed as argument vectors (`list[str]`), never through `shell=True`.
4. **Credential & Secret Hygiene**:
   - Kaggle tokens/keys are handled in-memory and in dedicated per-request temporary configs. Must ensure no credentials leak in logs, tracebacks, or API error payloads.

---

## 8. Recommended Phased Repair Roadmap

```text
Phase 0: Environment and Repository Forensics (COMPLETED)
Phase 1: Scientific Engine Forensic Audit & Core Fixes (Thermochemistry, Pressure, Atoms, Freqs, Hashes)
Phase 2: Test Coverage & Gaps Analysis (Add comprehensive regression tests)
Phase 3: Security & Resource Hardening (Archive extraction, limits, endpoint protection)
Phase 4: Release / Git Integrity (Track missing code, clean up artifacts, configure .gitignore, requirements)
Phase 5: Remove Fragile Startup Patches (Eliminate sitecustomize.py and usercustomize.py hacks)
Phase 6: Documentation Consistency (Sync README, ARCHITECTURE, DEPLOY with actual code)
Phase 7: Reproducibility Validation (Clean clone test in isolated venv)
Phase 8: Final Production Validation & Verdict Report
```

---
*End of Phase 0 Deliverable.*
