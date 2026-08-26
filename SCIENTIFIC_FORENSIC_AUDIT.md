# SCIENTIFIC FORENSIC AUDIT
**ORCA Quantum Chemistry & Thermochemistry Engine Audit**
**Date:** 2026-08-23
**Auditor:** Principal Computational Chemistry Software Engineer & Scientific QA Lead
**Target Package:** `orca_engine` and associated scientific modules in ORCA Web Lab

---

## 1. Scope & Methodology

This audit examines the scientific validity, numerical precision, unit conventions, standard state handling, temperature and pressure semantics, vibrational mode analysis, and failure reporting of the scientific calculation modules:
- `orca_engine/src/orca_engine/thermochemistry.py`
- `orca_engine/src/orca_engine/parser.py`
- `orca_engine/src/orca_engine/models.py`
- `orca_engine/src/orca_engine/experimental_spectrum.py`
- `orca_engine/src/orca_engine/adapters/web_adapter.py`
- `orca_engine/src/orca_engine/reporting.py`
- `chem_core/` and `chem_core.py` (molecular structure & 3D coordinate builder)

---

## 2. Scientific Issues & Root Cause Analysis

### SCI-01: Thermodynamic Non-Equivalence in Custom Temperature Calculations
- **ID**: `SCI-01`
- **Severity**: **P0 (Critical Scientific Correctness)**
- **File**: `orca_engine/src/orca_engine/thermochemistry.py`
- **Function**: `ThermochemistryEngine.evaluate()`
- **Current Behavior**:
  The calculation evaluates the equilibrium constant as $K_{\text{eq}} = \exp(-\Delta G / (R \cdot T))$. When $\Delta G$ is parsed from an ORCA calculation performed at $T_{\text{calc}}$ (e.g. 298.15 K), but evaluated or queried at $T_{\text{requested}} \neq T_{\text{calc}}$, the implementation directly divided $\Delta G(T_{\text{calc}})$ by $(R \cdot T_{\text{requested}})$.
- **Why it is scientifically problematic**:
  Gibbs free energy is an explicit function of temperature:
  $$G(T) = H(T) - T \cdot S(T)$$
  The partition functions (translational, rotational, vibrational, electronic) and thermal population of states shift with temperature. Applying $T_{\text{requested}}$ to a $\Delta G$ computed at $T_{\text{calc}}$ ignores changes in $\Delta H(T)$ and $\Delta S(T)$ and violates fundamental thermodynamics.
- **Correct Behavior**:
  1. Clearly separate $T_{\text{calc}}$ from $T_{\text{requested}}$.
  2. If $T_{\text{requested}} == T_{\text{calc}}$, report rigorous $\Delta G^\circ(T_{\text{calc}})$ and $K_{\text{eq}}(T_{\text{calc}})$ with status `"RIGOROUS_ORCA_THERMO"`.
  3. If $T_{\text{requested}} \neq T_{\text{calc}}$, evaluate temperature extrapolation using the van 't Hoff relation under the explicit assumption of temperature-independent enthalpy and entropy ($\Delta C_p^\circ \approx 0$):
     $$\Delta G^\circ(T_{\text{req}}) \approx \Delta H^\circ(T_{\text{calc}}) - T_{\text{req}} \cdot \Delta S^\circ(T_{\text{calc}})$$
     $$\ln K_{\text{eq}}(T_{\text{req}}) \approx -\frac{\Delta H^\circ(T_{\text{calc}})}{R \cdot T_{\text{req}}} + \frac{\Delta S^\circ(T_{\text{calc}})}{R}$$
     This must be explicitly tagged as `"VAN_T_HOFF_APPROXIMATION (Delta Cp = 0)"` with appropriate scientific warning flags.
- **Recommended Implementation**:
  Update `evaluate()` and `ReactionResult` to store both `temperature_k` ($T_{\text{calc}}$), `delta_g_kcal_mol`, and if requested `temperature_requested_k`, `delta_g_requested_kcal_mol`, `keq_requested`, and `temperature_treatment_mode`.
- **Regression Test**:
  `orca_engine/tests/test_thermochemistry_scientific_audit.py::test_custom_temperature_thermodynamics`

---

### SCI-02: Pressure Semantics & Ideal Gas Standard State
- **ID**: `SCI-02`
- **Severity**: **P1 (High)**
- **File**: `orca_engine/src/orca_engine/thermochemistry.py`, `orca_engine/src/orca_engine/models.py`
- **Function**: `ThermochemistryEngine.evaluate()`, `_check_levels_of_theory()`
- **Current Behavior**:
  Pressure is parsed as metadata (`pressure_atm`). However, when non-standard pressures are supplied or mixed, no ideal gas pressure correction to translational entropy and Gibbs free energy is applied.
- **Why it is scientifically problematic**:
  The translational entropy of an ideal gas depends logarithmically on pressure:
  $$S(T, P) = S(T, P_0) - R \ln\left(\frac{P}{P_0}\right)$$
  For a reaction involving ideal gas species with a change in net gas moles $\Delta n_{\text{gas}} = \sum_{\text{prod}} \nu_j - \sum_{\text{react}} \nu_i$:
  $$\Delta G(T, P) = \Delta G(T, P_0) + \Delta n_{\text{gas}} R T \ln\left(\frac{P}{P_0}\right)$$
  Failing to apply or disclose this correction when accepting a custom pressure parameter leads to unphysical free energies.
- **Correct Behavior**:
  Explicitly calculate the standard state pressure correction for gas-phase reactions:
  $$\Delta G_{\text{corr}} = \Delta n_{\text{gas}} \cdot R \cdot T \cdot \ln(P / P_0)$$
  where $P_0 = 1.0\,\text{atm}$. Tag the calculation with `pressure_standard_state="1.0 atm (Ideal Gas)"` and state whether the pressure correction has been applied.
- **Recommended Implementation**:
  Add gas mole counting $\Delta n_{\text{gas}}$ based on species phase (Gas vs Solution) and apply the $R T \ln(P/P_0)$ correction when $P \neq 1.0\,\text{atm}$.
- **Regression Test**:
  `orca_engine/tests/test_thermochemistry_scientific_audit.py::test_pressure_entropy_correction`

---

### SCI-03: Fractional Stoichiometry Integer Truncation in Atom Balance
- **ID**: `SCI-03`
- **Severity**: **P1 (High)**
- **File**: `orca_engine/src/orca_engine/thermochemistry.py`
- **Function**: `_check_atom_balance()`
- **Current Behavior**:
  ```python
  imbalance = {
      element: round(value)
      for element, value in deltas.items()
      if abs(value) > BALANCE_TOLERANCE
  }
  ```
  `round(value)` rounds floating-point atom imbalances (such as `0.1` or `0.25` from non-integer stoichiometric coefficients) to `0`.
- **Why it is scientifically problematic**:
  Fractional stoichiometry is standard in chemical thermodynamics (e.g., $1.0\,\text{H}_2 + 0.5\,\text{O}_2 \rightarrow 1.0\,\text{H}_2\text{O}$). If an unbalanced fractional equation is provided (e.g., $1.1\,\text{H}_2 + 0.5\,\text{O}_2 \rightarrow 1.0\,\text{H}_2\text{O}$), the hydrogen imbalance $+0.2$ is rounded to `0`. The dictionary `{'H': 0}` evaluates to truthy, but prints `H+0`, misleading the user into thinking zero atoms are unbalanced while flagging an error.
- **Correct Behavior**:
  Preserve exact floating-point imbalances with reasonable precision formatting:
  ```python
  imbalance = {
      element: round(value, 4) if not float(value).is_integer() else int(value)
      for element, value in deltas.items()
      if abs(value) > BALANCE_TOLERANCE
  }
  ```
- **Recommended Implementation**:
  Update `_check_atom_balance()` to avoid premature integer casting and format imbalances cleanly (e.g. `+0.2 H` or `-1 C`).
- **Regression Test**:
  `orca_engine/tests/test_thermochemistry_scientific_audit.py::test_fractional_stoichiometry_atom_balance`

---

### SCI-04: Stationary Point Classification & Vibrational Completeness
- **ID**: `SCI-04`
- **Severity**: **P1 (High)**
- **File**: `orca_engine/src/orca_engine/parser.py`, `orca_engine/src/orca_engine/models.py`
- **Function**: `JobData.stationary_point_status`, `JobData.is_transition_state`
- **Current Behavior**:
  A calculation was designated `MINIMUM` if `imaginary_frequencies_count == 0` and `TRANSITION_STATE` if `imaginary_frequencies_count == 1`, without verifying:
  1. That the calculation terminated normally (`terminated_normally is True`).
  2. That the vibrational mode count matches the expected $3N - 6$ (non-linear) or $3N - 5$ (linear) degrees of freedom.
  3. That near-zero frequencies ($|\nu| < 15\,\text{cm}^{-1}$) are treated as rotational/translational numerical noise rather than true imaginary modes.
- **Why it is scientifically problematic**:
  Incomplete or interrupted frequency jobs could be claimed as validated minima or transition states. Low-frequency numerical noise from loose SCF/grid settings could falsely turn a minimum into a transition state.
- **Correct Behavior**:
  1. Compute $N_{\text{expected}} = 3N - 6$ (or $3N - 5$ for linear, $0$ for monoatomic).
  2. Verify parsed vibrational frequencies against $N_{\text{expected}}$.
  3. Require `terminated_normally is True` and `not had_error_termination`.
  4. Classify status explicitly:
     - `VERIFIED_LOCAL_MINIMUM` ($N_{\text{imag}} == 0$, full modes, normal termination)
     - `VERIFIED_TRANSITION_STATE` ($N_{\text{imag}} == 1$, magnitude $> 20\,\text{cm}^{-1}$, full modes, normal termination)
     - `HIGHER_ORDER_SADDLE_POINT` ($N_{\text{imag}} \ge 2$)
     - `INCOMPLETE_VIBRATIONAL_ANALYSIS` (mode count mismatch or partial file)
     - `UNCONVERGED_CALCULATION` (error termination or missing convergence)
- **Recommended Implementation**:
  Implement `JobData.classify_stationary_point()` with full mode count, imaginary threshold, and normal termination validation.
- **Regression Test**:
  `orca_engine/tests/test_thermochemistry_scientific_audit.py::test_stationary_point_rigorous_classification`

---

### SCI-05: Multi-Level Single-Point Composite Thermochemistry Provenance
- **ID**: `SCI-05`
- **Severity**: **P1 (High)**
- **File**: `orca_engine/src/orca_engine/thermochemistry.py`
- **Function**: `_get_best_energy()`, `evaluate()`
- **Current Behavior**:
  Multi-level SP composite calculations (e.g. combining DLPNO-CCSD(T) electronic energy with DFT frequency thermal corrections) are calculated as:
  $$H_{\text{composite}} = E_{\text{elec}}^{\text{SP}} + (H_{\text{total}}^{\text{Freq}} - E_{\text{elec}}^{\text{Freq}})$$
  $$G_{\text{composite}} = E_{\text{elec}}^{\text{SP}} + (G_{\text{total}}^{\text{Freq}} - E_{\text{elec}}^{\text{Freq}})$$
  However, the resulting `ReactionResult` did not record the full structured provenance (electronic method, basis, dispersion, frequency method, basis, solvent, geometry match status).
- **Why it is scientifically problematic**:
  In scientific publications and computational workflows, composite calculations must explicitly cite both levels of theory and prove coordinate identity. Without structured provenance, downstream users cannot verify composite validity.
- **Correct Behavior**:
  Record `composite_provenance` dictionary on `ReactionResult` containing:
  - `is_composite`: `bool`
  - `sp_level`: `{method, basis, dispersion, solvent, e_elec_eh, job_index}`
  - `freq_level`: `{method, basis, dispersion, solvent, thermal_corr_eh, zpe_eh, job_index}`
  - `geometry_hash_match`: `bool`
- **Recommended Implementation**:
  Populate `result.composite_provenance` in `ThermochemistryEngine.evaluate()`.
- **Regression Test**:
  `orca_engine/tests/test_thermochemistry_scientific_audit.py::test_composite_provenance_tracking`

---

### SCI-06: Coordinate Table Hash Specification
- **ID**: `SCI-06`
- **Severity**: **P2 (Medium)**
- **File**: `orca_engine/src/orca_engine/models.py`
- **Function**: `JobData.compute_geometry_hash()`
- **Current Behavior**:
  The coordinate hash is computed by sorting formatted `ELEMENT:X:Y:Z` string tokens and computing SHA-256.
- **Why it is scientifically problematic**:
  This hash is coordinate-system dependent (not translationally or rotationally invariant). It represents the **Cartesian coordinate table identity** within the same reference frame (e.g. comparing the SP step with the Freq step of the same molecule), rather than an invariant canonical molecular graph/conformer hash.
- **Correct Behavior**:
  Explicitly document and name the attribute/property as `coordinate_table_hash` (or keep `geometry_hash` with precise docstrings defining it as a Cartesian coordinate table fingerprint).
- **Recommended Implementation**:
  Add clear scientific docstrings and provenance fields.
- **Regression Test**:
  `orca_engine/tests/test_thermochemistry_scientific_audit.py::test_coordinate_table_hash_determinism`

---

### SCI-07: Entropy Unit Consistency & Discrepancy Verification
- **ID**: `SCI-07`
- **Severity**: **P1 (High)**
- **File**: `orca_engine/src/orca_engine/thermochemistry.py`
- **Function**: `evaluate()`, `_sum_terms()`, `_get_best_energy()`
- **Current Behavior**:
  Delta entropy is computed both from $((\Delta H - \Delta G) \cdot 1000) / T$ and directly from the sum of $S_{\text{total}}$. Discrepancies between direct entropy summation and $(H-G)/T$ are checked against a threshold ($0.15\,\text{cal}/(\text{mol}\cdot\text{K})$).
- **Why it is scientifically problematic**:
  ORCA 5 and ORCA 6 outputs format entropy in multiple sections (e.g., $S_{\text{vib}}, S_{\text{rot}}, S_{\text{trans}}, S_{\text{el}}$ vs. "Final entropy term"). The parser must maintain exact conversion factors ($1\,\text{Eh} = 627.50947406311\,\text{kcal/mol}$, $1\,\text{cal} = 4.184\,\text{J}$) across all branches.
- **Correct Behavior**:
  Ensure exact unit conversions and cross-validation between $(H-G)/T$ and direct $S$ across all ORCA versions.
- **Recommended Implementation**:
  Validate and harmonize `_get_best_energy(kind=EnergyKind.ENTROPY)` with constant unit definitions.
- **Regression Test**:
  `orca_engine/tests/test_thermochemistry_scientific_audit.py::test_entropy_consistency_cross_check`

---

## 3. Audit Summary & Sign-off

All P0, P1, and P2 scientific issues have documented root causes, defined scientific models, and corresponding regression test specifications. Proceeding to Phase 2 (Test Coverage & Gap Analysis).
