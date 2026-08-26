# TEST COVERAGE AND GAPS ANALYSIS
**ORCA Web Lab - Test Infrastructure & Scientific Quality Assurance**
**Date:** 2026-08-23
**Auditor:** Principal QA Engineer & Scientific Test Architect

---

## 1. Test Suite Inventory

The repository comprises **249 test cases** distributed across two testing directories:
1. `orca_engine/tests/` (Quantum Chemistry Engine, Streaming Parser, Thermochemistry, Web Adapter)
2. `tests/` (Core Flask App, RDKit Drawing Bot, Orchestrator, Checkpointing, Security, Kaggle Simulation)

```
Test Distribution Overview:
├── orca_engine/tests/
│   ├── test_parser.py (18 test cases)
│   ├── test_thermochemistry.py (12 test cases)
│   ├── test_thermochemistry_scientific_audit.py (14 test cases)
│   ├── test_web_adapter.py (10 test cases)
│   └── test_io_and_cli.py (8 test cases)
└── tests/
    ├── test_account_control.py (4 test cases)
    ├── test_continuation.py (1 test case / 12 assertions)
    ├── test_deployment.py (1 test case / 15 assertions)
    ├── test_drawing.py (5 test cases)
    ├── test_dual_workflow_and_queue.py (2 test cases)
    ├── test_end_to_end_chain.py (1 test case / 9 multi-session scenarios)
    ├── test_experimental_spectrum.py (16 test cases)
    ├── test_frontend.py (12 test cases)
    ├── test_full_system_audit.py (14 test cases)
    ├── test_ir_spectrum.py (8 test cases)
    ├── test_lifecycle_simulation.py (1 test case / 8 lifecycle states)
    ├── test_orca_builder.py (10 test cases)
    ├── test_orca_engine_api.py (5 test cases)
    ├── test_orca_input_generator.py (6 test cases)
    ├── test_orca_runtime_smoke.py (8 test cases)
    ├── test_orchestrator.py (35 test cases)
    ├── test_production_forensic_fixes.py (16 test cases)
    ├── test_production_wiring.py (10 test cases)
    ├── test_pubchem_kaggle_thermo_fixes.py (12 test cases)
    ├── test_reaction.py (18 test cases)
    ├── test_scientific_benchmarks.py (6 test cases)
    ├── test_security_hardening.py (14 test cases)
    ├── test_thermochemistry_dual_slot.py (4 test cases)
    └── test_web_routes.py (18 test cases)
```

---

## 2. Categorical Gap Analysis

| Category | Existing Coverage | Identified Testing Gaps | Action Required |
|---|---|---|---|
| **Scientific Golden Tests** | Real ORCA outputs for $\text{H}_2\text{O}$, $\text{NH}_3$, $\text{C}_6\text{H}_6$ (TDDFT), $\text{CH}_4$, and standard atomization reactions. | Composite DLPNO-CCSD(T) // DFT energy combination golden benchmark with geometry mismatch checks. | Added in `test_thermochemistry_scientific_audit.py`. |
| **Parser Edge Cases** | Upper/lowercase filenames, ghost atoms (`H:`), Bohr units, truncated orbital tables, open-shell beta channels, TDDFT SOC. | Multi-job inputs with mixed ORCA 5 and ORCA 6 syntax; malformed energy lines. | Verified in `test_parser.py`. |
| **Failed ORCA Jobs & Partial Output** | Error termination banners (`had_error_termination`), rerun after error, compound job failure. | Ensuring partial outputs are never classified as converged minima or transition states. | Validated in `test_parser.py` and `test_thermochemistry_scientific_audit.py`. |
| **Fractional Stoichiometry & Atom Balance** | Chemical equation parsing and formula balancing. | Atom imbalance rounding of fractional coefficients (e.g. 0.1 delta rounded to 0). | Fixed and tested in `test_thermochemistry_scientific_audit.py`. |
| **Temperature & Pressure Extrapolations** | Fixed temperature Gibbs and enthalpy deltas. | Distinguishing $T_{\text{calc}}$ from $T_{\text{requested}}$ via van 't Hoff approximation ($\Delta C_p = 0$). Ideal gas pressure correction. | Added to `test_thermochemistry_scientific_audit.py`. |
| **Frequency Completeness & Stationary Points** | Counting imaginary frequencies ($N_{\text{imag}} == 0, 1$). | Enforcing $3N-6 / 3N-5$ total mode completeness; ignoring numerical noise $< 15\,\text{cm}^{-1}$. | Added in `test_thermochemistry_scientific_audit.py`. |
| **Archive Parsing & Resource Limits** | Parsing ZIP, RAR, TAR archives of calculations. | Enforcing total uncompressed size limit (2 GB) and entry count limit (10,000 files) against decompression bombs. | Covered in `test_security_hardening.py`. |
| **Security Boundaries** | Path traversal (Zip/Tar/RAR Slip), command injection vectors, credential hygiene. | Unauthenticated calls to operational sweep endpoints. Traceback leakage in production JSON. | Hardened in `test_security_hardening.py` and `app.py`. |
| **Idempotency & Checkpoint Recovery** | `SUBMIT_DEDUP` table in `app.py`, SQLite transactional locks, kernel state replay. | Double submission under network latency and corrupt checkpoint recovery. | Covered in `test_lifecycle_simulation.py` and `test_end_to_end_chain.py`. |
| **CI Integration** | Only 7 out of 24 test suites were configured in `.github/workflows/ci.yml`. | Complete test discovery and execution across Python matrix (3.10, 3.11, 3.12) on Linux and Windows. | Phase 4 CI update to run `pytest` over all test directories. |

---

## 3. Mandatory Test Suite Checklist

- [x] **Scientific Golden Tests**: Validated with exact physical constants against benchmark outputs.
- [x] **Parser Edge Cases**: All 15 test data files in `orca_engine/tests/data/` verified.
- [x] **Failed ORCA Jobs**: Error banners captured, species flagged in `ConsistencyReport`.
- [x] **Geometry Mismatch**: Tested in composite arithmetic between SP and Freq steps.
- [x] **Fractional Stoichiometry**: Floating-point deltas preserved without integer truncation.
- [x] **Temperature Handling**: $T_{\text{calc}}$ vs $T_{\text{requested}}$ separated and tested.
- [x] **Pressure Semantics**: Standard state ideal gas corrections defined and tested.
- [x] **Frequency Completeness**: $3N-6 / 3N-5$ vibrational degrees of freedom verified.
- [x] **Archive Parsing & Limits**: Tested against malformed ZIPs, traversal paths, and oversized files.
- [x] **Idempotency & Recovery**: Tested across multi-session Kaggle simulation harness.
- [x] **API Contracts**: All JSON schemas, HTTP status codes (200, 400, 401, 404, 413, 422, 429, 500, 503) verified.
