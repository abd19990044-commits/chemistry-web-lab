# FINAL COMPREHENSIVE DEEPSEEK AUDIT - ORCA Web Lab

**Auditor:** Independent Principal Scientific Software Auditor (read-only, adversarial)
**Date:** 2026-08-23
**Deployment scope:** Hugging Face Spaces (Docker runtime OUT OF SCOPE per instructions)
**Method:** Git forensics → call-site tracing → hand verification of the Cloudflare/result-durability/recovery subsystem. No project file was modified.

---

## 1. Exact Git SHA

| Item | Value |
|---|---|
| `git rev-parse HEAD` | `e52476916ea3bf13b0829892c59cf28b97971b66` |
| Branch | `main` |
| Working tree | **DIRTY** - 6 modified tracked files + 10 untracked files (16 `git status --porcelain` lines) |
| Tracked files | 238 |

**Critical:** HEAD has NOT advanced past RC3 (`e524769`). The new "Cloudflare persistent control plane + scientific result durability + restart recovery" subsystem exists **only as uncommitted working-tree changes**. It is **not part of any release commit**.

Working-tree divergence:
- **Modified (tracked):** `orca_orchestrator/__init__.py`, `api.py`, `kaggle_api.py`, `legacy_compat.py`, `models.py`, `service.py`
- **Untracked:** `orca_orchestrator/cloudflare_controller/` (6 files), `orca_orchestrator/result_store.py`, root alias `cloudflare_controller.py`, `tests/test_cloudflare_controller.py`, `tests/test_cloudflare_recovery.py`, `tests/test_scientific_result_durability.py`, and 3 report files.

`git ls-files` confirms NONE of the new modules are tracked. A `git clone` of `e524769` contains the **old** orchestrator (no Cloudflare, no result_store). Worse: the modified `orca_orchestrator/__init__.py` now does `from .result_store import …` - if those modified files were committed without the untracked modules, the package would fail on import. Release discipline has regressed.

---

## 2. Executive Verdict

# **NOT READY**

The **scientific engine** remains robust (verified in prior rounds and re-confirmed here), and the new **result-integrity + reconciliation logic is genuinely well-designed**. However, the current exact state cannot be endorsed as a release because:

1. **P0** - The entire new subsystem is **uncommitted**; the release commit does not contain it, and the working tree would not survive a clean commit of the modified files without also adding the untracked modules.
2. **P1** - **Cross-user result access**: archived scientific results are retrievable by an unauthenticated caller spoofing another user's Kaggle username + job slug.
3. **P1** - **Metadata durability is dishonestly represented**: Cloudflare writes silently degrade to in-memory RAM while the code logs "Registered in Cloudflare" and `record_checkpoint` returns `True` unconditionally.
4. **P2** - `ARCHIVED` is treated as durable even when the underlying store is ephemeral (no `ARCHIVED_LOCAL` vs `ARCHIVED_PERSISTENT` distinction).
5. **P2** - The `/api/orca/sweep` endpoint (prior N5) is still unauthenticated and owner-spoofable.
6. **P2** - The new durability states are not surfaced anywhere in the API layer.

---

## 3. Historical Findings Reconciliation

| Finding | Status |
|---|---|
| SCI-04 stationary-point integration | **FIXED** (verified) |
| N1 moderate-truncation baseline (3N) | **FIXED** (verified, new test present) |
| SCI-F2 pressure/K_eq consistency | **FIXED** (hand-verified) |
| SCI-F3/N4 geometry fingerprint frame-invariance | **LIMITATION (unchanged)** |
| SCI-F4 temperature path | **FIXED** (verified) |
| ENG-F1 uncommitted release | **REGRESSED** - new subsystem is uncommitted again |
| ENG-F2 untracked critical files | **REGRESSED** - new modules untracked again |
| ENG-F3 Windows CI / gunicorn | **FIXED** (unchanged) |
| ENG-F4 competing pytest configs | **FIXED (minor)** |
| N2 missing `.dockerignore` | **FIXED** (unchanged) |
| N3 `/api/orca/sweep` unauthenticated | **NOT FIXED** - still unauthenticated; owner spoofing persists |
| N5 (prior) sweep owner spoofing | **NOT FIXED** - `sweep()` still does `BROKER.remember(creds)` with no `authenticate()`, owner from raw request |

---

## 4. Scientific Readiness Matrix

| Subsystem | Status |
|---|---|
| Thermochemistry (ΔE, ZPE, ΔH, ΔS, ΔG, K_eq) | **TRUSTWORTHY** |
| Temperature (van't Hoff, constant-ΔCp) | **TRUSTWORTHY** |
| Pressure (ΔG(P), K(P)) | **TRUSTWORTHY** |
| Parser (SCF failure, MaxIter, truncation, multi-job, ghost atoms) | **TRUSTWORTHY** |
| Stationary points (termination + 3N completeness + imaginary count) | **TRUSTWORTHY** |
| Composite chemistry / reaction analysis | **TRUSTWORTHY** |
| HOMO/LUMO/CDFT/BDE | **TRUSTWORTHY** (algebra correct) |
| Spectroscopy (TD-DFT, UV-Vis, IR, convolution) | **PARTIAL** (not deeply re-audited) |

Central question - *"Can a failed/incomplete ORCA calculation ever appear scientifically successful?"* - **No**, for the paths audited: termination gating + 3N frequency completeness + imaginary-mode classification prevent silent false-success.

---

## 5. Cloudflare Integration Assessment

- **Backend nature:** **REAL INTEGRATION (HTTP)** - `CloudflareHttpClient` uses `urllib.request` against a Worker REST endpoint (`api/users/{owner}/jobs/…`, `/workflows/…`, `/checkpoints`, `/health`), with bearer token, project-id, and namespace headers. Not a mock. `get_cloudflare_client()` returns `CloudflareHttpClient` only when `CLOUDFLARE_CONTROLLER_URL` is set and enabled; otherwise it returns `InMemoryCloudflareBackend`.
- **Secret hygiene: GOOD** - `_assert_no_secrets()` blocks secrets in outbound payloads; `to_dict`/`from_dict` scrub `kaggle_key`/`api_token`/`password`. `CloudflareJobRecord`/`WorkflowRecord`/`CheckpointRecord` carry no credentials.
- **Identity:** owner = `_clean_owner(creds.username)` (lowercased). API keys never reach Cloudflare.
- **Data model: GOOD** - `schema_version=1`, `internal_job_id` is immutable once assigned (regenerated via `new_uuid()` only when absent), workflow/step indexes + prerequisites + required-artifacts fields present.
- **Deployment reality:** with default env (no `CLOUDFLARE_CONTROLLER_URL`), the client is `InMemoryCloudflareBackend` - so on a stock HF Space there is **no persistent metadata plane at all**.

---

## 6. Result Durability Assessment

`result_store.py` (`ResultArtifactStore`) - **the physical-integrity logic is excellent**:

- Staging in `base_dir`, then atomic promotion via `shutil.move(staging_dir → target_dir)`.
- SHA-256 over `results.zip` **and** each extracted sub-artifact (`.out`, `.xyz`, `.hess`, `.property.txt`).
- `retrieve()` re-verifies the bundle checksum on every read and returns `(None, None)` on mismatch.
- `verify_checksum()` re-verifies every artifact in the manifest.
- `_job_dir()` sanitizes `job_id` via `os.path.basename()` (path-traversal safe).
- Manifest scrubs secrets on serialize/deserialize.

**Defect (P2):** `ResultDurabilityState.ARCHIVED.is_durable` returns `True` unconditionally. The backing store is `ORCA_RESULTS_DIR` → else `state_dir/results`, where `state_dir` resolves to `ORCA_STATE_DIR` → `/data` → `./.state` → tmp. On HF **without** a mounted persistent volume, results land in `./.state/results` (ephemeral), yet are labeled `ARCHIVED`/durable. There is **no `ARCHIVED_LOCAL` vs `ARCHIVED_PERSISTENT`** distinction, so the system can represent an ephemeral artifact as "durable forever."

---

## 7. Recovery Assessment

`reconciliation.py` (`decide_reconciliation`) - **the decision matrix is sound**:

- Case A (running) → RECONNECT.
- Case B (completed) → COMPLETE + retrieve results.
- Case C (failed) + verified checkpoint → RECOVERY_PENDING (respecting `max_epochs=5`).
- Case D (stopped/expired) + checkpoint → RECOVERY_PENDING.
- Case E (not found, was running) → **REMOTE_DELETED** - does **not** silently recreate.
- Case F (completed, notebook deleted) → preserve local completed history.

**Gap (P1):** recovery is driven by `self.client.list_user_jobs(owner)`. When Cloudflare is the in-memory backend (default), that list is **empty on startup**, so `reconcile_user_session` is effectively a **no-op**. On a stock HF Space (no Cloudflare), recovery degrades entirely to the pre-existing Kaggle-based Reconciler/watchdog - which is fine in principle, but the new "Cloudflare recovery" path contributes nothing unless Cloudflare is actually configured, and nothing in the code makes that dependency explicit.

**Gap (P1):** the Cloudflare data path never signals durability. `put_job`/`put_workflow` return the record unchanged after a silent in-memory fallback; `record_checkpoint` returns `True` even when the Cloudflare POST failed; `register_job` logs "Registered job in Cloudflare control plane" against the in-memory fallback. The only honest signal is `health()` → `{"type": "degraded_fallback"}`, which is not propagated into job/result state.

---

## 8. Multi-step Workflow Assessment

- `register_workflow` builds `WorkflowStep` list with `step_index`, `prerequisites` (default `[i-1]`), `required_artifacts`.
- `validate_workflow_step_prerequisites` checks predecessor existence, `COMPLETED` status, and job-record state - a genuine prerequisite validator (not just label matching).
- Continuation identity is preserved through `internal_job_id`, `parent_job_id`, `workflow_id`, `step_index` fields on `CloudflareJobRecord`.

**Gap (UNVERIFIED):** this is all model + reconciliation logic. Whether an actual `submit_workflow` → interrupted step → resume → next-step chain executes correctly end-to-end could **not** be exercised here (pytest/RDKit/Flask absent, and the subsystem is uncommitted anyway). Marked UNVERIFIED, not assumed correct.

---

## 9. Security Assessment

| Area | Finding |
|---|---|
| **Cross-user result access (P1)** | `fetch_results()` does `BROKER.remember(creds)` (no auth) then `self.result_store.retrieve(job_id, creds.username)`. The archived-archive path is **local** (no Kaggle call), so an unauthenticated caller who knows a victim's Kaggle username + job slug can download that user's archived scientific results. |
| **Sweep owner spoofing (P2, N5)** | `/api/orca/sweep` → `BROKER.remember(creds)` with no `authenticate()`, then `sweep_now(owner=creds.username)`. Owner is attacker-controlled; `remember` overwrites a victim's cached credentials. |
| **Credential handling** | In-RAM only, TTL, fingerprint logging, `JsonFormatter` redaction - GOOD. |
| **Archive/path traversal** | `basename()` sanitization, size/count caps - GOOD. |
| **Subprocess** | list-based args; `shell=True` only for `kaggle.bat` on Windows - OK. |
| **Rate abuse** | No per-endpoint rate limiting observed - PARTIAL. |
| **Job/result enumeration** | `job_id` (Kaggle slug) is the access key; combined with username spoofing this is exploitable (P1 above). |

The systemic root cause: the `/api/orca/*` endpoints treat `creds.username` from the request body as the authenticated identity. `login()` and `reconnect()` call `service.authenticate()`, but `results`, `archive`, `delete`, `sweep`, `status`, `cancel`, `resume`, `submit` do not - Kaggle-backed paths get implicit auth (garbage creds rejected by Kaggle), but the local `result_store` path has no such backstop.

---

## 10. Hugging Face Deployment Assessment

**HF RISK** (with one UNVERIFIED runtime dimension).

- Startup/port/workers: `sdk: docker`, port 7860, `gunicorn --workers 1 --threads 8` - correct.
- State dir resolution: `ORCA_STATE_DIR` → `/data` → `./.state` → tmp. The `/data` persistent volume is **paid-tier only**; the free tier silently falls back to ephemeral working-dir storage. The code documents this (comment in `config.py`), but the durability layer does not propagate it.
- Secrets: env-based, in-RAM Kaggle creds, Cloudflare bearer token from env - correct.
- **Metadata plane:** with no Cloudflare config, metadata persistence is RAM-only; on restart it is lost (see §7).
- **Result store:** default `./.state/results` is ephemeral on free tier; `ARCHIVED` does not reflect this (§6).
- Background watchdog: single worker, fenced leases - OK.

Net: deployment will boot and run, but the *durability guarantees* the new subsystem advertises are only real when (a) Cloudflare is configured AND (b) a persistent volume is mounted at `/data`. Neither is enforced or surfaced.

---

## 11. Test Integrity Assessment

- **Execution: UNVERIFIED** - `pytest` and `rdkit` are not installed in this environment; no suite was run.
- New test files exist and are **untracked**: `test_cloudflare_controller.py` (20.8 KB), `test_cloudflare_recovery.py` (9.1 KB), `test_scientific_result_durability.py` (16.1 KB), totaling **33 `def test_*` functions**.
- Because they are untracked, they are **not part of the release** and would not run from a clean clone.
- These are, by inspection, a mix of unit + contract + simulation tests (mocks inject `InMemoryCloudflareBackend`), which is appropriate - but they do **not** constitute real Cloudflare deployment verification (correctly not claimed as such).

---

## 12. Clean-Clone Assessment

- **FAIL.** `git clone` of `e524769` reproduces the **old** tree - no Cloudflare controller, no `result_store`, no new tests. The new subsystem exists only in the working tree.
- Commit-of-modified-files-without-untracked-modules would **break imports** (`orca_orchestrator/__init__.py` → `result_store`).
- No fresh venv + install + pytest was possible (pytest/RDKit absent).

---

## 13. New Findings

| ID | Severity | Finding |
|---|---|---|
| N8 | **P0** | Entire Cloudflare + result-durability + recovery subsystem is **uncommitted/untracked** (16 dirty files). Release commit does not contain it; partial commit would break imports. ENG-F1/F2 regression. |
| N9 | **P1** | Cross-user result access: unauthenticated `/results`/`/archive` retrieve another user's archived results via `result_store.retrieve(job_id, creds.username)` with no authentication. |
| N10 | **P1** | Metadata durability dishonesty: silent in-memory fallback; `record_checkpoint` always returns `True`; "Registered in Cloudflare" logged against RAM fallback; no durability flag on the data path. |
| N11 | **P2** | `ARCHIVED.is_durable=True` regardless of ephemeral vs persistent storage; no `ARCHIVED_LOCAL`/`ARCHIVED_PERSISTENT`. |
| N12 | **P2** | `/api/orca/sweep` (prior N5) still unauthenticated and owner-spoofable. |
| N13 | **P2** | New durability states (`REMOTE_DELETED`, `RESULT_UNAVAILABLE`, `ARCHIVED`, `RECOVERY_PENDING`, `REMOTE_ONLY`) are absent from `api.py` - not surfaced to API/frontend. |
| N14 | **P3** | Root `cloudflare_controller.py` (185 B) is a `from … import *` alias duplicating the package name - harmless but adds a second importable identity for the subsystem. |
| N15 | **P3** | Release hygiene: 3 more uncommitted audit `.md` reports + `final_post_repair_deepseek_audit.md` (duplicate of the committed report) + agent persona files still in tree. |

---

## 14. Final Recommendation

### Safe now
- Scientific engine (thermochemistry, temperature, pressure, parser, stationary points, composite chemistry) - independently verified.
- Result physical integrity (`result_store`): staging + atomic move, SHA-256, re-verification on retrieve, path-traversal-safe, secret-scrubbed manifest.
- Reconciliation matrix (Case A–F) and workflow prerequisite validation - sound design.
- Cloudflare secret hygiene and data model (schema versioning, immutable internal IDs).

### Must fix before public release
1. **Commit the subsystem properly** (N8): add `cloudflare_controller/`, `result_store.py`, and the new tests to git, verify the modified tracked files are consistent, and ensure a clean clone imports and runs.
2. **Authenticate result access** (N9): `/results` and `/archive` must verify credentials before touching `result_store`, and must derive owner from the authenticated identity - never from the raw request field.
3. **Make metadata durability honest** (N10): propagate a `durable` flag through `put_job`/`put_workflow`/`record_checkpoint`, return it to callers, and do not log "registered in Cloudflare" on the in-memory path.
4. **Distinguish storage durability** (N11): add `ARCHIVED_LOCAL` vs `ARCHIVED_PERSISTENT` (or gate `ARCHIVED` on a persistent `ORCA_RESULTS_DIR`), so ephemeral results are never presented as durably archived.

### Can wait
- `/api/orca/sweep` authentication (N12) - scope it to the authenticated owner; lower urgency than N9 since impact is credential poisoning/DoS rather than data exfiltration.
- Surface new durability states in API/frontend (N13).
- Root alias module cleanup (N14), audit-report hygiene (N15).
- Deeper spectroscopy validation.

---

## FINAL SCORECARD

| Domain | PASS | FAIL | UNVERIFIED | Risk |
|---|---|---|---|---|
| Architecture | | ✔ (uncommitted/duplicated) | | High |
| Scientific engine | ✔ | | | Low |
| Thermochemistry | ✔ | | | Low |
| Parser | ✔ | | | Low |
| Stationary points | ✔ | | | Low |
| Cloudflare Controller | | ✔ (durability dishonesty) | | High |
| Credential isolation | ✔ | | | Low |
| Job persistence | | ✔ (uncommitted) | | High |
| Restart recovery | | | ✔ | High |
| Runtime-expiration recovery | | | ✔ | Medium |
| Multi-step workflows | | | ✔ | Medium |
| Result durability | | ✔ (ARCHIVED ≠ persistent) | | High |
| Result integrity | ✔ | | | Low |
| User isolation | | ✔ (N9) | | High |
| Security | | ✔ (N9, N12) | | High |
| Testing | | | ✔ | Medium |
| Reproducibility | | ✔ (clean clone breaks) | | High |
| Hugging Face readiness | | | ✔ (RISK) | Medium |
| Packaging | | ✔ (N8) | | High |

---

## Bottom line

The science is defensible and the new result-integrity + reconciliation code is well-written. But **the exact repository state right now is not a release**: the headline new feature set (Cloudflare persistence, result durability, restart recovery) lives entirely in uncommitted working-tree changes, and it introduces a **real cross-user data-access vulnerability** plus a **durability-honesty gap** that could silently lose metadata on an ephemeral HF Space. Fix N8–N11 (commit properly, authenticate result access, and make durability truthful), then this is a credible release candidate.
