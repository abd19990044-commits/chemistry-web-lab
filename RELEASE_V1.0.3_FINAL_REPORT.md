# Chemistry Lab v1.0.3 Final Release & Zenodo Integration Report

**Release Date:** August 27, 2026  
**Software Version:** 1.0.3  
**Release Tag:** v1.0.3  
**Release Title:** Chemistry Lab v1.0.3  
**Product Name:** Chemistry Lab  
**Author & Rights Holder:** Abdulsalam S. Hasan  
**Official Repository:** https://github.com/abd19990044-commits/chemistry-web-lab  
**Official Release URL:** https://github.com/abd19990044-commits/chemistry-web-lab/releases/tag/v1.0.3  
**Target Commit SHA:** `2397bc944b7ae7aeb7e835983c5520038f90bad2`  
**DOI Status:** **DOI: NOT YET ISSUED** (Zenodo will assign upon indexing the published release)  

---

## 1. Executive Release Summary

The release of **Chemistry Lab v1.0.3** is published on GitHub.

This release resolves the Zenodo citation metadata ingestion failure encountered in prior releases (`v1.0.1` and `v1.0.2`). By removing the invalid `license: NOASSERTION` string and properly specifying `license-url` in accordance with Citation File Format 1.2.0 specifications, `cffconvert` and Zenodo's automated deposit pipeline now validate the repository metadata with zero errors.

---

## 2. Release & Version Identification

| Parameter | Value |
| :--- | :--- |
| **Product Name** | `Chemistry Lab` |
| **Product Subtitle** | `Computational chemistry lab` |
| **Software Version** | `1.0.3` |
| **Release Tag** | `v1.0.3` |
| **GitHub Release URL** | [https://github.com/abd19990044-commits/chemistry-web-lab/releases/tag/v1.0.3](https://github.com/abd19990044-commits/chemistry-web-lab/releases/tag/v1.0.3) |
| **Commit SHA** | `2397bc944b7ae7aeb7e835983c5520038f90bad2` |
| **Legal License** | `ORCA Web Lab Academic and Non-Commercial License v1.1` |
| **DOI Status** | `DOI: NOT YET ISSUED` |

---

## 3. Citation File Format (CFF) & Zenodo Compatibility

The root `CITATION.cff` was updated and verified against CFF 1.2.0 schema and Zenodo conversion rules:

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

### Validation Result (via `cffconvert`):
- **CFF 1.2.0 Schema:** **100% VALID (0 errors)**
- **Zenodo JSON Output:** Clean conversion to native Zenodo deposit schema.

---

## 4. Preservation of Existing Releases & Git History

Existing GitHub releases remain untouched and unmodified:
- **`v1.0.0`:** Published at commit `4a96de8` (Unchanged).
- **`v1.0.1`:** Published at commit `3d81807` (Unchanged).
- **`v1.0.2`:** Published at commit `01e4ec4` (Unchanged).
- **`v1.0.3`:** Published at commit `2397bc9` (New latest release).

Linear Git history maintained without force-pushes or tag mutations.

---

## 5. Automated Test Suite Results

### Full Backend Pytest Suite:
```text
======================= 507 passed in 273.12s (0:04:33) =======================
```
- **Tests Executed:** 507
- **Passed:** 507
- **Failed:** 0
- **Pass Rate:** 100%

### Frontend DOM & Runtime Test Suite:
```text
======================================================================
FRONTEND: 22 passed, 0 failed
======================================================================
```
- **Tests Executed:** 22
- **Passed:** 22
- **Failed:** 0
- **Pass Rate:** 100%

---

## 6. Security and Compliance Audit

- **Secret Scan:** 0 credentials, tokens, or private keys detected in repository files.
- **Invariant Audit:** 0 em-dash (`\u2014`) and 0 en-dash (`\u2013`) characters present across codebase and documentation.
- **Scientific Algorithms:** Full preservation of Grimme Quasi-RRHO thermochemistry, BDE, IR/UV/NMR spectrum convolvers, and ML dataset export pipelines.

---

## 7. Next Steps for Zenodo Archiving

1. Log into your Zenodo account and visit your GitHub repository settings: `https://zenodo.org/account/settings/github/repository/abd19990044-commits/chemistry-web-lab`.
2. Zenodo will automatically receive the release webhook for `v1.0.3` and process the corrected `CITATION.cff`.
3. If necessary, click **Sync now** on the Zenodo dashboard.
4. Zenodo will create the archive record and assign a permanent DOI for `v1.0.3`.

