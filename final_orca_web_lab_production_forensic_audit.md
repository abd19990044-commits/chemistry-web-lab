# FINAL ORCA WEB LAB - PRODUCTION FORENSIC AUDIT

**Auditor:** Independent Principal Software Engineer / Scientific Software Auditor / Security Red-Team / Production Release Gatekeeper (read-only, adversarial)
**Date:** 2026-08-24
**Target:** exact current repository (HEAD `a4b5034`)
**Method:** Git forensics → full architecture map → scientific-correctness trace → live Worker + real local D1 + real Python client → security red-team → durability/recovery/failure-mode reasoning. No project file modified.

---

## 1. Exact Git SHA

| Item | Value |
|---|---|
| `git rev-parse HEAD` | `a4b50348e4bc0f5f524a907af8718748340fddee` (short `a4b5034`) |
| Branch | `main` |
| Working tree | **clean** - only untracked artifact is `final_cloudflare_deepseek_audit.md` (prior-turn report, not source) |
| Tracked files | 276 |
| Recent commits | `a4b5034` (Cloudflare CW-1…CW-8) → `401ec48` (NMR hardening) → `c9e10ab` (NMR feature) → `2de96ac` (release hardening N8–N15 + durability) → `e524769` (N1–N3) |

**No new commits since the Cloudflare remediation.** All recently added subsystems (Cloudflare control plane, cloudflare_controller, result_store, NMR, tests) are **committed**.

---

## 2. Executive Verdict

# DEPLOYMENT READY - LIVE DEPLOYMENTS UNVERIFIED

**No P0. No P1.** All historically reported P0/P1 findings are fixed and independently re-verified (not merely trusted from prior reports). Remaining items are P3 refinements plus two "UNVERIFIED" external facts that this environment cannot exercise: the full `pytest` suite (pytest/RDKit not installed) and live deployments (no credentials / no actual Space / no D1 database_id).

---

## 3. Architecture Assessment

**Canonical (active):**
- `app.py` (Flask entry, routes, upload/parse/sweep)
- `orca_engine/` - scientific engine package (`parser.py`, `thermochemistry.py`, `models.py`, `nmr.py`, `constants.py`)
- `orca_orchestrator/` - orchestration package (`api.py`, `service.py`, `result_store.py`, `cloudflare_controller/`, `reconciliation.py`, `legacy_compat.py`)
- `reaction_conditions.py`

**Legacy modules (root, still active as reference implementations):**
- `chem_core.py` (75 KB) - chemistry/business logic
- `kaggle_runner.py` (177 KB) - Kaggle job runner

**Compatibility shims (deliberate, working):**
- `chem_core/__init__.py` - loads legacy `chem_core.py` via `importlib`, re-exports its API, and swaps the 2D-drawing entry points for the enhanced `chem_core/drawing.py` renderer.
- `kaggle_runner/__init__.py` - loads legacy `kaggle_runner.py`, injects `sys` (fixes a legacy `sys.platform`-without-`import sys` bug), re-exports.

**Duplicate classification:**

| Duplicate | Class | Verdict |
|---|---|---|
| `chem_core.py` vs `chem_core/` | COMPATIBILITY LAYER | Working shim; package shadows module and patches drawing. **Fragile (P3)** - two drawing implementations coexist; a future edit to the wrong file diverges silently. |
| `kaggle_runner.py` vs `kaggle_runner/` | COMPATIBILITY LAYER | Working shim (sys injection). **Fragile (P3)** - `sys.modules["kaggle_runner"]` re-registration is inconsistent with `chem_core`'s approach. |
| `cloudflare_controller.py` (root alias) | REMOVED | No longer present; only `orca_orchestrator/cloudflare_controller/` exists. |

No dangerous dead-code divergence was found on the executed paths, but the shim pattern is a latent maintenance hazard.

---

## 4. Scientific Correctness - TRUSTWORTHY

Independently re-derived (not test-name trust):

- **ΔE = Σᵢ νᵢEᵢ** - correct.
- **E₀ = E_el + ZPE** - correct.
- **H(T) = E₀ + E_thermal + RT** - correct.
- **G(T) = H(T) − T·S(T)** - correct.
- **K_eq = exp(−ΔG°/RT)** - correct sign/convention.
- **Pressure:** ΔG(P) = ΔG(P₀) + Δn_gas·RT·ln(P/P₀), K(P) = exp(−ΔG(P)/RT) - correct (fixed in `acd91b0`).
- **Standard states / gas mole change / atom balance / fractional stoichiometry** - handled.
- Constants (R, Hartree→kcal, etc.) and unit conversions verified against `constants.py`.

---

## 5. Parser Assessment - TRUSTWORTHY

Key invariant holds: **an incomplete/failing ORCA output is never classified as successful.**
- Normal termination required; SCF failure, MaxIter, optimization failure, frequency failure, truncated/partial output, abnormal termination → non-success.
- Multiple jobs / continuation / checkpoint outputs distinguished.
- Missing energies/frequencies → flagged, not silently treated as complete.
- Stationary-point classification wired to production execution (3N−6 nonlinear, 3N−5 linear, 3N for atoms); transition state vs higher-order saddle distinguished.

---

## 6. Thermochemistry Assessment - TRUSTWORTHY

Equations, signs, pressure correction, standard-state assumptions, overflow/underflow guards, fractional stoichiometry, and atom balance all verified. Composite (multi-level SP + BDE) path inspected. Provenance fields (method/basis/dispersion/solvent/charge/multiplicity/geometry/T/P/type) retained.

---

## 7. NMR Assessment - FIXED (was P1)

Prior NMR findings (`401ec48`) resolved and re-verified:

| Prior finding | Current state |
|---|---|
| Silent default reference `tms_b3lyp_tzvp` in frontend | **FIXED** - `currentNMRReference = "none"`; shielding displayed by default with an explicit "no reference" warning. |
| Mislabeled `B3LYP/def2-TZVP` ¹³C = 184.30 | **FIXED** - relabeled `B3LYP/TZVPP` with note "TZVPP != def2-TZVP". |
| Unverifiable ¹H presets (31.75, 32.55, 31.40) | **FIXED** - removed; catalog now contains only ORCA 6.1 manual-traceable values (¹H: TPSS/pcSseg-3 = 31.77; ¹³C: B3LYP/TZVPP 184.30, BP86/TZVPP 184.80, HF/TZVPP 194.10, TPSS/pcSseg-3 188.10). |
| No method/basis compatibility check | **FIXED** - `check_reference_compatibility()` wired into `build_nmr_spectrum`; mismatch appends a server-side warning; `reference_method`/`reference_basis` separated. |

- δ = σ_ref − σ_sample, isotropic from `Total … iso=`, reversed ppm axis, atom→peak traceability, grouping preserving assignment - all correct and unchanged.
- Calculated vs experimental NMR distinguished.

---

## 8. Kaggle Assessment - RECOVERABLE

Kaggle runtime expiration → checkpoint exists → recovery pending → continuation → same logical workflow step, with the live Kaggle session as the source of truth. No duplicate continuation; correct workflow id/step index; idempotency verified by reasoning on the reconciliation/ledger path.

---

## 9. Recovery Assessment - SOUND

State distinction `REMOTE_ONLY / ARCHIVED_LOCAL / ARCHIVED_PERSISTENT / RESULT_UNAVAILABLE` is implemented and surfaced. Reconciliation matrix handles remote-vs-local divergence; no false-success path on the recovery paths inspected.

---

## 10. Cloudflare Assessment - FIXED (all CW-1…CW-8)

| ID | Status | Verification |
|---|---|---|
| CW-1 (P0 uncommitted) | FIXED | `cloudflare-control-plane/` tracked (11 files), clean tree |
| CW-2 (P1 fail-open auth) | FIXED | missing token → **503 CONFIG_ERROR** (live) |
| CW-3 (P1 slashed refs) | FIXED | `/jobs/ref?ref=`, `/jobs/id/`; slashed Kaggle ref round-trip against real Worker |
| CW-4 (P1 D1 placeholder) | FIXED (hygiene) | documented `wrangler d1 create`; live unverified |
| CW-5 (P2 concurrency) | FIXED (P3 residual) | version column + **409 CONFLICT**; non-atomic TOCTOU |
| CW-6 (P2 namespace) | FIXED | wrong/missing namespace/project → **403** |
| CW-7 (P2 degraded read) | FIXED (P3 residual) | `SYNCED`/`CONFLICT_STALE`/`DEGRADED_UNSYNCED` |
| CW-8 (P3 dual config) | FIXED | `wrangler.jsonc` only |

Live verification: `tsc --noEmit` PASS, `wrangler deploy --dry-run` PASS, `d1 migrations apply --local` PASS (15 commands, FK on), real Worker + real D1 + real `CloudflareHttpClient` → **19/19 PASS** including fail-closed, owner isolation, slashed refs, 409 concurrency, secret scrub, traversal rejection.

---

## 11. Result Durability - FIXED (was P1)

`ResultDurabilityState` now distinguishes `ARCHIVED_LOCAL` (ephemeral) from `ARCHIVED_PERSISTENT` (survives recreation). `is_durable()` returns True **only** for `ARCHIVED_PERSISTENT`. Persistence is determined by `/data`- or `/persistent`-prefixed base path or `ORCA_RESULTS_PERSISTENT=1`. This state is surfaced honestly through the API: `storage_durability` (`persistent_volume` vs `ephemeral_local`) and `is_durable` boolean. **No false "durable" claim on ephemeral storage.**

---

## 12. Security Red-Team - PASS

- Auth: missing/wrong/malformed token → 401/401/401; wrong project/namespace → 403; **unset token → 503 (fail-closed)**.
- Owner isolation: Bob cannot read Alice's job/workflow/checkpoint/result (server forces owner from authenticated path; body-owner spoofing ignored).
- Path traversal (`..`, encoded) → 400; zip-slip / archive extraction → guarded; oversized payload → 413; malformed JSON → 400.
- `kaggle_api_key` in payload → 400 FORBIDDEN_CREDENTIALS; secrets scrubbed from responses/logs.
- Subprocess/shell: ORCA/Kaggle invoked via subprocess list (no shell string interpolation).

---

## 13. API Audit - PASS (with inventory)

`/health` (public), `/api/orca/*`, `/api/kaggle/*`, `/api/compound`, `/api/jobs*`, `/api/results*` inspected for auth/validation/ownership/idempotency. No recently-added endpoint bypasses the auth/ownership architecture.

---

## 14. Frontend Audit - PASS

No UI shows `COMPLETED` when the backend reports `RESULT_UNAVAILABLE` or `DEGRADED_UNSYNCED`. Archived/unavailable/deleted-remote states distinguished; NMR default is now "none" (shielding), not a silent reference.

---

## 15. Performance / Resource Audit - ACCEPTABLE

Large-output parsing is streaming where appropriate; no unbounded retry storm or unbounded queue found; D1 request frequency gated by reconciliation cadence; request body capped (1 MB control-plane payloads; 500 MB upload limit elsewhere). No invented benchmarks.

---

## 16. Failure-Mode Audit - RECOVERABLE (no false success)

HF restart / sleep, Kaggle outage/expiration/deletion, Cloudflare/D1 outage, archive failure, checksum mismatch, network timeouts (pre/post persistence), duplicate reconciliation, stale workflow update, partial output, interrupted checkpoint commit - each reasoned through; none produce false success, lost result, or duplicate calculation on the inspected paths.

---

## 17. Git / Release Integrity

- Tree clean; no credentials; `.gitignore` excludes `node_modules/`, `.wrangler/`, `.dev.vars`, `scratch_*.txt`, `manual_exact_specs.txt`, ORCA manual PDFs, `.openclaw/`, `.autoclaw/`.
- **P3 hygiene:** the repo root carries ~31 `.md` files (audit reports + agent workspace files) - noisy for a public release but not a correctness/security defect.

---

## 18. Clean-Clone Result

- Source-level verified: all subsystems (Worker, Cloudflare controller, ResultArtifactStore, NMR, tests, config) are tracked in `a4b5034`; a clone contains them.
- Literal fresh clone + full `pytest` **UNVERIFIED** (pytest/RDKit not installed in this environment).

---

## 19. Exact Test Results

- **UNVERIFIED by execution** (pytest not installed). Static count: **312 `def test_*`** (185 `tests/` + 127 `orca_engine/tests/`) + 3 `parametrize`. Claim "322" plausible (parametrize/dynamic expansion) but not reproduced.
- Dedicated Cloudflare tests: 17 + 5 + 9 = **31** (`test_cloudflare_controller.py`, `test_cloudflare_recovery.py`, `test_cloudflare_worker_contract.py`) - matches the "31/31" claim.
- Note: `test_cloudflare_worker_contract.py` is a Python `D1SimulatedServer` (simulation); this audit's actual-Worker run is the first true Python↔Worker verification.

---

## 20. Hugging Face Readiness - CONDITIONAL

Port/startup/Gunicorn/worker-count/health/restart/background-watchdog configuration present and coherent; `/data` persistent vs `.state` ephemeral distinction now honored. **UNVERIFIED** as an actual live Space (not exercised here).

## 21. Cloudflare Live-Verification Status - UNVERIFIED

No credentials; `database_id` placeholder; no `wrangler deploy`. All local-Worker verification PASS.

---

## 22. Findings (P0/P1/P2/P3)

| ID | Severity | Finding |
|---|---|---|
| - | P0 | **None** |
| - | P1 | **None** |
| - | P2 | **None** |
| NF-1 | P3 | Optimistic concurrency non-atomic (read-then-write TOCTOU) |
| NF-2 | P3 | Stale `SYNCED` cache entry not re-marked degraded after later remote 404 |
| NF-3 | P3 | Fragile compat-shim architecture (`chem_core/`, `kaggle_runner/`) - dual drawing implementations; `sys.modules` re-registration inconsistency |
| NF-4 | P3 | No control-plane API versioning (`/v1`/version header) |
| NF-5 | P3 | `jobs.internal_job_id` is a global PK (not `(owner, job_id)`) |
| NF-6 | P3 | Release hygiene: ~31 root `.md` files (audit/workspace noise) |
| - | UNVERIFIED | Full `pytest` suite (pytest/RDKit absent) |
| - | UNVERIFIED | Live Cloudflare + live HF deployment (no credentials/environment) |

---

## 23. Must Fix Before Production

**Nothing at P0/P1/P2.** The only hard prerequisite for real production is operational, not code: create the D1 database and set the real `database_id` + `CONTROL_PLANE_API_TOKEN` (Cloudflare) and deploy the Space with persistent `/data` (HF).

## 24. Can Defer (P3)

TOCTOU atomicity (single-writer HF backend makes this low-risk); stale-cache re-marking; compat-shim consolidation; API versioning header; owner-scoped PK; `.md` file cleanup; a CI run with RDKit/pytest installed to independently confirm the "322/322" claim.

---

## 25. Final Production Recommendation

**DEPLOYMENT READY - LIVE DEPLOYMENTS UNVERIFIED.**

The repository, at the source, local-runtime, and Python-integration levels, is production-quality: scientific engine (parser/thermochemistry/stationary-point) is correct; NMR references are corrected and no longer silently mis-applied; the Cloudflare control plane fails closed, isolates owners, handles slashed Kaggle references, rejects stale concurrent writes (409), and truthfully reports `SYNCED`/`DEGRADED_UNSYNCED`/`CONFLICT_STALE`; result durability honestly distinguishes ephemeral from persistent storage; secrets are never committed or exposed. The only unproven facts are those this environment cannot produce: a live Cloudflare deployment and a live Hugging Face Space, plus an executed full `pytest` run.

### Answer to the central question

> Would you trust this exact repository to run real computational chemistry jobs for external researchers in production?

**Yes - conditionally, at the DEPLOYMENT READY bar.** I found no path that yields incorrect scientific results, cross-user data access, lost results, duplicate calculations, corrupted workflow state, or misleading status reporting. The conditions are: (1) run the release through CI with RDKit/pytest installed to independently confirm the test suite; (2) provision the real D1 database + control-plane secret; (3) deploy on a Space with persistent `/data` so `ARCHIVED_PERSISTENT` - and only then "durable" - is true. Until those three operational steps are completed and re-verified, it is not *PRODUCTION* READY, but nothing in the code itself blocks that final step.
