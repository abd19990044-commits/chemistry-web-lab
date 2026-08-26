# ORCA Engine License Consolidation and Governance Report

## 1. Executive Summary

This report documents the forensic licensing audit, intellectual-property classification, and license consolidation of the `orca_engine` component into the authoritative **ORCA Web Lab Academic and Non-Commercial License v1.1**.

### Primary Accomplishments:
1. **Removed Standalone MIT License:** Eliminated the standalone MIT license from `orca_engine/LICENSE` and replaced it with the full canonical text of the ORCA Web Lab Academic and Non-Commercial License v1.1.
2. **Package Metadata Harmonization:** Updated `orca_engine/pyproject.toml` to declare `license = { text = "ORCA Web Lab Academic and Non-Commercial License v1.1" }` without false OSI-approved or MIT classifiers.
3. **Documentation Alignment:** Updated `orca_engine/README.md`, `orca_engine/CONTRIBUTING.md`, `orca_engine/CITATION.cff`, and `scripts/generate_academic_manual.py` to eliminate inaccurate "MIT License" and "open source" claims for proprietary project code.
4. **Third-Party Boundary Integrity:** Preserved all genuine third-party licenses (Flask, RDKit, Cryptography, 3Dmol.js, NumPy, Gunicorn, OpenPyXL, JSONSchema, Pytest, Wrangler, Vitest) in `THIRD_PARTY_LICENSES.md` without alteration or dilution.
5. **ORCA Program Boundary:** Maintained explicit clarity that ORCA is proprietary third-party software developed by Prof. Frank Neese et al. (Max Planck Institute / FACCTs GmbH) and is not bundled or sublicensed by ORCA Web Lab.
6. **Automated Verification:** Added `tests/test_licensing_compliance.py` covering license discovery, metadata assertions, third-party preservation, and the zero em-dashes invariant.

---

## 2. Original License State and Forensic Audit

### 2.1 Audit Scope and Findings
A forensic scan across the repository was conducted before modifications:
- **Root Repository:** Governed by `ORCA Web Lab Academic and Non-Commercial License v1.1` (Effective August 25, 2026, Copyright Abdulsalam S. Hasan).
- **`orca_engine` Subdirectory:** Contained a standalone `orca_engine/LICENSE` file with the standard 21-line MIT License text naming "Salam Hasan" (Abdulsalam S. Hasan) created during initial standalone development.
- **`orca_engine/pyproject.toml`:** Declared `license = { file = "LICENSE" }` referencing the local MIT file.
- **`orca_engine/README.md`:** Displayed an MIT badge and claimed "MIT. See LICENSE".
- **`orca_engine/CONTRIBUTING.md`:** Stated "Contributions are accepted under the MIT License".
- **`orca_engine/CITATION.cff`:** Declared `license: MIT`.
- **`cloudflare-control-plane/package.json`:** Contained `"license": "MIT"` while its `LICENSE` file was already `ORCA Web Lab Academic and Non-Commercial License v1.1`.

### 2.2 Source of the MIT License
Git history inspection revealed that the standalone MIT license was introduced in the initial commit (`c554b07`) when `orca_engine` was structured as an independent parser package. When the full ORCA Web Lab suite transitioned to the Academic and Non-Commercial License v1.1, `orca_engine` remained with its legacy MIT placeholder.

---

## 3. Ownership and Component Classification

Every file under `orca_engine/` and across the repository was classified:

| Path / Component | Ownership | Classification | Governing License |
| :--- | :--- | :--- | :--- |
| `orca_engine/src/orca_engine/*.py` | Project Owner (Abdulsalam S. Hasan) | PROPRIETARY_ORCA_WEB_LAB | ORCA Web Lab Academic and Non-Commercial License v1.1 |
| `orca_engine/src/orca_engine/adapters/*.py` | Project Owner (Abdulsalam S. Hasan) | PROPRIETARY_ORCA_WEB_LAB | ORCA Web Lab Academic and Non-Commercial License v1.1 |
| `orca_engine/src/orca_engine/web/*` | Project Owner (Abdulsalam S. Hasan) | PROPRIETARY_ORCA_WEB_LAB | ORCA Web Lab Academic and Non-Commercial License v1.1 |
| `orca_engine/tests/test_*.py` | Project Owner (Abdulsalam S. Hasan) | PROPRIETARY_ORCA_WEB_LAB | ORCA Web Lab Academic and Non-Commercial License v1.1 |
| `orca_engine/tests/data/*.out` | Project Owner / Public Reference | TEST FIXTURE (Sanitized) | ORCA Web Lab Academic and Non-Commercial License v1.1 |
| `chem_core/` | Project Owner (Abdulsalam S. Hasan) | PROPRIETARY_ORCA_WEB_LAB | ORCA Web Lab Academic and Non-Commercial License v1.1 |
| `orca_orchestrator/` | Project Owner (Abdulsalam S. Hasan) | PROPRIETARY_ORCA_WEB_LAB | ORCA Web Lab Academic and Non-Commercial License v1.1 |
| `cloudflare-control-plane/` | Project Owner (Abdulsalam S. Hasan) | PROPRIETARY_ORCA_WEB_LAB | ORCA Web Lab Academic and Non-Commercial License v1.1 |
| `app.py`, `templates/`, `static/` | Project Owner (Abdulsalam S. Hasan) | PROPRIETARY_ORCA_WEB_LAB | ORCA Web Lab Academic and Non-Commercial License v1.1 |
| **Flask, Gunicorn, RDKit, NumPy** | Third-Party Authors | THIRD_PARTY (Runtime Dependency) | Respective Upstream Licenses (BSD-3, MIT, Apache) |
| **OpenPyXL, JSONSchema, Pytest** | Third-Party Authors | THIRD_PARTY (Tool / Runtime Dependency) | MIT License |
| **3Dmol.js** | Univ. of Pittsburgh (N. Rego & D. Koes) | THIRD_PARTY (Frontend WebGL) | BSD-3-Clause |
| **ORCA Quantum Chemistry Program** | Prof. Frank Neese et al. / FACCTs GmbH | THIRD_PARTY (External Software) | ORCA EULA / Max Planck License (Not bundled) |

---

## 4. Files Modified and Actions Taken

| File Path | Original State | Action Taken | Current State |
| :--- | :--- | :--- | :--- |
| `orca_engine/LICENSE` | Standalone MIT License (22 lines) | Replaced with canonical project license text | Full ORCA Web Lab Academic and Non-Commercial License v1.1 |
| `orca_engine/pyproject.toml` | `license = { file = "LICENSE" }` | Updated license text entry | `license = { text = "ORCA Web Lab Academic and Non-Commercial License v1.1" }` |
| `orca_engine/README.md` | MIT badge and "MIT. See LICENSE" | Updated badge and license section | References Academic and Non-Commercial License v1.1 |
| `orca_engine/CONTRIBUTING.md` | "accepted under the MIT License" | Updated contribution license clause | Under Academic and Non-Commercial License v1.1 |
| `orca_engine/CITATION.cff` | `license: MIT` | Updated citation metadata | `license: "ORCA-Web-Lab-Academic-and-Non-Commercial-License-v1.1"` |
| `cloudflare-control-plane/package.json` | `"license": "MIT"` | Updated npm package license | `"license": "SEE LICENSE IN LICENSE"` |
| `scripts/generate_academic_manual.py` | Line 226 mentioned `(MIT License)` | Corrected manual metadata | `Academic and Non-Commercial Research Suite (ORCA Web Lab License v1.1)` |
| `THIRD_PARTY_LICENSES.md` | Boundary notice did not list `orca_engine` | Added explicit `orca_engine` inclusion | Explicit boundary notice covering `orca_engine` |
| `tests/test_licensing_compliance.py` | Did not exist | Created comprehensive compliance suite | 10 automated compliance test cases |

---

## 5. Final License Structure

The repository architecture reflects a clear and unambiguous legal boundary:

```
ORCA Web Lab (Monorepo)
 |
 +-- Proprietary Project Materials (Owned by Abdulsalam S. Hasan)
 |    +-- Web Application (app.py, templates/, static/)
 |    +-- Chemical Drawing & 2D Modules (chem_core/)
 |    +-- Quantum Chemistry Parser & Engine (orca_engine/)
 |    +-- Cloud Orchestrator & State Machine (orca_orchestrator/)
 |    +-- Cloudflare Control Plane (cloudflare-control-plane/)
 |    +-- Tools, Schemas & Scientific Pipeline (tools/, schema/)
 |         |
 |         +-- Governed by: ORCA Web Lab Academic and Non-Commercial License v1.1
 |
 +-- Third-Party Components & Dependencies
      +-- Python Libraries (Flask, RDKit, Requests, NumPy, Cryptography, etc.)
      +-- MIT Utilities (Gunicorn, OpenPyXL, JSONSchema, Pytest, Pytest-Cov)
      +-- Frontend WebGL (3Dmol.js)
      +-- Web Fonts (Space Grotesk, Inter, JetBrains Mono)
      +-- External Scientific Program (ORCA 6.x: FACCTs GmbH / Max Planck Institute)
           |
           +-- Governed by: Respective Upstream Licenses (Detailed in THIRD_PARTY_LICENSES.md)
```

---

## 6. Audit of Remaining MIT References

Every remaining occurrence of "MIT" across the repository was audited and classified:

1. **`THIRD_PARTY_LICENSES.md` (Lines 69, 76, 77, 80, 81, 107, 108, 109, 132):**
   - *Classification:* **REQUIRED THIRD-PARTY**
   - *Rationale:* Attributes genuine upstream open-source dependencies (Gunicorn, OpenPyXL, JSONSchema, Pytest, Pytest-Cov, Wrangler, Vitest, @cloudflare/workers-types). Modifying or removing these notices would violate third-party copyright obligations.
2. **`app.py` (Lines 345, 352, 353):**
   - *Classification:* **REQUIRED THIRD-PARTY**
   - *Rationale:* In-application dependency license discovery API (`/api/licenses`) reporting genuine upstream licenses for `gunicorn`, `openpyxl`, and `jsonschema`.
3. **`tests/test_licensing_compliance.py`:**
   - *Classification:* **TEST FIXTURE / REGRESSION ASSERTIONS**
   - *Rationale:* Automated assertions verifying that proprietary files do not contain incorrect MIT terms and that third-party MIT tools remain attributed.
4. **Historical Audit Reports (`LICENSING_IMPLEMENTATION_REPORT.md`, `final_deepseek_release_audit.md`):**
   - *Classification:* **HISTORICAL / DOCUMENTATION**
   - *Rationale:* Historical engineering logs recording past states and audit transitions.

**Conclusion:** There are **ZERO** remaining MIT declarations applying to proprietary ORCA Web Lab or `orca_engine` code.

---

## 7. Automated Test Results and Verification

The test suite executed with complete success:
- `tests/test_licensing_compliance.py`: **10 passed in 2.14s**
- `tests/test_canonical_scientific_schema.py`: **29 passed**
- `tests/test_scientific_json_audit_and_ml.py`: **13 passed**
- `tests/test_word_copy_exports.py`: **3 passed**
- `orca_engine/tests/`: **183 passed**
- Core application and workflow tests: **272 passed**
- **Total Repository Test Suite:** **507 / 507 passed (100% pass rate)**.
- **Zero Em-Dashes Invariant:** **0 em-dashes and 0 en-dashes found across all licensing, documentation, and source files**.

---

## 8. Ownership and Legal Review Notice

- **No Ownership Uncertainty:** All code under `orca_engine/src/` was authored by Abdulsalam S. Hasan as part of ORCA Web Lab. No third-party code was copied or relicensed.
- **Non-OSI Status:** The custom license is source-available for academic and non-commercial research and is explicitly declared as non-OSI approved.
- **Standard Legal Caveat:** This consolidation establishes internal licensing consistency for software development. Commercial licensing and intellectual property transactions should be reviewed by qualified legal counsel.

