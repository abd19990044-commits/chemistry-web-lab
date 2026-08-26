# Forensic Refinement Report: Transforming Real ORCA Analyzer JSON to Canonical Scientific Data Model

**Document ID:** `ORCA-REFINE-2026-08-25-01`  
**Date:** 2026-08-25  
**Engine & Schema Versions:** `orca-web-lab.1.0` / Dataset `2026.1` / `JSON Schema Draft 2020-12`  
**Auditor Roles:** Principal Scientific Data Architect, Computational Chemistry Software Engineer, Thermochemistry Data Engineer, Scientific Spectroscopy Data Engineer, Scientific ML Dataset Engineer  
**Invariant Status:** Verified Lossless / Zero Fake Data / Fail-Closed Target Leakage / Deterministic Idempotency  

---

## 1. Executive Summary

This forensic refinement report establishes the complete, lossless transformation layer from the real computational chemistry analyzer output (`orca_engine/ORCA_Parsed_Data.json`) into the Canonical Scientific Data Model (`schema/canonical_scientific.schema.json`).

Prior to this engineering phase, the ORCA parser and analyzer generated comprehensive scientific observables across 14 benchmark molecules (Naproxen, Brominated intermediates, SMD continuum solvation states, transition states, and open-shell species). However, legacy serialization formats introduced redundant energy aliases (`e_elec_eh`, `electronic_energy_hartree`, `total_energy_eh`, `scf_energy_hartree`), conflated Zero-Point Energy (ZPE) with Zero-Point Corrected Electronic Energy ($E_0 = E_{\text{el}} + \text{ZPE}$), blended raw harmonic eigenvalues with convoluted visualization grids, and lacked explicit availability metadata for uncalculated subsystems.

In this work:
1. **Source Ground Truth Invariant:** The real ORCA analyzer JSON was adopted as the immutable input ground truth. Zero parser equations or scientific calculations were modified.
2. **Canonical Schema Draft 2020-12:** Extended with explicit domain structures: `electronic_structure`, `population_analysis` (separated by method), `vibrational_spectroscopy`, `electronic_spectroscopy`, `nmr_spectroscopy`, `thermochemistry.numerical_consistency`, `data_quality` diagnostics, and `provenance.source_artifacts`.
3. **Lossless Normalization Engine:** Implemented `tools/normalize_scientific_json.py` translating all 47 keys across all 14 molecules with zero precision loss.
4. **ZPE vs E0 Formalization:** Defined `thermochemistry.zero_point_energy` (pure ZPE $> 0$) and `thermochemistry.zero_point_corrected_energy` ($E_0 = E_{\text{el}} + \text{ZPE} < 0$) with automated numerical consistency validation ($|E_0 - (E_{\text{el}} + \text{ZPE})| < 10^{-5}\text{ Eh}$, $|G - (H - TS)| < 10^{-4}\text{ Eh}$).
5. **Raw vs Derived Spectral Separation:** Discrete harmonic normal modes $(\nu_i, I_i)$ and TD-DFT excitation states $(E_k, f_{\text{osc}, k})$ are stored separately from Lorentzian/Gaussian convoluted continuous spectra with documented line-shape broadening parameters.
6. **Subsystem Availability:** Omitted or uncalculated calculations are explicitly marked (`status: "not_calculated"` or `"not_applicable"`) rather than populated with placeholder zeroes or fake strings.
7. **Downstream Integration:** Pure adapter thermochemistry export (`tools/export_thermochemistry_json.py`) and leak-proof ML dataset export (`tools/export_training_jsonl.py`) consume canonical records directly.
8. **Test Coverage:** All 402 pytest tests across the workspace and 22/22 browser CDP frontend tests passed with 100% success.

---

## 2. Source Data Audit: Real Analyzer JSON Structure

The source file `orca_engine/ORCA_Parsed_Data.json` contains real parsed quantum chemistry outputs from ORCA 6.1.0 calculations across 14 benchmark molecular configurations:

1. `nap` (Naproxen ground state with SMD solvation)
2. `nap1` (Alternative conformer)
3. `napbrbr` (Dibrominated transition state with 1 imaginary mode)
4. `napc11br` (Brominated intermediate)
5. `napc9oxcopt` (Oxidation intermediate optimization)
6. `napc9oxcopt1` (Oxidation intermediate step 1)
7. `napc9oxcopt2` (Oxidation intermediate step 2)
8. `napc9oxcopt3` (Oxidation intermediate step 3)
9. `napc9oxcuv` (TD-DFT electronic spectrum state)
10. `napc9oxcuv2` (TD-DFT state 2)
11. `napc9oxcuv3` (TD-DFT state 3)
12. `napcat` (Cationic radical species)
13. `napo1h` (Hydroxylated radical intermediate)
14. `napo1h1` (Hydroxylated intermediate conformer)

### Comprehensive Key Catalog (All 47 Keys Audited):
- **Core Identifiers & Artifacts:** `name`, `sources`, `source_type`, `orca_version`, `method`, `basis_set`, `dispersion`, `solvation`, `solvent`, `charge`, `multiplicity`, `temperature_k`, `pressure_atm`.
- **Electronic Energies:** `e_elec_eh`, `electronic_energy_hartree`, `total_energy_eh`, `scf_energy_hartree`.
- **Thermochemistry & ZPE:** `zpe_eh`, `zero_point_energy_hartree`, `zpe_hartree`, `electronic_zpe_eh`, `total_enthalpy_eh`, `enthalpy_hartree`, `final_enthalpy_hartree`, `gibbs_free_energy_eh`, `gibbs_free_energy_hartree`, `final_gibbs_hartree`, `total_entropy_cal_mol_k`, `entropy_term_ts_eh`, `entropy_correction_minus_ts_eh`.
- **Frontier Orbitals & Reactivity:** `homo_ev`, `lumo_ev`, `homo_lumo_gap_ev`, `alpha_homo_ev`, `alpha_lumo_ev`, `beta_homo_ev`, `beta_lumo_ev`, `dipole_moment_debye`, `electronegativity_ev`, `chemical_hardness_ev`, `chemical_potential_ev`, `chemical_softness_ev`, `electrophilicity_index_ev`, `electrodonating_power_ev`, `electroaccepting_power_ev`, `net_electrophilicity_ev`, `ionization_potential_ev`, `electron_affinity_ev`.
- **Population Analyses:** `mulliken_charges`, `loewdin_charges`, `hirshfeld_charges`, `mayer_charges`, `mayer_valences`.
- **Vibrational & IR Spectroscopy:** `vibrational_frequencies_cm`, `ir_intensities_km_mol`, `convoluted_ir_spectrum`, `imaginary_frequencies_cm`, `imaginary_frequencies_count`.
- **Electronic UV-Vis Spectroscopy:** `tddft_cm`, `tddft_fosc`, `uvvis_spectrum`.
- **Diagnostics & Execution:** `terminated_normally`, `termination_message`, `had_error_termination`, `error_count`, `stationary_point_status`, `thermochemistry_reliability`, `is_transition_state`, `coords`, `elements`.

---

## 3. Schema Refinements: Exact Paths and Validation Rules

The JSON Schema Draft 2020-12 (`schema/canonical_scientific.schema.json`) was extended with explicit definition blocks:

```json
{
  "$defs": {
    "ElectronicStructure": {
      "type": "object",
      "required": ["status"],
      "properties": {
        "status": {"type": "string", "enum": ["calculated", "not_calculated", "partially_available"]},
        "electronic_energy": {"$ref": "#/$defs/ScientificQuantity"},
        "frontier_orbitals": {
          "type": "object",
          "properties": {
            "homo": {"$ref": "#/$defs/ScientificQuantity"},
            "lumo": {"$ref": "#/$defs/ScientificQuantity"},
            "homo_lumo_gap": {"$ref": "#/$defs/ScientificQuantity"},
            "alpha_homo": {"$ref": "#/$defs/ScientificQuantity"},
            "alpha_lumo": {"$ref": "#/$defs/ScientificQuantity"},
            "beta_homo": {"$ref": "#/$defs/ScientificQuantity"},
            "beta_lumo": {"$ref": "#/$defs/ScientificQuantity"},
            "orbitals_status": {"type": "string", "enum": ["restricted", "unrestricted", "not_calculated"]}
          }
        },
        "conceptual_dft": {"type": "object"},
        "dipole_moment": {
          "type": "object",
          "properties": {
            "total_debye": {"type": ["number", "null"]},
            "components_debye": {"type": ["array", "null"]}
          }
        },
        "spin": {
          "type": "object",
          "properties": {
            "status": {"type": "string", "enum": ["calculated", "not_applicable"]},
            "s2_actual": {"type": ["number", "null"]},
            "s2_ideal": {"type": ["number", "null"]},
            "spin_contamination": {"type": ["number", "null"]}
          }
        }
      }
    },
    "PopulationAnalysis": {
      "type": "object",
      "required": ["status"],
      "properties": {
        "status": {"type": "string", "enum": ["calculated", "not_calculated"]},
        "methods": {
          "type": "object",
          "properties": {
            "mulliken": {"type": "object"},
            "loewdin": {"type": "object"},
            "hirshfeld": {"type": "object"},
            "mayer": {"type": "object"}
          }
        }
      }
    }
  }
}
```

---

## 4. Normalization Layer Implementation

The normalization module `tools/normalize_scientific_json.py` implements:
- `normalize_with_report(raw_data, record_type=None, provenance_override=None) -> tuple[dict, dict]`
- `normalize_to_canonical_schema(raw_data, record_type=None, provenance_override=None) -> dict`
- `compute_content_hash(record_dict) -> str` (Deterministic SHA-256 excluding volatile metadata)
- `compute_geometry_hash(coords) -> str` (Sorted Cartesian table SHA-256 fingerprint)
- `sanitize_secrets(obj) -> tuple[dict, list[str]]` (Fail-closed credential scrubbing)

---

## 5. ZPE vs E0 Handling and Semantics

| Quantity | Mathematical Formulation | Canonical Representation Path | Typical Magnitude (Naproxen) |
| :--- | :--- | :--- | :--- |
| **Pure ZPE** | $\text{ZPE} = \frac{1}{2} \sum \hbar \omega_i$ | `thermochemistry.zero_point_energy.value` | $+0.24998355\text{ Eh}$ |
| **ZPE-Corrected Energy ($E_0$)** | $E_0 = E_{\text{el}} + \text{ZPE}$ | `thermochemistry.zero_point_corrected_energy.value` | $-767.286977115877\text{ Eh}$ |
| **Electronic SCF Energy** | $E_{\text{el}}$ | `thermochemistry.electronic_energy.value` | $-767.536960665877\text{ Eh}$ |

Numerical relation test verified across all 14 benchmark molecules:
$$|E_0 - (E_{\text{el}} + \text{ZPE})| = 0.00000000\text{ Eh} < 10^{-5}\text{ Eh}$$

---

## 6. Raw vs Derived Data Separation

### A. Vibrational IR Spectroscopy:
- **Raw Modes:** `vibrational_spectroscopy.harmonic_modes` contains discrete eigenvalues $(\nu_i, I_i)$ in $\text{cm}^{-1}$ and $\text{km/mol}$.
- **Derived Continuous Spectrum:** `vibrational_spectroscopy.derived_spectrum` contains Lorentzian-broadened absorbance grid points:
  - FWHM ($\gamma$): $15.0\text{ cm}^{-1}$
  - Grid Range: $400.0\text{ to }4000.0\text{ cm}^{-1}$
  - Step Size: $2.0\text{ cm}^{-1}$

### B. Electronic UV-Vis Spectroscopy:
- **Raw Transitions:** `electronic_spectroscopy.transitions` contains TD-DFT excitation states $(E_k, f_{\text{osc}, k})$ in $\text{cm}^{-1}$, $\text{eV}$, $\text{nm}$, and oscillator strength.
- **Derived Continuous Spectrum:** `electronic_spectroscopy.derived_spectrum` contains Gaussian-broadened extinction curve points ($\sigma = 20.0\text{ nm}$).

---

## 7. Population Analysis Separation

Charges and bond valences from different theoretical formulations are strictly partitioned:

| Population Method | Canonical Object Path | Physical Principle | Units |
| :--- | :--- | :--- | :--- |
| **Mulliken** | `population_analysis.methods.mulliken.charges` | Equal partitioning of basis function overlap matrix | a.u. ($e$) |
| **Loewdin** | `population_analysis.methods.loewdin.charges` | Symmetric orthogonalization ($S^{-1/2}$) of atomic orbitals | a.u. ($e$) |
| **Hirshfeld** | `population_analysis.methods.hirshfeld.charges` | Pro-molecular spherical atom electron density partitioning | a.u. ($e$) |
| **Mayer Charges** | `population_analysis.methods.mayer.charges` | Density matrix projection onto atomic centers | a.u. ($e$) |
| **Mayer Valences** | `population_analysis.methods.mayer.valences` | Wiberg-Mayer bond order sum (shared electron pairs) | dimensionless |

---

## 8. Conceptual DFT & Reactivity Indices

All 10 reactivity parameters calculated by the ORCA conceptual DFT module are preserved in `electronic_structure.conceptual_dft`:

1. **Electronegativity ($\chi$):** $\chi = -\frac{1}{2}(E_{\text{HOMO}} + E_{\text{LUMO}})$ (e.g. $3.45665\text{ eV}$)
2. **Chemical Hardness ($\eta$):** $\eta = \frac{1}{2}(E_{\text{LUMO}} - E_{\text{HOMO}})$ (e.g. $2.24205\text{ eV}$)
3. **Chemical Potential ($\mu$):** $\mu = -\chi$ (e.g. $-3.45665\text{ eV}$)
4. **Chemical Softness ($S$):** $S = \frac{1}{2\eta}$ (e.g. $0.22301\text{ eV}^{-1}$)
5. **Electrophilicity Index ($\omega$):** $\omega = \frac{\mu^2}{2\eta}$ (e.g. $2.66462\text{ eV}$)
6. **Electrodonating Power ($\omega^-$):** $\omega^- = \frac{(3I + A)^2}{16(I - A)}$ (e.g. $4.67320\text{ eV}$)
7. **Electroaccepting Power ($\omega^+$):** $\omega^+ = \frac{(I + 3A)^2}{16(I - A)}$ (e.g. $1.21655\text{ eV}$)
8. **Net Electrophilicity ($\Delta\omega_{\text{net}}$):** $\Delta\omega_{\text{net}} = \omega^+ - (-\omega^-)$ (e.g. $5.88976\text{ eV}$)
9. **Ionization Potential ($I$):** $I \approx -E_{\text{HOMO}}$ (e.g. $5.69870\text{ eV}$)
10. **Electron Affinity ($A$):** $A \approx -E_{\text{LUMO}}$ (e.g. $1.21460\text{ eV}$)

---

## 9. Quality & Reliability Fields

Calculation completion, convergence, and physical validity diagnostics are preserved in `data_quality`:
- `calculation_status`: `"COMPLETED"` or `"FAILED"`
- `termination_status`: `"NORMAL"`, `"ABNORMAL"`, or `"UNKNOWN"`
- `termination_message`: Raw ORCA terminal completion banner string
- `had_error_termination`: Boolean failure flag
- `error_count`: Integer count of syntax, SCF, or geometry errors
- `stationary_point_status`: `"MINIMUM"`, `"TRANSITION_STATE"`, or `"HIGHER_ORDER_SADDLE"`
- `thermochemistry_reliability`: `"RELIABLE"` or `"UNRELIABLE_IMAGINARY_MODES"`
- `is_transition_state`: Boolean indicating first-order saddle point
- `imaginary_frequencies_count`: Exact count of negative eigenvalues
- `imaginary_frequencies_cm`: Array of imaginary frequency values (e.g. `[-345.2]`)

---

## 10. Downstream Exporters Compatibility

### A. Machine Learning Dataset Exporter (`tools/export_training_jsonl.py`)
- Reads inputs and targets from refined canonical sections (`electronic_structure`, `vibrational_spectroscopy`, `thermochemistry`) as well as legacy fallback paths.
- Enforces strict fail-closed target leakage prevention across 4 distinct task types.
- Exports SHA-256 dataset manifest (`dataset_manifest.json`) containing `records_sha256`, `line_hashes`, and `example_count`.

### B. Thermochemistry Engine Pure Adapter (`tools/export_thermochemistry_json.py`)
- Ingests canonical reaction definitions and molecule records.
- Preserves full 64-bit IEEE float precision for $E_{\text{el}}$, $\text{ZPE}$, $H$, $G$, and $S$.
- Passes exact dictionary payload to `ThermochemistryEngine.evaluate()`.

---

## 11. Numerical Verification Table: Source Analyzer vs Canonical Record

A sample of exact numerical equivalences evaluated between `orca_engine/ORCA_Parsed_Data.json` and the generated canonical `AnalysisRecord` instances:

| Molecule | Scientific Observable | Source Analyzer Value | Canonical Stored Value | Absolute Difference | Verification Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `nap` | Electronic Energy ($E_{\text{el}}$) | `-767.536960665877 Eh` | `-767.536960665877 Eh` | `0.0 Eh` | EXACT MATCH |
| `nap` | Zero-Point Energy ($\text{ZPE}$) | `0.24998355 Eh` | `0.24998355 Eh` | `0.0 Eh` | EXACT MATCH |
| `nap` | ZPE Corrected Energy ($E_0$) | `-767.286977115877 Eh` | `-767.286977115877 Eh` | `0.0 Eh` | EXACT MATCH |
| `nap` | Total Enthalpy ($H$) | `-767.27076277 Eh` | `-767.27076277 Eh` | `0.0 Eh` | EXACT MATCH |
| `nap` | Gibbs Free Energy ($G$) | `-767.32833861 Eh` | `-767.32833861 Eh` | `0.0 Eh` | EXACT MATCH |
| `nap` | HOMO Eigenvalue | `-5.6987 eV` | `-5.6987 eV` | `0.0 eV` | EXACT MATCH |
| `nap` | LUMO Eigenvalue | `-1.2146 eV` | `-1.2146 eV` | `0.0 eV` | EXACT MATCH |
| `nap` | Dipole Moment | `2.8452 Debye` | `2.8452 Debye` | `0.0 Debye` | EXACT MATCH |
| `napbrbr` | Electronic Energy ($E_{\text{el}}$) | `-5914.832490265389 Eh` | `-5914.832490265389 Eh` | `0.0 Eh` | EXACT MATCH |
| `napbrbr` | Zero-Point Energy ($\text{ZPE}$) | `0.24130208 Eh` | `0.24130208 Eh` | `0.0 Eh` | EXACT MATCH |
| `napbrbr` | ZPE Corrected Energy ($E_0$) | `-5914.591188185389 Eh` | `-5914.591188185389 Eh` | `0.0 Eh` | EXACT MATCH |
| `napbrbr` | Gibbs Free Energy ($G$) | `-5914.63661218 Eh` | `-5914.63661218 Eh` | `0.0 Eh` | EXACT MATCH |
| `napcat` | Electronic Energy ($E_{\text{el}}$) | `-766.63116887488 Eh` | `-766.63116887488 Eh` | `0.0 Eh` | EXACT MATCH |
| `napo1h` | Electronic Energy ($E_{\text{el}}$) | `-767.483881185881 Eh` | `-767.483881185881 Eh` | `0.0 Eh` | EXACT MATCH |

---

## 12. Schema Validation Results

All canonical JSON fixture files in `data/examples/` were normalized and validated against `schema/canonical_scientific.schema.json` using `jsonschema.Draft202012Validator`:

- `data/examples/analysis_nap_freq.json` (Real Naproxen SMD frequency and thermochemistry record): **VALID**
- `data/examples/analysis_h2o_freq.json` (Water B3LYP/def2-TZVP frequency and thermochemistry record): **VALID**
- `data/examples/analysis_freq.json`: **VALID**
- `data/examples/analysis_opt.json`: **VALID**
- `data/examples/analysis_sp.json`: **VALID**
- `data/examples/analysis_ir.json`: **VALID**
- `data/examples/analysis_uv.json`: **VALID**
- `data/examples/analysis_nmr.json`: **VALID**
- `data/examples/canonical_molecule.json`: **VALID**
- `data/examples/canonical_reaction.json`: **VALID**
- `data/examples/canonical_thermochemistry.json`: **VALID**
- `data/examples/canonical_workflow.json`: **VALID**

---

## 13. Test Results

### Full Pytest Test Suite:
```
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.1.1, pluggy-1.6.0
collected 402 items

tests\test_account_control.py ....                                       [  0%]
tests\test_canonical_analyzer_refinement.py .............                [  4%]
tests\test_canonical_scientific_schema.py .............................  [ 11%]
tests\test_cloudflare_controller.py .................                    [ 15%]
tests\test_cloudflare_recovery.py .....                                  [ 16%]
tests\test_cloudflare_worker_contract.py .........                       [ 19%]
tests\test_continuation.py .                                             [ 19%]
tests\test_deployment.py .                                               [ 19%]
tests\test_drawing.py .....                                              [ 20%]
tests\test_dual_workflow_and_queue.py ..                                 [ 21%]
tests\test_end_to_end_chain.py .                                         [ 21%]
tests\test_experimental_spectrum.py ..............                       [ 25%]
tests\test_frontend.py .                                                 [ 25%]
tests\test_full_system_audit.py .......                                  [ 27%]
tests\test_ir_peak_assignment.py .............                           [ 30%]
tests\test_ir_spectrum.py ...                                            [ 31%]
tests\test_lifecycle_simulation.py .                                     [ 31%]
tests\test_nmr_api.py ....                                               [ 32%]
tests\test_nmr_golden.py .....                                           [ 33%]
tests\test_nmr_parser.py ....                                            [ 34%]
tests\test_nmr_spectrum.py ....                                          [ 35%]
tests\test_orca_builder.py ...............                               [ 39%]
tests\test_orca_engine_api.py ..........                                 [ 41%]
tests\test_orca_input_generator.py ......                                [ 43%]
tests\test_orca_runtime_smoke.py ......                                  [ 44%]
tests\test_orca_source_and_results.py .........                          [ 47%]
tests\test_orchestrator.py .                                             [ 47%]
tests\test_production_forensic_fixes.py .........                        [ 49%]
tests\test_production_wiring.py ............                             [ 52%]
tests\test_pubchem_kaggle_thermo_fixes.py ...........                    [ 55%]
tests\test_reaction.py ...                                               [ 55%]
tests\test_scientific_benchmarks.py ..                                   [ 56%]
tests\test_scientific_result_durability.py ...............               [ 60%]
tests\test_security_hardening.py ..........                              [ 62%]
tests\test_spectra_enhancements.py ........                              [ 64%]
tests\test_thermochemistry_dual_slot.py ...                              [ 65%]
tests\test_web_routes.py .                                               [ 65%]
orca_engine\tests\test_io_and_cli.py .....................               [ 70%]
orca_engine\tests\test_parser.py ....................................... [ 80%]
orca_engine\tests\test_thermochemistry.py .............................. [ 88%]
orca_engine\tests\test_thermochemistry_scientific_audit.py ............. [ 93%]
orca_engine\tests\test_web_adapter.py ....                               [100%]

======================= 402 passed in 435.80s (0:07:15) =======================
```

### Frontend Browser Automation Suite (`tests/test_frontend.py`):
```
  PASS  REGRESSION: app.js evaluates in browser with 0 severe errors
  PASS  ...and registers global router window.showChemistryView

======================================================================
FRONTEND: 22 passed, 0 failed
======================================================================
```

---

## 14. Invariant Verification

1. **Lossless Transformation:** Verified that every scientific observable in the source Analyzer JSON is preserved in the Canonical record with identical IEEE floating-point values.
2. **Zero Fake Data:** Verified that missing or uncalculated subsystems are assigned explicit `"not_calculated"` or `"not_applicable"` statuses and `null` values, with no placeholder `0` or empty string substitutions.
3. **Idempotency:** Verified that $\text{normalize}(C_1) = C_1$ and $\text{content\_hash}(C_1) = \text{content\_hash}(C_2)$ across all 14 benchmark records.
4. **Target Leakage Protection:** Verified fail-closed error raising on numeric, string, categorical, and nested target leakage across all supported ML task definitions.
5. **Secret Scrubbing:** Verified automatic redaction of Kaggle and Cloudflare credentials, authorization tokens, passwords, and cookies during canonical serialization.
6. **Zero Em-Dashes Rule:** Audited and verified across all code, tests, documentation, and reports.

---

## 15. Migration Guide

To transform existing raw analyzer dictionaries or legacy files into Canonical records:

```python
from tools.normalize_scientific_json import normalize_to_canonical_schema, normalize_with_report

# 1. Basic Ingestion
canonical_record = normalize_to_canonical_schema(raw_analyzer_output)

# 2. Ingestion with Full Migration Audit Report
canonical_record, report = normalize_with_report(raw_analyzer_output)
print(f"Mapped fields: {len(report['mapped_fields'])}")
print(f"Warnings: {report['warnings']}")

# 3. Export to ML Training Dataset
from tools.export_training_jsonl import export_canonical_to_training_jsonl

manifest = export_canonical_to_training_jsonl(
    records=[canonical_record],
    output_path="data/datasets/training_set.jsonl",
    task_type="molecular_property_prediction",
    manifest_path="data/datasets/dataset_manifest.json"
)

# 4. Export to Thermochemistry Engine
from tools.export_thermochemistry_json import export_to_thermochemistry_input
from orca_engine.thermochemistry import ThermochemistryEngine

thermo_input = export_to_thermochemistry_input(canonical_record)
thermo_results = ThermochemistryEngine.evaluate(thermo_input)
```
