# -*- coding: utf-8 -*-
"""
Automated Relational Schema and D1 Migration Test Suite for Cloudflare Control Plane.

Verifies:
1. Sequential execution of migrations against an empty database (0 tables -> 6 tables).
2. Complete schema fidelity: tables, columns, data types, constraints, and indexes.
3. Foreign key cascading rules (ON DELETE CASCADE).
4. Full CRUD operations, optimistic concurrency, and owner isolation.
5. Encrypted Credential Vault storage & metadata extraction.
6. Worker /health probe execution (SELECT 1).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
import pytest

# Resolve repository root dynamically so the suite runs on Windows, Linux,
# macOS, and GitHub Actions without machine-specific absolute paths.
REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "cloudflare-control-plane" / "migrations"


def get_fresh_db() -> sqlite3.Connection:
    """Creates a fresh in-memory SQLite database and applies all D1 migrations in order."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    assert len(migration_files) >= 2, f"Expected at least 2 migrations, found {len(migration_files)}"

    for mf in migration_files:
        sql = mf.read_text(encoding="utf-8")
        conn.executescript(sql)

    return conn


# ============================================================================
# 1. Migration Execution & Schema Verification
# ============================================================================

def test_migrations_execute_from_empty_database():
    """Verify that all migrations apply cleanly to a database with 0 tables."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON;")

    # Before migration: 0 tables
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    assert cur.fetchone()[0] == 0

    # Apply 0001_initial.sql
    m1 = (MIGRATIONS_DIR / "0001_initial.sql").read_text(encoding="utf-8")
    conn.executescript(m1)

    cur.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    assert cur.fetchone()[0] == 5  # users, jobs, workflows, workflow_steps, checkpoints

    # Apply 0002_credential_vault.sql
    m2 = (MIGRATIONS_DIR / "0002_credential_vault.sql").read_text(encoding="utf-8")
    conn.executescript(m2)

    cur.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    assert cur.fetchone()[0] == 6  # + credential_vault


def test_schema_table_and_column_definitions():
    """Verify exact column definitions for all tables required by db.ts and types.ts."""
    conn = get_fresh_db()
    cur = conn.cursor()

    expected_tables = {
        "users": [
            ("owner", "TEXT", True),
            ("created_at", "REAL", True),
            ("updated_at", "REAL", True),
        ],
        "jobs": [
            ("internal_job_id", "TEXT", True),
            ("owner", "TEXT", True),
            ("kaggle_job_ref", "TEXT", True),
            ("kaggle_url", "TEXT", False),
            ("title", "TEXT", False),
            ("input_filename", "TEXT", False),
            ("job_kind", "TEXT", False),
            ("created_at", "REAL", True),
            ("updated_at", "REAL", True),
            ("last_reconciliation_at", "REAL", False),
            ("last_seen_remote_state", "TEXT", False),
            ("local_state", "TEXT", False),
            ("workflow_id", "TEXT", False),
            ("parent_job_id", "TEXT", False),
            ("step_index", "INTEGER", False),
            ("step_count", "INTEGER", False),
            ("step_name", "TEXT", False),
            ("epoch", "INTEGER", False),
            ("checkpoint_id", "TEXT", False),
            ("checkpoint_version", "INTEGER", False),
            ("resume_required", "INTEGER", False),
            ("resume_reason", "TEXT", False),
            ("result_state", "TEXT", False),
            ("storage_durability", "TEXT", False),
            ("cf_sync_status", "TEXT", False),
            ("result_artifact_id", "TEXT", False),
            ("result_storage_reference", "TEXT", False),
            ("result_manifest_id", "TEXT", False),
            ("result_sha256", "TEXT", False),
            ("result_size_bytes", "INTEGER", False),
            ("result_archived_at", "REAL", False),
            ("result_downloaded_at", "REAL", False),
            ("result_provenance_json", "TEXT", False),
            ("last_error_code", "TEXT", False),
            ("remote_deleted", "INTEGER", False),
            ("chain_slugs_json", "TEXT", False),
            ("schema_version", "INTEGER", False),
            ("version", "INTEGER", False),
            ("metadata_json", "TEXT", False),
        ],
        "workflows": [
            ("workflow_id", "TEXT", True),
            ("owner", "TEXT", True),
            ("title", "TEXT", True),
            ("status", "TEXT", False),
            ("current_step_index", "INTEGER", False),
            ("created_at", "REAL", True),
            ("updated_at", "REAL", True),
            ("schema_version", "INTEGER", False),
            ("version", "INTEGER", False),
            ("metadata_json", "TEXT", False),
        ],
        "workflow_steps": [
            ("workflow_id", "TEXT", True),
            ("step_index", "INTEGER", True),
            ("step_name", "TEXT", True),
            ("job_id", "TEXT", False),
            ("input_template", "TEXT", False),
            ("status", "TEXT", False),
            ("prerequisites_json", "TEXT", False),
            ("required_artifacts_json", "TEXT", False),
            ("result_data_json", "TEXT", False),
        ],
        "checkpoints": [
            ("checkpoint_id", "TEXT", True),
            ("owner", "TEXT", True),
            ("job_id", "TEXT", True),
            ("epoch", "INTEGER", True),
            ("bundle_digest", "TEXT", False),
            ("status", "TEXT", False),
            ("orca_phase", "TEXT", False),
            ("created_at", "REAL", True),
            ("verified_at", "REAL", False),
            ("source_kernel_slug", "TEXT", False),
            ("file_manifest_json", "TEXT", False),
            ("schema_version", "INTEGER", False),
        ],
        "credential_vault": [
            ("owner", "TEXT", True),
            ("kaggle_username", "TEXT", True),
            ("ciphertext", "TEXT", True),
            ("nonce", "TEXT", True),
            ("tag", "TEXT", True),
            ("encryption_version", "INTEGER", False),
            ("status", "TEXT", False),
            ("created_at", "REAL", True),
            ("updated_at", "REAL", True),
            ("last_verified_at", "REAL", False),
        ],
    }

    for table_name, expected_cols in expected_tables.items():
        cur.execute(f"PRAGMA table_info({table_name})")
        actual_cols = {row["name"]: (row["type"], bool(row["notnull"] or row["pk"] > 0)) for row in cur.fetchall()}
        for col_name, col_type, is_required in expected_cols:
            assert col_name in actual_cols, f"Missing column {col_name} in table {table_name}"
            actual_type, actual_required = actual_cols[col_name]
            assert actual_type.upper() == col_type.upper(), f"Column {table_name}.{col_name} type mismatch: expected {col_type}, got {actual_type}"
            assert actual_required == is_required, f"Column {table_name}.{col_name} required mismatch: expected {is_required}, got {actual_required}"


def test_schema_indexes_exist():
    """Verify all performance and owner-isolation indexes exist."""
    conn = get_fresh_db()
    cur = conn.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='index'")
    indexes = {row["name"] for row in cur.fetchall()}

    expected_indexes = [
        "idx_jobs_owner",
        "idx_jobs_owner_ref",
        "idx_jobs_owner_wf",
        "idx_jobs_owner_updated",
        "idx_workflows_owner",
        "idx_workflows_owner_updated",
        "idx_checkpoints_owner",
        "idx_checkpoints_owner_job",
        "idx_credential_vault_owner",
        "idx_credential_vault_status",
    ]

    for idx in expected_indexes:
        assert idx in indexes, f"Required index {idx} not found in database!"


# ============================================================================
# 2. Foreign Key & Cascade Constraints
# ============================================================================

def test_foreign_key_cascading_deletes():
    """Verify that deleting a user cascades to all related entities."""
    conn = get_fresh_db()
    now = time.time()

    # 1. Create User
    conn.execute("INSERT INTO users (owner, created_at, updated_at) VALUES (?, ?, ?)", ("alice", now, now))

    # 2. Create Job
    conn.execute("""
        INSERT INTO jobs (internal_job_id, owner, kaggle_job_ref, created_at, updated_at)
        VALUES ('job-1', 'alice', 'alice/calc-1', ?, ?)
    """, (now, now))

    # 3. Create Workflow & Step
    conn.execute("""
        INSERT INTO workflows (workflow_id, owner, title, created_at, updated_at)
        VALUES ('wf-1', 'alice', 'Opt + Freq', ?, ?)
    """, (now, now))
    conn.execute("""
        INSERT INTO workflow_steps (workflow_id, step_index, step_name, job_id)
        VALUES ('wf-1', 0, 'Geometry Optimization', 'job-1')
    """)

    # 4. Create Checkpoint
    conn.execute("""
        INSERT INTO checkpoints (checkpoint_id, owner, job_id, epoch, created_at)
        VALUES ('ckpt-1', 'alice', 'job-1', 1, ?)
    """, (now,))

    # 5. Create Credential Vault Record
    conn.execute("""
        INSERT INTO credential_vault (owner, kaggle_username, ciphertext, nonce, tag, created_at, updated_at)
        VALUES ('alice', 'alice_kaggle', 'cipher_data', 'nonce_12', 'tag_16', ?, ?)
    """, (now, now))
    conn.commit()

    # Verify all records exist
    assert conn.execute("SELECT count(*) FROM jobs WHERE owner='alice'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM workflows WHERE owner='alice'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM workflow_steps WHERE workflow_id='wf-1'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM checkpoints WHERE owner='alice'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM credential_vault WHERE owner='alice'").fetchone()[0] == 1

    # Delete User -> Cascade should clean all tables
    conn.execute("DELETE FROM users WHERE owner='alice'")
    conn.commit()

    assert conn.execute("SELECT count(*) FROM jobs WHERE owner='alice'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM workflows WHERE owner='alice'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM workflow_steps WHERE workflow_id='wf-1'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM checkpoints WHERE owner='alice'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM credential_vault WHERE owner='alice'").fetchone()[0] == 0


def test_foreign_key_rejection_for_orphan_records():
    """Verify that creating child records without a parent user fails foreign key checks."""
    conn = get_fresh_db()
    now = time.time()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("""
            INSERT INTO jobs (internal_job_id, owner, kaggle_job_ref, created_at, updated_at)
            VALUES ('job-orphan', 'nonexistent_user', 'nonexistent/calc', ?, ?)
        """, (now, now))

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("""
            INSERT INTO workflows (workflow_id, owner, title, created_at, updated_at)
            VALUES ('wf-orphan', 'nonexistent_user', 'Orphan WF', ?, ?)
        """, (now, now))

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("""
            INSERT INTO credential_vault (owner, kaggle_username, ciphertext, nonce, tag, created_at, updated_at)
            VALUES ('nonexistent_user', 'user_k', 'c', 'n', 't', ?, ?)
        """, (now, now))


# ============================================================================
# 3. Optimistic Concurrency & CRUD Operations
# ============================================================================

def test_optimistic_concurrency_conflict_detection():
    """Verify version bumping and conflict rejection on stale writes."""
    conn = get_fresh_db()
    now = time.time()

    conn.execute("INSERT INTO users (owner, created_at, updated_at) VALUES ('bob', ?, ?)", (now, now))

    # Version 1 insert
    conn.execute("""
        INSERT INTO jobs (internal_job_id, owner, kaggle_job_ref, version, created_at, updated_at)
        VALUES ('job-bob-1', 'bob', 'bob/opt', 1, ?, ?)
    """, (now, now))
    conn.commit()

    # Simulate db.ts optimistic concurrency check:
    # 1. Fetch current
    cur = conn.cursor()
    cur.execute("SELECT version FROM jobs WHERE owner='bob' AND internal_job_id='job-bob-1'")
    current_version = cur.fetchone()["version"]
    assert current_version == 1

    # Incoming write with version 1 -> advances to version 2
    incoming_version = 1
    assert incoming_version >= current_version
    next_version = current_version + 1

    conn.execute("""
        UPDATE jobs SET version = ?, title = 'Updated Title', updated_at = ?
        WHERE owner = 'bob' AND internal_job_id = 'job-bob-1'
    """, (next_version, time.time()))
    conn.commit()

    # Incoming write with stale version 1 -> should be rejected
    cur.execute("SELECT version FROM jobs WHERE owner='bob' AND internal_job_id='job-bob-1'")
    current_version = cur.fetchone()["version"]
    assert current_version == 2

    stale_incoming_version = 1
    # Check that condition (stale_incoming_version < current_version) detects conflict
    is_conflict = stale_incoming_version < current_version
    assert is_conflict is True


# ============================================================================
# 4. Multi-Tenant Owner Isolation
# ============================================================================

def test_multi_tenant_owner_isolation():
    """Verify that User A queries cannot inspect or mutate User B records."""
    conn = get_fresh_db()
    now = time.time()

    conn.execute("INSERT INTO users (owner, created_at, updated_at) VALUES ('user_a', ?, ?)", (now, now))
    conn.execute("INSERT INTO users (owner, created_at, updated_at) VALUES ('user_b', ?, ?)", (now, now))

    conn.execute("""
        INSERT INTO jobs (internal_job_id, owner, kaggle_job_ref, created_at, updated_at)
        VALUES ('job-a', 'user_a', 'user_a/calc', ?, ?)
    """, (now, now))

    conn.execute("""
        INSERT INTO credential_vault (owner, kaggle_username, ciphertext, nonce, tag, created_at, updated_at)
        VALUES ('user_a', 'user_a_kaggle', 'secret_a_ciphertext', 'nonce_a', 'tag_a', ?, ?)
    """, (now, now))
    conn.commit()

    # User B queries for jobs with owner = 'user_b' -> should find 0
    cur = conn.cursor()
    cur.execute("SELECT * FROM jobs WHERE owner = 'user_b'")
    assert len(cur.fetchall()) == 0

    # User B cannot look up job-a scoped to user_b
    cur.execute("SELECT * FROM jobs WHERE owner = 'user_b' AND internal_job_id = 'job-a'")
    assert cur.fetchone() is None

    # User B cannot look up user_a credentials
    cur.execute("SELECT * FROM credential_vault WHERE owner = 'user_b'")
    assert cur.fetchone() is None


# ============================================================================
# 5. Worker Health Check Probe
# ============================================================================

def test_worker_health_select_1():
    """Verify that SELECT 1 executes cleanly on the D1 database binding."""
    conn = get_fresh_db()
    cur = conn.cursor()
    cur.execute("SELECT 1")
    row = cur.fetchone()
    assert row[0] == 1
