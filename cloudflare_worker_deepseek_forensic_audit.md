# CLOUDFLARE WORKER DEEPSEEK FORENSIC AUDIT - Control Plane (Workers + D1)

**Auditor:** Independent Principal Cloudflare Workers / Distributed Systems / Security Auditor (read-only, adversarial)
**Date:** 2026-08-24
**Target:** standalone `cloudflare-control-plane/` + Python `CloudflareHttpClient`
**Method:** Git forensics → full source trace (`index.ts`, `db.ts`, `security.ts`, `errors.ts`, `types.ts`, migration SQL) → **actual local Worker runtime (Wrangler + Miniflare) + real local D1** → real `CloudflareHttpClient` against the real Worker → error/failure red-team.

---

## 1. Exact Git SHA

| Item | Value |
|---|---|
| `git rev-parse HEAD` | `401ec48d8b6dd892d02a97d8ebb418dc6a009171` (short `401ec48`) |
| Branch | `main` |
| Working tree | **DIRTY** - `M orca_orchestrator/cloudflare_controller/client.py` + `?? cloudflare-control-plane/` (entire Worker project) + `?? tests/test_cloudflare_worker_contract.py` + `?? nmr_deepseek_forensic_audit.md` |

**P0 - Release integrity:** the entire `cloudflare-control-plane/` project (Worker source, D1 migration, wrangler configs, package.json) is **UNTRACKED**. It is not part of any commit. The Python client (`client.py`) is also **modified but uncommitted**. A `git clone` of HEAD yields no Worker at all. Same release-discipline failure as the earlier Cloudflare subsystem (N8).

---

## 2. Toolchain Versions

| Tool | Version |
|---|---|
| node | v22.22.0 |
| npm | 11.14.1 |
| npx | 4.92.0 |
| wrangler | **3.114.17** (package.json declares `^3.75.0`; npx resolved 3.114.17; CLI recommends v4.125.0) |
| typescript | ^5.5.4 |

---

## 3. Worker Configuration Assessment

| Check | Result |
|---|---|
| `wrangler.jsonc` vs `wrangler.toml` | **BOTH exist** with **identical** content (name `orca-cloudflare-control-plane`, main `src/index.ts`, `compatibility_date 2024-09-03`, `nodejs_compat`, D1 binding `DB`, vars). No data conflict, but **ambiguous canonical config** (v3 selects `.toml`, v4 prefers `.jsonc`) - **P2** hygiene/deployment risk. |
| D1 binding | `binding = "DB"`, `database_name = "orca-control-plane-db"` - unambiguous, matches `env.DB` usage. |
| `database_id` | `"REPLACE_WITH_YOUR_D1_DATABASE_ID"` - **placeholder**. The project **cannot be deployed as-is**; `wrangler deploy` would fail to bind D1. **P1 deployment blocker.** |
| `migrations_dir` | `migrations` - correct. |
| Secrets externalization | `.dev.vars.example` externalizes `CONTROL_PLANE_API_TOKEN` (dev token) with a "NEVER commit real tokens" note. Correct pattern. |
| `package.json` scripts | `dev`, `deploy`, `d1:migrate`, `d1:migrate:local`, `typecheck` (`tsc --noEmit`), `test` (`vitest run`). No `build` script. |

**Build/typecheck: PASS.** `npx tsc --noEmit` → exit 0. `npx wrangler deploy --dry-run` → success (22.88 KiB bundle, D1 + vars bindings listed).

---

## 4. D1 Schema Assessment

`migrations/0001_initial.sql` applied **successfully to a real local D1** (`wrangler d1 migrations apply --local` → "14 commands executed successfully").

- Tables: `users`, `jobs`, `workflows`, `workflow_steps`, `checkpoints` - coherent.
- Primary keys: `jobs.internal_job_id` (global), `workflows.workflow_id`, `workflow_steps(workflow_id, step_index)`, `checkpoints.checkpoint_id`.
- Owner-scoped indexes: `idx_jobs_owner`, `idx_jobs_owner_ref(owner, kaggle_job_ref)`, `idx_workflows_owner`, `idx_checkpoints_owner`.
- Owner isolation enforced in SQL via `WHERE owner = ?` in every query.
- `ON CONFLICT ... DO UPDATE` upserts = idempotent writes.
- **P3:** `jobs.internal_job_id` is a *global* PK, not `(owner, internal_job_id)`; fine for UUIDs, but "owner + job_id" is not the uniqueness key.
- **P3:** FK constraints (`owner → users ON DELETE CASCADE`) are declared but D1/SQLite does **not** enforce FKs without `PRAGMA foreign_keys = ON`; no such pragma is set (mitigated by `ensureUser()` pre-inserting the owner row).
- Result metadata columns (`result_state`, `storage_durability`, `result_sha256`, `result_size_bytes`, `result_manifest_id`, `result_storage_reference`) present; no binary/blob columns - correct (metadata only).

---

## 5. Security Assessment

| Test (against real local Worker) | Result |
|---|---|
| `GET /health` (public) | ✅ 200, `database_available: true`, no secrets |
| No `Authorization` header | ✅ **401** |
| Wrong token | ✅ **401** |
| Malformed bearer | ✅ 401 (source: regex `^Bearer\s+(.+)$`) |
| `X-Project-Id` mismatch | ✅ **403** |
| Secret field (`kaggle_api_key`) in payload | ✅ **400 FORBIDDEN_CREDENTIALS** (recursive `assertNoSecrets`) |
| Owner traversal (`..`, `%2e%2e`, encoded `%2F`) | ✅ **400** (`sanitizeOwner` rejects `..`, `/`, `\`, `%2e`, `%2f`) |
| Oversized payload (>1 MB) | ✅ **413 PAYLOAD_TOO_LARGE** |
| Malformed JSON | ✅ **400 INVALID_JSON** |

**P1 - Fail-open authentication:** `validateAuth` returns `null` (i.e., **allows the request**) when `CONTROL_PLANE_API_TOKEN` is not configured. A production Worker deployed without setting the token secret is **completely unauthenticated**. Must fail closed.

**P2 - Namespace not validated:** the client sends `X-Namespace`, and `CONTROL_PLANE_NAMESPACE` is declared in `Env`, but `validateAuth` never checks it. `X-Namespace: evil-ns` was accepted (HTTP 200). Namespace isolation is not enforced.

**P3 - Project-ID check is opt-in:** `X-Project-Id` is only rejected when present-and-mismatched; omitting the header bypasses the check (mitigated in practice because the Python client always sends it).

---

## 6. Owner Isolation Assessment

**PASS.** `sanitizeOwner` (lowercase, charset allow-list, traversal rejection) + server-side `WHERE owner = ?` scoping. Verified: `alice` cannot fetch `bob`'s job → 404; `bob` cannot fetch `alice`'s job → 404. `kaggle_username` is forced from the path (body value ignored), so body-based owner spoofing is blocked.

---

## 7. CRUD Contract Assessment

| Operation | Result |
|---|---|
| PUT / GET / LIST / DELETE job | ✅ PASS (round-trip, timestamps, result fields) |
| PUT / GET / LIST workflow (multi-step) | ✅ PASS (step index/name/prerequisites preserved) |
| POST checkpoint | ✅ **201** (metadata only; no binaries) |
| Duplicate PUT (same `internal_job_id`) | ✅ idempotent upsert (no duplicate record) |
| Missing job GET | ✅ 404 |

**P1 - Slashed Kaggle slug unreachable (contract bug):** Kaggle notebook refs are slugs containing `/` (e.g. `username/notebook-42`). The route regex `^/api/users/([^/]+)/jobs/([^/]+)$` **cannot match a job ref containing a slash**, so `getJobByIdOrRef`/`deleteJobById`'s "by `kaggle_job_ref`" lookup is unreachable over HTTP. Verified: `GET /api/users/alice/jobs/alice/real-notebook-42` → **404 "Route not found"** (and the `%2F`-encoded form → 404). The Python controller calls `get_job(manifest.job_id, owner)` where `job_id` is a slashed slug, so this path **always 404s against the real Worker** and is silently masked by the client's local fallback cache.

---

## 8. Workflow / Checkpoint Assessment

Workflow steps round-trip with `step_index`, `step_name`, `prerequisites`, `required_artifacts`, `result_data` preserved. Checkpoints store `checkpoint_id`, `job_id`, `epoch`, `bundle_digest`, `file_manifest` (hashes/references only - no binary bundles). Owner-scoped. **PASS.**

---

## 9. Result Metadata Assessment

Verified end-to-end (Python model → HTTP → Worker → D1 → HTTP → Python model): `result_state=ARCHIVED_PERSISTENT`, `storage_durability=persistent_external`, `result_sha256`, `result_size_bytes`, `result_manifest_id` all survive intact. No scientific binaries stored in D1. **PASS.**

---

## 10. Python ↔ Worker Integration Assessment

**Real `CloudflareHttpClient` ↔ real Worker (Wrangler+Miniflare): 14/14 PASS.**

- health, put/get/list/delete job, get-by-ref, workflow (with steps + prerequisites), checkpoint, owner isolation, secret-field rejection, 404→None handling - all correct.
- `put_job` returns `cf_sync_status = "SYNCED"`; the modified client only flips to `DEGRADED_UNSYNCED` when not already `SYNCED` (honesty fix present in the uncommitted `client.py` diff).

**Caveat:** the "get by kaggle ref" success in the client test is actually served from the client's **local fallback cache**, not the Worker (the Worker 404s on slashed refs - see §7). So the contract is only *fully* correct for `internal_job_id`-keyed lookups.

---

## 11. Local Worker Test Results

**PASS.** `wrangler dev --local --port 8787` booted the actual `src/index.ts` Worker via Miniflare, with a real local D1 (migration applied). The full battery in §5–§10 ran against this real runtime - not a Python mock.

Note: the repo's own `tests/test_cloudflare_worker_contract.py` is a **Python `D1SimulatedServer`** (hand-written `http.server` re-implementing the Worker), so the agent's "Worker contract tests passed" was **Python-vs-Python-simulation**, **not** Python-vs-actual-Worker. This audit is the first actual-Worker verification.

---

## 12. Live Cloudflare Test Results

**UNVERIFIED.** No real Cloudflare credentials were used and no deployment was attempted. `database_id` is a placeholder, so `wrangler deploy` + remote D1 migration cannot succeed as-is. Per instructions, no secrets were requested or exposed.

---

## 13. Error Contract Compatibility

| Status | Worker emits | Python client handles |
|---|---|---|
| 401 | ✅ | falls back to local cache |
| 403 | ✅ | falls back to local cache |
| 404 | ✅ | `get_job`/`get_workflow` → `None` (fallback) |
| 400 (secrets / validation) | ✅ | `put_*` raises `CloudflareSecurityViolation` (client-side pre-check) |
| 413 | ✅ | treated as request failure → fallback |
| 500 | ✅ (unhandled) | treated as request failure → fallback |

The Python client's universal "fallback to in-memory cache on ANY error" behavior means **a real Worker 404/401/403 is indistinguishable from success-with-cache at the caller level** (this is the known durability-honesty gap, partially addressed by `cf_sync_status`).

---

## 14. Failure Handling

- `/health` returns **503** with `database_available: false` when D1 is unreachable (verified by source; `SELECT 1` in try/catch).
- Route handlers return **500 INTERNAL_ERROR** on unhandled exceptions (no fake 200).
- Python client enters degraded/in-memory mode on transport errors and marks `cf_sync_status = "DEGRADED_UNSYNCED"` (uncommitted fix).
- **P2:** there is no distinction surfaced to the caller between "persisted to Cloudflare" and "retained only in local RAM" on the read path - a Worker outage silently degrades reads to the cache.

---

## 15. Configuration Risks

1. **P1** - `database_id` placeholder → not deployable.
2. **P2** - dual `wrangler.jsonc` + `wrangler.toml` (identical today; will drift / change resolution in Wrangler v4).
3. **P1** - auth fails open when token unset.
4. **P0** - entire Worker project uncommitted.

---

## 16. Remaining Findings

| ID | Severity | Finding |
|---|---|---|
| CW-1 | **P0** | `cloudflare-control-plane/` + `client.py` changes are uncommitted; a clone has no Worker. |
| CW-2 | **P1** | Auth fails open when `CONTROL_PLANE_API_TOKEN` unset (no fail-closed). |
| CW-3 | **P1** | Slashed Kaggle slug refs unreachable via `([^/]+)` route regex → `getJobByIdOrRef` by-ref lookup broken; client masks with local cache. |
| CW-4 | **P1** | `database_id` placeholder → not deployable. |
| CW-5 | **P2** | No optimistic concurrency / version check: stale PUT silently overwrites (no 409). |
| CW-6 | **P2** | `X-Namespace` not validated; namespace isolation not enforced. |
| CW-7 | **P2** | Read path cannot distinguish Cloudflare-persisted vs local-RAM-cache. |
| CW-8 | **P3** | Dual wrangler configs; `internal_job_id` global PK; FKs not enforced (no `PRAGMA foreign_keys`). |

---

## FINAL SCORECARD

| Area | PASS | FAIL | UNVERIFIED | Risk |
|---|---|---|---|---|
| Worker build | ✅ | | | Low |
| Wrangler config | | ✅ (placeholder ID, dual) | | High |
| D1 schema | ✅ | | | Low |
| Authentication | | ✅ (fail-open) | | High |
| Owner isolation | ✅ | | | Low |
| Job CRUD | | ✅ (slashed ref) | | High |
| Workflow CRUD | ✅ | | | Low |
| Checkpoint API | ✅ | | | Low |
| Secret isolation | ✅ | | | Low |
| Input validation | ✅ | | | Low |
| Versioning | | ✅ (none) | | Medium |
| Idempotency | ✅ | | | Low |
| Result metadata | ✅ | | | Low |
| Python client compatibility | | ✅ (slashed ref) | | High |
| Local Worker runtime | ✅ | | | Low |
| Local D1 runtime | ✅ | | | Low |
| Live Cloudflare deployment | | | ✅ | High |

---

## FINAL RELEASE CLASSIFICATION

# NOT READY

**Distinction (as required):**
- **Source validation: PASS** (typechecks, bundles, migration applies).
- **Local Worker validation: PASS** (real runtime; auth, isolation, scrubbing, traversal, size-limit all correct).
- **Local D1 validation: PASS.**
- **Python ↔ Worker integration: PARTIAL** (happy-path CRUD + isolation PASS; slashed-slug lookup FAIL).
- **Live Cloudflare validation: UNVERIFIED** (placeholder `database_id`, no credentials).

The Worker is **source-correct and locally-verified for the happy path**, but it is not deployable or production-safe until: (1) the project is committed; (2) auth fails closed; (3) the slashed-slug route is fixed; (4) a real D1 `database_id` is supplied; (5) optimistic concurrency (or an accepted last-write-wins policy with explicit documentation) is decided. Live Cloudflare verification remains outstanding.

---

## Bottom line

The control plane is genuinely well-built and - unlike the previous round - I verified it against the **actual Worker runtime and real local D1**, not a Python mock. Authentication, owner isolation, secret scrubbing, traversal protection, and payload limits all work correctly. But it is blocked by a release-integrity failure (uncommitted), a fail-open auth default, a real contract bug (slashed Kaggle slugs unreachable), and a placeholder database ID. Fix CW-1–CW-4, then it is a credible deployment candidate pending live Cloudflare verification.
