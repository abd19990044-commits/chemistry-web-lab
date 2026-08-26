# Chemistry Lab v1.0.3 Zenodo Metadata & Release Report

**Release Date:** August 27, 2026  
**Software Version:** 1.0.3  
**Release Tag:** v1.0.3  
**Product Name:** Chemistry Lab  
**Author & Rights Holder:** Abdulsalam S. Hasan  
**Repository:** https://github.com/abd19990044-commits/chemistry-web-lab  
**Target Branch:** main  

---

## 1. Original v1.0.2 CITATION.cff Status & Root Cause of Failure

In the previous release (`v1.0.2`), Zenodo detected the release webhook but failed with:
`Citation metadata load failed`

The forensic analysis identified that `CITATION.cff` contained:
```yaml
license: NOASSERTION
```
While `NOASSERTION` is recognized in certain SPDX tag-value files, it is **not** an authorized value in the CFF 1.2.0 schema enum. The official parser `cffconvert` used by Zenodo's ingestion pipeline threw `jsonschema.exceptions.ValidationError`, causing Zenodo's metadata parser to crash and reject the deposit.

---

## 2. Corrected CITATION.cff for v1.0.3

In CFF 1.2.0, non-SPDX proprietary and academic licenses are properly declared using `license-url` instead of an invalid `license` enum value:

```yaml
cff-version: 1.2.0
title: "Chemistry Lab: A Web-Based Computational Chemistry Platform"
type: software
version: 1.0.3
date-released: 2026-08-27
message: "If you use Chemistry Lab in research, please cite the software and the associated scientific documentation."
authors:
  - family-names: Hasan
    given-names: Abdulsalam S.
    email: abd.19990044@gmail.com
repository-code: "https://github.com/abd19990044-commits/chemistry-web-lab"
url: "https://github.com/abd19990044-commits/chemistry-web-lab"
license-url: "https://github.com/abd19990044-commits/chemistry-web-lab/blob/main/LICENSE"
keywords:
  - ORCA
  - computational chemistry
  - quantum chemistry
  - Kaggle
  - fault tolerance
  - checkpointing
  - workflow orchestration
  - RDKit
abstract: >-
  Chemistry Lab is a web-based research-software platform for molecular
  structure exploration, ORCA input generation, and fault-tolerant execution
  of long-running ORCA calculations through user-owned Kaggle notebooks.
  The orchestration layer uses an explicit finite-state machine, verified
  checkpoints, idempotent operations, leases, reconciliation, and watchdog
  recovery to survive hosted-notebook session limits and web-service restarts.
```

---

## 3. CFF Validation Output (via `cffconvert`)

Validation with the official `cffconvert` tool confirms 100% compliance:

```text
--- Validating root CITATION.cff ---
Root CITATION.cff: VALID!
Zenodo JSON output:
{
  "creators": [
    {
      "name": "Hasan, Abdulsalam S."
    }
  ],
  "description": "Chemistry Lab is a web-based research-software platform for molecular structure exploration, ORCA input generation, and fault-tolerant execution of long-running ORCA calculations through user-owned Kaggle notebooks. The orchestration layer uses an explicit finite-state machine, verified checkpoints, idempotent operations, leases, reconciliation, and watchdog recovery to survive hosted-notebook session limits and web-service restarts.",
  "keywords": [
    "ORCA",
    "computational chemistry",
    "quantum chemistry",
    "Kaggle",
    "fault tolerance",
    "checkpointing",
    "workflow orchestration",
    "RDKit"
  ],
  "publication_date": "2026-08-27",
  "title": "Chemistry Lab: A Web-Based Computational Chemistry Platform",
  "version": "1.0.3"
}

--- Validating orca_engine/CITATION.cff ---
orca_engine/CITATION.cff: VALID!
```

---

## 4. Zenodo Compatibility & `.zenodo.json` Status

- **Status of `.zenodo.json`:** `.zenodo.json` is not present and is not required.
- **Conversion Verification:** Root `CITATION.cff` converts cleanly into standard Zenodo deposit JSON with valid creators, description, keywords, publication date, title, and version.

---

## 5. Licensing Representation

- **Proprietary Software License:** `ORCA Web Lab Academic and Non-Commercial License v1.1` (unchanged, strictly preserved).
- **No False OSI Claim:** The project is not incorrectly marked as MIT or OSI open-source.
- **Third-Party Open Source Software:** All third-party dependencies remain documented in `THIRD_PARTY_LICENSES.md`.

---

## 6. Version Consistency Results

All release metadata files in the current main branch have been synchronized to version `1.0.3`:
- `CITATION.cff`: `version: 1.0.3`
- `PROJECT_METADATA.json`: `"version": "1.0.3"`
- `pyproject.toml`: `version = "1.0.3"`
- `orca_engine/pyproject.toml`: `version = "1.0.3"`
- `orca_engine/src/orca_engine/__init__.py`: `__version__ = "1.0.3"`
- `orca_engine/CITATION.cff`: `version: 1.0.3`
- `app.py`: `/api/license` (`"version": "1.0.3"`) and `/api/health` (`"version": "1.0.3"`)
- `templates/index.html`: Citation modal examples updated to `1.0.3`
- `README.md`: Citation block updated to `1.0.3`
- `cloudflare-control-plane/package.json`: `"version": "1.0.3"`
- `cloudflare-control-plane/src/index.ts`: `"version": "1.0.3"`

---

## 7. Security and Invariant Audit

- **Secret Scan:** 0 real secrets detected across tracked files.
- **Invariant Audit:** 0 em-dash (`\u2014`) and 0 en-dash (`\u2013`) characters present across modified code and documentation.

---

## 8. Exact Full Test Suite Results

### Full Pytest Suite (`pytest tests/ orca_engine/tests/`):
```text
======================= 507 passed in 273.12s (0:04:33) =======================
```
- **Total Tests:** 507
- **Passed:** 507
- **Failed:** 0
- **Pass Rate:** 100%

### Frontend Test Suite (`python tests/test_frontend.py`):
```text
======================================================================
FRONTEND: 22 passed, 0 failed
======================================================================
```
- **Total Tests:** 22
- **Passed:** 22
- **Failed:** 0
- **Pass Rate:** 100%

---

## 9. Immutability of Existing Releases

- **`v1.0.0`:** Unmodified, preserved at original release commit.
- **`v1.0.1`:** Unmodified, preserved at original release commit.
- **`v1.0.2`:** Unmodified, preserved at original release commit (`01e4ec4`).
- **No Force-Push / History Rewrite:** Strict linear Git commit history maintained.

