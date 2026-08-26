# Release Synchronization Difference Report

## 1. Synchronization Summary

The deployment packages have been synchronized directly from the authoritative Main Project:
- **GitHub Package (`GitHub/`):** Contains 464 total files (complete public source repository).
- **Hugging Face Package (`HuggingFace/`):** Contains 214 total files (minimal production runtime deployment).

---

## 2. GitHub Changes (`GitHub/`)

- **Files Added:** 72
- **Files Modified:** 14
- **Files Removed:** 2

### Added Files:
- `.gitattributes`
- `AGENTS.md`
- `ANALYZER_TO_CANONICAL_REFINEMENT_REPORT.md`
- `AUDIT_AND_REPAIR_LOG.md`
- `AUDIT_RECONCILIATION.md`
- `CANONICAL_DATA_MODEL_REFINEMENT_REPORT.md`
- `CANONICAL_DATA_MODEL_REMEDIATION_REPORT.md`
- `CLOUDFLARE_RECOVERY_FORENSIC_AUDIT.md`
- `CLOUDFLARE_RECOVERY_IMPLEMENTATION_REPORT.md`
- `CLOUDFLARE_REMEDIATION_REPORT.md`
- `CLOUDFLARE_SCHEMA_AUDIT.md`
- `DEPLOYMENT_PACKAGE_SPLIT_REPORT.md`
- `FINAL_PRODUCTION_READINESS_REPORT.md`
- `FINAL_RELEASE_READINESS_REPORT.md`
- `FINAL_REPAIR_VERIFICATION_REPORT.md`
- `GITHUB_UPLOAD_REPORT.md`
- `HEARTBEAT.md`
- `IDENTITY.md`
- `IR_AND_ORCA_UPLOAD_CLEANUP_REPORT.md`
- `IR_ASSIGNMENT_SCIENTIFIC_FORENSIC_AUDIT.md`
- `IR_SPECTRAL_ASSIGNMENT_FORENSIC_REPORT.md`
- `IUPAC_AUTO_DISPLAY_FIX_REPORT.md`
- `KAGGLE_ENCRYPTED_CREDENTIAL_PERSISTENCE_REPORT.md`
- `LICENSING_IMPLEMENTATION_REPORT.md`
- `LICENSING_LEGAL_REVIEW_AND_REVISION_REPORT.md`
- `NMR_ANALYZER_IMPLEMENTATION_REPORT.md`
- `NMR_REFERENCE_RECONCILIATION_REPORT.md`
- `ORCA_ENGINE_LICENSE_CONSOLIDATION_REPORT.md`
- `ORCA_SOURCE_AND_RESULTS_UPDATE_REPORT.md`
- `PRODUCTION_BLANK_PAGE_FORENSIC_REPORT.md`
- `PRODUCTION_HARDENING_VERIFICATION_REPORT.md`
- `PRODUCTION_STARTUP_HANG_FORENSIC_REPORT.md`
- `REACTION_DRAWING_VISUAL_REVIEW_REPORT.md`
- `RELEASE_FINALIZATION_REPORT.md`
- `RELEASE_SYNC_DIFFERENCE_REPORT.md`
- `RELEASE_SYNC_GITHUB_MANIFEST.txt`
- `RELEASE_SYNC_HUGGINGFACE_MANIFEST.txt`
- `REPOSITORY_FORENSIC_REPORT.md`
- `SCIENTIFIC_FORENSIC_AUDIT.md`
- `SCIENTIFIC_JSON_ML_DATASET_REFACTOR_REPORT.md`
- `SCIENTIFIC_RESULT_DURABILITY_REPORT.md`
- `SOUL.md`
- `TEST_COVERAGE_AND_GAPS.md`
- `TOOLS.md`
- `UFF_REPAIR_AND_VALIDATION_REPORT.md`
- `UI_SPECTRA_ENHANCEMENTS_REPORT.md`
- `USER.md`
- `cloudflare_worker_deepseek_forensic_audit.md`
- `data/examples/canonical_h2o_numfreq.json`
- `data/examples/training_dataset_h2o.jsonl`
- `data/examples/training_manifest_h2o.json`
- `deepseek_independent_audit.md`
- `final_cloudflare_deepseek_audit.md`
- `final_comprehensive_deepseek_audit.md`
- `final_deepseek_comprehensive_production_audit.md`
- `final_deepseek_forensic_audit.md`
- `final_deepseek_forensic_production_audit.md`
- `final_deepseek_post_remediation_audit.md`
- `final_deepseek_release_audit.md`
- `final_orca_web_lab_production_forensic_audit.md`
- `final_post_repair_deepseek_audit.md`
- `nmr_deepseek_forensic_audit.md`
- `sitecustomize.py`
- `tests/fixtures/ORCA_Calculation_quantum_analysis_3.json`
- `tests/fixtures/orca_parsed_data_1.json`
- `tests/fixtures/orca_parsed_data_3_2.json`
- `tests/fixtures/orca_parsed_data_small.json`
- `tests/test_licensing_compliance.py`
- `tests/test_scientific_json_audit_and_ml.py`
- `tests/test_word_copy_exports.py`
- `tools/sync_deploy_manager.py`
- `usercustomize.py`

### Modified Files:
- `.env.example`
- `CITATION.cff`
- `THIRD_PARTY_LICENSES.md`
- `cloudflare-control-plane/package.json`
- `orca_engine/CITATION.cff`
- `orca_engine/CONTRIBUTING.md`
- `orca_engine/LICENSE`
- `orca_engine/README.md`
- `orca_engine/pyproject.toml`
- `schema/canonical_scientific.schema.json`
- `scripts/generate_academic_manual.py`
- `static/js/app.js`
- `templates/index.html`
- `tools/normalize_scientific_json.py`

### Removed Files:
- `FILE_MANIFEST.txt`
- `RELEASE_MANIFEST.json`

---

## 3. Hugging Face Changes (`HuggingFace/`)

- **Files Added:** 0
- **Files Modified:** 3
- **Files Removed:** 0

### Added Files:
- None

### Modified Files:
- `RELEASE_SYNC_GITHUB_MANIFEST.txt`
- `RELEASE_SYNC_HUGGINGFACE_MANIFEST.txt`
- `tools/sync_deploy_manager.py`

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
