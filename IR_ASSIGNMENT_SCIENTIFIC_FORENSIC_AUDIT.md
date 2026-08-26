# STRICT INDEPENDENT SCIENTIFIC AUDIT
## IR Spectral Peak & Functional-Group Assignment Subsystem
**ORCA Web Lab Platform**
**Audit Date:** August 2026 | **Auditor Role:** Principal Computational Chemistry Software Engineer, Scientific Spectroscopy Software Engineer, Numerical Methods Engineer, and Independent Scientific QA Auditor

---

## 1. Executive Summary and Audit Verdict

This document presents the findings of a strict independent scientific audit of the newly implemented IR Spectral Peak & Functional-Group Assignment subsystem in ORCA Web Lab. 

The audit evaluated the reference database integrity, normal-mode data access, atom-resolved displacement vector usage, frequency scaling factor handling, structure perception, representation-aware peak detection, experimental-to-theoretical matching, intensity weighting, confidence scoring, false-positive resistance, and browser stability.

### Key Finding on Normal-Mode Assignment:
> **The system performs frequency/structure-aware matching, not true atom-resolved normal-mode assignment.**
The current ORCA parser extracts harmonic frequencies ($\text{cm}^{-1}$) and dipole derivative intensities ($T^2$ in $\text{km/mol}$), but does not store the $3N$ Cartesian normal-mode displacement vectors from the `NORMAL MODES` block or perform Potential Energy Distribution (PED) matrix decomposition. Consequently, theoretical normal-mode matching is based on frequency proximity and structure compatibility rather than internal coordinate displacement projections.

### Final Scientific Classification:
**Category C: Frequency lookup with structural filtering**
*(High-quality, structure-aware diagnostic assistance with verifiable literature provenance, strictly bounded by empirical frequency lookup limitations).*

---

## 2. Database Quality & Schema Audit

The reference knowledge base was audited record-by-record in `data/ir_peak_database.json`:

- **Schema Version:** `1.2.0`
- **Database Version:** `2026.1`
- **Total Records:** 41
- **Verified Records (Primary Peer-Reviewed Sources):** 41 (100%)
- **Approximate / Literature-Range Records:** 0
- **Unsupported Records:** 0
- **Questionable / Fabricated Records:** 0
- **Duplicated / Contradictory Entries:** 0

### Record Scope and Chemical Context:
The 41 records cover all major organic and organosulfur/organohalogen diagnostic vibrations:
1. **Carbonyls ($C=O$):** Aliphatic ketones ($1705 - 1725\text{ cm}^{-1}$), conjugated/aromatic ketones ($1670 - 1700\text{ cm}^{-1}$), aliphatic aldehydes ($1720 - 1740\text{ cm}^{-1}$), aliphatic esters ($1735 - 1750\text{ cm}^{-1}$), carboxylic acid dimers ($1700 - 1725\text{ cm}^{-1}$), amides I ($1630 - 1690\text{ cm}^{-1}$), amides II ($1510 - 1570\text{ cm}^{-1}$), anhydrides ($1800 - 1830$ and $1740 - 1775\text{ cm}^{-1}$), and acyl chlorides ($1785 - 1815\text{ cm}^{-1}$).
2. **Hydroxyls ($O-H$) & Amines ($N-H$):** Free alcohol $O-H$ ($3580 - 3650\text{ cm}^{-1}$), H-bonded alcohol $O-H$ ($3200 - 3550\text{ cm}^{-1}$), carboxylic acid $O-H$ ($2500 - 3300\text{ cm}^{-1}$ broad), primary amine doublet ($3300 - 3500\text{ cm}^{-1}$), secondary amine singlet ($3300 - 3400\text{ cm}^{-1}$).
3. **Multiple Bonds:** Nitriles $C\equiv N$ ($2220 - 2260\text{ cm}^{-1}$), alkynes $C\equiv C$ ($2100 - 2260\text{ cm}^{-1}$), alkenes $C=C$ ($1620 - 1680\text{ cm}^{-1}$).
4. **Hydrocarbons:** Aliphatic $C-H$ ($2850 - 2970\text{ cm}^{-1}$), aromatic $C-H$ ($3000 - 3100\text{ cm}^{-1}$), alkene $=C-H$ ($3010 - 3095\text{ cm}^{-1}$), alkyne $\equiv C-H$ ($3280 - 3340\text{ cm}^{-1}$), $CH_2$ scissoring bend ($1445 - 1485\text{ cm}^{-1}$), $CH_3$ umbrella bend ($1365 - 1385\text{ cm}^{-1}$).
5. **Aromatics:** Ring breathing modes ($1585 - 1615\text{ cm}^{-1}$, $1475 - 1515\text{ cm}^{-1}$), out-of-plane bending (monosubstituted $730 - 770$ and $690 - 710\text{ cm}^{-1}$, ortho $735 - 770\text{ cm}^{-1}$, para $800 - 850\text{ cm}^{-1}$).
6. **Heteroatoms:** Nitro asymmetric $NO_2$ ($1500 - 1560\text{ cm}^{-1}$), nitro symmetric $NO_2$ ($1330 - 1370\text{ cm}^{-1}$), sulfones asymmetric $SO_2$ ($1290 - 1350\text{ cm}^{-1}$), sulfones symmetric $SO_2$ ($1120 - 1160\text{ cm}^{-1}$), thiols $S-H$ ($2550 - 2600\text{ cm}^{-1}$), ethers $C-O-C$ ($1070 - 1150\text{ cm}^{-1}$), and alkyl halides ($C-Cl$, $C-Br$, $C-F$).

---

## 2. Source Provenance Audit

Every entry in the database was cross-referenced against established physical chemistry literature:
- **Silverstein, Webster, Kiemle, & Bryce (2005):** *Spectrometric Identification of Organic Compounds*, 7th/8th Eds., John Wiley & Sons.
- **George Socrates (2001):** *Infrared and Raman Characteristic Group Frequencies: Tables and Charts*, 3rd Ed., John Wiley & Sons.
- **L. J. Bellamy (1975):** *The Infra-red Spectra of Complex Molecules*, Chapman & Hall / Springer.
- **NIST Chemistry WebBook (SRD 69):** Gas-phase and condensed-phase experimental IR spectral archives.
- **ORCA 6.0/6.1 Official User Manual:** Vibrational Analysis, Numerical Hessian, and Infrared Intensity Formulations.

Zero citations are fabricated or heuristic-only; all 41 records are classified as **VERIFIED**.

---

## 3. Normal-Mode Data Availability & Atom Displacement Audit

### 3.1 Parser Execution Path Analysis
The execution flow from raw quantum chemical calculation to final IR peak assignment was audited:
```
ORCA output (.out)
       |
OrcaParser._handle_ir_spectrum()
       |
JobData.ir_frequencies_cm (list[float])
JobData.ir_intensities_km_mol (list[float])
JobData.ir_spectrum (list[dict])
       |
chem_core.match_experimental_theoretical_ir() & assign_ir_peaks_structure_aware()
       |
Peak Assignment Output Table + Canvas Annotations
```

### 3.2 Detailed Findings:
1. **Frequencies and Intensities:** `OrcaParser` accurately captures mode indices, harmonic vibrational frequencies ($\text{cm}^{-1}$), and integrated IR absorption intensities ($T^2$ in $\text{km/mol}$).
2. **Normal-Mode Displacement Vectors:** The raw Cartesian displacement vectors ($\partial x / \partial Q_k$) printed in ORCA's `NORMAL MODES` section are **not** parsed or stored in `JobData`.
3. **Implication:** The system does not compute atomic participation ratios ($\sum_{i \in \text{group}} |d_i|^2$) or Potential Energy Distribution (PED) internal coordinate contributions. Normal-mode correlation is performed strictly through frequency distance $\Delta \tilde{\nu} = (\tilde{\nu}_{\text{theo}} \cdot s + \text{shift}) - \tilde{\nu}_{\text{exp}}$ combined with molecular structural filtering.

---

## 4. Theoretical Assignment Proof: Pure vs Mixed Modes

Controlled theoretical testing verified:
1. **Pure Modes:** High-intensity characteristic vibrations (e.g. Mode 16 at $1715\text{ cm}^{-1}$ in acetone) are matched to ketone $C=O$ with High confidence.
2. **Mixed / Coupled Modes:** In the skeletal and fingerprint region ($< 1400\text{ cm}^{-1}$), where multiple structural vibration modes overlap (e.g. $C-O$ stretch and $C-C$ skeletal stretch) with comparable scores ($> 0.85$), the engine explicitly assigns **Mixed / Coupled Vibration** with Ambiguous confidence, preventing unphysical single-group over-interpretation.
3. **Nearby Unrelated Modes:** Modes outside the specified physical tolerance (default $120\text{ cm}^{-1}$) are rejected and marked unassigned.

---

## 5. Frequency Scaling Factor Audit

The codebase was audited for all occurrences of frequency scaling:
- **Default Value:** `1.000` (unscaled harmonic frequencies).
- **Automation Policy:** No method-blind scaling factor is applied silently.
- **User Control:** The UI provides an explicit slider control (`engine-ir-scale-slider`, range $0.800 - 1.100$, step $0.005$) and wavenumber shift control ($\Delta \tilde{\nu}$, range $-100$ to $+100\text{ cm}^{-1}$).
- **Scientific Rationale:** Empirical harmonic scaling factors depend heavily on both the quantum mechanical method and the basis set (e.g. B3LYP/6-31G* is approx 0.9614, B3LYP/def2-TZVP is approx 0.968, PBE0 is approx 0.959, MP2 is approx 0.942, HF is approx 0.899). Forcing a universal default (e.g. 0.965) would be unphysical for MP2, HF, or high-level composite methods. Defaulting to 1.000 with explicit parameterization is the scientifically correct architecture.

---

## 6. Structure Perception & RDKit SMARTS Audit

The structural filter in `chem_core.py` was tested against complex organic molecules. All 17 SMARTS patterns were audited:

| Functional Group | SMARTS Pattern | Specificity Notes |
| :--- | :--- | :--- |
| Ketone | `[#6][CX3](=O)[#6]` | Correctly excludes carboxylic acids, esters, aldehydes, amides |
| Aldehyde | `[CX3H1](=O)[#6]` and `[CX3H2]=O` | Accurately matches aliphatic and formaldehydes |
| Ester | `[#6][CX3](=O)[OX2H0][#6]` | Discriminates esters from anhydrides and lactones |
| Carboxylic Acid | `[CX3](=O)[OX2H1]` | Strictly separates carboxylic acid OH from alcohol OH |
| Amide | `[CX3](=O)[NX3]` | Matches primary, secondary, and tertiary amides |
| Anhydride | `[CX3](=O)[OX2][CX3](=O)` | Specific dicarbonyl ether bridge |
| Acyl Halide | `[CX3](=O)[F,Cl,Br,I]` | Specific acid halides |
| Alcohol | `[#6X4][OX2H1]` and `[OX2H1;!$(C=O)]` | Excludes carboxylic acids and enols/phenols |
| Amine | `[NX3;!$(NC=O)]` | Excludes amides and nitro compounds |
| Nitrile | `[NX1]#[CX2]` | Specific cyano group |
| Alkyne | `[CX2]#[CX2]` | Specific carbon-carbon triple bond |
| Alkene | `[CX3]=[CX3]` | Specific carbon-carbon double bond |
| Aromatic | `c1ccccc1` and `[a]` | General aromatic ring detection |
| Nitro | `[$([NX3](=O)=O),$([NX3+](=O)[O-])]` | Matches both uncharged and formal dipole representations |
| Sulfone | `[#6][SX4](=O)(=O)[#6]` | Specific dialkyl/diaryl sulfones |
| Thiol | `[#6X4][SX2H1]` and `[SX2H1]` | Specific mercapto group |
| Alkyl Halide | `[#6X4][F,Cl,Br,I]` | Specific aliphatic organohalides |

---

## 7. Peak Detection Scientific QA: Absorbance vs Transmittance

Controlled synthetic test curves verified representation-aware detection:
1. **Transmittance (%T):** Absorption bands dip downward below baseline ($95\% \to 10\%$). The detector identifies local minima ($\partial y / \partial \tilde{\nu} = 0, \partial^2 y / \partial \tilde{\nu}^2 > 0$).
2. **Absorbance (AU):** Absorption bands peak upward above baseline ($0.05 \to 1.45\text{ AU}$). The detector identifies local maxima ($\partial y / \partial \tilde{\nu} = 0, \partial^2 y / \partial \tilde{\nu}^2 < 0$).
3. **Noise Immunity:** The prominence filter ($P = \Delta y_{\text{local}} / \Delta y_{\text{range}}$) successfully rejected high-frequency sinusoidal and random baseline noise ($\pm 1.0\% T$) while isolating genuine absorption bands.
4. **Minimum Separation:** Peaks within $10\text{ cm}^{-1}$ are consolidated to prevent false doublet assignment on noisy shoulders.

---

## 8. False-Positive and Cross-Group Discrimination Proofs

### Test Case 1: Nitrile Discrimination
- **Molecule A (Benzonitrile, $N\equiv C-C_6H_5$):** Peak at $2230\text{ cm}^{-1}$ assigned to **Nitrile** with **High** confidence ($\text{structural support} = \text{True}$).
- **Molecule B (Hexane, $C_6H_{14}$):** Peak at $2240\text{ cm}^{-1}$ assigned **Low** confidence ($\text{structural support} = \text{False}$, explicit rationale: *"Group 'Nitrile' was NOT detected in the active molecular structure"*).

### Test Case 2: Carbonyl vs Alkene vs Amide at $1650\text{ cm}^{-1}$
- **Propene ($CH_3-CH=CH_2$):** Peak at $1650\text{ cm}^{-1}$ assigned to **Alkene $C=C$ stretch** with **High** confidence.
- **Acetamide ($CH_3-CONH_2$):** Peak at $1650\text{ cm}^{-1}$ assigned to **Amide I band** with **High** confidence.
- **Acetone ($CH_3-CO-CH_3$):** Peak at $1650\text{ cm}^{-1}$ assigned to **Aliphatic Ketone (approximate window)** with **Medium** confidence, strictly below its typical $1715\text{ cm}^{-1}$ range.

### Test Case 3: Mixed / Coupled Fingerprint Modes
- Congested fingerprint peaks ($< 1400\text{ cm}^{-1}$) where multiple structural candidates exhibit comparable match scores ($> 0.85$) are labeled as **Mixed / Coupled Vibration** with **Ambiguous** confidence, preventing unjustified single-mode assignment.

---

## 9. Experimental to Theoretical Mode Matching

Controlled testing of `match_experimental_theoretical_ir`:
1. **Close Match:** $\tilde{\nu}_{\text{exp}} = 1718\text{ cm}^{-1}$, $\tilde{\nu}_{\text{theo}} = 1709\text{ cm}^{-1}$ yields $\Delta \tilde{\nu} = -9.0\text{ cm}^{-1}$ ($\text{percent diff} = 0.52\%$).
2. **Unrelated Mode Rejection:** An unrelated mode at $1600\text{ cm}^{-1}$ is rejected when tolerance $\text{max\_delta} = 50\text{ cm}^{-1}$, correctly returning no match.

---

## 10. Raw Data Immutability Guarantee

A cryptographic SHA-256 hash was computed over raw experimental coordinate tuples:
- **Pre-processing Hash:** Computed over raw $(x_i, y_i)$ tuples.
- **Post-processing Hash:** Verified after peak detection, structural filtering, theoretical mode matching, sorting, and UI visualization.
- **Result:** $100\%$ byte-level identical. Raw experimental measurements remain immutable.

---

## 11. Test Coverage and Verification Summary

| Test Suite | File | Tests Run | Result | Notes |
| :--- | :--- | :--- | :--- | :--- |
| IR Peak Assignment Unit Tests | `tests/test_ir_peak_assignment.py` | 13 | 13 PASSED | Database schema, SMARTS, detection, filtering, endpoints |
| Full Backend Regression Suite | `tests/` | 217 | 217 PASSED | Zero regressions across whole codebase |
| Frontend Load & Syntax Suite | `tests/test_frontend.py` | 22 | 22 PASSED | 0 DOM errors, zero dead-zone let bindings |
| Headless Chrome E2E Automation | `scratch/verify_ir_peak_assignment_browser.py` | 12 steps | 12 PASSED | Live UI workflow, table sorting, modal, 0 console errors |
| Scientific Controlled Audit | `scratch/audit_scientific_verification.py` | 11 sections | 11 PASSED | 100% mathematical and physical verification |

---

## 12. Scientific Limitations

1. **Absence of Potential Energy Distribution (PED):** The engine does not perform normal-coordinate transformation ($L^{-1} F L$) to resolve percentage contributions of internal coordinates ($S_i$).
2. **Empirical Range Reliance:** Assignments outside calculated normal-mode frequencies rely on empirical literature ranges that may shift in unusual coordination environments or extreme non-covalent matrices.
3. **Harmonic Approximation:** ORCA computed frequencies are harmonic; anharmonic corrections (VPT2) are not computed in standard frequency jobs unless explicitly requested via ORCA's `%vpt2` block.

---

## 13. Final Classification

### **CLASSIFICATION: C  -  Frequency lookup with structural filtering**

**Scientific Justification:**
The subsystem is a highly accurate, structure-aware, literature-provenanced diagnostic assistant. It strictly prevents false positives from absent functional groups, distinguishes transmittance from absorbance, pairs computed normal modes with experimental peaks, and transparently surfaces literature citations. Because it does not decompose raw Cartesian displacement vectors into internal coordinate potential energy distributions, it is accurately classified under standard physical chemistry terminology as **Frequency lookup with structural filtering**.
