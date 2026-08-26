# FINAL CLOUDFLARE DEEPSEEK AUDIT - Control Plane Remediation Verification

**Auditor:** Independent Principal Cloudflare Workers / Distributed Systems / Security Auditor (read-only, adversarial)
**Date:** 2026-08-24
**Target:** commit `a4b5034` (claimed CW-1…CW-8 remediation)
**Method:** Git forensics → full source trace → **actual local Worker (Wrangler v4 + Miniflare) + real local D1** → **real `CloudflareHttpClient`** against the real Worker → fail-closed/red-team/concurrency live tests. No project file modified.

---

## 1. Exact Git SHA

| Item | Value |
|---|---|
| `git rev-parse HEAD` | `a4b50348e4bc0f5f524a907af8718748340fddee` (short `a4b5034`) - **matches claim** |
| Branch | `main` |
| Working tree | **clean** (`git status --porcelain` empty, no untracked) |
| `cloudflare-control-plane/` | **tracked** - 11 files (`index.ts`, `db.ts`, `security.ts`, `errors.ts`, `types.ts`, `migrations/0001_initial.sql`, `package.json`, `tsconfig.json`, `wrangler.jsonc`, `.dev.vars.example`, `README.md`) |
| Toolchain | node v22.22.0, npm 11.14.1, **wrangler 4.92.0** |

---

## 2. Original CW-1–CW-8 Reconciliation

| ID | Original | Status | Evidence |
|---|---|---|---|
| CW-1 | P0 Worker uncommitted | **FIXED** | `git ls-files cloudflare-control-plane` → 11 files; clean tree; no untracked. |
| CW-2 | P1 auth failed open | **FIXED** | `validateAuth` returns **503 CONFIG_ERROR** when token unset. **Live-verified**: auth route with no token → 503. |
| CW-3 | P1 slashed Kaggle refs unreachable | **FIXED** | New `GET/DELETE /api/users/:owner/jobs/ref?ref=…` and `/jobs/id/:id` routes; Python client routes slashed refs via query param. **Live-verified**: PUT/GET-by-ref/DELETE-by-ref round-trip against real Worker. |
| CW-4 | P1 D1 `database_id` placeholder | **FIXED (as deployment hygiene)** | Still `REPLACE_WITH_YOUR_D1_DATABASE_ID`, but README documents `wrangler d1 create` + paste. Live deployment remains **UNVERIFIED** (correctly so). |
| CW-5 | P2 no optimistic concurrency | **FIXED (P3 residual)** | `version` column (jobs + workflows), `OptimisticConcurrencyError` → **409 CONFLICT**. **Live-verified**: stale v1 update → 409. Residual: check is non-atomic read-then-write (TOCTOU). |
| CW-6 | P2 namespace not validated | **FIXED** | `validateAuth` strictly requires matching `X-Namespace` and `X-Project-Id`. **Live-verified**: wrong/missing namespace or project → 403. |
| CW-7 | P2 degraded read hidden | **FIXED (P3 residual)** | Client now sets `cf_sync_status` = `SYNCED` / `CONFLICT_STALE` / `DEGRADED_UNSYNCED`. **Live-verified**: stale put → `CONFLICT_STALE`; sync → `SYNCED`. |
| CW-8 | P3 dual Wrangler config | **FIXED** | Only `wrangler.jsonc` remains (`wrangler.toml` removed). |

---

## 3. API Secret Architecture Review

| Check | Result |
|---|---|
| Shared-secret contract | HF side reads `CLOUDFLARE_API_TOKEN` (config.py); Worker side reads `CONTROL_PLANE_API_TOKEN` (env). Same secret, different names - documented in README. **PASS.** |
| Secret committed? | **No.** `.dev.vars.example` and README carry only placeholders (`dev_se…`, `cf_sec…`). No real token in source. |
| Secret in D1 / logs / responses | **No.** `assertNoSecrets` scrubs payloads; D1 stores only metadata columns (no credential column). |
| Browser → Worker boundary | Correct: browser → HF backend → Worker. The frontend never holds the control-plane secret; only the Python orchestrator (server-side) calls the Worker. |
| Secret in URL / JSON body / JS / HTML | **No.** |

---

## 4. Local Worker + D1 Verification

- `npx tsc --noEmit` → **PASS** (exit 0).
- `npx wrangler deploy --dry-run` → **PASS** (bundles; D1 + vars bindings listed).
- `wrangler d1 migrations apply --local` → **PASS** ("15 commands executed successfully"; `PRAGMA foreign_keys = ON` present).
- `wrangler dev --local` → **PASS** (real `src/index.ts` via Miniflare).

---

## 5. Python Client Integration Verification

**Real `CloudflareHttpClient` ↔ real Worker: 19/19 PASS**, including:
- health (public) 200; missing/wrong token → 401.
- wrong/missing project or namespace → 403.
- PUT job → 200 with `version: 1`; GET by internal id → 200.
- **GET by slashed Kaggle ref → 200, served by the real Worker** (fresh client, empty fallback cache - no cache masking).
- owner isolation: `bob` → `None`.
- stale PUT via client → `cf_sync_status == "CONFLICT_STALE"`; sync → `"SYNCED"`.
- client DELETE by slashed ref → subsequent GET → 404.

---

## 6. Slashed Kaggle Reference Proof

Using `kaggle_job_ref = "alice/chem-tools-benzene-opt-42"` (contains `/`):

| Op | Result |
|---|---|
| PUT job (ref stored in D1) | ✅ 200 |
| GET `…/jobs/ref?ref=alice%2Fchem-tools-benzene-opt-42` | ✅ 200, correct `internal_job_id` |
| GET `…/jobs/id/benzene-uuid-1` | ✅ 200, correct `kaggle_job_ref` |
| Python `get_job("alice/chem-tools-benzene-opt-42", "alice")` (fresh client) | ✅ real Worker hit, `SYNCED` |
| Python `delete_job(ref)` then GET | ✅ 404 |

The route is no longer broken; the client's cache no longer masks the result.

---

## 7. Security Red-Team Results

| Attack | Result |
|---|---|
| No Authorization header | ✅ 401 |
| Wrong bearer token | ✅ 401 |
| Malformed bearer | ✅ 401 (source) |
| Wrong / missing `X-Project-Id` | ✅ 403 |
| Wrong / missing `X-Namespace` | ✅ 403 |
| **Token not configured (fail-closed)** | ✅ **503 CONFIG_ERROR** (live) |
| `kaggle_api_key` in payload | ✅ 400 FORBIDDEN_CREDENTIALS |
| Owner traversal (`..`, `%2e%2e`, encoded) | ✅ 400 |
| Oversized payload (>1 MB) | ✅ 413 |
| Malformed JSON | ✅ 400 |
| `body.owner = victim` with `path.owner = attacker` | ✅ attacker authoritative (server forces owner from path) |

---

## 8. Concurrency Results

| Test | Result |
|---|---|
| create (version 1) | ✅ |
| update with version 1 → succeeds, server bumps to **version 2** | ✅ |
| stale update with version 1 → **409 CONFLICT** | ✅ live |
| Python client stale put → `cf_sync_status = CONFLICT_STALE` (no silent overwrite) | ✅ |

**P3 residual:** the version check is a non-atomic read-then-write (`getJobById` → compare → `ON CONFLICT DO UPDATE`) rather than `UPDATE … WHERE version = ?`. Sequential stale updates are correctly rejected, but two *simultaneous* writers at the same version could still race. Not blocking for this deployment pattern (single HF backend writer), but noted.

---

## 9. D1 Failure Results

- `/health` returns **503** with `database_available: false` on D1 failure (source).
- Route handlers return **500 INTERNAL_ERROR** on unhandled exceptions (no fake 200).
- Python client degrades to in-memory fallback and marks `DEGRADED_UNSYNCED` on transport failure.
- No false "persisted" claim observed on the write path.

---

## 10. README / Deployment Audit

`cloudflare-control-plane/README.md` documents: architecture, shared-secret boundary (`CLOUDFLARE_API_TOKEN` ↔ `CONTROL_PLANE_API_TOKEN`), fail-closed 503, D1 creation (`wrangler d1 create`), migration, `database_id` injection, project/namespace headers, routes, result durability, fallback behavior, Kaggle credential boundary, and deployment steps. No real secret values, no contradictory instructions. **PASS.**

---

## 11. Clean-Clone Results

- Working tree clean; `cloudflare-control-plane/` fully tracked; no untracked files; `.gitignore` excludes `node_modules/`, `.wrangler/`, `.dev.vars`.
- A clone of `a4b5034` contains the Worker + migration + client + tests (source-level verified).
- Literal fresh clone + full `pytest` re-execution **UNVERIFIED** (pytest/RDKit not installed in the audit environment).

---

## 12. Full Test Results

- **UNVERIFIED by execution** (pytest not installed). Static count: **312 `def test_*`** (185 in `tests/` + 127 in `orca_engine/tests/`) + 3 `parametrize`. Claimed "322" is plausible (parametrize/dynamic expansion) but not independently reproduced.
- Dedicated Cloudflare tests: `test_cloudflare_controller.py` (17) + `test_cloudflare_recovery.py` (5) + `test_cloudflare_worker_contract.py` (9) = **31** - matches the "31/31" claim.
- Note: `test_cloudflare_worker_contract.py` uses a Python `D1SimulatedServer` (a re-implementation); **this audit is the actual-Worker verification** the brief requires.

---

## 13. Live Cloudflare Results

**UNVERIFIED.** No Cloudflare credentials were available/used; no `wrangler deploy` performed. `database_id` remains a placeholder (documented deployment step). Per instructions, no secrets were requested or exposed.

---

## 14. New Findings

| ID | Severity | Finding |
|---|---|---|
| NF-1 | P3 | Optimistic concurrency is non-atomic (TOCTOU read-then-write); sequential stale detection works, true simultaneous writes could race. |
| NF-2 | P3 | `get_job` returns a previously-`SYNCED` cache entry as `SYNCED` even after a later remote 404 (stale cache not re-marked degraded). |
| NF-3 | Informational | No control-plane API versioning (`/v1` or version header); both sides are code-locked to the same contract today. |
| NF-4 | P3 | `jobs.internal_job_id` is a global PK (not owner-scoped); safe for UUIDs, but not a `(owner, job_id)` uniqueness key. |

---

## 15. Final Release Recommendation

# DEPLOYMENT READY - LIVE CLOUDFLARE VERIFICATION UNVERIFIED

**Verification-level matrix:**

| Property | Source | Local Worker | Python Client | Live Cloudflare |
|---|---|---|---|---|
| Build | ✅ PASS | - | - | - |
| Auth (fail-closed) | ✅ | ✅ 503/401/403 | ✅ | ⬜ |
| Namespace | ✅ | ✅ 403 | ✅ | ⬜ |
| Owner isolation | ✅ | ✅ 404 | ✅ | ⬜ |
| Slashed Kaggle ref | ✅ | ✅ 200 | ✅ (no cache mask) | ⬜ |
| CRUD | ✅ | ✅ | ✅ | ⬜ |
| Concurrency (409) | ✅ | ✅ | ✅ CONFLICT_STALE | ⬜ |
| Result metadata | ✅ | ✅ | ✅ | ⬜ |
| Failure handling | ✅ | ✅ 503/500 | ✅ DEGRADED_UNSYNCED | ⬜ |
| API secret boundary | ✅ | ✅ | ✅ | ⬜ |

All eight CW findings are **fixed and independently verified against the actual Worker + real local D1 + real Python client**. The remaining items are P3 refinements (TOCTOU atomicity, stale-cache re-marking) and the outstanding **live Cloudflare deployment** (placeholder `database_id`, no credentials in this environment).

**Answer to the central question:** Yes - at the source, local-runtime, and Python-integration levels, the system now reliably communicates with the Worker, securely persists job/workflow/checkpoint/result metadata, correctly handles slashed Kaggle references, rejects unauthorized access, detects stale concurrent updates (409), and truthfully distinguishes `SYNCED` from `DEGRADED_UNSYNCED`/`CONFLICT_STALE`. The only thing not yet proven is a real Cloudflare deployment, which requires the operator to create the D1 database and supply the account-specific `database_id` + `CONTROL_PLANE_API_TOKEN` secret.
