# ORCA Source Selection & Scientific Result Downloads Finalization Report

**Date**: August 24, 2026  
**Status**: COMPLETE & PRODUCTION-VERIFIED (331/331 Pytest, 22/22 Frontend checks, Live Headless Chrome)  
**Authors**: Principal Computational Chemistry Engineer & Scientific UX Architect  

---

## 1. Executive Summary

This report documents the architectural finalization and comprehensive forensic verification of the **ORCA Source Provisioning UX** and the **Canonical Result Downloads System** in ORCA Web Lab.

The update accomplishes four key goals:
1. **Preservation of Canonical Kaggle Dataset Workflow**:
   - `username/dataset` (e.g. `jon534/orca6`) is preserved and set as the prominent placeholder and default. Full Kaggle dataset URLs (`https://www.kaggle.com/datasets/owner/dataset-slug`) are automatically normalized to `owner/dataset-slug`.
2. **Local ORCA Archive Upload (`.tar.xz`) as 3rd Source Choice**:
   - Added secure, client-side & server-side validated direct upload for Linux ORCA `.tar.xz` distributions.
   - Comprehensive **Tar-Slip & Path Traversal Protection**: Inspects members using `tarfile.getmembers()` without blind extraction, rejecting directory traversal (`../`), absolute/drive-rooted paths, and external symlinks.
   - Enforces max archive size (4 GB) and checks for binary presence (`orca`, `orca_2mkl`).
   - Retained and stored locally under `data/orca_archives/` for automatic reuse across jobs.
3. **Streamlined Device-Local Persistence**:
   - Stores user choice (`kaggle_dataset`, `google_drive`, `uploaded_archive`) and associated references securely in `localStorage`.
   - Dedicated **"Forget it"** button resets all three source states without touching Kaggle API keys or jobs.
4. **Single Canonical "📦 Full ZIP" Download with Automated Molden Generation**:
   - Completely eliminated the legacy `"Fast Results (.out / .xyz)"` button to avoid ambiguity and dead handlers.
   - Ensured `"📦 Full ZIP"` is the single canonical download for all completed calculations.
   - Enhanced the ORCA runners (`kaggle_runner.py` and `orca_orchestrator/runner/kernel_runner.py`) to automatically execute `orca_2mkl <basename> -molden` when `.gbw` wavefunction files exist.
   - Registered `artifact_type = "molden"` in `ResultArtifactStore` manifest provenance with SHA-256 integrity hashing.

---

## 2. Architecture & Implementation Details

### A. ORCA Archive Validator & Store (`orca_engine/archive_validator.py`)
- `validate_orca_tar_xz(file_source, filename)`:
  - Validates `.tar.xz` / `.txz` extension.
  - Enforces `MAX_ARCHIVE_SIZE_BYTES` (4 GB upload limit).
  - Validates `member.name` and `member.linkname` against `..`, leading `/` or `\`, and Windows drive letters `[A-Za-z]:`.
  - Flags whether standard ORCA binaries are included.
- `OrcaArchiveStore`:
  - Stores validated archive files and metadata JSON records under `data/orca_archives/`.
  - Provides atomic write, retrieval, and GC-safe deletion.

### B. REST Endpoints in `app.py`
- `POST /api/orca/upload-archive`:
  - Accepts multipart `archive_file`, validates against `validate_orca_tar_xz`, saves to archive store, and returns archive ID + SHA-256.
- `GET /api/orca/archive/<archive_id>`:
  - Serves `.tar.xz` stream with correct `Content-Disposition` and `application/x-xz` MIME type.
- `DELETE /api/orca/archive/<archive_id>`:
  - Safely unlinks the package and metadata JSON.
- `POST /api/kaggle/submit`:
  - Dynamically resolves `orca_archive_id` to local endpoint URL if supplied, while supporting `dataset_sources` and `orca_link`.

### C. Frontend UI & State Management (`templates/index.html`, `static/js/app.js`)
- **Three-way radio selector**:
  - `Kaggle Dataset` (Default, with placeholder `jon534/orca6` and hint `username/dataset`)
  - `Google Drive / direct link`
  - `Upload ORCA .tar.xz`
- **Upload Status Card**:
  - Displays selected filename, formatted size in MB, and status badge (`Ready`, `Invalid (.tar.xz only)`, `Uploading…`).
  - Clear guidance distinguishing ORCA installation packages from `.inp` calculation input files.
- **Job Card Action Cleanup**:
  - Removed `downloadEssentialBtn` from DOM and event listeners.
  - `downloadFullBtn` rendered as primary `📦 Full ZIP` button.
  - Default download mode set to `"full"` with IDM/FDM stream support.

### D. Molden Generation & Provenance Tracking
- **Runner Execution**:
  - `kaggle_runner.py` and `kernel_runner.py` check for `<basename>.gbw`. If found and no `.molden` exists, runs `orca_2mkl <basename> -molden`.
- **Result Artifact Store**:
  - `orca_orchestrator/result_store.py` classifies `*.molden` and `*.molden.input` as `art_type = "molden"`.
  - Computes SHA-256 hash, byte size, and indexes into `manifest.json`.

---

## 3. Verification & Test Evidence

### A. Dedicated Test Suite (`tests/test_orca_source_and_results.py`)
- **9/9 Tests Passed (100%) in 2.04s**:
  1. `test_valid_tar_xz_with_orca_binary`: Verifies safe tar.xz validation with binary detection.
  2. `test_reject_invalid_extension`: Rejects `.zip`, `.inp`, `.exe`.
  3. `test_reject_tar_slip_path_traversal`: Traps and blocks `../` members.
  4. `test_reject_absolute_path_tar_member`: Traps `/usr/bin/orca` members.
  5. `test_reject_windows_drive_tar_member`: Traps `C:/...` members.
  6. `test_archive_store_save_get_delete`: Verifies full store lifecycle.
  7. `test_upload_archive_endpoint_valid`: Tests HTTP POST, GET, DELETE.
  8. `test_upload_archive_endpoint_invalid_ext`: Tests HTTP 400 rejection.
  9. `test_store_includes_molden_artifact`: Verifies Molden artifact hashing and provenance in `ResultArtifactStore`.

### B. Frontend Verification (`tests/test_frontend.py`)
- **22/22 Checks Passed (100%)**:
  - Static DOM ID validation.
  - All 3 ORCA source radio options present.
  - Archive upload UI and status card present.
  - Completed jobs display canonical `📦 Full ZIP` button.
  - Fast Results button completely absent with 0 dead handlers.
  - Headless Chrome browser runtime evaluation passed with 0 severe console errors.

### C. Live Browser Automation Test (`verify_orca_source_and_results_browser.py`)
- Executed against live running Flask server on `http://127.0.0.1:7860/`:
  - Tested 3-way toggle interactions.
  - Verified placeholder `jon534/orca6` and hint `username/dataset`.
  - Verified file accept attribute `.tar.xz`.
  - Tested "Forget it" state reset.
  - Verified job list rendering with canonical `📦 Full ZIP` button.

### D. Full Pytest Regression Suite
- **331 / 331 Tests Passed with 0 Failures** across all submodules:
  - Account control, Cloudflare controller & recovery, Continuation chains, Deployment simulations, 2D Drawing & Reactions, Full system audit, IR/NMR spectra & parsing, Quantum engine APIs, Result durability, Security hardening, Thermochemistry, and Web routes.

---

## 4. Conclusion & Release Status

The ORCA input UX and result downloads system is complete, hardened, secure, and production-ready.
