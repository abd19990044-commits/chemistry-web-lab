# IUPAC AUTOMATIC DISPLAY AND COPY FIX REPORT

## 1. Executive Summary

This report documents the forensic analysis, remediation, and verification of automatic IUPAC systematic chemical name retrieval and display in the ORCA Web Lab Molecule Explorer interface.

Previously, looking up a molecule retrieved chemical structures and properties, but required users to manually click an obsolete "Show IUPAC name" button, which frequently failed with an error toast. The interface has now been refactored to retrieve authoritative IUPAC systematic names automatically during compound lookup, render them directly in the core property list, enable inline one-click copying and text selection, and eliminate all obsolete toggle buttons and duplicate network requests.

---

## 2. Root Cause Analysis

Forensic inspection of `static/js/kaggle_credentials.js`, `static/js/app.js`, `app.py`, and `chem_core.py` identified the following root causes:

1. **Obsolete Client-Side DOM Injection:**
   `static/js/kaggle_credentials.js` previously contained an `installExplorerActions()` hook that dynamically created a `#explorer-iupac-toggle` button labeled "Show IUPAC name" and an empty `#explorer-iupac-box` container with class `hidden`.
2. **Redundant and Fragile Second Network Request:**
   When the user clicked "Show IUPAC name", `kaggle_credentials.js` dispatched a second, redundant `POST /api/compound` network request. If PubChem returned 503 ServerBusy or if properties were synthesized via SMILES fallback without resolving systematic nomenclature, the handler threw `"An IUPAC name is not available for this PubChem record."`
3. **Template / Property List Disconnect:**
   In `templates/index.html`, `#explorer-iupac-name` was placed as a detached paragraph rather than a native member of the definition list `.prop-list` alongside Molecular formula, Molecular weight, and Canonical SMILES.

---

## 3. PubChem Property and Authority Hierarchy

When resolving molecule properties, the backend queries PubChem PUG REST property endpoints:
- Primary property query: `CanonicalSMILES,IUPACName,MolecularFormula,MolecularWeight,Title/JSON`
- Primary field: `IUPACName` (standard PubChem PropertyTable key)
- Fallback resolver: National Cancer Institute (NIH) Chemical Identifier Resolver (`https://cactus.nci.nih.gov/chemical/structure/{query}/iupac_name`) and Cambridge OPSIN parser.
- Authoritative hierarchy:
  1. Systematic IUPAC name returned in PubChem `PropertyTable.Properties[0].IUPACName`.
  2. NIH CIR `/iupac_name` authoritative resolution.
  3. Direct systematic input string if the user entered a valid IUPAC name.
  4. Non-error unavailable state (`"Unavailable for this PubChem record"`) if no systematic name is registered.

---

## 4. Backend and API Changes

### A. `chem_core.py`
- Implemented `fetch_iupac_name(query: str, smiles: str | None = None) -> str` to query authoritative NIH CIR endpoints when PubChem is busy or lacks IUPAC entries.
- Enhanced `fetch_pubchem_properties(name: str)` to verify that `IUPACName` is populated, falling back to `fetch_iupac_name` before returning.
- Updated SMILES fallback synthesis to resolve IUPAC systematic names via `fetch_iupac_name`.

### B. `app.py`
- Updated `/api/compound` endpoint to ensure `props` are fetched for all query types (common name, systematic name, SMILES, InChI).
- Optimized Wikipedia summary retrieval (`fetch_wikipedia_summary`) to query the input query and compound title first, eliminating 404 delays on Wikipedia.
- Guaranteed `iupac_name` field in `/api/compound` response payload:
  ```python
  iupac_name = (props or {}).get("IUPACName")
  if not iupac_name:
      iupac_name = core.fetch_iupac_name(query, smiles) or (query if core.is_iupac_name(query) else "")
  ```

---

## 5. Frontend and UI/UX Refactoring

### A. Template Architecture (`templates/index.html`)
- Integrated IUPAC name into `.prop-list`:
  ```html
  <dl class="prop-list">
    <div><dt>Molecular formula</dt><dd id="explorer-formula">-</dd></div>
    <div><dt>Molecular weight</dt><dd id="explorer-weight">-</dd></div>
    <div id="explorer-iupac-row">
      <dt>IUPAC name</dt>
      <dd id="explorer-iupac" class="iupac-dd">
        <span id="explorer-iupac-val" class="iupac-text">-</span>
        <button type="button" id="explorer-copy-iupac" class="btn-copy-inline hidden" title="Copy IUPAC name" aria-label="Copy IUPAC name">📋</button>
      </dd>
    </div>
    <div><dt>Canonical SMILES</dt><dd id="explorer-smiles" class="mono">-</dd></div>
  </dl>
  ```

### B. Styling & Responsive Design (`static/css/style.css`)
- Styled `.iupac-dd` with `display: inline-flex`, `max-width: 72%`, `word-break: break-word`, and `overflow-wrap: anywhere` to handle long IUPAC names cleanly without expanding the card or causing horizontal overflow.
- Added `.btn-copy-inline` with subtle hover transitions (`var(--teal)` glow).
- Added `.iupac-unavailable` style with subtle muted italics for molecules without registered systematic names.

### C. Application Logic (`static/js/app.js`)
- Automatic display: Populates `#explorer-iupac-val` immediately on compound resolution.
- Clipboard copy: Integrated `#explorer-copy-iupac` click handler with fallback to `document.execCommand("copy")` and notification toast `"IUPAC name copied."`.
- Reset on input: Resets `#explorer-iupac-val` to `"-"` and hides the copy button when the query input changes.

### D. Deprecation & Cleanup (`static/js/kaggle_credentials.js`)
- Completely removed `#explorer-iupac-toggle` button creation.
- Completely removed `#explorer-iupac-box` and its secondary fetch event listeners.

---

## 6. Verification and Test Results

### A. Compound Test Suite
| Compound | Query Type | Formula | MW (g/mol) | Retrieved IUPAC Name | Auto-Displayed | Copyable |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Naproxen** | Common name | C14H14O3 | 230.263 | 2-(6-methoxynaphthalen-2-yl)propanoic acid | YES | YES |
| **Paracetamol** | Common name | C8H9NO2 | 151.165 | N-(4-Hydroxyphenyl)acetamide | YES | YES |
| **Aspirin** | Common name | C9H8O4 | 180.159 | 2-acetyloxybenzoic acid | YES | YES |
| **Benzene** | Common name | C6H6 | 78.114 | BENZENE | YES | YES |
| **Atorvastatin** | Long name | C33H35FN2O5 | 558.65 | (3R,5R)-7-[2-(4-fluorophenyl)-3-phenyl-4-(phenylcarbamoyl)-5-propan-2-ylpyrrol-1-yl]-3,5-dihydroxyheptanoic acid | YES (wrapped) | YES |

### B. Automated Test Suites
- **Frontend Test Suite (`python tests/test_frontend.py`):**
  - **22/22 checks passed (100%)**
  - 0 severe browser console errors.
- **Full Backend & Engine Test Suite (`pytest -q`):**
  - **389/389 tests passed (100%) in 305.86s**

### C. Real Browser Chrome CDP Verification
- Verified on Google Chrome across Desktop (1920x1080) and Mobile (390x844) viewports:
  - Automatic IUPAC display on initial lookup.
  - Zero "Show IUPAC name" buttons present in the DOM.
  - Correct word wrapping on mobile viewports for long chemical strings.
  - 0 console exceptions.

---

## 7. Remaining Limitations

1. Inorganic compounds or complex mixtures without standardized IUPAC systematic names display `"Unavailable for this PubChem record"`.
2. Offline environments without local IUPAC dictionaries rely on the input string when systematic nomenclature regex is detected.
