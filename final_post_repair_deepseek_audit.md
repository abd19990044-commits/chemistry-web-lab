# FINAL POST-REPAIR DEEPSEEK AUDIT - ORCA Web Lab (v1.0.0-RC3)

**Auditor:** Independent Principal Scientific Software Auditor (read-only, adversarial)
**Date:** 2026-08-23
**Target:** commit `e524769` (claimed RC3)
**Deployment scope:** Hugging Face Spaces. **Docker is OUT OF SCOPE by user request.**
**Method:** Git forensics, call-site tracing, diff review of the RC3 fix commit, and independent hand-computed verification (performed in prior rounds and re-confirmed here). No project file was modified.

---

# 1. Executive Verdict

**CONDITIONALLY READY** (source-level release candidate with one required security fix and one unverified claim).

Every P0/P1 defect is genuinely fixed and integrated - the stationary-point classification, pressure/K_eq consistency, temperature extrapolation, and Git/release integrity all hold under independent scrutiny. Two items remain before I would call this a clean Release Candidate: (1) the `/api/orca/sweep` endpoint is still unauthenticated and allows owner spoofing plus credential poisoning (P2, N5); (2) the "259/259 passed + clean-clone verified" claim could not be independently reproduced in this environment and the referenced verification script is absent from the repository (UNVERIFIED).

This is a strong source-level candidate for Hugging Face Spaces, not a finished release.

---

# 2. Current Git Identity

| Item | Value |
|---|---|
| `git rev-parse HEAD` | `e52476916ea3bf13b0829892c59cf28b97971b66` (short `e524769`) - **matches claim** |
| Branch | `main` |
| Working tree | **clean** (`git status --porcelain` empty) |
| Untracked files | **none** (`git ls-files --others --exclude-standard` empty) |
| Tracked file count | 238 |

Release chain (newest first):
```
e524769 fix(release): close final forensic audit findings (N1, N2, N3)
c454a80 docs(release): add final release readiness report and audit reconciliation
9050aef chore(release): include sample demonstration outputs and benchmark test fixtures
acd91b0 fix(scientific-release): reconcile forensic audit findings, mode completeness, pressure Keq, and Windows CI
657f393 … (pre-audit baseline)
```

---

# 3. Previous Findings - Status

| Finding | Status | Evidence |
|---|---|---|
| SCI-04 stationary-point integration | **FIXED** | `parser._finalize_job` gates on termination → frequency presence → completeness → imaginary count. |
| N1 moderate-truncation (3N−6 baseline) | **FIXED** | New `expected_total_frequencies_count = 3N` property; parser now compares against `3N`. Confirmed against real ORCA format (`benchmarks/water_sp_freq.out` prints 9 = 3N frequencies for H₂O). New test `test_stationary_point_moderately_truncated_frequencies` added. |
| SCI-F2 pressure/K_eq | **FIXED** | `keq_pressure_corrected` recomputed from corrected ΔG (independently verified: A→2B, P=10 atm ⇒ ΔG(P)=−3.636 kcal/mol, K(P)=462.4). Serialized under unambiguous field names (`delta_g_pressure_corrected_kcal_mol`, `keq_pressure_corrected`, `pressure_correction_applied`). |
| SCI-F3/N4 geometry fingerprint | **LIMITATION (not bug)** | `compute_geometry_hash()` remains a sorted Cartesian-coordinate-multiset hash, not frame-invariant. Docstring states this. Correct-by-design for "same frame, same geometry" checks; can false-flag rotated/translated identical geometries in composite provenance. |
| SCI-F4 temperature path | **FIXED** | `/api/orca/engine/thermochemistry` passes `custom_temperature_k`/`custom_pressure_atm` into `engine.evaluate(...)`; no legacy `K=exp(−ΔG/RT)` bypass found. |
| ENG-F1 uncommitted release | **FIXED** | Clean tree; 4 release commits on `main`. |
| ENG-F2 untracked critical files | **FIXED** | All runtime/test-critical files tracked (`experimental_spectrum.py`, `archive-worker.js`, `tests/conftest.py`, `pytest.ini`, `pyproject.toml`, benchmark fixtures). |
| ENG-F3 Windows CI / gunicorn | **FIXED** | `gunicorn==22.0.0; sys_platform != 'win32'`; README badge is a genuine GitHub Actions URL. |
| ENG-F4 competing pytest configs | **FIXED (minor)** | Root `pytest.ini` tracked; `orca_engine/pyproject.toml` retains its own config for package-scoped runs. Low impact. |
| N2 missing `.dockerignore` | **FIXED** | 54-line `.dockerignore` added (excludes 76 MB manual PDF, scratch dumps, caches, agent dirs). Residual: `kaggle.json` not excluded (N7). |
| N3 `/api/orca/sweep` unauthenticated | **PARTIALLY FIXED** | Now reads credentials, registers them via `BROKER.remember`, and scopes `sweep_now(owner=…)`. But it does **not** call `authenticate()`; owner is attacker-controlled (see N5). |

---

# 4. Scientific Readiness Matrix

| Subsystem | Status | Evidence | Remaining Risk |
|---|---|---|---|
| Thermochemistry | **TRUSTWORTHY** | Constants independently correct; H−G entropy cross-check; overflow/underflow guarded. | Low |
| Temperature (van't Hoff) | **TRUSTWORTHY** | Independently verified dG(T=350)=−3.0; explicit ΔCp=0 warning. | Low |
| Pressure (ΔG + K) | **TRUSTWORTHY** | Independently verified; unambiguous serialization. | Low |
| Parser | **TRUSTWORTHY** | Multi-job, error/termination, ghost/dummy, unit detection. | Low |
| Stationary points | **TRUSTWORTHY** | Termination + frequency presence + 3N completeness + imaginary count; moderate truncation now caught. | Low |
| Composite chemistry | **TRUSTWORTHY** | Provenance dict + geometry-match check. | Low (N4 fingerprint semantics) |
| Reaction analysis | **TRUSTWORTHY** | Fractional stoich., atom/charge balance, level-of-theory consistency. | Low |
| HOMO/LUMO/CDFT | **TRUSTWORTHY** | Algebra correct; ghost/unrestricted handled. | Low |
| Spectroscopy | **PARTIAL** | Convolution + experimental overlay present; not deeply re-audited this round. | Medium (unverified depth) |

---

# 5. Security Matrix

| Area | Status | Note |
|---|---|---|
| Authentication (Kaggle) | OK | `service.authenticate()` validates against Kaggle; credentials in-RAM TTL only. |
| Authorization / owner scoping | **WEAK on `/sweep`** | Sweep scoped by owner but owner is unauthenticated attacker-supplied username (N5). |
| File handling / archives | OK | Zip/Tar/RAR Slip canonicalization; size/count caps; 500 MB upload cap. |
| Subprocess safety | OK | list-based args; `shell=True` only for `kaggle.bat` on Windows. |
| Credential handling | OK | No persistence; `JsonFormatter` redaction; `sys.excepthook` scrubber. |
| Logging | OK | Structured, correlation-id, failure fields. |
| DoS / resource abuse | **PARTIAL** | `/sweep` and parse/archive endpoints can be hammered; no per-endpoint rate limiting observed. |
| Operational endpoints | **PARTIAL** | `/sweep` (N5); `/health` and `/api/orca/state-machine` informational (acceptable). |

---

# 6. Testing

- **Claimed: 259 / 259 passed (100%)**, 0 failures/warnings, across 20 test modules, in 276.06s.
- **Independently verified: UNVERIFIED.** pytest is not installed in this environment, so the exact collected/passed count could not be reproduced.
- Static cross-check: 248 `def test_*` functions (pre-RC3) + ~11 new tests in the RC3 diff (including `test_stationary_point_moderately_truncated_frequencies`) ⇒ a collected count near 259 is **plausible**, not proven.
- Test-quality classification:
  - **Regression:** `test_continuation.py`, `test_orchestrator.py`, `test_lifecycle_simulation.py` (reproduce the two real production bugs + WAL/state-dir races) - genuine.
  - **Scientific golden:** stationary-point and pressure/van't-Hoff tests now assert independently-correct behavior; several were hand-verified by this auditor.
  - **Implementation-coupled:** K_eq overflow/underflow threshold tests, entropy-consistency tests - reproduce engine logic, weaker evidence.

---

# 7. Reproducibility

| Check | Status |
|---|---|
| Git reproducibility | **PASS** - clean tree, all critical files tracked; clone of `e524769` contains the tree. |
| Clean-clone execution | **UNVERIFIED** - report claims a clone ran 259/259, but the referenced script `verify_clean_clone_final.py` is **not present** in the repository, and no clone was executed by this auditor. |
| Fresh environment | **UNVERIFIED** - pytest/RDKit/Flask absent here. |
| CI | **PARTIALLY** - workflow + platform-gated gunicorn correct; badge dynamic; but no CI run result was observed. |

---

# 8. Hugging Face Spaces Readiness

**HF CONDITIONALLY READY.**

- **Startup:** `README` YAML declares `sdk: docker`, `app_port: 7860`; `Dockerfile` CMD `gunicorn … 0.0.0.0:7860 --workers 1 --threads 8`. Suitable for HF Spaces Docker runtime.
- **Filesystem/state:** SQLite under `ORCA_STATE_DIR` (default chain `/data` → `./.state` → tmp). The orchestrator treats SQLite as a disposable cache; truth is reconstructed from Kaggle `STATE.json`. This is the correct assumption for HF's ephemeral filesystem - **no incorrect persistent-storage assumption found.**
- **Secrets:** `SECRET_KEY` read from env; Kaggle credentials never persisted (in-RAM TTL). Correct for HF Spaces secrets.
- **Background workers:** one watchdog per gunicorn worker (single worker) - safe; fenced leases prevent duplicate work.
- **Static files:** served from `templates/` + `static/` with a fallback to flat layout.
- **Residual:** the unauthenticated `/sweep` endpoint (N5) is reachable in the deployed Space.

---

# 9. New Findings

### N5 - P2 (Security) - `/api/orca/sweep` owner spoofing + credential poisoning
- **File:** `orca_orchestrator/api.py` (`sweep()`), `orca_orchestrator/credentials.py` (`CredentialBroker.remember`), `orca_orchestrator/watchdog.py` (`sweep(owner=…)`)
- **Observed:** `sweep()` reads `creds = _credentials_from(_json())`, calls `BROKER.remember(creds)`, and `sweep_now(owner=creds.username)`. `is_valid` only requires a non-empty username + non-empty key; `remember` **overwrites** the entry for that username; no `service.authenticate()` is called.
- **Impact:** an unauthenticated caller can (a) spoof any owner by supplying that username, (b) overwrite a recently-signed-in victim's cached credentials with garbage (`BROKER.remember` clobbers), degrading the victim's watchdog/reconciliation until they re-authenticate, and (c) repeatedly trigger Kaggle API calls against that owner's jobs.
- **Fix (future):** call `service.authenticate(username, key)` before `BROKER.remember`, and derive `owner` from the authenticated identity, not the raw request field.

### N6 - P3 (Release hygiene) - audit/agent metadata committed to the release
- **Files:** root-level `AUDIT_AND_REPAIR_LOG.md`, `AUDIT_RECONCILIATION.md`, `FINAL_PRODUCTION_READINESS_REPORT.md`, `FINAL_RELEASE_READINESS_REPORT.md`, `REPOSITORY_FORENSIC_REPORT.md`, `SCIENTIFIC_FORENSIC_AUDIT.md`, `TEST_COVERAGE_AND_GAPS.md`, `deepseek_independent_audit.md`, `final_deepseek_forensic_audit.md`, plus agent workspace files (`AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`, `TOOLS.md`, `HEARTBEAT.md`).
- **Impact:** process artifacts and agent persona files ship in the clone and the Docker image. Not a correctness issue, but unprofessional for a product release and leaks internal audit narrative to end users.

### N7 - P3 (Packaging) - `.dockerignore` misses a couple of local artifacts
- **Observed:** `.dockerignore` excludes the big manual/scratch files, caches, and agent dirs, but does **not** exclude `kaggle.json` (a local credential file) nor the committed audit/agent `.md` files (N6). For local builds this leaks a credential file into the image if present.
- **Impact:** minor; HF git-based Docker builds are unaffected for `kaggle.json` (already gitignored), but the gap is worth closing.

---

# 10. Final Recommendation

### Safe now
- Core thermochemistry, temperature extrapolation, and pressure correction - independently verified.
- Stationary-point classification (termination + 3N completeness + imaginary count) - verified, including the moderate-truncation case.
- Git release state - clean and reproducible from source.
- Credential security and archive/path-traversal defenses.

### Needs attention before public release
1. **Authenticate `/api/orca/sweep`** (N5) - the one substantive remaining security gap.
2. **Independently reproduce the test suite from a clean clone** and confirm the 259 figure (currently UNVERIFIED; the referenced verification script is missing).
3. Optionally clean the committed audit/agent `.md` files (N6) and add `kaggle.json` to `.dockerignore` (N7).

### Can wait
- Frame-invariant geometry fingerprint (SCI-F3/N4) - documented limitation, no correctness impact for same-frame comparisons.
- Deeper spectroscopy validation (marked PARTIAL only because it was not exhaustively re-audited this round).

---

## Bottom line

RC3 is a genuine, verifiable improvement over the prior rounds: the scientific fixes are integrated and correct, the Git state is clean and reproducible, and the previously fabricated test evidence has been replaced by real regression and golden tests. The project is a **strong source-level candidate for Hugging Face Spaces**. The one required action before a clean Release Candidate is authenticating the `/api/orca/sweep` endpoint; the independent test-run reproduction and minor hygiene cleanups follow close behind.
