# Scientific JSON Export, Machine Learning Dataset Engine, and UI Word Copy Restoration Report

## 1. Executive Summary and Forensic Audit

This report documents the architectural redesign and validation of the scientific data model, deterministic hashing engine, Machine Learning (ML) dataset export pipeline, and Microsoft Word export capabilities for ORCA Web Lab.

### 1.1 Forensic Audit of User Fixtures
An audit of the two supplied reference files was conducted:
1. `ORCA Calculation_quantum_analysis(3).json` (Byte size: 882,025 bytes, SHA-256: `fd572da5245dc4d87ffd9ce70f0cff22b56d4fd0ee4d3f44d9881d91a36aa090`)
2. `orca_parsed_data (3)(2).json` (Byte size: 882,025 bytes, SHA-256: `fd572da5245dc4d87ffd9ce70f0cff22b56d4fd0ee4d3f44d9881d91a36aa090`)

**Audit Findings:**
- **Byte Identity:** Both files are 100% byte-for-byte identical duplicates.
- **Quantum Calculation Identity:** The calculation corresponds to an ORCA 6.1.0 numerical frequency calculation (`NUMFREQ`) of water ($H_2O$) at the DFT/B3LYP/def2-SVP level of theory with gas-phase thermochemistry at $298.15\text{ K}$ and $1.0\text{ atm}$.
- **Electronic Structure:** Single-point electronic SCF energy $E_{\text{elec}} = -76.36077000299\text{ }E_{\text{h}}$, HOMO energy $-6.3366\text{ eV}$, LUMO energy $+0.8003\text{ eV}$, HOMO-LUMO gap $7.1369\text{ eV}$, Dipole moment $1.985185\text{ Debye}$.
- **Thermochemistry:** Zero-point vibrational energy (ZPE) $0.02066673\text{ }E_{\text{h}}$, Total enthalpy $H = -76.33632341\text{ }E_{\text{h}}$, Gibbs free energy $G = -76.35779526\text{ }E_{\text{h}}$, Entropy $-TS = -0.02147185\text{ }E_{\text{h}}$.
- **Vibrational Spectroscopy:** 3 real harmonic vibrational modes at $1608.89\text{ cm}^{-1}$ ($0.009369\text{ km/mol}$), $3681.74\text{ cm}^{-1}$ ($0.000547\text{ km/mol}$), and $3781.01\text{ cm}^{-1}$ ($0.003836\text{ km/mol}$), accompanied by a 2001-point convoluted IR spectrum across the $0\text{ to }4000\text{ cm}^{-1}$ range ($2\text{ cm}^{-1}$ step size).
- **Atomic Population Charges:** Mulliken charges ($O: -0.298612, H_1: +0.149307, H_2: +0.149305$) and Loewdin charges ($O: -0.155094, H_1: +0.077550, H_2: +0.077545$).

---

## 2. Deterministic Calculation Identity Architecture

A foundational distinction has been introduced between instance records, scientific calculation identity, and immutable content fingerprints.

```
+-----------------------------------------------------------------------------------+
|                              ORCA WEB LAB IDENTIFIERS                             |
+------------------------------------+----------------------------------------------+
| record_id                          | Volatile UUID assigned per database export   |
|                                    | (e.g. rec_ac2579e8)                          |
+------------------------------------+----------------------------------------------+
| calculation_id                     | Deterministic hash of scientific inputs      |
|                                    | (e.g. calc_ac2579e8a442ac8e)                 |
+------------------------------------+----------------------------------------------+
| content_hash                       | SHA-256 fingerprint of canonical payload     |
|                                    | (e.g. d15f1bb30d0d07504280eed2aeb917cb...)   |
+------------------------------------+----------------------------------------------+
| split_group_key                    | Molecular identity key for zero ML leakage   |
|                                    | (e.g. InChIKey, SMILES, or Formula+GeomHash) |
+------------------------------------+----------------------------------------------+
```

### Deterministic calculation_id Signature:
$$\text{Signature} = \text{geo\_hash} \mid \text{formula} \mid \text{elements} \mid \text{charge} \mid \text{mult} \mid \text{method} \mid \text{functional} \mid \text{basis} \mid \text{aux\_basis} \mid \text{disp} \mid \text{solv} \mid \text{rel} \mid \text{calc\_type} \mid T \mid P \mid \text{version} \mid \text{stage}$$

```python
calculation_id = f"calc_{hashlib.sha256(signature.encode('utf-8')).hexdigest()[:16]}"
```

---

## 3. Zero Data Loss and High-Precision Preservation Proof

All floating-point numerical values from ORCA raw logs and analyzer objects are stored without downscaling or loss of precision:
- **Electronic Energies:** Preserved up to 14 decimal places in atomic units (Hartree, $E_{\text{h}}$).
- **Cartesian Coordinates:** Preserved up to 6 to 12 decimal places in Angstroms ($\text{\AA}$).
- **Vibrational Frequencies and Intensities:** Preserved exactly as computed by ORCA.
- **Population Charges:** Preserved to full 6 decimal places.

---

## 4. Explicit Units and Type Safety Guarantee

Every physical quantity in top-level ML objects and thermodynamic properties is paired with explicit SI or standard computational chemistry units:

```json
{
  "total_energy": { "value": -76.36077000299, "unit": "hartree" },
  "homo": { "value": -6.3366, "unit": "eV" },
  "lumo": { "value": 0.8003, "unit": "eV" },
  "homo_lumo_gap": { "value": 7.1369, "unit": "eV" },
  "dipole_moment": { "value": 1.985185343, "unit": "debye" },
  "temperature": { "value": 298.15, "unit": "kelvin" },
  "pressure": { "value": 1.0, "unit": "atm" }
}
```

---

## 5. Classical ML, Deep Learning, and GNN Representation Engine

The canonical record provides native structures ready for multiple machine learning paradigms:

### 5.1 Geometry ML Object (3D Equivariant GNNs / SchNet / PaiNN / DimeNet)
```json
{
  "geometry": {
    "num_atoms": 3,
    "elements": ["O", "H", "H"],
    "atomic_numbers": [8, 1, 1],
    "coordinates": {
      "unit": "angstrom",
      "values": [
        [-0.007931, -0.005874, 0.004147],
        [0.282385, 0.892339, 0.249322],
        [0.609746, -0.231865, -0.715969]
      ]
    },
    "geometry_type": "optimized",
    "geometry_hash": "75082322bcd07469a56761002df35b4ce70f8087610a514d79df9d8b8e0bbf4f"
  }
}
```

### 5.2 Molecular Graph Object (2D GNNs / GCN / GAT / MPNN)
```json
{
  "graph": {
    "graph_source": "rdkit",
    "num_nodes": 3,
    "num_edges": 2,
    "nodes": [
      { "atom_index": 0, "element": "O", "atomic_number": 8, "formal_charge": 0, "is_aromatic": false },
      { "atom_index": 1, "element": "H", "atomic_number": 1, "formal_charge": 0, "is_aromatic": false },
      { "atom_index": 2, "element": "H", "atomic_number": 1, "formal_charge": 0, "is_aromatic": false }
    ],
    "edges": [
      { "source": 0, "target": 1, "bond_type": "SINGLE", "bond_order": 1.0, "is_aromatic": false },
      { "source": 0, "target": 2, "bond_type": "SINGLE", "bond_order": 1.0, "is_aromatic": false }
    ],
    "adjacency_matrix": [
      [0, 1, 1],
      [1, 0, 0],
      [1, 0, 0]
    ]
  }
}
```

---

## 6. Charge Decomposition Architecture

Population analysis charges are segregated by method to prevent mixing disparate physical definitions:
- **Mulliken Charges:** Based on Hilbert-space orbital overlap partitioning.
- **Loewdin Charges:** Based on symmetric orthogonalization ($S^{-1/2}$).
- **Hirshfeld Charges:** Based on real-space stockholder partitioning of molecular electron density.
- **Mayer Charges and Valences:** Bond-order and atomic-valence matrix elements.

---

## 7. Thermochemistry Component Provenance Architecture

Thermochemical values are preserved alongside component provenance metadata:
- Single-point electronic SCF energy ($E_{\text{elec}}$)
- Zero-point vibrational energy ($E_{\text{ZPE}}$)
- Thermal vibrational, rotational, and translational corrections ($E_{\text{thermal}}$)
- Total enthalpy ($H = E_{\text{elec}} + E_{\text{ZPE}} + E_{\text{thermal}} + k_{\text{B}}T$)
- Total Gibbs free energy ($G = H - TS$)
- Component provenance tracking indicating whether values derived from direct ORCA output, quasi-RRHO harmonic analysis, or composite multi-job workflows.

---

## 8. Multi-Modal Spectroscopy Engine

### 8.1 Infrared (IR) Spectroscopy
- **Theoretical Harmonic Modes:** Mode number, raw harmonic frequency ($\text{cm}^{-1}$), transition dipole IR intensity ($\text{km/mol}$).
- **Convoluted Theoretical Spectrum:** Continuous Gaussian/Lorentzian broadening array with explicit full-width at half-maximum (FWHM), step size, and scaling factor metadata.
- **Experimental Overlays:** Digitized or imported experimental absorbance/transmittance spectra.
- **Peak Assignments:** Functional group classification tags, confidence levels, and mode correlations.

### 8.2 UV/Vis Electronic Spectroscopy
- **Theoretical Transitions:** State index, excitation energy ($\text{cm}^{-1}$, $\text{nm}$, $\text{eV}$), oscillator strength ($f_{\text{osc}}$), spin symmetry character.
- **Convoluted Spectrum:** Broadened spectral profiles.

### 8.3 NMR Spectroscopy
- **Nuclei:** Chemical shielding tensors, isotropic chemical shifts ($\text{ppm}$), reference standards (TMS/solvent), nucleus isotope ($^1\text{H}$, $^{13}\text{C}$, $^{19}\text{F}$, $^{31}\text{P}$).

---

## 9. Fail-Closed Target Leakage Protection Framework

The dataset exporter enforces recursive validation prohibiting target information from appearing in input features:
1. **Forbidden Key Search:** Recursively traverses input feature dictionaries and fails immediately if any property key from `forbidden_in_inputs` is encountered.
2. **Numeric Target Search:** Traverses input features for exact or rounded floats matching any target scalar value.
3. **String Target Search:** Identifies textual target names, chemical classes, or functional group assignments inside input strings.

---

## 10. Scientific Split-Group Segregation Engine

To avoid cross-split data leakage (e.g. having different conformers or vibrational snapshots of the same molecule across train, validation, and test sets), `split_group_key` establishes clean grouping:
1. **InChIKey** (when available)
2. **Canonical SMILES** (2D topological connectivity)
3. **Reaction SMILES** (for reaction energetics datasets)
4. **Formula + Geometry Hash** (for quantum cluster structures without 2D graph)

---

## 11. Multi-Tier Duplicate Detection Engine

The system categorizes duplicate records into distinct levels:
- `EXACT_SCIENTIFIC_CONTENT`: Identical `content_hash` (pure duplicate computation).
- `SAME_CALCULATION_PARAMETERS`: Identical `calculation_id` (re-run of identical method, basis, geometry, and conditions).
- `DISTINCT`: Unique scientific calculation.

---

## 12. JSONL Training Exporter and SHA-256 Manifest Specification

The training exporter generates strictly formatted JSONL lines with self-contained provenance:
- Each row contains `example_id`, `split_group_key`, `task`, `input`, `target`, `provenance`, and `data_quality`.
- Generates a cryptographically verified `manifest.json` recording total record count, dataset SHA-256 digest, line hashes, and schema version.

---

## 13. Provenance, Auditing, and Secret Sanitization

All exports are scrubbed through `sanitize_secrets()` to remove API tokens, file system paths, passwords, and private credentials before serialization.

---

## 14. JSON Schema Draft 2020-12 Verification

The JSON schema (`schema/canonical_scientific.schema.json`) was extended to formalize all new ML structures (`calculation_id`, `molecule`, `geometry`, `graph`, `calculation`, `electronic_properties`, `vibrational_properties`, `charges`, `quality`, `duplicate_status`, `source_artifacts`, `derived_features`, `targets`, `dataset`) with `additionalProperties: false` enforcement.

---

## 15. Data Quality, Diagnostics, and Stationary Point Verification

Each record includes data quality diagnostics:
- Validation status (`verified`, `partially_verified`, `unverified`, `invalid`)
- Completeness score ($0.0 \text{ to } 1.0$)
- Stationary point status (`MINIMUM`, `TRANSITION_STATE`, `HIGHER_ORDER_SADDLE`, `NO_FREQUENCY_CALCULATION`)
- Convergence and imaginary frequency counts.

---

## 16. "Copy for Word" 2D Molecule and Reaction Scheme Architecture

The user-facing web interface was restored and enhanced to provide direct clipboard copying for Microsoft Word:
- **Molecule Explorer:** `#explorer-copy-word` button renders a high-resolution 2D chemical drawing with title, formula, IUPAC name, and SMILES metadata formatted into standard Word-compatible HTML and PNG clipboard items.
- **Reaction Scheme:** `#reaction-copy-word` button packages reaction equation diagrams with balanced stoichiometric captions and reaction SMILES.

---

## 17. Automated Verification and Regression Test Results

The test suite executed with zero failures across all modules:
- `tests/test_canonical_scientific_schema.py`: 29 passed
- `tests/test_scientific_json_audit_and_ml.py`: 13 passed
- `tests/test_word_copy_exports.py`: 3 passed
- Total Repository Test Suite: 495+ tests passed.

---

## 18. Invariant Verifications (Zero Em-Dashes and English Only)

- **Zero Em-Dashes Invariant:** Scanned `tools/normalize_scientific_json.py`, `tools/export_training_jsonl.py`, `schema/canonical_scientific.schema.json`, `templates/index.html`, `static/js/app.js`, and test suites. Result: **0 em-dashes (`\u2014`) and 0 en-dashes (`\u2013`) found**.
- **Language Policy:** 100% English across all UI labels, code comments, error messages, and documentation.

---

## 19. Production Readiness and Next Steps

The updated scientific JSON data model, ML dataset exporter, and Word copy features are fully production-ready, backwards-compatible, and integrated into the ORCA Web Lab engine.
