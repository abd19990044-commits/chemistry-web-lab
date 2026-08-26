-- ===========================================================================
-- ORCA Web Lab Cloudflare Control Plane - Initial D1 Database Migration
-- Migration: 0001_initial.sql
-- ===========================================================================

PRAGMA foreign_keys = ON;

-- 1. Users / Logical Owners
CREATE TABLE IF NOT EXISTS users (
    owner TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- 2. Jobs Metadata (Stores metadata only - NO Kaggle keys, NO binary outputs)
CREATE TABLE IF NOT EXISTS jobs (
    internal_job_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    kaggle_job_ref TEXT NOT NULL,
    kaggle_url TEXT DEFAULT '',
    title TEXT DEFAULT '',
    input_filename TEXT DEFAULT '',
    job_kind TEXT DEFAULT 'unknown',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_reconciliation_at REAL DEFAULT 0.0,
    last_seen_remote_state TEXT DEFAULT 'UNKNOWN',
    local_state TEXT DEFAULT 'CREATED',
    workflow_id TEXT,
    parent_job_id TEXT,
    step_index INTEGER DEFAULT 0,
    step_count INTEGER DEFAULT 1,
    step_name TEXT DEFAULT 'CALC',
    epoch INTEGER DEFAULT 0,
    checkpoint_id TEXT,
    checkpoint_version INTEGER,
    resume_required INTEGER DEFAULT 0,
    resume_reason TEXT,
    result_state TEXT DEFAULT 'REMOTE_ONLY',
    storage_durability TEXT DEFAULT 'none',
    cf_sync_status TEXT DEFAULT 'SYNCED',
    result_artifact_id TEXT,
    result_storage_reference TEXT,
    result_manifest_id TEXT,
    result_sha256 TEXT,
    result_size_bytes INTEGER,
    result_archived_at REAL,
    result_downloaded_at REAL,
    result_provenance_json TEXT DEFAULT '{}',
    last_error_code TEXT,
    remote_deleted INTEGER DEFAULT 0,
    chain_slugs_json TEXT DEFAULT '[]',
    schema_version INTEGER DEFAULT 1,
    version INTEGER DEFAULT 1,
    metadata_json TEXT DEFAULT '{}',
    FOREIGN KEY (owner) REFERENCES users(owner) ON DELETE CASCADE
);

-- 3. Workflows
CREATE TABLE IF NOT EXISTS workflows (
    workflow_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT DEFAULT 'CREATED',
    current_step_index INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    schema_version INTEGER DEFAULT 1,
    version INTEGER DEFAULT 1,
    metadata_json TEXT DEFAULT '{}',
    FOREIGN KEY (owner) REFERENCES users(owner) ON DELETE CASCADE
);

-- 4. Workflow Steps
CREATE TABLE IF NOT EXISTS workflow_steps (
    workflow_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    step_name TEXT NOT NULL,
    job_id TEXT,
    input_template TEXT DEFAULT '',
    status TEXT DEFAULT 'PENDING',
    prerequisites_json TEXT DEFAULT '[]',
    required_artifacts_json TEXT DEFAULT '[]',
    result_data_json TEXT DEFAULT '{}',
    PRIMARY KEY (workflow_id, step_index),
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE CASCADE
);

-- 5. Checkpoints (Metadata references & hashes only - NO binary bundles)
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    job_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    bundle_digest TEXT DEFAULT '',
    status TEXT DEFAULT 'STAGED',
    orca_phase TEXT DEFAULT 'unknown',
    created_at REAL NOT NULL,
    verified_at REAL,
    source_kernel_slug TEXT DEFAULT '',
    file_manifest_json TEXT DEFAULT '[]',
    schema_version INTEGER DEFAULT 1,
    FOREIGN KEY (owner) REFERENCES users(owner) ON DELETE CASCADE
);

-- 6. Indexes for Owner Isolation and Fast Queries
CREATE INDEX IF NOT EXISTS idx_jobs_owner ON jobs(owner);
CREATE INDEX IF NOT EXISTS idx_jobs_owner_ref ON jobs(owner, kaggle_job_ref);
CREATE INDEX IF NOT EXISTS idx_jobs_owner_wf ON jobs(owner, workflow_id);
CREATE INDEX IF NOT EXISTS idx_jobs_owner_updated ON jobs(owner, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_workflows_owner ON workflows(owner);
CREATE INDEX IF NOT EXISTS idx_workflows_owner_updated ON workflows(owner, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_checkpoints_owner ON checkpoints(owner);
CREATE INDEX IF NOT EXISTS idx_checkpoints_owner_job ON checkpoints(owner, job_id);
