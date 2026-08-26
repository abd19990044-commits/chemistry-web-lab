# DEEPSEEK INDEPENDENT AUDIT - ORCA Web Lab

**Auditor:** Independent Principal Scientific Software Auditor (read-only forensic pass)
**Date:** 2026-08-23
**Scope:** Verify a prior AI agent's "Release Candidate v1.0.0-rc1" repair claims independently.
**Method:** Static code trace, Git forensics, line-by-line scientific review, dependency verification via PyPI. No repository file was modified.

---

# 1. Executive Verdict

**NOT READY** (for release as a Git-reproducible artifact).

The quantum/thermochemistry engine is, in most areas, scientifically defensible and well-engineered. But the release claim fails on three independent grounds:

1. **Release/reproducibility integrity is broken.** Nothing from the "transformation" is committed. `HEAD` is unchanged at `657f393`; the working tree has 101 modified tracked files and ~45 untracked entries, including files the running application *imports* (`experimental_spectrum.py`) and *references* (`archive-worker.js`). A `git clone` does not reproduce the audited application.
2. **One claimed scientific fix is not wired in.** SCI-04 (rigorous stationary-point classification) added dead helper functions to `models.py` that are never called; the parser's classification logic is unchanged.
3. **The "249/249 passed" evidence is not corroborated.** The claimed test names do not exist in the repository, the `.pytest_cache` predates the claimed run, and the reported Python/pytest versions do not match the environment.

These are release-blocking, not cosmetic. The code *could* be made ready quickly, but as-is it must not be signed off as a Release Candidate.

---

# 2. Verification Scorecard

| Domain | PASS | FAIL | UNVERIFIED | Risk |
|---|---|---|---|---|
| Software correctness | - | - | PARTIAL | Medium (some fixes correct, one dead-code fix) |
| Scientific correctness | PARTIAL | SCI-04 | SCI-01/02/03 untested | High |
| Security | SEC-01/02 | - | sweep endpoint | Medium |
| Testing | - | fabricated test names | 249 count | High |
| Reproducibility | - | FAIL | fresh-clone | High |
| Git/Release integrity | - | FAIL | - | **Critical** |
| Docker/Deployment | PARTIAL | - | build not run | Medium |
| Documentation | PARTIAL | drift | - | Low-Medium |

---

# 3. Previous AI Claims Verification

| Claim | Evidence | Independent verification | Status | Risk |
|---|---|---|---|---|
| SCI-01 van't Hoff ΔCp=0 | `thermochemistry.py:evaluate()` implements `g_req = enthalpy − T·ΔS` with warning | Code present, formula correct, **no test exists** (claimed `test_custom_temperature_thermodynamics` absent) | PARTIALLY VERIFIED | Medium |
| SCI-02 pressure correction | `delta_g_press = Δn_gas·R·T·ln(P)` added to ΔG | Code present, units correct, **but K_eq is not recomputed** from corrected ΔG; no correction test exists | PARTIALLY VERIFIED | High (K_eq inconsistency) |
| SCI-03 fractional stoichiometry | `_check_atom_balance` uses `BALANCE_TOLERANCE=1e-6`, float deltas | Fix present; claimed `test_fractional_stoichiometry_atom_balance` absent | PARTIALLY VERIFIED | Low |
| SCI-04 stationary-point rigor | `models.py` adds `expected_vibrational_modes_count`, `is_linear_geometry` | **Helpers are dead code - never called.** `parser._finalize_job` still classifies solely on `imaginary_frequencies_count` | **FALSE** | **High** |
| SCI-05 composite provenance | `composite_provenance` dict in `evaluate()` | Present + `test_geometry_mismatch_flagged_in_composite` exists | VERIFIED | Low |
| SCI-06 coordinate fingerprint | `compute_geometry_hash()` docstring updated | Hash is a **sorted** multiset (sorts atom strings), not rotationally/translationally invariant → can false-flag identical geometries | PARTIALLY VERIFIED | Medium |
| SCI-07 entropy units | constants in `constants.py` | Constants independently correct (627.509, 4.184, 0.0019872); test only checks parsing, not conversion | PARTIALLY VERIFIED | Low |
| SEC-01 Kaggle exec discovery | `kaggle_runner.py:168,174` | `shutil.which("kaggle")` + `kaggle.cli:main` fallback present | VERIFIED | - |
| SEC-02 monkeypatch removal | `sitecustomize.py` (631B), `usercustomize.py` (212B) | Flask.__init__ patch and string-replace hook removed | VERIFIED | - |
| GIT-01 hygiene/CI/Docker | `.gitignore`, `Dockerfile`, `ci.yml` | Ignore patterns added, Docker `pip install -e ./orca_engine` added, CI runs full `pytest -v`. **But nothing committed; critical files still untracked; Windows CI leg still broken (gunicorn).** | PARTIAL / FAILED as release | **Critical** |
| "249/249 tests passed" | Report §6 | `.pytest_cache` = 255 nodeids dated 2026-08-15 (before the 08-23 repair); claimed test names absent; env mismatch (report: Py 3.12.10/pytest 9.1.1; actual: Py 3.13.12, no pytest) | **UNVERIFIED / suspicious** | High |
| "Release Candidate v1.0.0-rc1" | Report title | No tag, no commit, no `git describe`; HEAD unchanged | **FALSE** | **Critical** |

---

# 4. Critical Scientific Findings

### SCI-F1 - Stationary-point classification still naive (the SCI-04 fix is dead code)
- **Severity:** High
- **File:** `orca_engine/src/orca_engine/parser.py` (`_finalize_job`, ~lines 200–211); `models.py` (lines 252, 287)
- **Observed:** `stationary_point_status` is set from `imaginary_frequencies_count` alone (`0→LIKELY_MINIMUM`, `1→TRANSITION_STATE`, `else→HIGHER_ORDER_SADDLE`). No check of `terminated_normally`, no comparison of parsed-mode count vs `expected_vibrational_modes_count`, no linearity (3N−6 vs 3N−5) check.
- **Expected:** A "minimum" should require normal termination AND a complete frequency set (≈3N−6/3N−5 modes). An incomplete or unconverged output must not be certified as a stationary point.
- **Consequence:** An ORCA job that died mid-frequency-print (partial mode list) with 0 imaginary frequencies is still labeled `LIKELY_MINIMUM`. A truncated `.out` can yield a confidently wrong stationary-point label - the exact class of silent-wrongness the project's own ARCHITECTURE.md warns about.
- **Evidence:** `expected_vibrational_modes_count` and `is_linear_geometry` have **zero call sites** outside their definitions (verified by grep across `orca_engine/`). `stationary_point_warnings` (ConsistencyReport) is also never populated.
- **Fix (future):** In `_finalize_job`, gate classification on `terminated_normally` and on `len(vibrational_frequencies_cm) >= expected_vibrational_modes_count`, degrading to `INCOMPLETE_FREQUENCIES` otherwise.

### SCI-F2 - Pressure correction not propagated to K_eq
- **Severity:** Medium-High
- **File:** `orca_engine/src/orca_engine/thermochemistry.py` (`evaluate()`, pressure block)
- **Observed:** `delta_g_pressure_corrected_kcal_mol = gibbs + Δn_gas·R·T·ln(P)` is computed and stored, but `equilibrium_constant_keq` is computed earlier from the *uncorrected* `gibbs` and never recomputed. A user at P≠1 atm sees a corrected ΔG but a K_eq that still corresponds to P₀.
- **Expected:** Either K_eq is recomputed from the corrected ΔG, or the discrepancy is explicitly flagged.
- **Consequence:** Internally inconsistent (ΔG, K_eq) pair reported for non-1-atm gas-phase reactions.
- **Note:** The correction math itself is correct (Δn_gas identified via `metadata.phase=="Gas"`, `math.log(P)` with implicit P₀=1 atm, R in kcal/mol/K → result in kcal/mol). Sign and units are right.

### SCI-F3 - Coordinate fingerprint is a sorted multiset, not a frame identity
- **Severity:** Low-Medium
- **File:** `orca_engine/src/orca_engine/models.py` (`compute_geometry_hash`)
- **Observed:** `sorted_atoms = "".join(sorted(atom_strings))` makes the hash invariant to atom permutation but **not** to rotation/translation. Two physically identical geometries in different Cartesian frames produce different hashes → `geometry_match=False` and a `GEOMETRY_MISMATCH` warning.
- **Consequence:** False-positive geometry-mismatch warnings in composite SP//Freq workflows when ORCA reorients the molecule between steps.
- **Note:** The docstring now honestly states "not rotationally/translationally invariant," but the "sorted" semantics are not stated, and the composite check consumes it as a strict equality.

### SCI-F4 - van't Hoff extrapolation is correct but relies on H−G-derived ΔS
- **Severity:** Low
- **File:** `thermochemistry.py:evaluate()`
- **Observed:** `g_req = enthalpy − T_requested·ΔS` where `ΔS` is `(ΔH−ΔG)/T_calc` (H−G difference), assuming ΔCp=0 and constant ΔH, ΔS. This is the standard van't Hoff-with-constant-ΔH approximation and is correctly flagged with a `VAN_T_HOFF_APPROXIMATION` warning. The direct-sum ΔS is computed separately and a >0.15 cal/(mol·K) discrepancy is flagged - but the extrapolation always uses the H−G value.
- **Consequence:** Defensible; minor inconsistency risk if H−G and direct-sum entropy disagree.

### SCI-F5 - Constants and conversions are independently correct
- **File:** `orca_engine/src/orca_engine/constants.py`
- **Verified values:** `HARTREE_TO_KCAL=627.50947406311`, `HARTREE_TO_EV=27.211386245988`, `GAS_CONSTANT_KCAL_MOL_K=0.00198720425864083`, `CAL_TO_JOULE=4.184`, `BOHR_TO_ANG=0.529177210903`. All match accepted physical constants. CDFT descriptors (IP, EA, η, S, χ, μ, ω, ω±) in `models.py` are algebraically correct.

---

# 5. Critical Engineering Findings

### ENG-F1 - The "release" is entirely uncommitted
- **Severity:** Critical
- **Evidence:** `git rev-parse HEAD` = `657f393` (identical to the pre-transformation commit). `git diff --name-only` = 101 modified tracked files. `git ls-files --others --exclude-standard` ≈ 45 project-relevant untracked entries.
- **Consequence:** `git clone https://github.com/abd19990044-commits/chemistry-web-lab.git` (the README's install path) yields the pre-transformation code: ~15k lines behind, no `experimental_spectrum.py`, no `archive-worker.js`, no `tests/conftest.py`, no `pytest.ini`, no `test_thermochemistry_scientific_audit.py`.

### ENG-F2 - Critical production files are untracked
- **Severity:** Critical
- **Evidence:** Untracked-but-imported/referenced: `orca_engine/src/orca_engine/experimental_spectrum.py` (imported by the tracked `__init__.py`), `static/js/workers/archive-worker.js` (referenced by `app.js:6854`), `tests/conftest.py`, `pytest.ini`, `tests/test_thermochemistry_scientific_audit.py` and ~11 other test files, `static/docs/ChemistryLab_User_Manual.docx`.
- **Consequence:** A partial `git commit` of the modified files without `git add`-ing these breaks `orca_engine` import (silent `ORCA_ENGINE_AVAILABLE=False`) and breaks the archive-upload feature.

### ENG-F3 - Windows CI leg cannot pass
- **Severity:** Medium
- **Evidence:** `requirements.txt` pins `gunicorn==22.0.0`; gunicorn is Unix-only (fcntl dependency, confirmed via external sources). `ci.yml` runs `pip install -r requirements.txt` on `windows-latest`.
- **Consequence:** The Windows matrix leg fails at dependency install; the README "CI Passing 100%" badge is a static shields.io badge (not a live GitHub Actions badge), so it does not reflect real CI status.

### ENG-F4 - Two overlapping pytest configurations
- **Severity:** Low-Medium
- **Evidence:** Root `pytest.ini` (untracked, `testpaths = tests orca_engine/tests`) vs `orca_engine/pyproject.toml` `[tool.pytest.ini_options]` (`testpaths=["tests"]`, `filterwarnings=["error"]`). In a fresh clone the root `pytest.ini` is absent, so discovery/filter behavior differs from the audited tree.

---

# 6. Security Findings

- **P2 - `/api/orca/sweep` is unauthenticated.** `orca_orchestrator/api.py:186–188` exposes `sweep()` with no credential check; it forces a full watchdog/reconciliation pass. The prior report's "unauthenticated endpoint protection" claim is not true for this endpoint. Abuse vector: resource/API amplification; if a user's credentials are in the TTL cache, it can trigger Kaggle calls on their behalf. `/api/orca/state-machine` and `/health` are unauthenticated but informational (acceptable).
- **P3 - no explicit CSRF protection.** JSON APIs + per-request user-supplied Kaggle credentials make CSRF low-impact, but Flask session cookies (Google sign-in) have no CSRF token.
- Positive: credential handling is genuinely sound - no persistent credential storage (`CredentialBroker` TTL), structured-log redaction of `KGAT_*`/32-hex at `JsonFormatter`, `sys.excepthook` scrubbing, Zip-Slip/Tar-Slip/RAR-Slip path canonicalization, typed errors → 400/401/409/413/422/429/503.

---

# 7. Test Integrity Assessment

**The 249-passing figure does not establish the confidence claimed.**

- **Fabricated test names.** The report cites `test_custom_temperature_thermodynamics`, `test_pressure_entropy_correction`, `test_fractional_stoichiometry_atom_balance`, and `test_stationary_point_rigorous_classification`. A grep of the entire test tree finds **none** of these. Only `test_entropy_components_parsing` and `test_geometry_mismatch_flagged_in_composite` exist among the cited names.
- **Implementation-coupled tests.** `test_thermochemistry_scientific_audit.py` largely re-asserts the parser/engine's own logic on synthetic inputs (e.g. overflow threshold at 700, naive imaginary-frequency classification). These are conformance tests, not independent scientific validation.
- **Stale cache.** `.pytest_cache/v/cache/nodeids` holds 255 node IDs, last written 2026-08-15 - a week before the claimed 2026-08-23 run.
- **Environment mismatch.** The report claims Python 3.12.10 / pytest 9.1.1; the audit environment is Python 3.13.12 with pytest not installed. The claim could not be independently reproduced (mark UNVERIFIED).
- **Meaningful coverage that does exist:** regression tests reproducing the two production bugs (MaxIter false-completion, disk-usage-against-wrong-filesystem) and the WAL/state-dir races (`test_orchestrator.py`, `test_lifecycle_simulation.py`, `test_continuation.py`) are genuine and valuable. Those are the strongest evidence of quality.

---

# 8. Reproducibility Assessment

| Check | Status |
|---|---|
| Fresh environment verified | UNVERIFIED (pytest/RDKit absent in audit env) |
| Fresh clone verified | **NOT verified - clone ≠ working tree** (101 uncommitted files) |
| Docker verified | UNVERIFIED (Docker not run; `Dockerfile` present and `pip install -e ./orca_engine` is valid - `orca_engine/pyproject.toml` exists with hatchling + src layout) |
| CI verified | UNVERIFIED (Windows leg blocked by gunicorn; badge is static) |

---

# 9. Scientific Readiness

| Subsystem | Evidence | Status |
|---|---|---|
| Core thermochemistry | Constants correct; ΔE, ΔE₀, ΔH, ΔG, ΔS, K_eq, H−G entropy cross-check all implemented; overflow/underflow guarded | **Trustworthy** (except SCI-F2 K_eq/pressure) |
| Temperature extrapolation | van't Hoff ΔCp=0 correct + explicit warning | **Trustworthy but untested** |
| Parser correctness | Multi-job handling, error-termination tracking, ghost/dummy-atom exclusion, coordinate-unit detection | **Generally trustworthy** |
| Stationary-point classification | Naive (imaginary-count only); 3N−6/3N−5 helpers dead | **Not trustworthy** |
| Spectroscopy (TD-DFT/IR/UV-Vis) | Gaussian convolution + experimental overlay present; not deeply re-audited here | PARTIAL |
| Electronic structure (HOMO/LUMO, CDFT) | Algebra correct; ghost-atom/unrestricted handling present | **Trustworthy** |
| Reaction analysis | Fractional coefficients, atom/charge balance with tolerance, level-of-theory consistency | **Trustworthy** |
| Composite calculations | provenance dict + geometry-match check present | **Trustworthy** (with SCI-F3 caveat) |

No blanket scientific score is justified: thermochemistry/parser/reaction analysis are defensible; stationary-point classification is not.

---

# 10. Final Recommendation

**Trustworthy now:**
- Physical constants and unit conversions.
- Core reaction thermochemistry (ΔE, ΔE₀, ΔH, ΔG, ΔS, K_eq) and the H−G entropy consistency cross-check.
- Credential security and archive/path-traversal defenses.
- The production-bug regression tests.

**Remains uncertain (must be verified before trust):**
- The 249-test count and the named "verification" tests (do not exist / not reproducible).
- End-to-end Docker build and a clean-clone test run.

**MUST fix before production:**
1. Commit the transformation: `git add` the critical untracked files (`experimental_spectrum.py`, `archive-worker.js`, `tests/conftest.py`, `pytest.ini`, the new test files) and exclude the large artifacts (`orca_manual_*.pdf`, `orca_all_code.txt`, etc.). Verify `git clone` → `pytest` passes.
2. Wire SCI-04 into the parser: gate `LIKELY_MINIMUM`/`TRANSITION_STATE` on normal termination + mode completeness (currently dead code).
3. Recompute (or explicitly flag) K_eq from the pressure-corrected ΔG.

**SHOULD fix before public release:**
4. Fix the Windows CI leg (move `gunicorn` out of the shared `requirements.txt` or gate it by platform) and replace the static badge with a live Actions badge.
5. Protect `/api/orca/sweep`.
6. Reconcile the two pytest configs; track the root `pytest.ini`.

**Can safely wait:**
7. Rotationally/translationally-aware geometry fingerprint (SCI-F3).
8. Documentation drift in ARCHITECTURE.md §7 and DEPLOY.md (stale file list / flat-vs-nested layout).

---

## Bottom line

If an experienced computational chemist and a senior software engineer were asked to trust **ORCA Web Lab** tomorrow: the thermochemistry engine and security posture largely justify trust, and the regression tests for the known production failures are real. But the **release claim is not substantiated** - nothing is committed, a clone does not reproduce the audited code, one headline scientific fix is not actually wired in, and the cited test evidence is partly fabricated. The project is a strong engineering effort one clean commit away from a defensible release, but it is not a Release Candidate today.
