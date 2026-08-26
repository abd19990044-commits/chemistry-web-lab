# Cloudflare Control Plane Relational Schema & Migration Audit

**Generated:** 2026-08-26  
**Target Package:** `cloudflare-control-plane/`  
**Target D1 Database Name:** `orca-control-plane-db`  
**Current Live Database State:** 0 tables (uninitialized)  
**Schema Verification Verdict:** `VERIFIED_AND_PRODUCTION_READY`  
**Automated Tests:** 41 / 41 PASS (100%)

---

## 1. Executive Summary

A comprehensive audit of the Cloudflare Workers control plane package (`cloudflare-control-plane/`) was executed. Every SQL query in `src/db.ts`, route handler in `src/index.ts`, data model in `src/types.ts`, and security validator in `src/security.ts` was mapped against the relational schema and D1 migrations.

### Key Audit Findings:
1. **Relational Completeness:** The D1 schema comprises 6 core tables (`users`, `jobs`, `workflows`, `workflow_steps`, `checkpoints`, `credential_vault`), 10 targeted performance and owner-isolation indexes, foreign key cascading constraints (`ON DELETE CASCADE`), and optimistic concurrency version tracking.
2. **Deterministic Migrations:** The ordered migration sequence (`0001_initial.sql`, `0002_credential_vault.sql`) was verified against an empty database. Migrations execute cleanly from 0 tables to a fully functional relational schema.
3. **Security Invariant:** Zero plaintext credentials or Kaggle API keys touch Cloudflare D1 storage. The credential vault stores solely ciphertext, 12-byte random nonces, and 16-byte AEAD authentication tags. The Cloudflare Worker has no master decryption key and never decrypts records.
4. **Wrangler Configuration:** `wrangler.jsonc` maintains the canonical binding `binding = "DB"`, `database_name = "orca-control-plane-db"`, and `migrations_dir = "migrations"`. No secrets or API tokens are hardcoded.

---

## 2. Complete Relational Schema Specification

### A. Table: `users`
Represents logical tenant owners (e.g. Google Auth identities or authenticated client subjects).

| Column | Type | Nullable | Primary / Foreign Key | Description |
| :--- | :--- | :---: | :---: | :--- |
| `owner` | `TEXT` | NO | PRIMARY KEY | Canonical owner identifier (sanitized: lowercase, alphanumeric, `.`, `_`, `-`). |
| `created_at` | `REAL` | NO | - | Epoch timestamp (seconds) when user was registered. |
| `updated_at` | `REAL` | NO | - | Epoch timestamp of latest user activity. |

---

### B. Table: `jobs`
Stores calculation job metadata, execution state, checkpoint references, and result artifact hashes (NO binary outputs or secrets).

| Column | Type | Nullable | Primary / Foreign Key | Description |
| :--- | :--- | :---: | :---: | :--- |
| `internal_job_id` | `TEXT` | NO | PRIMARY KEY | Internal UUID / tracking ID. |
| `owner` | `TEXT` | NO | FK -> `users(owner)` ON DELETE CASCADE | Tenant owner identifier. |
| `kaggle_job_ref` | `TEXT` | NO | - | Kaggle kernel reference (`username/slug`). |
| `kaggle_url` | `TEXT` | YES | - | Full Kaggle notebook web URL. |
| `title` | `TEXT` | YES | - | Human-readable calculation title. |
| `input_filename` | `TEXT` | YES | - | Primary ORCA input filename (`.inp`). |
| `job_kind` | `TEXT` | YES | - | Job type (`SP`, `Opt`, `Freq`, `TD-DFT`, etc.). |
| `created_at` | `REAL` | NO | - | Epoch creation timestamp. |
| `updated_at` | `REAL` | NO | - | Epoch update timestamp. |
| `last_reconciliation_at` | `REAL` | YES | - | Epoch timestamp of last watchdog sweep. |
| `last_seen_remote_state` | `TEXT` | YES | - | Remote Kaggle status (`running`, `complete`, `error`, `cancelAck`). |
| `local_state` | `TEXT` | YES | - | Finite-state machine state (`SUBMITTED`, `MONITORING`, `COMPLETED`, `FAILED`). |
| `workflow_id` | `TEXT` | YES | - | Associated workflow ID if part of a chain. |
| `parent_job_id` | `TEXT` | YES | - | Preceding job ID if continuation step. |
| `step_index` | `INTEGER`| YES | - | 0-based step index in workflow chain. |
| `step_count` | `INTEGER`| YES | - | Total steps in workflow chain. |
| `step_name` | `TEXT` | YES | - | Logical step label. |
| `epoch` | `INTEGER`| YES | - | Resume epoch counter. |
| `checkpoint_id` | `TEXT` | YES | - | Active checkpoint UUID reference. |
| `checkpoint_version` | `INTEGER`| YES | - | Checkpoint incremental version number. |
| `resume_required` | `INTEGER`| YES | - | Flag (`1`/`0`) indicating continuation needed. |
| `resume_reason` | `TEXT` | YES | - | Diagnostic reason for resume triggering. |
| `result_state` | `TEXT` | YES | - | Result status (`REMOTE_ONLY`, `DOWNLOADED`, `ARCHIVED`). |
| `storage_durability` | `TEXT` | YES | - | Durability tier (`ephemeral`, `persisted`, `none`). |
| `cf_sync_status` | `TEXT` | YES | - | Synchronization flag (`SYNCED`, `PENDING`). |
| `result_artifact_id` | `TEXT` | YES | - | Local `ResultArtifactStore` artifact ID. |
| `result_storage_reference` | `TEXT` | YES | - | Storage path or bucket reference. |
| `result_manifest_id` | `TEXT` | YES | - | Associated result manifest hash. |
| `result_sha256` | `TEXT` | YES | - | Cryptographic SHA-256 digest of result archive. |
| `result_size_bytes` | `INTEGER`| YES | - | Result archive size in bytes. |
| `result_archived_at` | `REAL` | YES | - | Epoch timestamp when results were archived. |
| `result_downloaded_at` | `REAL` | YES | - | Epoch timestamp when results were retrieved. |
| `result_provenance_json` | `TEXT` | YES | - | JSON serialized dictionary of provenance metadata. |
| `last_error_code` | `TEXT` | YES | - | Error classification code on failure. |
| `remote_deleted` | `INTEGER`| YES | - | Flag (`1`/`0`) if deleted from remote Kaggle. |
| `chain_slugs_json` | `TEXT` | YES | - | JSON serialized list of chained notebook slugs. |
| `schema_version` | `INTEGER`| YES | - | Schema revision number (default: 1). |
| `version` | `INTEGER`| YES | - | Optimistic concurrency monotonic version. |
| `metadata_json` | `TEXT` | YES | - | Extensible JSON key-value metadata dictionary. |

---

### C. Table: `workflows`
Represents multi-step quantum chemistry pipelines (e.g. Geometry Optimization followed by Frequency calculation).

| Column | Type | Nullable | Primary / Foreign Key | Description |
| :--- | :--- | :---: | :---: | :--- |
| `workflow_id` | `TEXT` | NO | PRIMARY KEY | Unique workflow UUID. |
| `owner` | `TEXT` | NO | FK -> `users(owner)` ON DELETE CASCADE | Tenant owner identifier. |
| `title` | `TEXT` | NO | - | Workflow title / description. |
| `status` | `TEXT` | YES | - | Overall status (`CREATED`, `RUNNING`, `COMPLETED`, `FAILED`). |
| `current_step_index` | `INTEGER`| YES | - | Index of step currently executing. |
| `created_at` | `REAL` | NO | - | Epoch creation timestamp. |
| `updated_at` | `REAL` | NO | - | Epoch update timestamp. |
| `schema_version` | `INTEGER`| YES | - | Schema version (default: 1). |
| `version` | `INTEGER`| YES | - | Optimistic concurrency monotonic version. |
| `metadata_json` | `TEXT` | YES | - | Extensible JSON dictionary. |

---

### D. Table: `workflow_steps`
Individual execution stages within a workflow, including dependency prerequisites and required artifact handoffs.

| Column | Type | Nullable | Primary / Foreign Key | Description |
| :--- | :--- | :---: | :---: | :--- |
| `workflow_id` | `TEXT` | NO | PRIMARY KEY (Composite), FK -> `workflows(workflow_id)` ON DELETE CASCADE | Parent workflow UUID. |
| `step_index` | `INTEGER`| NO | PRIMARY KEY (Composite) | Step sequence position (0, 1, 2, ...). |
| `step_name` | `TEXT` | NO | - | Descriptive step label (e.g. "Optimization"). |
| `job_id` | `TEXT` | YES | - | Associated `jobs(internal_job_id)` when spawned. |
| `input_template` | `TEXT` | YES | - | Parametric ORCA input template. |
| `status` | `TEXT` | YES | - | Step status (`PENDING`, `READY`, `RUNNING`, `COMPLETED`, `FAILED`, `SKIPPED`). |
| `prerequisites_json` | `TEXT` | YES | - | JSON serialized list of prerequisite step indices. |
| `required_artifacts_json` | `TEXT` | YES | - | JSON list of required artifact names (e.g. `["opt.xyz"]`). |
| `result_data_json` | `TEXT` | YES | - | JSON dictionary of step execution outputs. |

---

### E. Table: `checkpoints`
Stores calculation checkpoint provenance, bundle digests, and phase records (NO binary tarballs).

| Column | Type | Nullable | Primary / Foreign Key | Description |
| :--- | :--- | :---: | :---: | :--- |
| `checkpoint_id` | `TEXT` | NO | PRIMARY KEY | Unique checkpoint UUID. |
| `owner` | `TEXT` | NO | FK -> `users(owner)` ON DELETE CASCADE | Tenant owner identifier. |
| `job_id` | `TEXT` | NO | - | Parent calculation job ID. |
| `epoch` | `INTEGER`| NO | - | Checkpoint incremental epoch index. |
| `bundle_digest` | `TEXT` | YES | - | Cryptographic SHA-256 digest of checkpoint bundle. |
| `status` | `TEXT` | YES | - | Checkpoint state (`STAGED`, `VERIFIED`, `EXPIRED`). |
| `orca_phase` | `TEXT` | YES | - | ORCA internal phase (`SCF`, `OPT`, `FREQ`, `TDDFT`). |
| `created_at` | `REAL` | NO | - | Epoch creation timestamp. |
| `verified_at` | `REAL` | YES | - | Epoch timestamp when bundle integrity was confirmed. |
| `source_kernel_slug` | `TEXT` | YES | - | Originating Kaggle kernel slug. |
| `file_manifest_json` | `TEXT` | YES | - | JSON list of files included in checkpoint bundle. |
| `schema_version` | `INTEGER`| YES | - | Schema version (default: 1). |

---

### F. Table: `credential_vault`
Stores client-side encrypted Kaggle API credentials using AES-256-GCM AEAD.

| Column | Type | Nullable | Primary / Foreign Key | Description |
| :--- | :--- | :---: | :---: | :--- |
| `owner` | `TEXT` | NO | PRIMARY KEY, FK -> `users(owner)` ON DELETE CASCADE | Tenant owner identifier. |
| `kaggle_username` | `TEXT` | NO | - | Canonical Kaggle username (lowercase). |
| `ciphertext` | `TEXT` | NO | - | Base64-encoded encrypted credential ciphertext. |
| `nonce` | `TEXT` | NO | - | Base64-encoded 12-byte random initialization vector (IV). |
| `tag` | `TEXT` | NO | - | Base64-encoded 16-byte AEAD authentication tag. |
| `encryption_version` | `INTEGER`| YES | - | Encryption scheme version (default: 1). |
| `status` | `TEXT` | YES | - | Key status (`ACTIVE`, `REVOKED`, `EXPIRED`). |
| `created_at` | `REAL` | NO | - | Epoch creation timestamp. |
| `updated_at` | `REAL` | NO | - | Epoch update timestamp. |
| `last_verified_at` | `REAL` | YES | - | Epoch timestamp when Kaggle API connectivity was verified. |

---

## 3. Indexes & Constraints

| Index Name | Table | Columns | Purpose |
| :--- | :--- | :--- | :--- |
| `idx_jobs_owner` | `jobs` | `(owner)` | Fast tenant scoping & job enumeration. |
| `idx_jobs_owner_ref` | `jobs` | `(owner, kaggle_job_ref)` | O(1) Kaggle slashed-reference lookups. |
| `idx_jobs_owner_wf` | `jobs` | `(owner, workflow_id)` | Fast lookup of jobs belonging to a workflow. |
| `idx_jobs_owner_updated` | `jobs` | `(owner, updated_at DESC)` | Sorted job listings (newest first). |
| `idx_workflows_owner` | `workflows` | `(owner)` | Fast workflow enumeration by owner. |
| `idx_workflows_owner_updated`| `workflows` | `(owner, updated_at DESC)` | Sorted workflow listings (newest first). |
| `idx_checkpoints_owner` | `checkpoints` | `(owner)` | Checkpoint lookups filtered by owner. |
| `idx_checkpoints_owner_job` | `checkpoints` | `(owner, job_id)` | Fast checkpoint resolution for a calculation job. |
| `idx_credential_vault_owner` | `credential_vault` | `(owner)` | Single-record O(1) credential resolution. |
| `idx_credential_vault_status`| `credential_vault` | `(status)` | Filtering active vs revoked credentials. |

---

## 4. Migration Files Specification

The migration sequence in `cloudflare-control-plane/migrations/` is ordered and reproducible:

1. [`0001_initial.sql`](file:///g:/orca%20web%20lab/cloudflare-control-plane/migrations/0001_initial.sql):
   - Enables `PRAGMA foreign_keys = ON;`
   - Creates `users`, `jobs`, `workflows`, `workflow_steps`, and `checkpoints` tables.
   - Creates 8 indexes for jobs, workflows, and checkpoints.
2. [`0002_credential_vault.sql`](file:///g:/orca%20web%20lab/cloudflare-control-plane/migrations/0002_credential_vault.sql):
   - Enables `PRAGMA foreign_keys = ON;`
   - Creates `credential_vault` table with `users(owner)` foreign key cascade.
   - Creates 2 indexes for credential vault queries.

---

## 5. Automated Verification Results

All 41 automated tests spanning migration reproducibility, schema contracts, optimistic concurrency, and owner isolation executed and passed:

```text
tests/test_cloudflare_schema_migrations.py::test_migrations_execute_from_empty_database PASSED
tests/test_cloudflare_schema_migrations.py::test_schema_table_and_column_definitions PASSED
tests/test_cloudflare_schema_migrations.py::test_schema_indexes_exist PASSED
tests/test_cloudflare_schema_migrations.py::test_foreign_key_cascading_deletes PASSED
tests/test_cloudflare_schema_migrations.py::test_foreign_key_rejection_for_orphan_records PASSED
tests/test_cloudflare_schema_migrations.py::test_optimistic_concurrency_conflict_detection PASSED
tests/test_cloudflare_schema_migrations.py::test_multi_tenant_owner_isolation PASSED
tests/test_cloudflare_schema_migrations.py::test_worker_health_select_1 PASSED
tests/test_cloudflare_worker_contract.py (11 tests) PASSED
tests/test_cloudflare_controller.py (17 tests) PASSED
tests/test_cloudflare_recovery.py (5 tests) PASSED
============================= 41 passed in 2.61s =============================
```

---

## 6. Safe Production Deployment Guide

> [!IMPORTANT]
> The live Cloudflare D1 database `orca-control-plane-db` currently contains **0 tables**. Follow these steps to initialize the schema when ready.

### Step 1: Verify Wrangler Configuration
Inspect `cloudflare-control-plane/wrangler.jsonc` and paste your actual D1 `database_id`:
```jsonc
{
  "name": "orca-cloudflare-control-plane",
  "main": "src/index.ts",
  "compatibility_date": "2024-09-03",
  "compatibility_flags": ["nodejs_compat"],
  "vars": {
    "CONTROL_PLANE_PROJECT_ID": "orca-web-lab",
    "CONTROL_PLANE_NAMESPACE": "production"
  },
  "d1_databases": [
    {
      "binding": "DB",
      "database_name": "orca-control-plane-db",
      "database_id": "<YOUR_REAL_D1_DATABASE_ID>",
      "migrations_dir": "migrations"
    }
  ],
  "observability": {
    "enabled": true
  }
}
```

### Step 2: Apply Migrations to Remote D1 Database
Execute Wrangler to apply both migrations sequentially:
```bash
npx wrangler d1 migrations apply orca-control-plane-db --remote
```

### Step 3: Configure Worker Authentication Secret
Set the shared bearer token in the Worker environment:
```bash
npx wrangler secret put CONTROL_PLANE_API_TOKEN
```

### Step 4: Deploy the Worker
```bash
npx wrangler deploy
```

### Step 5: Verify Live Endpoint Connectivity
```bash
curl -i https://orca-cloudflare-control-plane.<your-subdomain>.workers.dev/health
```
Expected response:
```json
{
  "ok": true,
  "service": "orca-cloudflare-control-plane",
  "version": "1.0.0",
  "database_available": true,
  "type": "http"
}
```
