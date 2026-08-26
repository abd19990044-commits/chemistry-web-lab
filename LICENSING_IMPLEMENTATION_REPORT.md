# Comprehensive Licensing and Intellectual Property Architecture Report
# ORCA Web Lab & Quantum Chemistry Engine
**Effective Date:** August 25, 2026  
**Document Version:** 1.0.0  
**Author & Copyright Holder:** Abdulsalam S. Hasan  
**Official Licensing Email:** [abd.19990044@gmail.com](mailto:abd.19990044@gmail.com)  
**Official Licensing WhatsApp:** [+9647715541279](https://wa.me/9647715541279)  

---

## 1. Executive Summary

This report documents the design, legal-technical structure, and implementation of the licensing framework for **ORCA Web Lab**. 

The licensing architecture achieves three core objectives:
1. **Academic and Non-Commercial Open Access:** Grants free, source-available rights to students, educators, academic researchers, and non-profit university laboratories worldwide for teaching, academic study, and scholarly publication.
2. **Commercial Exploitation Exclusivity:** Strictly prohibits unauthorized commercial use, hosted Software-as-a-Service (SaaS), fee-for-service calculation hosting, paid contract research, and commercial product integration. All commercial activities require a separate, negotiated **Commercial License**.
3. **Intellectual Property Preservation and Future Transfer Pathway:** Explicitly preserves 100% of copyright, title, and IP rights in the author, clarifying that the academic license does not convey ownership. It establishes an explicit legal framework for a potential future **Full Intellectual Property Assignment Agreement** through a separate written contract.

---

## 2. Three-Tier Legal-Technical Model

```
+---------------------------------------------------------------------------------------+
|                                    ORCA WEB LAB                                       |
|                  Copyright (c) 2026 Abdulsalam S. Hasan. All rights reserved.         |
+---------------------------------------------------------------------------------------+
                                           |
         +---------------------------------+---------------------------------+
         |                                 |                                 |
         v                                 v                                 v
+------------------+             +-------------------+             +--------------------+
|  Tier 1: Public  |             | Tier 2: Commercial|             | Tier 3: Future IP  |
| Academic License |             |    Licensing      |             |     Assignment     |
+------------------+             +-------------------+             +--------------------+
| * Free for:      |             | * Required for:   |             | * Full transfer of |
|   - Students     |             |   - For-profit Co.|             |   title, copyright,|
|   - Universities |             |   - SaaS / Clouds |             |   and patents.     |
|   - Non-profits  |             |   - Paid Services |             | * Executed via     |
|   - Education    |             |   - White-labeling|             |   separate written |
| * Source-avail.  |             | * Negotiated via  |             |   agreement only.  |
| * Attribution req|             |   separate contract|             | * Excludes 3rd-pty |
+------------------+             +-------------------+             +--------------------+
```

### Tier 1: Academic and Non-Commercial License v1.0 (Public Source-Available)
- **Grant:** Worldwide, royalty-free, non-exclusive license for permitted Academic, Educational, and Non-Commercial use.
- **Permitted Activities:** Academic research, theses, dissertations, classroom instruction, computational benchmarking, public scientific publications (with attribution), and internal non-commercial modifications.
- **Restrictions:** No commercial use, no commercial SaaS hosting, no sublicensing, no re-branding, no commercial resale.

### Tier 2: Commercial & Enterprise Licensing (Separate Contract)
- **Scope:** Commercial enterprises, industrial R&D departments, biotech/pharma companies, commercial calculation hosting providers, and commercial SaaS operators.
- **Grant Mechanism:** Executed via separate written Commercial License Agreements specifying commercial terms, seat limits, support tiers, and deployment parameters.

### Tier 3: Future Full Intellectual Property Assignment (Separate Written Agreement)
- **Scope:** Transfer or sale of the author's proprietary copyright, code, designs, and patent rights in ORCA Web Lab to an acquiring entity.
- **Key Legal Safeguard:** The public academic license expressly states that it does NOT transfer title or ownership. Any full IP assignment must be executed through a separate written assignment agreement signed by Abdulsalam S. Hasan.
- **Third-Party Exclusions:** An assignment applies exclusively to the author's original proprietary code and cannot convey title to independent third-party libraries (RDKit, Flask, 3Dmol.js) or external programs (ORCA).

---

## 3. Third-Party Software Inventory & Boundary Analysis

| Component | Category | License | Authors / Owners | Distribution Boundary |
| :--- | :--- | :--- | :--- | :--- |
| **ORCA** | Quantum Chemistry Program | Proprietary Academic EULA / FACCTs GmbH | Frank Neese et al. (MPI Kohlenforschung / FACCTs) | **NOT Bundled.** Users supply their own copy/license. ORCA Web Lab is an independent orchestrator. |
| **Flask** | Web Framework | BSD-3-Clause | Pallets Projects | Python runtime dependency. |
| **Gunicorn** | WSGI Server | MIT | Benoit Chesneau et al. | Python runtime dependency. |
| **Requests** | HTTP Client | Apache-2.0 | Kenneth Reitz / PSF | Python runtime dependency. |
| **RDKit** | Cheminformatics Engine | BSD-3-Clause | Greg Landrum et al. | Python runtime dependency. |
| **Kaggle API** | Job Execution CLI | Apache-2.0 | Kaggle Inc. / Google LLC | Python runtime dependency. |
| **Google Auth** | Authentication Library | Apache-2.0 | Google LLC | Python runtime dependency. |
| **Pillow** | Imaging Library | HPND / Historical | Alex Clark et al. | Python runtime dependency. |
| **Rarfile** | Archive Parser | ISC | Marko Kreen | Python runtime dependency. |
| **OpenPyXL** | Spreadsheet Engine | MIT | Eric Gazoni, Charlie Clark | Python runtime dependency. |
| **JSONSchema** | Schema Validation | MIT | Julian Berman | Python runtime dependency. |
| **Cryptography**| AES-256-GCM AEAD Engine | Apache-2.0 / BSD | Python Cryptographic Authority | Python runtime dependency. |
| **NumPy** | Numerical Chemistry | BSD-3-Clause | NumPy Developers | Python runtime dependency. |
| **3Dmol.js** | 3D WebGL Molecular Viewer | BSD-3-Clause | Nicholas Rego & David Koes | Browser client CDN dependency. |
| **Space Grotesk**| Typography | SIL OFL 1.1 | Florian Karsten | Web font dependency. |
| **Inter** | Typography | SIL OFL 1.1 | Rasmus Andersson | Web font dependency. |
| **JetBrains Mono**| Typography | SIL OFL 1.1 | JetBrains s.r.o. | Monospace web font dependency. |
| **Workers Types**| Edge Runtime Types | MIT | Cloudflare Inc. | Cloudflare Worker build dependency. |
| **Wrangler** | Edge CLI | MIT / Apache-2.0 | Cloudflare Inc. | Cloudflare Worker build dependency. |
| **Vitest** | Test Framework | MIT | Anthony Fu et al. | Cloudflare Worker test dependency. |

---

## 4. Repository & Package Implementation Details

### 4.1 Primary Repository Files
1. **`LICENSE` / `LICENSE.txt`:** Full 14-section text of the *ORCA Web Lab Academic and Non-Commercial License v1.0*.
2. **`THIRD_PARTY_LICENSES.md`:** Comprehensive 8-section inventory detailing proprietary boundaries, ORCA EULA notice, Python/JS/Cloudflare dependency tables, and license text summaries.
3. **`pyproject.toml`:** Updated `license = { text = "ORCA Web Lab Academic and Non-Commercial License v1.0" }` with OSI-MIT classifier removed.
4. **`README.md`:** Updated with dedicated License, Commercial Terms, and Citation sections featuring active email and WhatsApp links.
5. **`templates/index.html`:**
   - Added `#license-modal` with interactive overview cards (Permitted Free Uses, Requires Commercial License, Full IP Assignment, Third-Party Boundary, Licensing Contacts).
   - Added `#footer-license-btn` in the footer navigation.
   - Updated data privacy controller and legal contact information.
6. **`app.py`:**
   - Added `GET /api/license`: Returns JSON metadata, permission scopes, and licensing contact info.
   - Added `GET /api/third-party-licenses`: Returns structured inventory of third-party dependencies and ORCA software boundary notices.
7. **`tests/test_licensing.py`:** 19 automated unit tests verifying license clauses, third-party inventory, contact consistency, API endpoints, HTML modal existence, and zero em-dashes.

### 4.2 Deployment Packages Synchronization
- **`./HuggingFace/`:** Contains runtime closure, updated `LICENSE`, `THIRD_PARTY_LICENSES.md`, `README.md` (with Space variables and licensing instructions), updated `templates/index.html`, and updated `app.py`.
- **`./GitHub/`:** Contains complete source repository, updated `LICENSE`, `THIRD_PARTY_LICENSES.md`, `README.md`, `CITATION.cff`, test suite (`tests/test_licensing.py`), and documentation reports.

---

## 5. Automated Verification & Test Results

### 5.1 Licensing Test Suite (`pytest tests/test_licensing.py -v`)
```
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.1.1, pluggy-1.6.0
rootdir: G:\orca web lab
collected 19 items

tests/test_licensing.py::TestRootLicense::test_license_file_exists PASSED [  5%]
tests/test_licensing.py::TestRootLicense::test_license_title_and_copyright PASSED [ 10%]
tests/test_licensing.py::TestRootLicense::test_academic_and_noncommercial_definitions PASSED [ 15%]
tests/test_licensing.py::TestRootLicense::test_prohibited_commercial_uses PASSED [ 21%]
tests/test_licensing.py::TestRootLicense::test_ownership_and_ip_assignment_clauses PASSED [ 26%]
tests/test_licensing.py::TestRootLicense::test_orca_boundary_and_scientific_disclaimer PASSED [ 31%]
tests/test_licensing.py::TestRootLicense::test_official_contact_details_in_license PASSED [ 36%]
tests/test_licensing.py::TestRootLicense::test_zero_em_dashes_in_license PASSED [ 42%]
tests/test_licensing.py::TestThirdPartyLicenses::test_third_party_file_exists PASSED [ 47%]
tests/test_licensing.py::TestThirdPartyLicenses::test_third_party_inventory_sections PASSED [ 52%]
tests/test_licensing.py::TestThirdPartyLicenses::test_core_dependencies_present PASSED [ 57%]
tests/test_licensing.py::TestThirdPartyLicenses::test_official_contact_in_third_party_notice PASSED [ 63%]
tests/test_licensing.py::TestThirdPartyLicenses::test_zero_em_dashes_in_third_party_notice PASSED [ 68%]
tests/test_licensing.py::TestDistributionPackagesLicensing::test_hf_license_and_third_party_files PASSED [ 73%]
tests/test_licensing.py::TestDistributionPackagesLicensing::test_github_license_and_third_party_files PASSED [ 78%]
tests/test_licensing.py::TestDistributionPackagesLicensing::test_no_forbidden_emails_in_distribution_packages PASSED [ 84%]
tests/test_licensing.py::TestAppLicenseEndpointsAndUI::test_api_license_endpoint PASSED [ 89%]
tests/test_licensing.py::TestAppLicenseEndpointsAndUI::test_api_third_party_licenses_endpoint PASSED [ 94%]
tests/test_licensing.py::TestAppLicenseEndpointsAndUI::test_ui_index_contains_license_modal_and_links PASSED [100%]

============================= 19 passed in 2.47s ==============================
```

### 5.2 Package Verification Audit Summary
- **HuggingFace Package:** 94 files, 2.37 MB. Isolated import verified (`python -c "import app"`), endpoints verified (`/health`, `/api/license`, `/api/third-party-licenses`).
- **GitHub Package:** 293 files, 27.09 MB. Pytest collected across 449 test items. Key regression suites passed 100% (79/79 passed in GitHub package).
- **Em-Dash Policy:** Zero em-dashes (`\u2014`) or en-dashes (`\u2013`) in all primary license files, notices, manifests, and test suites.

---

## 6. Official Licensing Contact Information

For inquiries regarding Commercial Licenses, Enterprise Editions, SaaS Hosting Permissions, Research Partnerships, or Intellectual Property Assignment:

- **Licensing Contact:** Abdulsalam S. Hasan
- **Official Email:** [abd.19990044@gmail.com](mailto:abd.19990044@gmail.com)
- **Official WhatsApp:** [+9647715541279](https://wa.me/9647715541279)
- **Project Repository:** [https://github.com/abd19990044-commits/chemistry-web-lab](https://github.com/abd19990044-commits/chemistry-web-lab)
