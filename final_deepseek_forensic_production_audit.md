# FINAL DEEPSEEK FORENSIC PRODUCTION AUDIT — Canonical Data Model, ML Export, IR Assignment

**Auditor:** Independent Principal Scientific Software Engineer / Thermochemistry Auditor / Scientific-ML Dataset Auditor / Security Red-Team / Release Gatekeeper (read-only, adversarial)
**Date:** 2026-08-25
**Target:** current exact working tree (HEAD `1529d40`)
**Method:** Git forensics → source trace → **direct Python execution** of the normalization engine, ML JSONL exporter, thermochemistry exporter, and IR database (no pytest/RDKit/Flask available) → adversarial leakage/precision/determinism tests. No project file modified.

---

## 1. Exact Git SHA & Working-Tree State

| Item | Value |
|---|---|
| `git rev-parse HEAD` | `1529d403430901ca9c136d31a508796b722230dc` (short `1529d40`) |
| Branch | `main` |
| Last 2 commits | `1529d40` (3Dmol CDN defer), `15735e4` (MutationObserver loop fix) on top of `a4b5034` |
| Working tree | **HEAVILY DIRTY** — 51 modified tracked files + ~30 untracked files |
| Environment | Python 3.13.12; **pytest / rdkit / flask NOT installed** |

### Release-integrity finding (P1-scope)
The entire subject of this audit is **UNCOMMITTED**:
- `schema/canonical_scientific.schema.json`
- `tools/` (normalize, export_training_jsonl, export_thermochemistry_json)
- `data/` (ir_peak_database.json + 10 example files)
- `docs/SCIENTIFIC_JSON_SCHEMA.md`
- `orca_engine/src/orca_engine/archive_validator.py`
- 4 new test files (`test_canonical_scientific_schema.py`, `test_ir_peak_assignment.py`, `test_spectra_enhancements.py`, `test_orca_source_and_results.py`)
- Plus 51 modified tracked files (app.py, chem_core.py, kaggle_runner.py, cloudflare-control-plane/*, orca_engine/*, result_store.py, …).

**The canonical data model / ML export / IR assignment feature is not committed to Git.** A clean clone of `1529d40` does NOT contain it. This is a release-integrity P1 (the audited capability cannot be reproduced from the repo).

---

## 2. Executive Verdict

# NOT READY — for the canonical-data / ML-export capability under audit

The core pre-existing scientific engine (ORCA parser, thermochemistry, stationary-point, NMR, Cloudflare control plane, result durability) remains sound and was verified in prior audits. **However, the new canonical-scientific-data-model + ML-training-export subsystem — the specific subject of this audit — is NOT READY**, for concrete, independently-executed reasons:

1. **P1 — the ML exporter silently emits all-null targets when fed canonical records** (its documented input). This is silent scientific-data loss in the ML dataset pipeline.
2. **P2 — target-leakage validation fails open for string targets** (only numeric values are checked).
3. **P2 — the canonical JSON schema is permissive** (unknown fields pass silently), so it cannot enforce the advertised domain separation.
4. **P1 — the whole subsystem is uncommitted and unwired** to any production endpoint.

---

## 3. Architecture Assessment

The pipeline in `docs/SCIENTIFIC_JSON_SCHEMA.md` claims:

```
ORCA output → Parser → Analyzer → Canonical AnalysisRecord → { Thermochemistry Engine, ML Training Dataset }
```

**Reality check:** the canonical normalizer + exporters are **standalone tools with zero production callers**. `export_canonical_to_training_jsonl`, `export_to_thermochemistry_input`, and `normalize_to_canonical_schema` are imported only by the tests — no `app.py` route, no service call, no CLI `__main__`. The architecture diagram is aspirational, not wired. The existing production path (app.py → orca_engine → result_store → cloudflare_controller) does not pass through the canonical model.

---

## 4. Canonical AnalysisRecord — Domain Separation (Phase 4A)

The schema/record-type model is conceptually correct (AnalysisRecord vs ReactionDefinition vs WorkflowRecord vs CalculationRecord). The normalizer detects `record_type` via field heuristics and builds separate `analysis_results`, `reaction`, `workflows`, `calculations`, `thermochemistry`, `spectroscopy` blocks. **However, the separation is not enforced at validation time** (permissive schema, §7), and the detection is fragile (§6).

---

## 5. Real Analyzer Normalization (Phase 4B) — FIELD-LOSS FAILURE

**Independently executed** (all 6 `data/examples/analysis_*.json` are canonical records):

| Example | Normalized record_type | analysis_results |
|---|---|---|
| analysis_opt / sp / freq | **workflow_record** (should be analysis_record) | **empty** |
| analysis_ir / nmr / uv | **workflow_record** | **empty + thermo empty** |

The normalizer **re-classifies already-canonical analysis records as `workflow_record`** and drops `analysis_results`, `thermochemistry`, and `spectroscopy`. Root cause: `record_type` auto-detection does not consult the existing `record_type` field; a canonical record always carries a top-level `workflows` key, which triggers the `workflow_record` branch.

Also: the `migration_report` (`omitted_fields`, `unsupported_fields`, `missing_fields`, `warnings`) is **initialized empty and never populated**, so unexplained field loss is never reported (§ Phase 4B requirement unmet).

---

## 6. Content Hash vs Precision (Phase 4C/4D) — CORRECT

Independently executed:

- Stored values preserve full float precision (e.g. `1.123456784` stored exactly; test_9 asserts this).
- `round(…, 8)` is applied **only inside `compute_content_hash`**, not to stored values.
- `record_id` (UUID) ≠ `content_hash` (SHA-256 over canonicalized content): same content twice → **different record_id, identical content_hash** (verified); different energy → **different content_hash** (verified).
- Volatile keys (`record_id`, `created_at`, `updated_at`, `migration_report`, all `*_id`/`*_ref`, `split_group_key`) are correctly excluded from the hash.

**P3:** two floats differing only beyond the 8th decimal collide in the hash (verified: `1.12345678341` vs `1.12345678349` → same hash) but remain distinct in storage. Physically negligible (~1e-10 hartree), but it means `content_hash` is not a bijective content fingerprint at sub-1e-8 precision.

**P3:** `compute_geometry_hash` sorts atom strings, making it atom-order-invariant (could conflate distinct connectivity with identical atom positions). The primary `content_hash` preserves order, so impact is limited.

---

## 7. Schema Validity (Phase 4E) — PERMISSIVE

- `$schema`: Draft 2020-12 ✅
- Root `additionalProperties`: **unset** (defaults to open). Only **1** explicit `additionalProperties: true`, **0** `false` across the entire 33 KB schema.
- Consequence: arbitrary/unknown fields (including editor/UI state) **validate silently**. The schema cannot enforce "no UI state in scientific ground truth" — that separation is only by convention in the normalizer, not by schema constraint.

---

## 8. ML JSONL Exporter (Phase 7) — CRITICAL DEFECTS

### 8A. Silent target loss (P1) — independently executed

Feeding the canonical `analysis_freq.json` (which contains `gibbs_free_energy_eh = -76.416173`, `single_point_energy_eh = -76.419948`) through `export_canonical_to_training_jsonl(..., "molecular_property_prediction")` produced:

```json
"target": {"dipole_moment_debye": null, "electronic_energy_eh": null,
           "gibbs_free_energy_eh": null, "homo_ev": null,
           "homo_lumo_gap_ev": null, "lumo_ev": null}
```

**Every target is null.** The exporter re-normalizes its input, and the re-normalization (finding §5) strips `analysis_results`/`thermochemistry`, so the target extraction reads nothing. Feeding a **raw** analyzer dict produced correct populated targets — confirming the bug is specific to canonical input (the exporter's named and documented input).

### 8B. Crash on IR classification (P2) — independently executed

`export_canonical_to_training_jsonl([analysis_ir.json], "ir_peak_assignment_classification")` raised:

```
AttributeError: 'NoneType' object has no attribute 'get'  (spectro.get("ir") where spectro is None)
```

The canonical record's `spectroscopy` is dropped by re-normalization, then the exporter dereferences it.

### 8C. Target leakage fail-open for strings (P2) — independently executed

- Numeric target value appearing in input → **caught** (`TargetLeakageError`) ✅
- String target (`functional_group="Ketone"`) appearing in an input string → **NOT caught** ❌ (the check only matches numeric targets >1e-4).
- `bond_or_mode` is in `allowed_targets` but **missing from `forbidden_in_inputs`** for `ir_peak_assignment_classification` (verified via `TASK_SCHEMAS`).

No active leak exists in the current input construction (the input fields don't currently inject string targets), but the validator does not fail closed for string leakage.

### 8D. Determinism (Phase 7C) — PASS

Two runs produced **byte-identical JSONL** and equal manifest `records_sha256` (verified).

---

## 9. Thermochemistry Exporter / Round-Trip (Phase 5/6)

- `export_to_thermochemistry_input` is a **pure adapter** (no arithmetic, no constant changes) — as its docstring honestly states.
- `test_14` performs a **true adapter-fidelity round-trip**: raw molecule values → canonical → exporter → `ThermochemistryEngine.evaluate`, compared to direct `evaluate` at `abs=1e-8`. This verifies zero value drift through the adapter. It does **not** re-derive physics (the physics itself was independently verified in the prior thermochemistry audit).
- Composite provenance fields (`electronic_energy_source`, `vibrational_source`, `geometry_source`, `zpe_source`, …) are preserved (test_15).
- **P3:** for a reaction record, the exporter reads `thermo` from the single top-level block, which is empty for re-normalized canonical reactions — the same non-idempotency issue (§5) affects reaction round-trips.

---

## 10. IR Database & Assignment (Phase 8)

### 10A. Database quality — 41 records, all cited
- **41 records** (matches the "41/41" claim); **41/41 carry an explicit `source`/citation field**.
- Ranges are physically plausible (Ketone C=O: aliphatic 1680–1750, conjugated/aromatic 1660–1700; nitrile, carbonyl, etc. all in expected windows).
- **3 duplicate (functional_group, subgroup) pairs** — `Aromatic/Benzene Ring`, `Nitro`, `Sulfone` — each appearing twice (likely symmetric/asymmetric or different modes); not a confirmed defect, but unverified whether they are conflicting or complementary.

### 10B. Scientific classification — Category C (correctly bounded)
The parser stores **frequencies + IR intensities** (`vibrational_frequencies_cm`, `ir_intensities_km_mol`) but **no normal-mode displacement vectors, no atom participation, no PED**. The canonical `theoretical_modes` contain only `{mode, frequency_cm, intensity_km_mol}`. Therefore the system is **Category C: frequency lookup + structural filtering**, **NOT** atom-resolved normal-mode assignment. The docs do not over-claim this.

### 10C. Structure-aware filtering & ambiguity — PRESENT
`chem_core.py` implements `IR_FUNCTIONAL_GROUP_SMARTS` (SMARTS functional-group detection), `detect_molecular_functional_groups`, and `assign_ir_peaks_structure_aware` which:
- filters assignments by structural presence (`"Group X was NOT detected in the active molecular structure"` → lowered confidence),
- reports alternate candidates (`alt_fg`) for overlapping ranges,
- distinguishes ambiguity rather than forcing a single answer.
This satisfies Phase 8C/8D at the code level (browser-level behavior not exercised).

---

## 11. UV/IR Visualization, 3D Builder, Frontend (Phase 9–11) — UNVERIFIED (code-level only)

These are frontend/JS concerns. Without a browser runtime in this environment they were **not executed**. Code inspection shows the two recent commits target exactly the reported failures (recursive `MutationObserver` loop → `15735e4`; parser-blocking 3Dmol CDN → `1529d40` `defer`). Classification of UV/IR as "direct overlay (Model A)" vs "true interpolation (Model B)" was **not** instrumented; treat as UNVERIFIED.

---

## 12. Cloudflare & Result Durability (Phase 12–13)

Already verified in the prior audit at `a4b5034`: all CW-1…CW-8 fixed; fail-closed auth (503), owner isolation, slashed-ref routing, 409 concurrency, `ARCHIVED_LOCAL` vs `ARCHIVED_PERSISTENT` honest distinction — all live-verified against the real Worker + local D1. **No regression observed in the current tree** (the Cloudflare sources are modified but consistent). Live production deployment remains UNVERIFIED.

---

## 13. Security Red-Team (Phase 14) — PASS (no new exposure)

- `sanitize_secrets` strips `kaggle_*`, `cloudflare_*`, `api_key`, `password`, `private_key`, `access_token`, `bearer_token`, `cookie` keys recursively (used by both the normalizer and the exporter).
- No secret appears in the example canonical records or the IR database.
- No new endpoint added for canonical/ML export (the tools are unreachable from HTTP), so there is no new attack surface. (This is also why the subsystem is not production-active.)

---

## 14. API Audit (Phase 15)

No new HTTP endpoints. The canonical/ML export tools are import-only and have no route. Existing `/api/orca/*`, `/api/kaggle/*`, `/api/compound`, `/api/jobs*`, `/api/results*` were audited in prior cycles.

---

## 15. Test Results (Phase 1/18) — UNVERIFIED by execution

- `pytest`, `rdkit`, `flask` **not installed** → the full suite cannot run here.
- Static count: **367 `def test_*`** across `tests/` + `orca_engine/tests/`, + 3 `parametrize`. The "377 collected" claim is plausible (parametrize expansion) but **UNVERIFIED**.
- The new test files import `app`, `chem_core`, `flask`, `orca_engine.experimental_spectrum` — all requiring rdkit/flask, so they could not be executed in this environment.
- `test_14` (thermo round-trip) and `test_9` (precision) are meaningful and their logic is sound (reviewed at source), but were not run.

---

## 16. P0/P1/P2/P3 Findings

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| — | P0 | None | — |
| CF-1 | P1 | ML exporter emits all-null targets when fed canonical records (non-idempotent normalizer) | direct execution |
| CF-2 | P1 | Entire canonical/ML/IR subsystem uncommitted + unwired to production | `git status`, import scan |
| CF-3 | P2 | Exporter crashes (AttributeError) on `ir_peak_assignment_classification` for canonical input | direct execution |
| CF-4 | P2 | Target-leakage validator fails open for string targets; `bond_or_mode` missing from `forbidden_in_inputs` | direct execution + TASK_SCHEMAS |
| CF-5 | P2 | Canonical schema permissive (`additionalProperties` open) — cannot enforce domain separation | schema walk |
| CF-6 | P3 | `migration_report` never reports omitted/unsupported/missing/warnings | normalize examples |
| CF-7 | P3 | `content_hash` collides for sub-1e-8 differences; `geometry_hash` atom-order-invariant | direct execution |
| CF-8 | P3 | Reaction thermochemistry round-trip affected by same non-idempotency | source trace |

---

## 17. Must-Fix Before Production

1. **CF-1** — Make the normalizer idempotent and/or make the exporter short-circuit already-canonical records (never re-normalize a canonical record). Currently the ML exporter silently produces empty-target datasets — a misleading-science failure.
2. **CF-2** — Commit the subsystem and wire it (or explicitly scope it as an offline tool).
3. **CF-4** — Make `validate_no_target_leakage` fail closed on string targets and add `bond_or_mode` to `forbidden_in_inputs`.
4. **CF-5** — Set `additionalProperties: false` (or explicitly whitelist) so the schema enforces the advertised domain separation.

## 18. Deferrable (P3)

Migration-report population; hash collision at sub-1e-8; geometry_hash order-invariance; reaction round-trip hardening; IR duplicate-subgroup verification; browser-level verification of UV/IR overlay, 3D builder, and frontend startup.

---

## 19. Final Production Recommendation

**NOT READY** for the canonical-scientific-data-model + ML-training-export capability that this audit targets. The ML export pipeline currently produces empty targets on its documented input and fails open on string target leakage — exactly the "misleading scientific results" and "target leakage" classes the audit forbids. The subsystem is also uncommitted and unwired.

**The pre-existing production core** (ORCA parsing, thermochemistry, stationary-point classification, NMR, Cloudflare control plane, result durability, security) remains sound and, for that scope alone, is at the DEPLOYMENT-READY bar (verified in prior audits; no regression observed here).

### Answer to the absolute final question

> Would you trust the current exact repository to process ORCA calculations … and generate ML training data without silently changing scientific values, losing provenance, producing false assignments, leaking targets …?

**No — not yet, for the ML dataset generation path.** The scientific-value handling (full-precision storage, deterministic content hash, correct provenance) is sound, and the IR assignment is honest (Category C, structure-aware, ambiguity-aware). But the ML exporter **silently drops all targets when fed canonical records** and its leakage guard **does not fail closed for string labels**. Until CF-1 and CF-4 are fixed and the feature is committed + wired, the "generate ML training data" claim is not trustworthy. The rest of the platform (calculation, analysis, thermochemistry, NMR, Cloudflare, durability, security) does not exhibit these defects.
