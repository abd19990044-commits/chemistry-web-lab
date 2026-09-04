// ===========================================================================
// Type Definitions matching Python Cloudflare Models
// ===========================================================================

export interface Env {
  DB: D1Database;
  CONTROL_PLANE_API_TOKEN?: string;
  CONTROL_PLANE_PROJECT_ID?: string;
  CONTROL_PLANE_NAMESPACE?: string;
  ALLOWED_ORIGINS?: string;
  CHEMISTRY_LAB_ALLOWED_ORIGINS?: string;
}

export interface WorkflowStep {
  step_index: number;
  step_name: string;
  job_id?: string | null;
  input_template?: string;
  status: "PENDING" | "READY" | "RUNNING" | "COMPLETED" | "FAILED" | "SKIPPED";
  prerequisites?: number[];
  required_artifacts?: string[];
  result_data?: Record<string, any>;
}

export interface CloudflareJobRecord {
  internal_job_id: string;
  kaggle_username: string;
  kaggle_job_ref: string;
  kaggle_url?: string;
  title?: string;
  input_filename?: string;
  job_kind?: string;
  created_at?: number;
  updated_at?: number;
  last_reconciliation_at?: number;
  last_seen_remote_state?: string;
  local_state?: string;
  workflow_id?: string | null;
  parent_job_id?: string | null;
  step_index?: number;
  step_count?: number;
  step_name?: string;
  epoch?: number;
  checkpoint_id?: string | null;
  checkpoint_version?: number | null;
  resume_required?: boolean;
  resume_reason?: string | null;
  result_state?: string;
  storage_durability?: string;
  cf_sync_status?: string;
  result_artifact_id?: string | null;
  result_storage_reference?: string | null;
  result_manifest_id?: string | null;
  result_sha256?: string | null;
  result_size_bytes?: number | null;
  result_archived_at?: number | null;
  result_downloaded_at?: number | null;
  result_provenance?: Record<string, any>;
  last_error_code?: string | null;
  remote_deleted?: boolean;
  chain_slugs?: string[];
  schema_version?: number;
  version?: number;
  metadata?: Record<string, any>;
}

export interface CloudflareWorkflowRecord {
  workflow_id: string;
  kaggle_username: string;
  title: string;
  status?: string;
  steps?: WorkflowStep[];
  current_step_index?: number;
  created_at?: number;
  updated_at?: number;
  schema_version?: number;
  version?: number;
  metadata?: Record<string, any>;
}

export interface CloudflareCheckpointRecord {
  checkpoint_id: string;
  job_id: string;
  epoch: number;
  bundle_digest?: string;
  status?: string;
  orca_phase?: string;
  created_at?: number;
  verified_at?: number | null;
  source_kernel_slug?: string;
  file_manifest?: Array<{
    name: string;
    size: number;
    sha256: string;
    role?: string;
  }>;
  schema_version?: number;
}

export interface CloudflareCredentialVaultRecord {
  owner: string;
  kaggle_username: string;
  ciphertext: string;
  nonce: string;
  tag: string;
  encryption_version?: number;
  status?: string;
  created_at?: number;
  updated_at?: number;
  last_verified_at?: number | null;
}

export interface CloudflareCredentialMetadata {
  exists: boolean;
  owner: string;
  kaggle_username?: string;
  status?: string;
  encryption_version?: number;
  created_at?: number;
  updated_at?: number;
  last_verified_at?: number | null;
}
