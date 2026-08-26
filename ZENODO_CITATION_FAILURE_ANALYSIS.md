# Forensic Diagnosis: Zenodo Citation Metadata Load Failure

**Author:** Metadata & Release Compliance Team  
**Date:** August 27, 2026  
**Incident Scope:** Consecutive release citation metadata load failures on Zenodo for releases `v1.0.1` and `v1.0.2`.  
**Repository:** https://github.com/abd19990044-commits/chemistry-web-lab  

---

## 1. Exact CITATION.cff Content in v1.0.2

Below is the exact `CITATION.cff` retrieved from the `v1.0.2` Git tag:

```yaml
cff-version: 1.2.0
title: "Chemistry Lab: A Web-Based Computational Chemistry Platform"
type: software
version: 1.0.2
date-released: 2026-08-27
message: "If you use Chemistry Lab in research, please cite the software and the associated scientific documentation."
authors:
  - family-names: Hasan
    given-names: Abdulsalam S.
repository-code: "https://github.com/abd19990044-commits/chemistry-web-lab"
url: "https://github.com/abd19990044-commits/chemistry-web-lab"
license: NOASSERTION
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

## 2. Exact Validation Error (Reproduced with Zenodo's Parser)

Zenodo's GitHub release webhook uses `cffconvert` (and Invenio-RDM metadata loaders) to parse `CITATION.cff`. Running the validator on the v1.0.2 file produces the following failure:

```text
jsonschema.exceptions.ValidationError: 'NOASSERTION' is not one of [
  '0BSD', 'AAL', 'Abstyles', 'Adobe-2006', 'Adobe-Glyph', 'ADSL', 'AFL-1.1',
  'AFL-1.2', 'AFL-2.0', 'AFL-2.1', 'AFL-3.0', 'Afmparse', 'AGPL-1.0',
  'AGPL-1.0-only', 'AGPL-1.0-or-later', 'AGPL-3.0', 'AGPL-3.0-only',
  'AGPL-3.0-or-later', 'Aladdin', 'AMDPLPA', 'AML', 'AMPAS', 'ANTLR-PD',
  'Apache-1.0', 'Apache-1.1', 'Apache-2.0', 'APAFML', 'APL-1.0', 'APSL-1.0',
  'APSL-1.1', 'APSL-1.2', 'APSL-2.0', 'Artistic-1.0', 'Artistic-1.0-cl8',
  'Artistic-1.0-Perl', 'Artistic-2.0', 'Bahyph', 'Barr', 'Beerware',
  'BitTorrent-1.0', 'BitTorrent-1.1', 'Borceux', 'BSD-1-Clause', 'BSD-2-Clause',
  'BSD-2-Clause-Patent', 'BSD-3-Clause', 'BSD-3-Clause-Attribution',
  'BSD-3-Clause-Clear', 'BSD-3-Clause-LBNL', 'BSD-4-Clause', 'BSD-4-Clause-UC',
  'BSL-1.0', 'BUSL-1.1', 'bzip2-1.0.6', 'CAL-1.0', 'CAL-1.0-Combined-Work-Exception',
  ...
  'Zlib', 'ZPL-1.1', 'ZPL-2.0', 'ZPL-2.1'
]

On instance:
    'NOASSERTION'
```

---

## 3. Root Cause Analysis: Why Zenodo Rejects the Release

1. **Strict SPDX Enum Constraint in CFF 1.2.0:**
   The Citation File Format specification (schema 1.2.0) explicitly defines `license` as an enumerated string type containing strictly authorized SPDX license identifier abbreviations.
2. **`NOASSERTION` is Not a Valid CFF Enum Value:**
   While `NOASSERTION` is a standard tag used in raw SPDX 2.x tag-value manifests, it is NOT an item in the CFF 1.2.0 JSON schema enum.
3. **Custom Non-Commercial Licenses in CFF:**
   Because the project's legal license is `ORCA Web Lab Academic and Non-Commercial License v1.1` (a custom source-available license, not an OSI open-source license), putting custom strings or `NOASSERTION` under `license:` causes `jsonschema` to abort with a fatal schema violation.
4. **How Zenodo Ingests Metadata:**
   Zenodo attempts to load `CITATION.cff` via `cffconvert`. When `cffconvert` throws `ValidationError`, Zenodo marks the automated ingestion as:
   `Citation metadata load failed`

---

## 4. Field-by-Field Analysis

| Field | Value in v1.0.2 | CFF 1.2.0 Status | Zenodo Compatibility | Diagnostic Verdict |
| :--- | :--- | :--- | :--- | :--- |
| `cff-version` | `1.2.0` | **VALID** | **SUPPORTED** | Correct CFF schema version. |
| `title` | `"Chemistry Lab: A Web-Based Computational Chemistry Platform"` | **VALID** | **SUPPORTED** | Clean string matching product branding. |
| `type` | `software` | **VALID** | **SUPPORTED** | Correct software citation type. |
| `version` | `1.0.2` | **VALID** | **SUPPORTED** | Matches release tag version. |
| `date-released` | `2026-08-27` | **VALID** | **SUPPORTED** | Valid ISO 8601 release date. |
| `message` | `"If you use Chemistry Lab in research..."` | **VALID** | **SUPPORTED** | Standard citation message. |
| `authors` | `[{"family-names": "Hasan", "given-names": "Abdulsalam S."}]` | **VALID** | **SUPPORTED** | Standard author structure. |
| `repository-code` | `"https://github.com/abd19990044-commits/chemistry-web-lab"` | **VALID** | **SUPPORTED** | Valid Git repository URL. |
| `url` | `"https://github.com/abd19990044-commits/chemistry-web-lab"` | **VALID** | **SUPPORTED** | Valid website/repository URL. |
| `license` | `NOASSERTION` | **INVALID** | **REJECTED** | **FATAL CAUSE OF FAILURE.** Must not be a non-SPDX string. |
| `license-url` | *Not present in v1.0.2* | **OPTIONAL / VALID** | **SUPPORTED** | Standard CFF 1.2.0 field for custom licenses. |
| `keywords` | `["ORCA", "computational chemistry", ...]` | **VALID** | **SUPPORTED** | Valid keyword array. |
| `abstract` | Multi-line string | **VALID** | **SUPPORTED** | Accurate, clean plain-text summary. |

---

## 5. Repository State of `.zenodo.json`

- **Existence:** `.zenodo.json` does NOT currently exist in the repository.
- **Why Zenodo Relied on CITATION.cff:** In the absence of `.zenodo.json`, Zenodo automatically falls back to translating `CITATION.cff` into deposit metadata.
- **Role of `.zenodo.json` for Custom Licenses:** Zenodo's native deposit metadata schema natively supports non-OSI licenses via `"license": "other-non-commercial"` and `"access_right": "open"`. Providing a `.zenodo.json` guarantees that Zenodo receives valid native deposit metadata and classifies the custom license correctly.

---

## 6. Recommended Multi-Layer Solution

To achieve 100% Zenodo compatibility while faithfully representing the proprietary academic license:

### Layer 1: Correct `CITATION.cff`
- Remove the enum-violating `license: NOASSERTION` key.
- Add `license-url: "https://github.com/abd19990044-commits/chemistry-web-lab/blob/main/LICENSE"`.
- Add author email (`abd.19990044@gmail.com`).

### Layer 2: Add Native `.zenodo.json`
- Add a canonical `.zenodo.json` file in the repository root.
- Declare `"license": "other-non-commercial"` and `"access_right": "open"` in Zenodo's native vocabulary.

---

## 7. Corrected CITATION.cff

```yaml
cff-version: 1.2.0
title: "Chemistry Lab: A Web-Based Computational Chemistry Platform"
type: software
version: 1.0.2
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

## 8. Corrected .zenodo.json

```json
{
  "title": "Chemistry Lab: A Web-Based Computational Chemistry Platform",
  "description": "Chemistry Lab is a web-based computational chemistry platform for molecular structure exploration, ORCA quantum-chemistry workflows, scientific result analysis, spectroscopy visualization, thermochemistry, and fault-tolerant execution of long-running computational jobs.",
  "version": "1.0.2",
  "publication_date": "2026-08-27",
  "upload_type": "software",
  "creators": [
    {
      "name": "Hasan, Abdulsalam S.",
      "affiliation": "Independent Researcher"
    }
  ],
  "access_right": "open",
  "license": "other-non-commercial",
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
  "related_identifiers": [
    {
      "identifier": "https://github.com/abd19990044-commits/chemistry-web-lab",
      "relation": "isSupplementTo",
      "scheme": "url"
    }
  ]
}
```

---

## 9. Experimental Validation Results

Running `cffconvert` (the official Zenodo citation parser) against the corrected `CITATION.cff` yields:

```text
=== Testing CFF Validation ===
CFF Validation: VALID (0 errors)

=== Testing Zenodo Output via cffconvert ===
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
  "version": "1.0.2"
}
```

Result: **100% PASS (Zero validation errors, clean Zenodo schema conversion).**

