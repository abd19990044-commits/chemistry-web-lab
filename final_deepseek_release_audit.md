# FINAL DEEPSEEK RELEASE AUDIT — MAIN + GITHUB PACKAGE + HUGGING FACE PACKAGE

**Auditor:** Independent Principal Scientific Software Engineer / Security / DevOps / Release Gatekeeper (read-only, adversarial)
**Date:** 2026-08-25
**Target:** repo HEAD `a4eceb0` + `GitHub/` (source package) + `HuggingFace/` (runtime package)
**Method:** Git forensics → three-way SHA-256 differential → full pytest execution in the GitHub package (446 collected, 6:08) → HF import/startup test → secrets scan → CI/Dockerfile/license audit. No project file modified.

---

## 1. Exact State

| Item | Value |
|---|---|
| HEAD | `a4eceb0ee3ecf6661dc2e1768493e16ed073c649` |
| Branch | `main` |
| Working tree | **DIRTY — 49 modified tracked files** (2189 insertions / 271 deletions), 0 untracked beyond audit `.md`s |
| GitHub package | 288 files, 27.0 MB, no `.git` — manifest `git_sha = a4eceb0` |
| HuggingFace package | 93 files, 2.32 MB, no `.git` — manifest `git_sha = a4eceb0` |
| Test env | rdkit 2026.03.5, flask 3.1.3, pytest 9.1.1, jsonschema 4.26.0, cryptography 50.0.0, requests 2.34.2 |

---

## 2. Executive Verdict

# NOT READY — for the RELEASE (packages)

- **Main repository source**: CONDITIONALLY READY (tests largely pass; only P2/P3 remain).
- **GitHub/ + HuggingFace/ release packages**: **NOT READY (P1)** — the packages are **not synchronized with the main source**, and their manifest provenance (`git_sha = a4eceb0`) **does not match their actual content**.

---

## 3. Phase 1/19 — Three-Way Differential (VERIFIED by SHA-256)

**14 runtime-critical/scientific files diverge between main (working tree) and both packages** (GitHub ≡ HF, self-consistent snapshot):

```
app.py, chem_core.py, kaggle_runner.py,
orca_engine/src/orca_engine/{experimental_spectrum, models, nmr, parser, regex, thermochemistry}.py,
orca_orchestrator/orca_artifacts.py,
static/js/app.js, templates/index.html,
data/ir_peak_database.json, schema/canonical_scientific.schema.json
```

- **Neither package matches HEAD `a4eceb0`** (`git show HEAD:app.py` = `c7cc495b…`; package app.py = `2f47b8cf…`; working tree = `d730b474…` — three distinct versions).
- **Neither package matches the current working tree** (working tree is 49 files ahead of HEAD with newer refinements).
- Route surface is identical (44/44/44 `@app.route`), so the divergence is **implementation content** (parser/thermo/NMR/models/schema/IR-database behavior), not endpoint inventory.

**Consequence (P1):** a researcher or Space operator running the packages gets **different scientific code** than the audited main repository, and the packages **cannot be reproduced from any committed Git state**. The release manifest's claimed provenance is false for content.

---

## 4. Phase 25 — Full Test Results (INDEPENDENTLY EXECUTED)

| Target | Collected | Passed | Failed | Skipped | Duration |
|---|---|---|---|---|---|
| **GitHub package** (`pytest -q`) | 446 | **443** | 2 | 1 | 368s |
| Main (collect-only) | 445 | — | — | — | 11s |

Claim "430/430 passed" is **false as stated** (actual: 443/446 in GitHub, with 2 failures).

**Failure 1 (P2, both main & GitHub):** `tests/test_frontend.py::test_suite` — 4 of 25 checks fail:
`TypeError: window.dispatchEvent is not a function` at `static/js/app.js:4668` (`renderJobs`) — the Node harness `global.window` mock lacks `dispatchEvent` (valid browser API; harness gap, suite is RED).

**Failure 2 (P3, environment):** `test_spectra_enhancements.py::test_multi_series_excel_parsing` — `openpyxl` not installed (it **is** pinned in `requirements.txt`).

---

## 5. HuggingFace Package (Phase 17) — VERIFIED independently runnable

- `python -c "import app"` in `HuggingFace/` → **app imported OK**; watchdog started; recovery ledger loaded ("no job is repaired at startup; each is reconciled against the Kaggle ledger on its next pass"). ✅
- `requirements.txt` pinned and complete for runtime: Flask, gunicorn (non-Windows), requests, rdkit, `kaggle>=2.2.3`, google-auth, pillow, rarfile, **openpyxl**, **jsonschema**, **cryptography**. ✅
- `Dockerfile`: python:3.11-slim + Cairo libs, `PORT=7860`, non-root `appuser`, **single gunicorn worker / 8 threads** with documented reasoning (process-local session state, no duplicate-worker races), `--timeout 900` for archive streaming. ✅
- **Minimality claim VERIFIED:** 93 files / 2.32 MB vs 288 / 27 MB; no tests, no `.github`, no docs, no `tools/`, no `cloudflare-control-plane/` (Worker is separate), no `benchmarks/`. Runtime-import confirmed (no `tools.` imports).

---

## 6. Credential Vault (Phase 4) — STATICALLY VERIFIED sound

`orca_orchestrator/credential_vault.py` (identical in all three trees):
- AES-256-GCM (`cryptography.hazmat.primitives.ciphers.aead.AESGCM`).
- 12-byte `os.urandom` nonce per encryption; 16-byte auth tag appended; `associated_data` (AAD) used; `InvalidTag` → `CredentialDecryptionError`.
- Master key consumed from environment only (`KAGGLE_CREDENTIALS_ENCRYPTION_KEY` placeholder in `.env.example`); never in Cloudflare D1, never sent to browser. Ownership-bound (owner param in `encrypt_credentials`). ✅
- **UNVERIFIED by live run** (no real Cloudflare/Kaggle creds; flow reasoned from source).

---

## 7. Secrets Scan (Phase 21) — ZERO live secrets (VERIFIED)

`kaggle.json`/token/private-key/password patterns across MAIN + GitHub + HF: all hits are **code references** (writing `kaggle.json` at runtime) or **placeholder** `.env.example` values (`012345…cdef`, `test_c…2345` — visibly redacted). No real credentials, no `.dev.vars`, no keys. ✅

---

## 8. CI (Phase 16) — STATICALLY VERIFIED clean; live execution UNVERIFIED

`.github/workflows/ci.yml`: push/PR triggers; `test-matrix` (Python 3.11/3.12/3.13 × OS), `lint-and-static-check` (ruff + jsonschema), `security-and-secret-audit`, `clean-install-verification`, release gate (`needs: [lint, test-matrix, security, clean-install]`). **No `continue-on-error` on critical jobs; no local paths** (`G:\`, `C:\Users`) in CI or Dockerfiles. Live GitHub Actions run: **UNVERIFIED** (no access).

---

## 9. Licensing (Phase 20) — CONSISTENT across packages (VERIFIED)

- GitHub/HF `LICENSE`/`LICENSE.txt` = **"ORCA Web Lab Academic and Non-Commercial License v1.1"** (308 lines), Copyright (c) 2026 Abdulsalam S. Hasan, ALL rights reserved, explicit ownership retention, commercial contact policy; **no "Open Source" claim**.
- READMEs reference the same license + repo `abd19990044-commits/chemistry-web-lab`.
- **Mismatch finding (P2/INFO):** HEAD commit still carries the **MIT** license (17 lines); the new license lives only in the uncommitted working tree + packages. The legal/state transition is not committed.

---

## 10. New Findings

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| REL-1 | **P1** | GitHub/HF packages **not synchronized** with main (14 files) and **not reproducible** from any commit; manifest `git_sha=a4eceb0` ≠ content | SHA-256 differential; `git show HEAD` |
| REL-2 | **P1** | Main working tree dirty (49 files, 2189+/271−) — release source not committed; HEAD license (MIT) ≠ package license (Non-Commercial v1.1) | `git status`; license diff |
| REL-3 | P2 | `test_frontend.py` red in main AND GitHub (4 checks; `window.dispatchEvent` harness mock gap) | executed suite |
| REL-4 | P3 | `openpyxl` not installed → 1 test fails (documented dep; env gap) | executed suite |
| REL-5 | P3 | Dev/report `.md` artifacts inside GitHub package (`KAGGLE_ENCRYPTED_CREDENTIAL_PERSISTENCE_REPORT.md`, `UFF_REPAIR_AND_VALIDATION_REPORT.md`) | inventory |

## 11. Unverified

Live CI run; live HF Space (health/`/`/browser); live Cloudflare deploy; browser functional/responsive QA (no Chrome automation); literal `git clone` re-run (GitHub package test ≈ clean-copy equivalent).

## 12. Must-Fix Before Release

1. **REL-1/REL-2:** commit the current main state; rebuild `GitHub/` and `HuggingFace/` **from that exact commit**; verify package hashes == `git show <sha>` for every runtime file; regenerate manifests with the true `git_sha`.
2. **REL-3:** add `dispatchEvent` to the `test_frontend.py` `window` mock (and/or guard `app.js`) so the frontend suite is green.
3. **REL-4:** ensure `openpyxl` is installed in the test/CI environment.

## 13. Final Verdict

**NOT READY for release.** The core code is in good shape (GitHub package: **443/446 pass**; UFF, canonical model, credential vault, licenses, and secrets all sound; HF package imports and starts cleanly). But the release itself fails the gate on reproducibility/integrity: the packages are not synchronized with the audited main source, cannot be reproduced from any committed state, and the manifest's claimed provenance is contradicted by content hashes — while the main tree carries a large uncommitted change set including a full license rewrite. These are P1 release-integrity defects, not scientific or security defects. Rebuild both packages from a committed release SHA, verify the differential is empty, and re-run the two P2 fixes; then the release is DEPLOYMENT-ready (live CI/HF/Cloudflare remain the only unverified externals).