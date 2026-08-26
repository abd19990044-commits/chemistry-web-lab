# Release Synchronization Difference Report

## 1. Synchronization Summary

The deployment packages have been synchronized directly from the authoritative Main Project:
- **GitHub Package (`GitHub/`):** Contains 464 total files (complete public source repository).
- **Hugging Face Package (`HuggingFace/`):** Contains 214 total files (minimal production runtime deployment).

---

## 2. GitHub Changes (`GitHub/`)

- **Files Added:** 0
- **Files Modified:** 3
- **Files Removed:** 0

### Added Files:
- None

### Modified Files:
- `RELEASE_SYNC_DIFFERENCE_REPORT.md`
- `RELEASE_SYNC_GITHUB_MANIFEST.txt`
- `RELEASE_SYNC_HUGGINGFACE_MANIFEST.txt`

### Removed Files:
- None

---

## 3. Hugging Face Changes (`HuggingFace/`)

- **Files Added:** 0
- **Files Modified:** 2
- **Files Removed:** 0

### Added Files:
- None

### Modified Files:
- `RELEASE_SYNC_GITHUB_MANIFEST.txt`
- `RELEASE_SYNC_HUGGINGFACE_MANIFEST.txt`

### Removed Files:
- None

---

## 4. Architectural Differences Between GitHub/ and HuggingFace/

| Component / Layer | Included in GitHub/ | Included in HuggingFace/ | Architectural Rationale |
| :--- | :--- | :--- | :--- |
| **Core Web App (`app.py`, `templates/`, `static/`)** | YES | YES | Essential runtime user interface and API endpoints. |
| **2D Chemical Drawing (`chem_core/`)** | YES | YES | In-browser SVG/PNG molecular and reaction rendering. |
| **Quantum Engine (`orca_engine/src/`)** | YES | YES | Output parsing, thermochemistry, and spectroscopy engine. |
| **Cloud Orchestrator (`orca_orchestrator/`)** | YES | YES | Kaggle runner and Cloudflare background state machine. |
| **Canonical Schemas (`schema/`)** | YES | YES | JSON schema validation for scientific exports. |
| **Scientific Data & Tools (`tools/`, `data/`)** | YES | YES | Machine learning JSONL exporters and peak databases. |
| **Test Suites (`tests/`, `orca_engine/tests/`)** | YES | NO | Developer testing suites omitted from production Docker image. |
| **Test Fixtures & Corpus (`data/orcafile/`)** | YES | NO | Large testing logs omitted from lightweight Space container. |
| **Cloudflare Control Plane Source** | YES | NO | Deployed separately to Cloudflare Workers edge runtime. |
| **Engineering Reports & Audit Logs** | YES | NO | Development and legal audit documentation omitted from runtime. |
| **Space Docker Configuration (`Dockerfile`)** | YES | YES | Container definition for Hugging Face Spaces deployment. |
