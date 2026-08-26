# FINAL DEEPSEEK FORENSIC AUDIT - ORCA Web Lab

**Auditor:** Independent Principal Scientific Software Auditor (adversarial, read-only)
**Date:** 2026-08-23
**Target:** commit `c454a80` (claimed final release)
**Method:** Git forensics, call-site tracing, and **independent** hand-computed scientific verification via a temporary script placed outside the repository (in `.openclaw/tmp/`, which is gitignored). No project file was modified.

> Transparency note: the temporary verification script could not be deleted this turn because the file-delete operation required an approval that timed out. It resides in `.openclaw/tmp/` (gitignored, outside the tracked tree) and does not affect release integrity.

---

## 1. Verified Git SHA

| Item | Value |
|---|---|
| `git rev-parse HEAD` | `c454a8004772ce34afd20db85d6df55d197a19ab` (short `c454a80`) - **matches claim** |
| Branch | `main` |
| `git status --porcelain` | **empty (clean)** |
| Untracked files | **none** (`git ls-files --others --exclude-standard` empty) |
| Tracked file count | 236 |

Release commits on top of the pre-audit baseline `657f393`:
```
c454a80 docs(release): add final release readiness report and audit reconciliation
9050aef chore(release): include sample demonstration outputs and benchmark test fixtures in repository
acd91b0 fix(scientific-release): reconcile forensic audit findings, mode completeness, pressure Keq, and Windows CI
```

**Previously release-blocking files are now tracked:** `orca_engine/src/orca_engine/experimental_spectrum.py`, `static/js/workers/archive-worker.js`, `tests/conftest.py`, `pytest.ini`, `orca_engine/tests/test_thermochemistry_scientific_audit.py`, `orca_engine/pyproject.toml`, `benchmarks/*.out`. ENG-F1 and ENG-F2 are resolved.

---

## 2. Exact Test Counts

- Static count of `def test_*` functions: **248** (across `tests/` + `orca_engine/tests/`).
- Plain-harness `check()` assertions (non-pytest, in `test_continuation.py`/`test_orchestrator.py`/`test_lifecycle_simulation.py`): **433**.
- Claimed `258 / 258` : **UNVERIFIED** - pytest is not installed in the audit environment, so the exact collected/executed count could not be reproduced. The figure is *plausible* (248 functions + parametrization), but not independently confirmed.
- The previously **fabricated** test names are now superseded by real, meaningful tests (see §5 SCI-04): `test_stationary_point_truncated_frequencies`, `test_stationary_point_abnormal_termination`, `test_stationary_point_linear_molecule`, etc. These assert the *new* behavior and were independently re-verified by hand computation (see §5).

---

## 3. Clean-Clone Result

**UNVERIFIED (execution), but source-level consistency now holds.**

- The working tree is clean and equal to `HEAD`; all runtime-critical and test-critical files are tracked; ignored files are confined to caches, scratch dumps, and large manual PDFs that are not runtime-required.
- Therefore a `git clone` of `c454a80` *should* reproduce the tree. However, a fresh venv + `pip install -r requirements.txt` + `pip install -e orca_engine` + `pytest` run was **not executed** in this environment (pytest/RDKit/Flask absent; no network clone performed). Marked UNVERIFIED rather than assumed.

---

## 4. Docker Result

**UNVERIFIED.** Docker was not available; no `docker build`/`docker run` was performed.

Static observations:
- `Dockerfile` now runs `pip install --no-cache-dir -e ./orca_engine`, which is valid (`orca_engine/pyproject.toml` exists, hatchling + src-layout, `dependencies = []`).
- **`.dockerignore` is still absent.** For a *local* `docker build` (where `COPY . .` uses the on-disk directory), the gitignored-but-present `orca_manual_6_1_0.pdf` (~76 MB), `orca_all_code.txt` (~2.2 MB), and `scratch_*.txt` files will be copied into the image. See N2.

---

## 5. Previous DeepSeek Finding Status

| Finding | Status | Evidence |
|---|---|---|
| SCI-04 stationary-point helpers not integrated | **FIXED** | `parser.py:_finalize_job` now gates classification on termination + frequency completeness + imaginary count. Independently verified (7 cases, incl. truncated→INCOMPLETE_FREQUENCIES, abnormal→FAILED_CALCULATION, linear CO2→3N−5=4). Residual: N1. |
| SCI-F2 pressure ΔG without K_eq | **FIXED** | `keq_pressure_corrected` now recomputed from corrected ΔG. Independently verified: A→2B, Δn_gas=+1, P=10 atm ⇒ ΔG(P)=−3.636 kcal/mol, K(P)=462.4 (hand-computed). |
| SCI-F3 coordinate fingerprint semantics | **NOT FIXED (documented, low)** | `compute_geometry_hash()` still sorts atom strings → sorted multiset, not frame-invariant. Docstring accurate; residual false-GEOMETRY_MISMATCH risk (P3). |
| SCI-F4 `app.py` bypass of corrected engine | **FIXED** | `/api/orca/engine/thermochemistry` now passes `custom_temperature_k`/`custom_pressure_atm` into `engine.evaluate(...)`. |
| ENG-F1 release changes uncommitted | **FIXED** | Clean tree; 3 release commits on `main`. |
| ENG-F2 critical files untracked | **FIXED** | All critical runtime/test files now tracked. |
| ENG-F3 Windows CI vs gunicorn | **FIXED** | `gunicorn==22.0.0; sys_platform != 'win32'`; README badge is now a genuine GitHub Actions URL. |
| ENG-F4 competing pytest configs | **FIXED (minor residual)** | Root `pytest.ini` tracked; `orca_engine/pyproject.toml` retains its own `[tool.pytest.ini_options]` (with `filterwarnings=["error"]`), which only governs runs rooted inside `orca_engine/`. Low impact. |
| Security `/api/orca/sweep` unauthenticated | **PARTIALLY FIXED** | Endpoint now *reads* credentials but does **not** `authenticate()` them and does not scope `sweep_now()` to the owner. See N3. |

---

## 6. New Findings

### N1 - P2 (Scientific) - Stationary-point completeness uses the wrong baseline
- **File:** `orca_engine/src/orca_engine/parser.py` (`_finalize_job`), `models.py:expected_vibrational_modes_count`
- **Observed:** `expected_vibrational_modes_count` returns `3N−6`/`3N−5` (vibrational DOF only), but `vibrational_frequencies_cm` holds **all 3N** frequencies (ORCA prints translational + rotational near-zero modes too). The completeness check `parsed_modes_count < expected_modes` therefore only fires when fewer than `3N−6` modes are present.
- **Consequence:** A *moderately* truncated output (between 3N−6 and 3N modes) is still classified `LIKELY_MINIMUM`/`TRANSITION_STATE`. Independently reproduced: H₂O (N=3, 3N=9) with only 5 frequencies printed → `LIKELY_MINIMUM` instead of `INCOMPLETE_FREQUENCIES`.
- **Fix (future):** compare against `3N` (total modes), or filter near-zero translational/rotational modes and compare the nonzero count against `3N−6`/`3N−5`.

### N2 - P2 (Packaging) - No `.dockerignore`; large artifacts enter local Docker builds
- **File:** (missing) `.dockerignore`
- **Observed:** `orca_manual_6_1_0.pdf` (~76 MB, ORCA manual), `orca_all_code.txt` (~2.2 MB), `scratch_*.txt`, `manual_exact_specs.txt` remain on disk and are gitignored but **not** dockerignored.
- **Consequence:** a local `docker build` copies these into the image (bloat + potential redistribution of the proprietary ORCA manual). HF git-based Docker builds are unaffected (build context = git tree).

### N3 - P2 (Security) - `/api/orca/sweep` reads but does not authenticate credentials
- **File:** `orca_orchestrator/api.py` (`sweep()`)
- **Observed:** `creds = _credentials_from(_json())` is read and echoed as `owner`, but no `service.authenticate(...)` call; `sweep_now()` is invoked unscoped.
- **Consequence:** endpoint remains effectively unauthenticated; forces a full sweep pass (CPU/API work) and can act on any owner's jobs whose credentials are currently in the TTL broker. Less severe than a data leak, but the prior claim of "unauthenticated endpoint protection" is not met.

### N4 - P3 (Scientific/UX) - Geometry fingerprint not frame-invariant
- **File:** `orca_engine/src/orca_engine/models.py:compute_geometry_hash`
- **Observed:** hash sorts atom strings → invariant to permutation but not rotation/translation; physically identical geometries in different frames hash differently.
- **Consequence:** false `GEOMETRY_MISMATCH` in composite SP//Freq provenance. Documented as intentional; no change recommended unless frame-invariance is required.

---

## 7. Scientific Readiness (per subsystem)

| Subsystem | Status | Basis |
|---|---|---|
| Core thermochemistry (ΔE, ΔE₀, ΔH, ΔG, ΔS, K_eq) | **TRUSTWORTHY** | constants independently correct; H−G entropy cross-check; overflow/underflow guarded. |
| Temperature extrapolation (van't Hoff ΔCp=0) | **TRUSTWORTHY** | independently verified dG(T=350)=−3.0; explicit warning + mode flag. |
| Pressure correction (ΔG and K_eq) | **TRUSTWORTHY** | independently verified ΔG(10 atm)=−3.636, K(10 atm)=462.4. |
| Stationary-point classification | **PARTIAL** | termination + completeness + imaginary-count integrated and verified, but completeness baseline is 3N−6/3N−5 instead of 3N (N1). |
| ORCA parser (multi-job, error/termination, ghost/dummy atoms, unit detection) | **TRUSTWORTHY** | no false-success path found for severe truncation or abnormal termination; moderate truncation gap captured in N1. |
| Composite (SP + Freq) provenance | **TRUSTWORTHY** | provenance dict + geometry-match check; N4 caveat only. |
| Spectroscopy (TD-DFT / UV-Vis / IR) | **PARTIAL** | Gaussian convolution + experimental overlay present; not deeply re-audited this pass. |
| Electronic structure (HOMO/LUMO, CDFT, charges) | **TRUSTWORTHY** | algebra correct; ghost/unrestricted handling present. |
| Reaction analysis (fractional stoich., atom/charge balance) | **TRUSTWORTHY** | tolerance-based balance; level-of-theory consistency checks. |

---

## 8. Scorecard

| Domain | PASS | FAIL | UNVERIFIED | Risk |
|---|---|---|---|---|
| Git / Release | ✅ | | | - |
| Clean Clone (execution) | | | ✅ | Medium |
| Test Integrity | ✅ (real tests) | | ✅ (258 count) | Low |
| Scientific Correctness | ✅ | | | - |
| Thermochemistry | ✅ | | | - |
| ORCA Parser | ✅ | | | - |
| Stationary Points | ⚠️ PARTIAL | | | Medium (N1) |
| Composite Chemistry | ✅ | | | Low (N4) |
| Spectroscopy | ⚠️ PARTIAL | | | Low |
| Security | ⚠️ PARTIAL | | | Medium (N3) |
| API path | ✅ | | | - |
| Kaggle Orchestration | ✅ | | | - |
| Docker | | | ✅ | Medium (N2) |
| CI | ✅ | | | Low |
| Documentation | ⚠️ PARTIAL | | | Low |

---

## 9. Release Recommendation

**CONDITIONALLY READY** (source-level).

Rationale against a stricter label:
- All previously release-blocking defects (uncommitted work, untracked critical files, dead-code SCI-04, pressure-K_eq gap, fabricated test names, Windows CI) are **genuinely fixed** and, where scientific, independently re-verified by hand computation.
- The Git state is now clean and reproducible from source.

Conditions that must be met before a full `RELEASE CANDIDATE`/`PRODUCTION READY`:
1. **Fix N1** (completeness baseline 3N−6 → 3N) - the one remaining scientific false-success path.
2. **Authenticate `/api/orca/sweep`** (N3) or scope it to a validated owner.
3. **Add `.dockerignore`** (N2) to keep the 76 MB ORCA manual and scratch dumps out of local image builds.
4. **Reproduce the test suite from a clean clone** (currently UNVERIFIED - pytest unavailable in this environment) and confirm the `258` figure.
5. **Build and smoke-test the Docker image** if Docker-based deployment is intended.

Because Docker and the clean-clone test run could not be executed here, this is a **source-level** readiness assessment, not a deployment verification.

---

## Bottom line

The second repair pass is **substantially real, not cosmetic**: the release is committed and clean, the stationary-point and pressure-K_eq fixes are integrated *and* independently verified, and the fabrication of test names has been replaced by genuine regression tests. The remaining work is small and specific - one scientific false-success window (N1), one unauthenticated operational endpoint (N3), one missing `.dockerignore` (N2), and an unexecuted clean-clone/Docker verification. This is a strong release candidate once those four items close.
