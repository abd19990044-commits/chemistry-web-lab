# Cloudflare Persistent Control Plane & Restart Recovery Architecture

## Executive Summary

The **ORCA Web Lab** distributed computing system has been augmented with an external, restart-tolerant **Cloudflare Persistent Control Plane** (`orca_orchestrator/cloudflare_controller/`).

This architecture solves the core operational problem of ephemeral cloud hosting (e.g. Hugging Face Spaces sleeping, restarting, or cycling process-local RAM/storage):
- **Kaggle calculations continue executing remotely** even when Hugging Face Spaces restart or sleep.
- **Durable calculation and workflow metadata is persisted in Cloudflare** without requiring anti-sleep hacks or continuous polling.
- **On system restart or user re-authentication**, ORCA Web Lab reconstructs the user's active session, discovers previously launched jobs and multi-step workflows, reconciles external Cloudflare metadata against live Kaggle kernel states via a deterministic reconciliation matrix (Cases A–F), repopulates the local cache (`JobStore`), and automatically prepares continuation for resumable calculations (such as calculations interrupted by Kaggle's 12-hour limit).

---

## 1. Architectural Principles & Security Boundary

### 1.1 Kaggle Identity Model
- **No third-party OAuth** is introduced.
- The **authenticated Kaggle username** is the canonical logical user identity.
- User metadata in Cloudflare is scoped and indexed under `kaggle_username` (normalized to lowercase).

### 1.2 Strict Zero-Credential Boundary
- **Kaggle API keys, passwords, private tokens, and secret material are NEVER transmitted to or stored in Cloudflare**.
- Kaggle credentials remain strictly in ephemeral process RAM on Hugging Face with a TTL broker (`orca_orchestrator.credentials.BROKER`), wiped on process termination.
- All Cloudflare serializers (`to_dict()`, `from_dict()`, `InMemoryCloudflareBackend`, `CloudflareHttpClient`) strictly validate and scrub payload dictionaries and assert `CloudflareSecurityViolation` if any credential fields are present.

### 1.3 State Separation: Local vs. Remote vs. Control Plane
- **Remote Truth**: Kaggle execution status (`running`, `queued`, `complete`, `error`, `stopped`).
- **Control Plane Truth**: Durable external workflow definitions, step progression, and checkpoint references in Cloudflare (`CloudflareJobRecord`, `CloudflareWorkflowRecord`, `CloudflareCheckpointRecord`).
- **Local Cache**: Local SQLite database (`JobStore`) maintaining leases, idempotency records, and cached manifests.

---

## 2. Package Architecture (`orca_orchestrator/cloudflare_controller/`)

The Cloudflare integration is encapsulated in a dedicated subsystem with clean boundaries:

```
orca_orchestrator/cloudflare_controller/
├── __init__.py          # Exports public interface, models, exceptions, and clients
├── config.py            # Environment configuration (URL, tokens, timeouts, retries)
├── models.py            # Versioned dataclasses (CloudflareJobRecord, CloudflareWorkflowRecord, WorkflowStep, CloudflareCheckpointRecord)
├── client.py            # Abstract CloudflareClientProtocol, CloudflareHttpClient with retry/backoff, InMemoryCloudflareBackend
├── reconciliation.py    # Deterministic reconciliation matrix (Cases A..F) & prerequisite validation
└── controller.py        # Central facade coordinating Cloudflare with JobStore and Reconciler
```

---

## 3. Data Model & Versioned Schema (v1)

### 3.1 `CloudflareJobRecord`
| Field | Type | Description |
|---|---|---|
| `internal_job_id` | `str` (UUID) | Immutable system UUID |
| `kaggle_username` | `str` | Normalized user identity |
| `kaggle_job_ref` | `str` | Base slug (`chem-tools-<name>-<rand>`) |
| `kaggle_url` | `str` | URL to notebook on Kaggle |
| `local_state` | `str` | `LocalWorkflowState` enum |
| `last_seen_remote_state` | `str` | `RemoteExecutionState` enum |
| `workflow_id` | `str \| None` | Associated multi-step workflow UUID |
| `parent_job_id` | `str \| None` | Predecessor job reference |
| `step_index` / `step_count` | `int` | Position in workflow graph |
| `step_name` | `str` | e.g. `"OPT"`, `"FREQ"`, `"TD-DFT"`, `"SP"` |
| `epoch` | `int` | Calculation continuation window index |
| `checkpoint_id` | `str \| None` | Verified checkpoint manifest ID |
| `resume_required` | `bool` | Flag set when calculation can be resumed |
| `resume_reason` | `str \| None` | Reason for resumption (e.g. `"kaggle_runtime_expired_checkpoint_available"`) |

### 3.2 `CloudflareWorkflowRecord` & `WorkflowStep`
Supports directed multi-step calculation pipelines:
```json
{
  "workflow_id": "8f31e9c2-...",
  "kaggle_username": "dr_chemist",
  "title": "Naproxen Multi-Stage Study",
  "status": "IN_PROGRESS",
  "steps": [
    {
      "step_index": 0,
      "step_name": "OPT",
      "job_id": "chem-tools-naproxen-opt-a1b2",
      "status": "COMPLETED",
      "prerequisites": [],
      "required_artifacts": ["geometry"]
    },
    {
      "step_index": 1,
      "step_name": "FREQ",
      "job_id": "chem-tools-naproxen-freq-c3d4",
      "status": "RUNNING",
      "prerequisites": [0],
      "required_artifacts": ["hessian"]
    },
    {
      "step_index": 2,
      "step_name": "TD-DFT",
      "job_id": null,
      "status": "PENDING",
      "prerequisites": [1],
      "required_artifacts": []
    }
  ]
}
```

---

## 4. Deterministic Reconciliation Matrix

When the application boots or a user re-authenticates, `reconcile_user_session()` queries live Kaggle kernel states for all known jobs and executes the reconciliation matrix:

| Case | Cloudflare Local State | Remote Kaggle State | Reconciled Decision & Action |
|---|---|---|---|
| **Case A** | `RUNNING` | `running` / `queued` | **`RECONNECT`**: Active remote kernel observed; reconnect local watchdog monitor. |
| **Case B** | `RUNNING` | `complete` | **`COMPLETE`**: Calculation finished; mark `COMPLETED`, fetch output artifacts, unlock dependent workflow steps. |
| **Case C** | `RUNNING` | `error` | **`FAIL / RECOVERY_PENDING`**: If verified checkpoint and epoch budget exist $\rightarrow$ `RECOVERY_PENDING`; otherwise mark `FAILED`. |
| **Case D** | `RUNNING` | `stopped` / `cancelled` (12h limit) | **`RECOVERY_PENDING`**: Kaggle runtime limit reached. If checkpoint exists $\rightarrow$ `RECOVERY_PENDING` (`resume_required=True`); otherwise mark `FAILED`. |
| **Case E** | `RUNNING` / `CREATED` | `NOT_FOUND` (deleted) | **`MARK_DELETED`**: Notebook deleted on Kaggle; mark `REMOTE_DELETED`. Do NOT silently recreate. |
| **Case F** | `COMPLETED` | `NOT_FOUND` (deleted) | **`PRESERVE`**: Historical calculation finished earlier; preserve completed local results and metadata (`deleted_on_kaggle=True`). |

---

## 5. Graceful Degradation & Network Resiliency

If the Cloudflare service experiences an outage, network timeout, or connection refusal:
1. `CloudflareHttpClient` employs exponential backoff retry up to `CLOUDFLARE_MAX_RETRIES`.
2. Upon retry exhaustion, requests fall back transparently to thread-safe local memory/cache without raising unhandled exceptions.
3. Long-running ORCA calculations, local submissions, and state transitions **never fail or abort due to external Cloudflare connectivity issues**.

---

## 6. Verification and Test Results

### 6.1 Cloudflare Test Suites
- `tests/test_cloudflare_controller.py`: 17 / 17 tests passed.
  - Data model creation, serialization, conversion to/from `JobManifest`.
  - Security validation: zero secrets in Cloudflare payloads, cross-user isolation.
  - Reconciliation matrix: deterministic coverage of Cases A through F.
  - Workflow prerequisite validation.
  - Simulated Hugging Face restart recovery.
  - Service and API workflow submission integration.
- `tests/test_cloudflare_recovery.py`: 5 / 5 tests passed.
  - Multi-step calculation progression (Step 0 $\rightarrow$ Step 1 $\rightarrow$ Step 2).
  - Kaggle 12-hour runtime expiration and automated resumption from checkpoint.
  - Cloudflare outage resilience (dead endpoint, degraded fallback).
  - Credential safety and isolation under adversarial payload injection.

### 6.2 Full Project Regression Suite
- **281 / 281 tests passed** in 297.20s across all modules (0 failures, 0 errors).
