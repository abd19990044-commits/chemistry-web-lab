// ===========================================================================
// Cloudflare D1 Database Layer - Fully Parameterized Owner-Scoped Queries
// ===========================================================================

import {
  CloudflareCheckpointRecord,
  CloudflareCredentialMetadata,
  CloudflareCredentialVaultRecord,
  CloudflareJobRecord,
  CloudflareWorkflowRecord,
  WorkflowStep,
} from "./types";

export class OptimisticConcurrencyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "OptimisticConcurrencyError";
  }
}

export async function ensureUser(db: D1Database, owner: string): Promise<void> {
  const now = Date.now() / 1000.0;
  await db
    .prepare(
      `INSERT INTO users (owner, created_at, updated_at)
       VALUES (?, ?, ?)
       ON CONFLICT (owner) DO UPDATE SET updated_at = ?`
    )
    .bind(owner, now, now, now)
    .run();
}

// ---------------------------------------------------------------------------
// Jobs
// ---------------------------------------------------------------------------

export async function getJobById(
  db: D1Database,
  owner: string,
  internalJobId: string
): Promise<CloudflareJobRecord | null> {
  const row = await db
    .prepare(
      `SELECT * FROM jobs 
       WHERE owner = ? AND internal_job_id = ? 
       LIMIT 1`
    )
    .bind(owner, internalJobId)
    .first<any>();

  if (!row) return null;
  return rowToJobRecord(row);
}

export async function getJobByRef(
  db: D1Database,
  owner: string,
  kaggleJobRef: string
): Promise<CloudflareJobRecord | null> {
  const row = await db
    .prepare(
      `SELECT * FROM jobs 
       WHERE owner = ? AND kaggle_job_ref = ? 
       LIMIT 1`
    )
    .bind(owner, kaggleJobRef)
    .first<any>();

  if (!row) return null;
  return rowToJobRecord(row);
}

export async function getJobByIdOrRef(
  db: D1Database,
  owner: string,
  idOrRef: string
): Promise<CloudflareJobRecord | null> {
  const row = await db
    .prepare(
      `SELECT * FROM jobs 
       WHERE owner = ? AND (internal_job_id = ? OR kaggle_job_ref = ?) 
       LIMIT 1`
    )
    .bind(owner, idOrRef, idOrRef)
    .first<any>();

  if (!row) return null;
  return rowToJobRecord(row);
}

export async function listJobsByOwner(
  db: D1Database,
  owner: string
): Promise<CloudflareJobRecord[]> {
  const { results } = await db
    .prepare(
      `SELECT * FROM jobs 
       WHERE owner = ? 
       ORDER BY created_at DESC`
    )
    .bind(owner)
    .all<any>();

  return (results || []).map(rowToJobRecord);
}

export async function upsertJob(
  db: D1Database,
  owner: string,
  job: CloudflareJobRecord
): Promise<CloudflareJobRecord> {
  await ensureUser(db, owner);
  const now = Date.now() / 1000.0;
  const createdAt = job.created_at || now;
  const updatedAt = job.updated_at || now;

  // CW-5: Check existing version for optimistic concurrency
  const existing = await getJobById(db, owner, job.internal_job_id);
  let nextVersion = 1;
  if (existing) {
    const existingVer = existing.version ?? 1;
    const incomingVer = job.version ?? existingVer;
    if (incomingVer < existingVer) {
      throw new OptimisticConcurrencyError(
        `Optimistic concurrency conflict: current job version is ${existingVer}, incoming payload version is ${incomingVer}.`
      );
    }
    nextVersion = existingVer + 1;
  }

  const resultProvJson = JSON.stringify(job.result_provenance || {});
  const chainSlugsJson = JSON.stringify(job.chain_slugs || []);
  const metaJson = JSON.stringify(job.metadata || {});

  await db
    .prepare(
      `INSERT INTO jobs (
        internal_job_id, owner, kaggle_job_ref, kaggle_url, title,
        input_filename, job_kind, created_at, updated_at, last_reconciliation_at,
        last_seen_remote_state, local_state, workflow_id, parent_job_id,
        step_index, step_count, step_name, epoch, checkpoint_id,
        checkpoint_version, resume_required, resume_reason, result_state,
        storage_durability, cf_sync_status, result_artifact_id,
        result_storage_reference, result_manifest_id, result_sha256,
        result_size_bytes, result_archived_at, result_downloaded_at,
        result_provenance_json, last_error_code, remote_deleted,
        chain_slugs_json, schema_version, version, metadata_json
      ) VALUES (
        ?, ?, ?, ?, ?,
        ?, ?, ?, ?, ?,
        ?, ?, ?, ?,
        ?, ?, ?, ?, ?,
        ?, ?, ?, ?,
        ?, ?, ?,
        ?, ?, ?,
        ?, ?, ?,
        ?, ?, ?,
        ?, ?, ?, ?
      )
      ON CONFLICT(internal_job_id) DO UPDATE SET
        owner = excluded.owner,
        kaggle_job_ref = excluded.kaggle_job_ref,
        kaggle_url = excluded.kaggle_url,
        title = excluded.title,
        input_filename = excluded.input_filename,
        job_kind = excluded.job_kind,
        updated_at = excluded.updated_at,
        last_reconciliation_at = excluded.last_reconciliation_at,
        last_seen_remote_state = excluded.last_seen_remote_state,
        local_state = excluded.local_state,
        workflow_id = excluded.workflow_id,
        parent_job_id = excluded.parent_job_id,
        step_index = excluded.step_index,
        step_count = excluded.step_count,
        step_name = excluded.step_name,
        epoch = excluded.epoch,
        checkpoint_id = excluded.checkpoint_id,
        checkpoint_version = excluded.checkpoint_version,
        resume_required = excluded.resume_required,
        resume_reason = excluded.resume_reason,
        result_state = excluded.result_state,
        storage_durability = excluded.storage_durability,
        cf_sync_status = 'SYNCED',
        result_artifact_id = excluded.result_artifact_id,
        result_storage_reference = excluded.result_storage_reference,
        result_manifest_id = excluded.result_manifest_id,
        result_sha256 = excluded.result_sha256,
        result_size_bytes = excluded.result_size_bytes,
        result_archived_at = excluded.result_archived_at,
        result_downloaded_at = excluded.result_downloaded_at,
        result_provenance_json = excluded.result_provenance_json,
        last_error_code = excluded.last_error_code,
        remote_deleted = excluded.remote_deleted,
        chain_slugs_json = excluded.chain_slugs_json,
        schema_version = excluded.schema_version,
        version = excluded.version,
        metadata_json = excluded.metadata_json`
    )
    .bind(
      job.internal_job_id,
      owner,
      job.kaggle_job_ref,
      job.kaggle_url || "",
      job.title || "",
      job.input_filename || "",
      job.job_kind || "unknown",
      createdAt,
      updatedAt,
      job.last_reconciliation_at || 0.0,
      job.last_seen_remote_state || "UNKNOWN",
      job.local_state || "CREATED",
      job.workflow_id || null,
      job.parent_job_id || null,
      job.step_index ?? 0,
      job.step_count ?? 1,
      job.step_name || "CALC",
      job.epoch ?? 0,
      job.checkpoint_id || null,
      job.checkpoint_version || null,
      job.resume_required ? 1 : 0,
      job.resume_reason || null,
      job.result_state || "REMOTE_ONLY",
      job.storage_durability || "none",
      "SYNCED",
      job.result_artifact_id || null,
      job.result_storage_reference || null,
      job.result_manifest_id || null,
      job.result_sha256 || null,
      job.result_size_bytes || null,
      job.result_archived_at || null,
      job.result_downloaded_at || null,
      resultProvJson,
      job.last_error_code || null,
      job.remote_deleted ? 1 : 0,
      chainSlugsJson,
      job.schema_version || 1,
      nextVersion,
      metaJson
    )
    .run();

  const saved = await getJobById(db, owner, job.internal_job_id);
  return saved || { ...job, version: nextVersion, cf_sync_status: "SYNCED" };
}

export async function deleteJobById(
  db: D1Database,
  owner: string,
  internalJobId: string
): Promise<boolean> {
  const res = await db
    .prepare(
      `DELETE FROM jobs 
       WHERE owner = ? AND internal_job_id = ?`
    )
    .bind(owner, internalJobId)
    .run();

  return (res.meta.changes || 0) > 0;
}

export async function deleteJobByRef(
  db: D1Database,
  owner: string,
  kaggleJobRef: string
): Promise<boolean> {
  const res = await db
    .prepare(
      `DELETE FROM jobs 
       WHERE owner = ? AND kaggle_job_ref = ?`
    )
    .bind(owner, kaggleJobRef)
    .run();

  return (res.meta.changes || 0) > 0;
}

// ---------------------------------------------------------------------------
// Workflows
// ---------------------------------------------------------------------------

export async function getWorkflowById(
  db: D1Database,
  owner: string,
  workflowId: string
): Promise<CloudflareWorkflowRecord | null> {
  const wfRow = await db
    .prepare(
      `SELECT * FROM workflows 
       WHERE owner = ? AND workflow_id = ? 
       LIMIT 1`
    )
    .bind(owner, workflowId)
    .first<any>();

  if (!wfRow) return null;

  const { results: stepRows } = await db
    .prepare(
      `SELECT * FROM workflow_steps 
       WHERE workflow_id = ? 
       ORDER BY step_index ASC`
    )
    .bind(workflowId)
    .all<any>();

  return rowToWorkflowRecord(wfRow, stepRows || []);
}

export async function listWorkflowsByOwner(
  db: D1Database,
  owner: string
): Promise<CloudflareWorkflowRecord[]> {
  const { results: wfRows } = await db
    .prepare(
      `SELECT * FROM workflows 
       WHERE owner = ? 
       ORDER BY created_at DESC`
    )
    .bind(owner)
    .all<any>();

  if (!wfRows || wfRows.length === 0) return [];

  const workflows: CloudflareWorkflowRecord[] = [];
  for (const wf of wfRows) {
    const { results: stepRows } = await db
      .prepare(
        `SELECT * FROM workflow_steps 
         WHERE workflow_id = ? 
         ORDER BY step_index ASC`
      )
      .bind(wf.workflow_id)
      .all<any>();
    workflows.push(rowToWorkflowRecord(wf, stepRows || []));
  }

  return workflows;
}

export async function upsertWorkflow(
  db: D1Database,
  owner: string,
  wf: CloudflareWorkflowRecord
): Promise<CloudflareWorkflowRecord> {
  await ensureUser(db, owner);
  const now = Date.now() / 1000.0;
  const createdAt = wf.created_at || now;
  const updatedAt = wf.updated_at || now;
  const metaJson = JSON.stringify(wf.metadata || {});

  // CW-5: Check existing version for optimistic concurrency
  const existing = await getWorkflowById(db, owner, wf.workflow_id);
  let nextVersion = 1;
  if (existing) {
    const existingVer = existing.version ?? 1;
    const incomingVer = wf.version ?? existingVer;
    if (incomingVer < existingVer) {
      throw new OptimisticConcurrencyError(
        `Optimistic concurrency conflict: current workflow version is ${existingVer}, incoming payload version is ${incomingVer}.`
      );
    }
    nextVersion = existingVer + 1;
  }

  await db
    .prepare(
      `INSERT INTO workflows (
        workflow_id, owner, title, status, current_step_index,
        created_at, updated_at, schema_version, version, metadata_json
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(workflow_id) DO UPDATE SET
        owner = excluded.owner,
        title = excluded.title,
        status = excluded.status,
        current_step_index = excluded.current_step_index,
        updated_at = excluded.updated_at,
        schema_version = excluded.schema_version,
        version = excluded.version,
        metadata_json = excluded.metadata_json`
    )
    .bind(
      wf.workflow_id,
      owner,
      wf.title,
      wf.status || "CREATED",
      wf.current_step_index ?? 0,
      createdAt,
      updatedAt,
      wf.schema_version || 1,
      nextVersion,
      metaJson
    )
    .run();

  // Upsert steps
  if (wf.steps && wf.steps.length > 0) {
    for (const step of wf.steps) {
      const prereqsJson = JSON.stringify(step.prerequisites || []);
      const reqArtsJson = JSON.stringify(step.required_artifacts || []);
      const resDataJson = JSON.stringify(step.result_data || {});

      await db
        .prepare(
          `INSERT INTO workflow_steps (
            workflow_id, step_index, step_name, job_id, input_template,
            status, prerequisites_json, required_artifacts_json, result_data_json
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(workflow_id, step_index) DO UPDATE SET
            step_name = excluded.step_name,
            job_id = excluded.job_id,
            input_template = excluded.input_template,
            status = excluded.status,
            prerequisites_json = excluded.prerequisites_json,
            required_artifacts_json = excluded.required_artifacts_json,
            result_data_json = excluded.result_data_json`
        )
        .bind(
          wf.workflow_id,
          step.step_index,
          step.step_name,
          step.job_id || null,
          step.input_template || "",
          step.status || "PENDING",
          prereqsJson,
          reqArtsJson,
          resDataJson
        )
        .run();
    }
  }

  const saved = await getWorkflowById(db, owner, wf.workflow_id);
  return saved || { ...wf, version: nextVersion };
}

// ---------------------------------------------------------------------------
// Checkpoints
// ---------------------------------------------------------------------------

export async function upsertCheckpoint(
  db: D1Database,
  owner: string,
  ckpt: CloudflareCheckpointRecord
): Promise<CloudflareCheckpointRecord> {
  await ensureUser(db, owner);
  const now = Date.now() / 1000.0;
  const createdAt = ckpt.created_at || now;
  const fileManJson = JSON.stringify(ckpt.file_manifest || []);

  await db
    .prepare(
      `INSERT INTO checkpoints (
        checkpoint_id, owner, job_id, epoch, bundle_digest,
        status, orca_phase, created_at, verified_at,
        source_kernel_slug, file_manifest_json, schema_version
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(checkpoint_id) DO UPDATE SET
        owner = excluded.owner,
        job_id = excluded.job_id,
        epoch = excluded.epoch,
        bundle_digest = excluded.bundle_digest,
        status = excluded.status,
        orca_phase = excluded.orca_phase,
        verified_at = excluded.verified_at,
        source_kernel_slug = excluded.source_kernel_slug,
        file_manifest_json = excluded.file_manifest_json,
        schema_version = excluded.schema_version`
    )
    .bind(
      ckpt.checkpoint_id,
      owner,
      ckpt.job_id,
      ckpt.epoch,
      ckpt.bundle_digest || "",
      ckpt.status || "STAGED",
      ckpt.orca_phase || "unknown",
      createdAt,
      ckpt.verified_at || null,
      ckpt.source_kernel_slug || "",
      fileManJson,
      ckpt.schema_version || 1
    )
    .run();

  return ckpt;
}

// ---------------------------------------------------------------------------
// Row to Model Deserializers
// ---------------------------------------------------------------------------

function parseJsonSafe(str: string | null | undefined, fallback: any): any {
  if (!str) return fallback;
  try {
    return JSON.parse(str);
  } catch {
    return fallback;
  }
}

function rowToJobRecord(row: any): CloudflareJobRecord {
  return {
    internal_job_id: row.internal_job_id,
    kaggle_username: row.owner,
    kaggle_job_ref: row.kaggle_job_ref,
    kaggle_url: row.kaggle_url || "",
    title: row.title || "",
    input_filename: row.input_filename || "",
    job_kind: row.job_kind || "unknown",
    created_at: row.created_at,
    updated_at: row.updated_at,
    last_reconciliation_at: row.last_reconciliation_at || 0.0,
    last_seen_remote_state: row.last_seen_remote_state || "UNKNOWN",
    local_state: row.local_state || "CREATED",
    workflow_id: row.workflow_id || null,
    parent_job_id: row.parent_job_id || null,
    step_index: row.step_index ?? 0,
    step_count: row.step_count ?? 1,
    step_name: row.step_name || "CALC",
    epoch: row.epoch ?? 0,
    checkpoint_id: row.checkpoint_id || null,
    checkpoint_version: row.checkpoint_version || null,
    resume_required: Boolean(row.resume_required),
    resume_reason: row.resume_reason || null,
    result_state: row.result_state || "REMOTE_ONLY",
    storage_durability: row.storage_durability || "none",
    cf_sync_status: row.cf_sync_status || "SYNCED",
    result_artifact_id: row.result_artifact_id || null,
    result_storage_reference: row.result_storage_reference || null,
    result_manifest_id: row.result_manifest_id || null,
    result_sha256: row.result_sha256 || null,
    result_size_bytes: row.result_size_bytes || null,
    result_archived_at: row.result_archived_at || null,
    result_downloaded_at: row.result_downloaded_at || null,
    result_provenance: parseJsonSafe(row.result_provenance_json, {}),
    last_error_code: row.last_error_code || null,
    remote_deleted: Boolean(row.remote_deleted),
    chain_slugs: parseJsonSafe(row.chain_slugs_json, []),
    schema_version: row.schema_version || 1,
    version: row.version ?? 1,
    metadata: parseJsonSafe(row.metadata_json, {}),
  };
}

function rowToWorkflowRecord(
  wfRow: any,
  stepRows: any[]
): CloudflareWorkflowRecord {
  const steps: WorkflowStep[] = stepRows.map((s) => ({
    step_index: s.step_index,
    step_name: s.step_name,
    job_id: s.job_id || null,
    input_template: s.input_template || "",
    status: s.status || "PENDING",
    prerequisites: parseJsonSafe(s.prerequisites_json, []),
    required_artifacts: parseJsonSafe(s.required_artifacts_json, []),
    result_data: parseJsonSafe(s.result_data_json, {}),
  }));

  return {
    workflow_id: wfRow.workflow_id,
    kaggle_username: wfRow.owner,
    title: wfRow.title,
    status: wfRow.status || "CREATED",
    current_step_index: wfRow.current_step_index ?? 0,
    created_at: wfRow.created_at,
    updated_at: wfRow.updated_at,
    schema_version: wfRow.schema_version || 1,
    version: wfRow.version ?? 1,
    metadata: parseJsonSafe(wfRow.metadata_json, {}),
    steps,
  };
}

// ---------------------------------------------------------------------------
// Credential Vault
// ---------------------------------------------------------------------------

export async function getCredentialVault(
  db: D1Database,
  owner: string
): Promise<CloudflareCredentialVaultRecord | null> {
  const row = await db
    .prepare(
      `SELECT * FROM credential_vault 
       WHERE owner = ? 
       LIMIT 1`
    )
    .bind(owner)
    .first<any>();

  if (!row) return null;
  return {
    owner: row.owner,
    kaggle_username: row.kaggle_username,
    ciphertext: row.ciphertext,
    nonce: row.nonce,
    tag: row.tag,
    encryption_version: row.encryption_version ?? 1,
    status: row.status || "ACTIVE",
    created_at: row.created_at,
    updated_at: row.updated_at,
    last_verified_at: row.last_verified_at || null,
  };
}

export async function getCredentialMetadata(
  db: D1Database,
  owner: string
): Promise<CloudflareCredentialMetadata> {
  const row = await db
    .prepare(
      `SELECT owner, kaggle_username, encryption_version, status, created_at, updated_at, last_verified_at 
       FROM credential_vault 
       WHERE owner = ? 
       LIMIT 1`
    )
    .bind(owner)
    .first<any>();

  if (!row) {
    return { exists: false, owner };
  }
  return {
    exists: true,
    owner: row.owner,
    kaggle_username: row.kaggle_username,
    status: row.status || "ACTIVE",
    encryption_version: row.encryption_version ?? 1,
    created_at: row.created_at,
    updated_at: row.updated_at,
    last_verified_at: row.last_verified_at || null,
  };
}

export async function upsertCredentialVault(
  db: D1Database,
  record: CloudflareCredentialVaultRecord
): Promise<void> {
  await ensureUser(db, record.owner);
  const now = Date.now() / 1000.0;
  const createdAt = record.created_at || now;
  const updatedAt = record.updated_at || now;
  const lastVerifiedAt = record.last_verified_at || null;
  const version = record.encryption_version ?? 1;
  const status = record.status || "ACTIVE";

  await db
    .prepare(
      `INSERT INTO credential_vault (
         owner, kaggle_username, ciphertext, nonce, tag, encryption_version, status, created_at, updated_at, last_verified_at
       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT (owner) DO UPDATE SET
         kaggle_username = excluded.kaggle_username,
         ciphertext = excluded.ciphertext,
         nonce = excluded.nonce,
         tag = excluded.tag,
         encryption_version = excluded.encryption_version,
         status = excluded.status,
         updated_at = excluded.updated_at,
         last_verified_at = COALESCE(excluded.last_verified_at, credential_vault.last_verified_at)`
    )
    .bind(
      record.owner,
      record.kaggle_username,
      record.ciphertext,
      record.nonce,
      record.tag,
      version,
      status,
      createdAt,
      updatedAt,
      lastVerifiedAt
    )
    .run();
}

export async function deleteCredentialVault(
  db: D1Database,
  owner: string
): Promise<boolean> {
  const res = await db
    .prepare(`DELETE FROM credential_vault WHERE owner = ?`)
    .bind(owner)
    .run();
  return (res.meta?.changes ?? 0) > 0;
}
