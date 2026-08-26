# NMR DEEPSEEK FORENSIC AUDIT - Calculated ¹H / ¹³C NMR Analyzer

**Auditor:** Independent Principal Computational Chemistry Auditor (read-only, adversarial)
**Date:** 2026-08-24
**Target commit:** `c9e10ab` (claimed NMR implementation)
**Method:** Git forensics → ORCA 6.1 manual (local PDF + extracted Section 5.21) cross-check → full source trace of `orca_engine/nmr.py`, `parser.py`, `regex.py`, `web_adapter.py`, `app.py`, `app.js` → test classification. No project file was modified.

---

## 1. Exact Current Git SHA

| Item | Value |
|---|---|
| `git rev-parse HEAD` | `c9e10ab2d778b509bb4f1e546e17de5296cd440e` (short `c9e10ab`) - **matches claim** |
| Branch | `main` |
| Working tree | **clean** (`git status --porcelain` empty) |
| Tracked files | 260 |
| NMR commit contents | `orca_engine/src/orca_engine/nmr.py` (772 lines), `parser.py` (+53/−2), `regex.py` (+8), `adapters/web_adapter.py` (+49), `models.py` (+1), `app.py` (+144), `static/js/app.js` (+646), `templates/index.html` (+116), 4 test files, `NMR_ANALYZER_IMPLEMENTATION_REPORT.md` |

**Release integrity: PASS this round.** Unlike the previous Cloudflare subsystem, the NMR module and its tests are **committed and tracked**; a clean clone of `c9e10ab` contains them. (Preceding commit `2de96ac` addressed the earlier N8–N15 findings.)

---

## 2. ORCA Manual Sections Actually Consulted

Local manual `orca_manual_6_1_0.pdf` (76.8 MB, present on disk, gitignored). NMR content located in **Section 5.21** (pages 1032–1038):

- **5.21.1 NMR Chemical Shifts** (p. 1032–1035) - shielding output format, summary table, TMS reference shieldings.
- **5.21.2 NMR Spin-Spin Coupling Constants** (p. 1035–1036).
- **5.21.3 Simulating NMR Spectra** (p. 1036–1038) - TMS ¹H/¹³C reference values (31.77 / 188.10 ppm).

**Documented output format (p. 1033):** per-nucleus block
```
Nucleus 0C :
Diamagnetic contribution to the shielding tensor (ppm) :  <3×3 matrix>
Paramagnetic contribution to the shielding tensor (ppm): <3×3 matrix>
Total shielding tensor (ppm):                              <3×3 matrix>
Diagonalized sT*s matrix:
sDSO  … iso= 208.629
sPSO  … iso= 20.855
Total … iso= 229.484
```
followed by a summary table whose header is `Nucleus  Element  Isotropic  Anisotropy` - **there is no "CHEMICAL SHIELDING SUMMARY (ppm)" title line in the manual.**

**Documented reference shieldings (p. 1034):** TMS ¹³C at `HF/TZVPP = 194.1`, `BP86/TZVPP = 184.8`, `B3LYP/TZVPP = 184.3` ppm. **(p. 1037):** TMS `¹H = 31.77`, `¹³C = 188.10` ppm (TPSS/pcSseg-3). Manual states explicitly: δ_mol = σ_ref − σ_mol, and "it is of course important that the reference and target calculations have been done with the same basis set and functional."

---

## 3. Parser Validation

| Item | Result |
|---|---|
| Per-nucleus header `Nucleus <idx><elem> :` | **MATCHES** manual → `NUCLEUS_HEADER` regex correct |
| Isotropic shielding source | `Total … iso= …` line (trace-average of diagonalized tensor) - **correct source** |
| Diamagnetic/paramagnetic contributions | `sDSO … iso=` / `sPSO … iso=` - **correct** |
| Full 3×3 tensor | `Total shielding tensor (ppm)` block - **parsed correctly** |
| Summary-table entry | `CHEMICAL SHIELDING SUMMARY (ppm)` - **NOT in the ORCA 6.1 manual**; this path is effectively dead on real manual-format output |
| Multi-job / restart / malformed handling | not NMR-specific; relies on the main parser's section-boundary logic |

**Net:** real ORCA 6.1 output is parsed correctly **via the per-nucleus tensor path**, which is the authoritative source. The summary-table path (and its regex) targets a header string not documented in the manual - harmless for the isotropic value (redundant), but it means **anisotropy is never populated in practice** (see §6).

---

## 4. ¹H Validation

- Element H → isotope `1H` inferred correctly (1H is the standard NMR-active hydrogen isotope).
- Atom index, element, assignment (`H0`, `H2`, …) preserved.
- All H atoms in a sample parse correctly (ethanol golden: 6 H entries).
- Chemically equivalent H (identical shielding 55.460) retained as distinct records - **correct** (deduplication is a visualization concern, not a parse concern).

---

## 5. ¹³C Validation

- Element C → isotope `13C` inferred correctly (¹²C is NMR-inactive).
- Carbon not confused with other elements; oxygen (¹⁷O) correctly excluded from ¹³C spectrum via element filter.
- Multiple/symmetric carbons preserved with correct indices and ordering.

**Limitation (P3):** isotope is **inferred from element**, not read from the output. If a user requests a non-default isotope (`Nuclei = all H { shift, ist = 2 }` for deuterium), the parser still labels it `1H`. Acceptable for the stated ¹H/¹³C scope, but not isotope-aware.

---

## 6. Shielding Validation

| Quantity | Source in parser | Correct? |
|---|---|---|
| σ_iso (isotropic shielding) | `Total … iso=` | **CORRECT** |
| σ_dia (diamagnetic) | `sDSO … iso=` | **CORRECT** |
| σ_para (paramagnetic) | `sPSO … iso=` | **CORRECT** |
| Anisotropy | summary table column 4 | **NOT POPULATED** - summary path never fires on manual-format output, so `anisotropy` stays `None` |
| Full tensor | `Total shielding tensor (ppm)` 3×3 | **CORRECT** |

Isotropic shielding is read from ORCA's explicitly printed `iso=` value (the trace average), **not** recomputed from tensor components - correct approach.

---

## 7. Chemical-Shift Validation

`NMRReference.compute_chemical_shift = round(sigma_ref − sigma_sample, 4)` - **correct**; matches the manual's δ = σ_ref − σ_mol.

Independent check (brief's example): σ_sample = 180.0, σ_ref = 190.0 → δ = **10.0 ppm**. ✓ (sign and units correct).

The propionic-acid golden test verifies δ = {10.4, 29.3, 181.4} for TMS/B3LYP(TZVPP) = 184.3 against manual Table 5.21.1 - **genuinely independent golden values**.

---

## 8. Reference Catalog Audit

| Preset (as labeled) | Value | Manual source | Correct? |
|---|---|---|---|
| ¹³C TMS HF/TZVPP | 194.10 | p.1034 (194.1) | ✅ **CORRECT** |
| ¹³C TMS BP86/TZVPP | 184.80 | p.1034 (184.8) | ✅ **CORRECT** |
| ¹³C TMS "B3LYP/def2-TZVP" | 184.30 | p.1034 (B3LYP/**TZVPP**) | ⚠️ **MISLABELED** - value is TZVPP, not def2-TZVP |
| ¹³C TMS TPSS/pcSseg-3 | 188.10 | p.1037 (188.10) | ✅ **CORRECT** |
| ¹H TMS TPSS/pcSseg-3 | 31.77 | p.1037 (31.77) | ✅ **CORRECT** |
| ¹H TMS "B3LYP/def2-TZVP" | 31.75 | **not in manual** | ❌ **UNVERIFIABLE** |
| ¹H TMS HF/TZVPP | 32.55 | **not in manual** (manual gives ¹H TMS only at TPSS) | ❌ **UNVERIFIABLE** |
| ¹H CH₄ (gas) | 31.40 | "literature standard", **not in manual** | ❌ **UNVERIFIABLE** |

**Summary:** 5 of 8 values are traceable to the manual; 1 is mislabeled (def2-TZVP vs TZVPP), and **3 of the 4 ¹H presets are not sourced from the manual and cannot be verified**. The "B3LYP/def2-TZVP" ¹³C preset has a **method/basis mismatch**: `def2-TZVP ≠ TZVPP`, so a B3LYP/def2-TZVP calculation must **not** reuse the B3LYP/TZVPP reference value.

---

## 9. Method/Basis Compatibility

- **Backend:** `parse_nmr_output` applies a reference **only** when the user supplies `reference_1h`/`reference_13c`; no automatic selection. This satisfies "no silent fallback" **at the backend**.
- **Frontend (FAIL):** `static/js/app.js` initializes `currentNMRReference = "tms_b3lyp_tzvp"` and `updateNMRPresetOptions()` defaults the dropdown to `presets[1]` (= `tms_b3lyp_tzvp`). On load, `recomputeNMRSpectrum()` immediately recomputes the spectrum with the **B3LYP/def2-TZVP TMS reference for every calculation, regardless of the actual method/basis/solvent**.
  - A user who ran HF/SVP (e.g., the manual's own ethanol example) or B3LYP/6-31G* is shown "chemical shifts" relative to a B3LYP reference - **scientifically invalid** and directly contrary to the manual's "same basis set and functional" rule.
  - The UI warns only about shielding-vs-shift, **not** about reference-vs-method mismatch. No method/basis validation, no alias normalization, no dispersion/solvent/restricted-unrestricted awareness.

---

## 10. Visualization Audit

- **Axis direction: CORRECT.** `mapX` inverts the x-axis when `is_reference_applied` (chemical shift): `padding.left + ((maxPpm − ppm)/(maxPpm − minPpm))·plotW` → high ppm on the left. Shielding mode is non-inverted, with distinct labeling (σ vs δ).
- Peak generation: one atom → one stick (default). **Grouping (Δδ ≤ 0.02 ppm) preserves `atom_indices`, `assignments`, and full `atoms` list** - visualization-only, and disabling grouping restores individual peaks. Verified by test.
- Axis labels and units (`ppm`) correct; PNG export uses the same canvas renderer.

---

## 11. API Audit

Endpoints present: `POST /api/orca/engine/nmr/parse`, `/nmr/spectrum`, `/nmr/export/csv`, `/nmr/export/json`.

- **Output schema:** matches the engine (`atoms`, `h1_spectrum`, `c13_spectrum`, `references`, `warnings`, `provenance`).
- **`/nmr/spectrum` accepts `reference_shielding`/`reference_method` with no validation against the calculation's method/basis** - the endpoint trusts whatever the client sends (the frontend default is the wrong-reference vector).
- **Authentication:** none (consistent with the app's other engine endpoints; noted, not specific to NMR).
- **Input limits:** `content` is read with no size cap on the JSON path (potential memory abuse via a huge payload); no rate limiting.
- **Warning propagation:** `warnings` and `is_reference_applied` flow through correctly.

---

## 12. Test Integrity

- **Execution: UNVERIFIED** - pytest/rdkit not installed in the audit environment; no suite was run.
- **Static count:** 302 `def test_*` functions (175 in `tests/`, 127 in `orca_engine/tests/`) + 3 `parametrize` decorators. The claimed "312" is **plausible but unverified**.
- Classification of the NMR tests:
  - `test_golden_propionic_acid_13c_chemical_shifts` - **INDEPENDENT GOLDEN** (manual Table 5.21.1).
  - `test_golden_ethanol_hf_svp_shieldings` - values from the manual, but the fixture **invents a "CHEMICAL SHIELDING SUMMARY (ppm)" header not present in the manual** → **IMPLEMENTATION-COUPLED** (format mirrors the parser regex, not the documented output).
  - `test_reference_catalog_integrity` - only asserts value **ranges** (30–35 / 180–200), so it **cannot catch wrong or mislabeled reference values** → weak.
  - `test_nmr_parser_*`, `test_nmr_spectrum_*`, `test_nmr_api_*` - parser-regression / integration / structural (reasonable, but several reuse the fabricated summary header).

---

## 13. Clean-Clone Reproducibility

- Git state clean; NMR module + tests are tracked; a clone of `c9e10ab` contains them. **PASS** at the source level.
- Fresh-venv + install + pytest could **not** be executed (pytest/RDKit absent) → runtime reproduction **UNVERIFIED**.

---

## 14. Security / Regression Issues

- No new credential/path-traversal/shell risks introduced (NMR endpoints are pure text→model→JSON; no filesystem writes, no subprocess).
- No authentication and no payload size cap on NMR endpoints (consistent with app-wide posture; P3).
- Regression: NMR integration is additive (new module + a state-machine branch + regexes). Static inspection shows no interference with thermochemistry/spectra; runtime regression **UNVERIFIED**.

---

## 15. Remaining Findings with Severity

| ID | Severity | Finding |
|---|---|---|
| NMR-1 | **P1** | Frontend silently defaults to `tms_b3lyp_tzvp` (B3LYP/def2-TZVP TMS) for **every** calculation, with no method/basis matching - violates the manual's "same basis set and functional" requirement and the "no silent fallback" rule. |
| NMR-2 | **P1** | `B3LYP/def2-TZVP` ¹³C reference (184.30) is **mislabelled**: the manual value is **B3LYP/TZVPP**. def2-TZVP ≠ TZVPP. |
| NMR-3 | **P2** | Three ¹H reference presets (31.75 "B3LYP/def2-TZVP", 32.55 "HF/TZVPP", 31.40 "CH₄ gas") are **not in the ORCA manual** - unverifiable/fabricated. |
| NMR-4 | **P2** | Parser's summary-table entry condition (`CHEMICAL SHIELDING SUMMARY (ppm)`) does not match ORCA 6.1 documented output; golden test fixtures fabricate this header → implementation-coupled tests. (Isotropic shielding is still correct via the per-nucleus path.) |
| NMR-5 | **P2** | No incomplete/truncation detection: a partial NMR block is presented as a valid spectrum with no "incomplete/unverified" flag. |
| NMR-6 | **P3** | Isotope inferred from element only (cannot detect non-default isotopes); anisotropy never populated (dead summary path). |
| NMR-7 | **P3** | 3D atom coordinates are not passed in the text-parse API path, so click-to-highlight 3D sync only works when parsing a full `.out` through `OrcaParser`. |
| NMR-8 | **P3** | Release hygiene: 11 developer scratch scripts (`scripts/extract_*.py`, `analyze_manual_details.py`, `generate_academic_manual.py` 36 KB, etc.) are committed; the ORCA manual PDF is correctly gitignored. |

---

## FINAL CLASSIFICATION

| Area | Verdict |
|---|---|
| ORCA NMR parser | **TRUSTWORTHY** (per-nucleus path; summary path dead) |
| ¹H support | **TRUSTWORTHY** |
| ¹³C support | **TRUSTWORTHY** |
| Isotropic shielding | **TRUSTWORTHY** |
| Shielding tensor | **TRUSTWORTHY** |
| Chemical shift calculation | **TRUSTWORTHY** (δ = σ_ref − σ_sample correct) |
| TMS references | **PARTIAL** (5/8 verifiable; 1 mislabeled; 3 unverifiable) |
| Method/basis matching | **NOT TRUSTWORTHY** (silent wrong-reference default in frontend) |
| NMR spectrum generation | **TRUSTWORTHY** |
| Peak assignment | **TRUSTWORTHY** |
| Grouping | **TRUSTWORTHY** (visualization-only, assignments preserved) |
| 3D integration | **UNVERIFIED** (coordinate-dependent) |
| API | **PARTIAL** (correct schema; no method/basis validation; no auth/limits) |
| Export | **TRUSTWORTHY** |
| Tests | **PARTIAL** (1 genuine golden; others implementation-coupled or range-only) |
| Clean clone | **PASS at source level** (runtime UNVERIFIED) |

---

## RELEASE GATE VERDICT

The NMR feature **cannot be called "scientifically validated"** in its current form. The core is sound - the shielding/chemical-shift distinction, the δ = σ_ref − σ_sample equation, the isotropic extraction from `Total … iso=`, the reversed axis, and atom→peak traceability are all correct. But two release-gate conditions fail:

1. **Reference values are not all verified** (3 of 8 unverifiable, 1 mislabeled method/basis).
2. **Silent reference fallback exists in the frontend** (default B3LYP/def2-TZVP TMS applied regardless of the calculation's actual method/basis).

**Answer to the central question:** an experienced computational chemist can trust the *parsed shieldings and assignments*, but **cannot trust the displayed chemical shifts** - because the frontend will apply an unmatched (and in one case mislabeled, in three cases unverifiable) TMS reference by default, without checking the calculation's method/basis.

### Must fix before calling it validated
1. **Remove the silent default reference** (NMR-1): default to "None (shielding)" and require an explicit, method/basis-consistent selection.
2. **Correct the catalog** (NMR-2, NMR-3): relabel B3LYP ¹³C as TZVPP; remove or properly source the three unverifiable ¹H values.
3. **Match reference to method/basis** (NMR-1): warn when the selected preset's method/basis ≠ the calculation's, and never auto-apply.

### Can wait
- Truncation/incomplete detection (NMR-5), isotope-from-output (NMR-6), anisotropy population, 3D coordinates in the text API (NMR-7), scratch-script hygiene (NMR-8), auth/size limits.
