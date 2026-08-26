# ORCA Web Lab: Canonical Scientific Data Model Specification

**Schema Version:** `orca-web-lab.1.0`  
**Dataset Version:** `2026.1`  
**JSON Schema Standard:** `JSON Schema Draft 2020-12`

---

## 1. Executive Architecture and Domain Separation

The **Canonical Scientific Data Model** establishes an immutable, versioned, machine-readable, and provenance-aware scientific data contract for ORCA Web Lab. It enforces a strict conceptual separation between **User Intent / Input**, **Workflow Execution Graphs**, and **Verified Scientific Analysis Results**.

```
USER INPUT / MOLECULAR WIZARD
              |
              v
REACTION / WORKFLOW DEFINITION
              |
              v
     CALCULATION RECORD
              |
              v
       ORCA ENGINE OUTPUT
              |
              v
   CANONICAL ANALYSIS RECORD (Primary Scientific Ground Truth)
              |
  +-----------+----------------+
  |                            |
  v                            v
Thermochemistry Engine    ML Training Dataset
(Delta H, Delta G, K_eq)  (JSONL + SHA-256 Manifest)
```

---

## 2. Domain Entity Classification

The schema formally delineates 4 primary top-level domain entity classes:

### A. Analysis Record (`record_type: "analysis_record"`)
The core scientific unit of truth. Represents parsed, verified observables produced by the computational chemistry engine and analyzer:
- **Optimization:** Converged geometry, Cartesian coordinate block, geometry hash, final single-point electronic energy.
- **Electronic Structure:** Single-point electronic energy, frontier orbitals (HOMO, LUMO, alpha/beta spin channels, HOMO-LUMO gap), conceptual DFT reactivity indices, dipole moment, and spin expectation values.
- **Population Analysis:** Explicit method-separated atomic charge vectors (Mulliken, Loewdin, Hirshfeld, Mayer charges, and Mayer valences).
- **Vibrational Frequencies:** Harmonic normal modes, zero-point energy (ZPE), zero-point-corrected energy (E0), vibrational entropy, imaginary frequency count, and Lorentzian-convoluted IR derived spectra.
- **Electronic Spectroscopy:** Theoretical TD-DFT excitation transitions, oscillator strengths, and Gaussian-convoluted UV-Vis derived spectra.
- **NMR Spectroscopy:** Isotropic chemical shifts, nuclear shielding tensors, references, and simulated 1H/13C spectra.
- **Assigned Peaks:** Structure-aware functional group assignments with literature citations and confidence classification.

### B. Reaction Definition (`record_type: "reaction_definition"`)
Represents user-specified chemical stoichiometry and reaction conditions without assuming calculation outputs:
- Reactants and products with explicit stoichiometric coefficients (`role: "reactant" | "product"`).
- Thermodynamic conditions: temperature (T), pressure (P), solvent, catalyst, standard state.
- Atom and charge balance verification vectors.

### C. Workflow Record (`record_type: "workflow_record"`)
Represents execution graphs and multi-step dependency DAGs:
- Step indices and calculation types (OPT -> FREQ -> SP).
- Input and output geometry linkages (`input_geometry_id`, `output_geometry_id`).
- Referenced `analysis_record_id` instances for each completed step.

### D. Calculation Record (`record_type: "calculation_record"`)
Links requested input parameters (method, basis, dispersion, solvent) to the resulting output `analysis_record_id`.

---

## 3. Analyzer JSON to Canonical Record Migration Mapping

The table below catalogs the lossless translation of all 47 keys from the real ORCA analyzer JSON (`ORCA_Parsed_Data.json`) into the Canonical Scientific Schema:

| Source Analyzer JSON Key | Target Canonical Field Path | Data Type | Units / Format | Transformation Semantics |
| :--- | :--- | :--- | :--- | :--- |
| `name` | `name`, `molecular_system.name` | `string` | identifier | Preserved directly as primary human-readable title. |
| `sources` | `provenance.source_artifacts` | `array[object]` | artifact refs | Converted to structured source artifact references with filename and storage path. |
| `e_elec_eh` | `thermochemistry.electronic_energy.value`, `electronic_structure.electronic_energy.value` | `number` | Hartree (Eh) | Canonical electronic SCF energy without ZPE or thermal correction. |
| `electronic_energy_hartree` | `thermochemistry.electronic_energy.value` | `number` | Hartree (Eh) | Consolidated duplicate alias into primary electronic energy. |
| `total_energy_eh` | `thermochemistry.electronic_energy.value` | `number` | Hartree (Eh) | Consolidated duplicate alias into primary electronic energy. |
| `scf_energy_hartree` | `thermochemistry.electronic_energy.value` | `number` | Hartree (Eh) | Consolidated duplicate alias into primary electronic energy. |
| `zpe_eh` | `thermochemistry.zero_point_energy.value`, `thermochemistry.zpe.value` | `number` | Hartree (Eh) | Pure vibrational Zero-Point Energy (sum of 0.5 * h * nu). |
| `zero_point_energy_hartree` | `thermochemistry.zero_point_energy.value` | `number` | Hartree (Eh) | Consolidated duplicate alias into primary zero-point energy. |
| `zpe_hartree` | `thermochemistry.zero_point_energy.value` | `number` | Hartree (Eh) | Consolidated duplicate alias into primary zero-point energy. |
| `electronic_zpe_eh` | `thermochemistry.zero_point_corrected_energy.value` | `number` | Hartree (Eh) | Explicitly represented as E0 = E_electronic + ZPE. Never conflated with pure ZPE. |
| `total_enthalpy_eh` | `thermochemistry.enthalpy.value` | `number` | Hartree (Eh) | Total enthalpy H = E_electronic + H_corr (at specified T and P). |
| `enthalpy_hartree` | `thermochemistry.enthalpy.value` | `number` | Hartree (Eh) | Consolidated duplicate alias into primary enthalpy. |
| `final_enthalpy_hartree` | `thermochemistry.enthalpy.value` | `number` | Hartree (Eh) | Consolidated duplicate alias into primary enthalpy. |
| `gibbs_free_energy_eh` | `thermochemistry.gibbs_free_energy.value` | `number` | Hartree (Eh) | Total Gibbs free energy G = H - TS (at specified T and P). |
| `gibbs_free_energy_hartree`| `thermochemistry.gibbs_free_energy.value` | `number` | Hartree (Eh) | Consolidated duplicate alias into primary Gibbs free energy. |
| `final_gibbs_hartree` | `thermochemistry.gibbs_free_energy.value` | `number` | Hartree (Eh) | Consolidated duplicate alias into primary Gibbs free energy. |
| `total_entropy_cal_mol_k` | `thermochemistry.entropy.value` | `number` | cal/mol/K | Total entropy (translational + rotational + vibrational + electronic). |
| `entropy_term_ts_eh` | `thermochemistry.entropy_term_ts.value` | `number` | Hartree (Eh) | Entropy thermal contribution TS in Hartree. |
| `entropy_correction_minus_ts_eh` | `thermochemistry.entropy_correction_minus_ts.value` | `number` | Hartree (Eh) | Entropy correction term -TS in Hartree. |
| `homo_ev` | `electronic_structure.frontier_orbitals.homo.value` | `number` | eV | Highest Occupied Molecular Orbital energy eigenvalue. |
| `lumo_ev` | `electronic_structure.frontier_orbitals.lumo.value` | `number` | eV | Lowest Unoccupied Molecular Orbital energy eigenvalue. |
| `homo_lumo_gap_ev` | `electronic_structure.frontier_orbitals.homo_lumo_gap.value` | `number` | eV | Fundamental frontier orbital eigenvalue difference (LUMO - HOMO). |
| `alpha_homo_ev` | `electronic_structure.frontier_orbitals.alpha_homo.value` | `number` | eV | Unrestricted alpha spin channel HOMO eigenvalue. |
| `alpha_lumo_ev` | `electronic_structure.frontier_orbitals.alpha_lumo.value` | `number` | eV | Unrestricted alpha spin channel LUMO eigenvalue. |
| `beta_homo_ev` | `electronic_structure.frontier_orbitals.beta_homo.value` | `number` | eV | Unrestricted beta spin channel HOMO eigenvalue. |
| `beta_lumo_ev` | `electronic_structure.frontier_orbitals.beta_lumo.value` | `number` | eV | Unrestricted beta spin channel LUMO eigenvalue. |
| `dipole_moment_debye` | `electronic_structure.dipole_moment.total_debye` | `number` | Debye | Total molecular dipole moment magnitude. |
| `electronegativity_ev` | `electronic_structure.conceptual_dft.electronegativity.value` | `number` | eV | Mulliken electronegativity chi = -0.5 * (HOMO + LUMO). |
| `chemical_hardness_ev` | `electronic_structure.conceptual_dft.chemical_hardness.value` | `number` | eV | Chemical hardness eta = 0.5 * (LUMO - HOMO). |
| `chemical_potential_ev` | `electronic_structure.conceptual_dft.chemical_potential.value` | `number` | eV | Electronic chemical potential mu = -chi. |
| `chemical_softness_ev` | `electronic_structure.conceptual_dft.chemical_softness.value` | `number` | eV^-1 | Chemical softness S = 0.5 / eta. |
| `electrophilicity_index_ev` | `electronic_structure.conceptual_dft.electrophilicity_index.value` | `number` | eV | Global electrophilicity index omega = mu^2 / (2 * eta). |
| `electrodonating_power_ev` | `electronic_structure.conceptual_dft.electrodonating_power.value` | `number` | eV | Gazquez-Cedillo-Vela electrodonating power omega_minus. |
| `electroaccepting_power_ev` | `electronic_structure.conceptual_dft.electroaccepting_power.value` | `number` | eV | Gazquez-Cedillo-Vela electroaccepting power omega_plus. |
| `net_electrophilicity_ev` | `electronic_structure.conceptual_dft.net_electrophilicity.value` | `number` | eV | Net electrophilicity Delta_omega_net = omega_plus - (-omega_minus). |
| `ionization_potential_ev` | `electronic_structure.conceptual_dft.ionization_potential.value` | `number` | eV | Koopmans theorem vertical ionization potential IP = -HOMO. |
| `electron_affinity_ev` | `electronic_structure.conceptual_dft.electron_affinity.value` | `number` | eV | Koopmans theorem vertical electron affinity EA = -LUMO. |
| `mulliken_charges` | `population_analysis.methods.mulliken.charges` | `array[number]` | a.u. (e) | Atomic partial charges via Mulliken population analysis. |
| `loewdin_charges` | `population_analysis.methods.loewdin.charges` | `array[number]` | a.u. (e) | Atomic partial charges via Loewdin symmetric orthogonalization. |
| `hirshfeld_charges` | `population_analysis.methods.hirshfeld.charges` | `array[number]` | a.u. (e) | Atomic partial charges via Hirshfeld electron density partitioning. |
| `mayer_charges` | `population_analysis.methods.mayer.charges` | `array[number]` | a.u. (e) | Atomic partial charges via Mayer population analysis. |
| `mayer_valences` | `population_analysis.methods.mayer.valences` | `array[number]` | valency index | Total Mayer atomic valencies (sum of shared electron pairs). |
| `vibrational_frequencies_cm` | `vibrational_spectroscopy.harmonic_modes[].frequency_cm` | `array[number]` | cm^-1 | Discrete theoretical harmonic normal mode vibrational frequencies. |
| `ir_intensities_km_mol` | `vibrational_spectroscopy.harmonic_modes[].intensity_km_mol` | `array[number]` | km/mol | Integrated dipole derivative IR absorption intensities. |
| `convoluted_ir_spectrum` | `vibrational_spectroscopy.derived_spectrum.data_points` | `array[object]` | wavenumber / abs | Continuous Lorentzian-broadened IR absorbance curve sampled on fine grid. |
| `tddft_cm` | `electronic_spectroscopy.transitions[].energy_cm` | `array[number]` | cm^-1 | Discrete electronic transition excitation energies from TD-DFT. |
| `tddft_fosc` | `electronic_spectroscopy.transitions[].oscillator_strength` | `array[number]` | dimensionless | Dimensionless dipole oscillator strengths for TD-DFT excitations. |
| `uvvis_spectrum` | `electronic_spectroscopy.derived_spectrum.data_points` | `array[object]` | wavelength / int | Continuous Gaussian-broadened UV-Vis extinction spectrum on fine grid. |
| `nmr_result` | `nmr_spectroscopy.nuclei`, `references` | `object` | ppm | GIAO magnetic shielding tensors and chemical shifts relative to TMS. |
| `terminated_normally` | `data_quality.calculation_status`, `termination_status` | `boolean` | flag | Parser verification of clean ORCA calculation completion. |
| `termination_message` | `data_quality.termination_message` | `string` | status text | Raw ORCA terminal completion banner string. |
| `had_error_termination` | `data_quality.had_error_termination` | `boolean` | flag | Diagnostic flag indicating abnormal termination or fatal SCF failure. |
| `error_count` | `data_quality.error_count` | `integer` | count | Number of syntax, SCF, or geometry optimization errors encountered. |
| `stationary_point_status` | `data_quality.stationary_point_status`, `thermochemistry.stationary_point_status` | `string` | enum | Stationary point characterization: MINIMUM, TRANSITION_STATE, or HIGHER_ORDER_SADDLE. |
| `thermochemistry_reliability` | `data_quality.thermochemistry_reliability` | `string` | enum | Thermochemical validity classification: RELIABLE, UNRELIABLE_IMAGINARY_MODES, or INCOMPLETE. |
| `is_transition_state` | `data_quality.is_transition_state` | `boolean` | flag | True if the structure is a first-order saddle point with exactly 1 imaginary frequency. |
| `imaginary_frequencies_count` | `vibrational_spectroscopy.imaginary_frequencies_count` | `integer` | count | Count of negative/imaginary vibrational eigenvalues (nu < 0 cm^-1). |
| `imaginary_frequencies_cm` | `vibrational_spectroscopy.imaginary_frequencies_cm` | `array[number]` | cm^-1 | Magnitudes of negative/imaginary vibrational eigenvalues. |
| `coords`, `elements` | `geometries[].coordinates`, `molecular_system.species[].atoms` | `array[object]` | Angstrom | Cartesian atomic coordinates (x, y, z) mapped to Hill chemical formula and elements. |

---

## 4. Thermochemistry Semantics: ZPE versus Zero-Point Corrected Energy (E0)

A critical distinction enforced by the Canonical Scientific Data Model is the separation between pure Zero-Point Energy (ZPE) and the Zero-Point Corrected Electronic Energy (E0):

1. **`zero_point_energy` (ZPE):**
   - The quantum mechanical ground-state vibrational energy:
     $$\text{ZPE} = \frac{1}{2} \sum_{i=1}^{3N-6} h \nu_i$$
   - Always a positive quantity in Hartree (e.g. `0.24998355 Eh`).

2. **`zero_point_corrected_energy` (E0):**
   - The sum of the electronic SCF potential energy and the zero-point vibrational correction:
     $$E_0 = E_{\text{electronic}} + \text{ZPE}$$
   - Always a negative energy in Hartree (e.g. `-767.286977115877 Eh`).
   - In legacy analyzer outputs, this was occasionally labeled `electronic_zpe_eh`. In the canonical schema, it is stored under `thermochemistry.zero_point_corrected_energy` with explicit component linkages.

3. **Numerical Consistency Verification:**
   - Every canonical record containing thermochemistry undergoes automated mathematical verification:
     $$|E_0 - (E_{\text{electronic}} + \text{ZPE})| < 10^{-5}\text{ Eh}$$
     $$|G - (H - TS)| < 10^{-4}\text{ Eh}$$
   - Verification status and numerical discrepancies are recorded in `thermochemistry.numerical_consistency`.

---

## 5. Raw versus Derived Spectroscopy Data

To ensure complete scientific reproducibility and clean separation between fundamental observables and visualization models:

### Vibrational IR Spectroscopy:
- **Raw Harmonic Modes:** Discrete normal modes $(\nu_i, I_i)$ extracted directly from the ORCA Hessian diagonalization stored in `vibrational_spectroscopy.harmonic_modes`.
- **Derived Convoluted Spectrum:** Continuous absorbance curve calculated by applying a Lorentzian line-shape broadening function:
  $$A(\nu) = \sum_{i} \frac{I_i \cdot (\gamma / 2\pi)}{(\nu - \nu_i)^2 + (\gamma / 2)^2}$$
  with explicit broadening parameters: FWHM ($\gamma = 15\text{ cm}^{-1}$), frequency range ($400\text{ to }4000\text{ cm}^{-1}$), and step size ($2\text{ cm}^{-1}$).

### Electronic UV-Vis Spectroscopy:
- **Raw Electronic Transitions:** Discrete TD-DFT excitation states $(E_k, f_{\text{osc}, k})$ stored in `electronic_spectroscopy.transitions`.
- **Derived Convoluted Spectrum:** Continuous extinction curve generated via Gaussian line-shape convolution with standard deviation parameter $\sigma = 20\text{ nm}$.

---

## 6. Strict Idempotency Guarantee

The normalization engine (`tools/normalize_scientific_json.py`) enforces strict idempotency:
- When a record is already in canonical format (`schema_version == "orca-web-lab.1.0"` with valid `record_id` and `record_type`), normalization preserves all canonical sections (`analysis_results`, `thermochemistry`, `spectroscopy`, `electronic_structure`, `population_analysis`, `vibrational_spectroscopy`, `electronic_spectroscopy`, `nmr_spectroscopy`, `molecular_system`, `geometries`, `workflows`, `provenance`, `data_quality`).
- Re-normalizing an existing canonical record satisfies:
  $$\text{normalize}(C_1) = C_1 \quad \text{and} \quad \text{content\_hash}(C_1) = \text{content\_hash}(C_2)$$
- Downstream exporters (`tools/export_training_jsonl.py` and `tools/export_thermochemistry_json.py`) consume canonical records directly without data loss or target erasure.

---

## 7. Deterministic Content Hash versus Record Identity versus Precision

To ensure scientific reproducibility, dataset deduplication, and immutable archiving, the canonical model distinguishes between:

1. **`record_id`:** Logical identifier (UUIDv4 or stable identifier).
2. **`content_hash`:** Deterministic SHA-256 hash computed over canonicalized scientific data.
3. **Stored Scientific Values:** Preserved at full 64-bit IEEE floating-point precision in JSON without truncation.

### Content Hash Algorithm and Rationale:
- **Included:** Chemical species, atomic elements, Cartesian coordinates, formal charges, calculation methods, Gaussian basis sets, energies, frequencies, dipole moments, and spectroscopic modes.
- **Excluded (Volatile):** `record_id`, `content_hash`, `created_at`, `updated_at`, `migration_report`, and runtime warning lists.
- **Precision Normalization in Hashing:** Floating-point numbers are rounded to 8 decimal places during hash computation to eliminate cross-platform serialization drift. This rounding applies **only to the hash fingerprint payload** and never mutates the full-precision numbers stored in `analysis_results`, `thermochemistry`, or `geometries`.
- **Geometry Hash:** `compute_geometry_hash` produces a Cartesian table fingerprint with sorted atom coordinates to track frame identity across optimization and frequency calculation steps.

---

## 8. Machine Learning Target-Leakage Protection

The dataset exporter (`tools/export_training_jsonl.py`) enforces strict task schemas and fail-closed target leakage prevention across numeric, string, categorical, list, dictionary, and nested representations:

```python
TASK_SCHEMAS = {
    "molecular_property_prediction": {
        "allowed_inputs": ["smiles", "formula", "charge", "multiplicity", "coordinates", "coordinate_units"],
        "allowed_targets": ["homo_ev", "lumo_ev", "homo_lumo_gap_ev", "dipole_moment_debye", "gibbs_free_energy_eh", "electronic_energy_eh"],
        "forbidden_in_inputs": ["homo_ev", "lumo_ev", "homo_lumo_gap_ev", "dipole_moment_debye", "gibbs_free_energy_eh", "electronic_energy_eh", "total_enthalpy_eh"]
    },
    "reaction_energy_prediction": {
        "allowed_inputs": ["equation", "reaction_smiles", "reactants", "products", "conditions"],
        "allowed_targets": ["reaction_energy", "delta_h_kcal_mol", "delta_g_kcal_mol", "k_eq", "balanced"],
        "forbidden_in_inputs": ["reaction_energy", "delta_h_kcal_mol", "delta_g_kcal_mol", "k_eq", "gibbs_free_energy_eh", "electronic_energy_eh"]
    },
    "vibrational_spectrum_prediction": {
        "allowed_inputs": ["smiles", "formula", "coordinates", "coordinate_units"],
        "allowed_targets": ["vibrational_modes", "ir_spectrum"],
        "forbidden_in_inputs": ["vibrational_modes", "ir_spectrum", "peak_assignments", "frequencies"]
    },
    "ir_peak_assignment_classification": {
        "allowed_inputs": ["peak_wavenumber_cm", "intensity_level", "signal_type", "molecular_structure", "theoretical_modes"],
        "allowed_targets": ["functional_group", "subgroup", "bond_or_mode", "confidence", "confidence_score"],
        "forbidden_in_inputs": ["functional_group", "subgroup", "bond_or_mode", "confidence", "confidence_score", "assigned_group", "target_assignment"]
    }
}
```

If any target label or value appears anywhere inside the input features, `validate_no_target_leakage()` raises `TargetLeakageError` and fails closed.

---

## 9. Thermochemistry Engine Pure Adapter Integration

The thermochemistry exporter (`tools/export_thermochemistry_json.py`) is a pure adapter. It translates canonical reaction records and species thermochemistry dictionaries into `ThermochemistryEngine.evaluate()` inputs, preserving composite provenance and temperature/pressure corrections with zero floating-point drift.
