// ===========================================================================
// ORCA Web Lab Cloudflare Control Plane - Worker Entry Point & REST API
// ===========================================================================

import {
  OptimisticConcurrencyError,
  deleteCredentialVault,
  deleteJobById,
  deleteJobByRef,
  getCredentialMetadata,
  getCredentialVault,
  getJobById,
  getJobByIdOrRef,
  getJobByRef,
  getWorkflowById,
  listJobsByOwner,
  listWorkflowsByOwner,
  upsertCheckpoint,
  upsertCredentialVault,
  upsertJob,
  upsertWorkflow,
} from "./db";
import { errorResponse, jsonResponse, getCorsHeaders } from "./errors";
import { assertNoSecrets, sanitizeOwner, validateAuth } from "./security";
import {
  CloudflareCheckpointRecord,
  CloudflareCredentialMetadata,
  CloudflareCredentialVaultRecord,
  CloudflareJobRecord,
  CloudflareWorkflowRecord,
  Env,
} from "./types";

const MAX_PAYLOAD_BYTES = 1024 * 1024; // 1 MB payload limit

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+/g, "/");
    const method = request.method.toUpperCase();

    // 1. CORS Preflight
    if (method === "OPTIONS") {
      const corsHeaders = getCorsHeaders(request, env);
      return new Response(null, {
        status: 204,
        headers: corsHeaders,
      });
    }

    // 2. Health Endpoint (Public / Unauthenticated)
    if (path === "/health" || path === "/health/") {
      try {
        // Quick ping to D1
        await env.DB.prepare("SELECT 1").first();
        return jsonResponse({
          ok: true,
          service: "orca-cloudflare-control-plane",
          version: "1.0.4",
          database_available: true,
          type: "http",
        });
      } catch (err: any) {
        return jsonResponse(
          {
            ok: false,
            service: "orca-cloudflare-control-plane",
            version: "1.0.4",
            database_available: false,
            error: err.message,
          },
          503
        );
      }
    }

    // 3. Security & Authentication Check (CW-2 Fail Closed & CW-6 Namespace Validation)
    const authError = validateAuth(request, env);
    if (authError) {
      return authError;
    }

    // 4. Request Body Inspection & Size Cap
    let bodyJson: any = null;
    if (["POST", "PUT", "PATCH"].includes(method)) {
      const contentLength = request.headers.get("content-length");
      if (contentLength && parseInt(contentLength, 10) > MAX_PAYLOAD_BYTES) {
        return errorResponse("Payload exceeds maximum size limit of 1 MB.", 413, "PAYLOAD_TOO_LARGE");
      }

      try {
        const text = await request.text();
        if (text.length > MAX_PAYLOAD_BYTES) {
          return errorResponse("Payload exceeds maximum size limit of 1 MB.", 413, "PAYLOAD_TOO_LARGE");
        }
        if (text.trim()) {
          bodyJson = JSON.parse(text);
        }
      } catch (err) {
        return errorResponse("Malformed JSON request body.", 400, "INVALID_JSON");
      }

      // Assert NO Kaggle API keys or credential fields exist in payload
      const secretViolation = assertNoSecrets(bodyJson);
      if (secretViolation) {
        return errorResponse(secretViolation, 400, "FORBIDDEN_CREDENTIALS");
      }
    }

    try {
      // ---------------------------------------------------------------------
      // Route 1: Jobs
      // ---------------------------------------------------------------------

      // CW-3: GET /api/users/:owner/jobs/ref?ref=... (supports slashed Kaggle refs like "alice/notebook-42")
      let match = path.match(/^\/api\/users\/([^/]+)\/jobs\/ref$/);
      if (match && method === "GET") {
        const owner = sanitizeOwner(match[1]);
        const ref = url.searchParams.get("ref");
        if (!owner || !ref) return errorResponse("Invalid owner or ref parameter.", 400);

        const job = await getJobByRef(env.DB, owner, ref.trim());
        if (!job) {
          return errorResponse("Job record not found for reference.", 404, "NOT_FOUND");
        }
        return jsonResponse({ ok: true, job });
      }

      // CW-3: DELETE /api/users/:owner/jobs/ref?ref=...
      if (match && method === "DELETE") {
        const owner = sanitizeOwner(match[1]);
        const ref = url.searchParams.get("ref");
        if (!owner || !ref) return errorResponse("Invalid owner or ref parameter.", 400);

        await deleteJobByRef(env.DB, owner, ref.trim());
        return jsonResponse({ ok: true });
      }

      // CW-3: GET /api/users/:owner/jobs/id/:job_id (explicit internal UUID lookup)
      match = path.match(/^\/api\/users\/([^/]+)\/jobs\/id\/([^/]+)$/);
      if (match && method === "GET") {
        const owner = sanitizeOwner(match[1]);
        const jobId = match[2].trim();
        if (!owner || !jobId) return errorResponse("Invalid owner or job ID parameter.", 400);

        const job = await getJobById(env.DB, owner, jobId);
        if (!job) {
          return errorResponse("Job record not found.", 404, "NOT_FOUND");
        }
        return jsonResponse({ ok: true, job });
      }

      // CW-3: DELETE /api/users/:owner/jobs/id/:job_id
      if (match && method === "DELETE") {
        const owner = sanitizeOwner(match[1]);
        const jobId = match[2].trim();
        if (!owner || !jobId) return errorResponse("Invalid owner or job ID parameter.", 400);

        await deleteJobById(env.DB, owner, jobId);
        return jsonResponse({ ok: true });
      }

      // GET /api/users/:owner/jobs/:job_id (backward-compatible fallback)
      match = path.match(/^\/api\/users\/([^/]+)\/jobs\/([^/]+)$/);
      if (match && method === "GET") {
        const owner = sanitizeOwner(match[1]);
        const idOrRef = match[2].trim();
        if (!owner || !idOrRef) return errorResponse("Invalid owner or job ID parameter.", 400);

        const job = await getJobByIdOrRef(env.DB, owner, idOrRef);
        if (!job) {
          return errorResponse("Job record not found.", 404, "NOT_FOUND");
        }
        return jsonResponse({ ok: true, job });
      }

      // PUT /api/users/:owner/jobs/:job_id
      if (match && method === "PUT") {
        const owner = sanitizeOwner(match[1]);
        const idOrRef = match[2].trim();
        if (!owner || !idOrRef) return errorResponse("Invalid owner or job ID parameter.", 400);
        if (!bodyJson || typeof bodyJson !== "object") return errorResponse("Missing job record body.", 400);

        const jobRecord: CloudflareJobRecord = {
          ...bodyJson,
          internal_job_id: bodyJson.internal_job_id || idOrRef,
          kaggle_username: owner,
          kaggle_job_ref: bodyJson.kaggle_job_ref || idOrRef,
        };

        try {
          const saved = await upsertJob(env.DB, owner, jobRecord);
          return jsonResponse({ ok: true, job: saved }, 200);
        } catch (err: any) {
          if (err instanceof OptimisticConcurrencyError) {
            return errorResponse(err.message, 409, "CONFLICT");
          }
          throw err;
        }
      }

      // DELETE /api/users/:owner/jobs/:job_id (backward-compatible fallback)
      if (match && method === "DELETE") {
        const owner = sanitizeOwner(match[1]);
        const idOrRef = match[2].trim();
        if (!owner || !idOrRef) return errorResponse("Invalid owner or job ID parameter.", 400);

        await deleteJobById(env.DB, owner, idOrRef);
        return jsonResponse({ ok: true });
      }

      // GET /api/users/:owner/jobs
      match = path.match(/^\/api\/users\/([^/]+)\/jobs$/);
      if (match && method === "GET") {
        const owner = sanitizeOwner(match[1]);
        if (!owner) return errorResponse("Invalid owner parameter.", 400);

        const jobs = await listJobsByOwner(env.DB, owner);
        return jsonResponse({ ok: true, jobs });
      }

      // ---------------------------------------------------------------------
      // Route 2: Workflows
      // ---------------------------------------------------------------------

      // GET /api/users/:owner/workflows/:workflow_id
      match = path.match(/^\/api\/users\/([^/]+)\/workflows\/([^/]+)$/);
      if (match && method === "GET") {
        const owner = sanitizeOwner(match[1]);
        const wfId = match[2].trim();
        if (!owner || !wfId) return errorResponse("Invalid owner or workflow ID parameter.", 400);

        const wf = await getWorkflowById(env.DB, owner, wfId);
        if (!wf) {
          return errorResponse("Workflow record not found.", 404, "NOT_FOUND");
        }
        return jsonResponse({ ok: true, workflow: wf });
      }

      // PUT /api/users/:owner/workflows/:workflow_id
      if (match && method === "PUT") {
        const owner = sanitizeOwner(match[1]);
        const wfId = match[2].trim();
        if (!owner || !wfId) return errorResponse("Invalid owner or workflow ID parameter.", 400);
        if (!bodyJson || typeof bodyJson !== "object") return errorResponse("Missing workflow record body.", 400);

        const wfRecord: CloudflareWorkflowRecord = {
          ...bodyJson,
          workflow_id: bodyJson.workflow_id || wfId,
          kaggle_username: owner,
          title: bodyJson.title || "Untitled Workflow",
        };

        try {
          const saved = await upsertWorkflow(env.DB, owner, wfRecord);
          return jsonResponse({ ok: true, workflow: saved }, 200);
        } catch (err: any) {
          if (err instanceof OptimisticConcurrencyError) {
            return errorResponse(err.message, 409, "CONFLICT");
          }
          throw err;
        }
      }

      // GET /api/users/:owner/workflows
      match = path.match(/^\/api\/users\/([^/]+)\/workflows$/);
      if (match && method === "GET") {
        const owner = sanitizeOwner(match[1]);
        if (!owner) return errorResponse("Invalid owner parameter.", 400);

        const workflows = await listWorkflowsByOwner(env.DB, owner);
        return jsonResponse({ ok: true, workflows });
      }

      // ---------------------------------------------------------------------
      // Route 3: Checkpoints
      // ---------------------------------------------------------------------

      // POST /api/users/:owner/checkpoints
      match = path.match(/^\/api\/users\/([^/]+)\/checkpoints$/);
      if (match && method === "POST") {
        const owner = sanitizeOwner(match[1]);
        if (!owner) return errorResponse("Invalid owner parameter.", 400);
        if (!bodyJson || typeof bodyJson !== "object") return errorResponse("Missing checkpoint record body.", 400);
        if (!bodyJson.checkpoint_id || !bodyJson.job_id) {
          return errorResponse("Checkpoint requires checkpoint_id and job_id.", 400);
        }

        const ckptRecord: CloudflareCheckpointRecord = {
          ...bodyJson,
          epoch: bodyJson.epoch ?? 0,
        };

        const saved = await upsertCheckpoint(env.DB, owner, ckptRecord);
        return jsonResponse({ ok: true, checkpoint: saved }, 201);
      }

      // ---------------------------------------------------------------------
      // Route 4: Credential Vault (Encrypted Storage - NO Master Key on Worker)
      // ---------------------------------------------------------------------

      // PUT /api/users/:owner/credentials/kaggle
      match = path.match(/^\/api\/users\/([^/]+)\/credentials\/kaggle$/);
      if (match && method === "PUT") {
        const owner = sanitizeOwner(match[1]);
        if (!owner) return errorResponse("Invalid owner parameter.", 400);
        if (!bodyJson || typeof bodyJson !== "object") return errorResponse("Missing credential body.", 400);

        if (!bodyJson.ciphertext || !bodyJson.nonce || !bodyJson.tag || !bodyJson.kaggle_username) {
          return errorResponse(
            "Encrypted credential requires kaggle_username, ciphertext, nonce, and tag.",
            400,
            "INVALID_CREDENTIAL_PAYLOAD"
          );
        }

        const vaultRecord: CloudflareCredentialVaultRecord = {
          owner,
          kaggle_username: bodyJson.kaggle_username.trim().toLowerCase(),
          ciphertext: bodyJson.ciphertext,
          nonce: bodyJson.nonce,
          tag: bodyJson.tag,
          encryption_version: bodyJson.encryption_version ?? 1,
          status: bodyJson.status || "ACTIVE",
          created_at: bodyJson.created_at,
          updated_at: bodyJson.updated_at,
          last_verified_at: bodyJson.last_verified_at,
        };

        await upsertCredentialVault(env.DB, vaultRecord);
        return jsonResponse({ ok: true, owner, status: vaultRecord.status }, 200);
      }

      // GET /api/users/:owner/credentials/kaggle
      if (match && method === "GET") {
        const owner = sanitizeOwner(match[1]);
        if (!owner) return errorResponse("Invalid owner parameter.", 400);

        const includeVault = url.searchParams.get("vault") === "1";
        if (includeVault) {
          const vault = await getCredentialVault(env.DB, owner);
          if (!vault) {
            return errorResponse("No stored credentials found for owner.", 404, "NOT_FOUND");
          }
          return jsonResponse({ ok: true, credential: vault }, 200);
        } else {
          const metadata = await getCredentialMetadata(env.DB, owner);
          return jsonResponse({ ok: true, credential: metadata }, 200);
        }
      }

      // DELETE /api/users/:owner/credentials/kaggle
      if (match && method === "DELETE") {
        const owner = sanitizeOwner(match[1]);
        if (!owner) return errorResponse("Invalid owner parameter.", 400);

        const deleted = await deleteCredentialVault(env.DB, owner);
        return jsonResponse({ ok: true, deleted }, 200);
      }

      // 404 Fallback
      return errorResponse(`Route not found: ${method} ${path}`, 404, "NOT_FOUND");
    } catch (err: any) {
      console.error(`Unhandled Worker error [${method} ${path}]:`, err);
      return errorResponse(`Internal Server Error: ${err.message}`, 500, "INTERNAL_ERROR");
    }
  },
};
