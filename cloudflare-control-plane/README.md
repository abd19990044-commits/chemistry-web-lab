# ORCA Web Lab - Cloudflare Persistent Control Plane

A high-availability, serverless metadata and control plane for **ORCA Web Lab** built on **Cloudflare Workers** and **Cloudflare D1**.

---

## 1. System Architecture & Dual API Security

```text
                  Hugging Face Space
                  (ORCA Web Lab App)
                         │
                         │ HTTPS (REST API)
                         │ Headers:
                         │   Authorization: Bearer <CLOUDFLARE_API_TOKEN>
                         │   X-Project-Id: orca-web-lab
                         │   X-Namespace: production
                         ▼
              ┌──────────────────────┐
              │  Cloudflare Worker   │
              │  Control Plane API   │
              │ (CONTROL_PLANE_TOKEN)│
              └──────────┬───────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │    Cloudflare D1     │
              │  (SQLite at the Edge)│
              └──────────────────────┘
                         │
       ┌─────────────────┼─────────────────┐
       ▼                 ▼                 ▼
   Jobs Table      Workflows Table    Checkpoints Table
 (Job Metadata)   (Multi-Step Steps) (Hashes & References)
```

### Security & Data Boundaries

| Component | What it Stores | What it NEVER Stores |
| :--- | :--- | :--- |
| **Hugging Face App** | Process state, in-memory caches, UI sessions | Permanent single point of failure |
| **Kaggle** | Live calculation executions, remote kernels | Local application credentials |
| **Cloudflare Control Plane** | Calculation metadata, workflow states, checkpoint references, result hashes | **Kaggle API keys, passwords, secrets, ORCA output binaries (`.out`, `.gbw`, `.hess`), checkpoint `.tar.gz` files** |
| **ResultArtifactStore** | Local archived scientific outputs and tarballs | Non-persisted RAM states |

### Dual API Secret Policy
- **Hugging Face App Side**: Set `CLOUDFLARE_API_TOKEN` in Space secrets / `.env`.
- **Cloudflare Worker Side**: Set `CONTROL_PLANE_API_TOKEN` via `wrangler secret put CONTROL_PLANE_API_TOKEN`.
- **Zero In-Tree Secrets**: Real production tokens are **never** committed to Git, hardcoded in source, exposed in test fixtures, or sent to frontend browsers.
- **Fail-Closed Authentication**: If `CONTROL_PLANE_API_TOKEN` is unconfigured on the Worker, it fails closed with HTTP `503 Service Unavailable` (`CONFIG_ERROR`).

---

## 2. REST API Endpoints

All endpoints except `/health` require authentication via `Authorization: Bearer <TOKEN>` plus matching `X-Project-Id` and `X-Namespace` headers.

| Route | Method | Purpose |
| :--- | :--- | :--- |
| `GET /health` | `GET` | Service liveness & D1 database connectivity status (public). |
| `GET /api/users/:owner/jobs/ref?ref=<ref>` | `GET` | Fetch job metadata by Kaggle reference (supports slashed slugs e.g. `alice/notebook-42`). |
| `DELETE /api/users/:owner/jobs/ref?ref=<ref>` | `DELETE` | Delete job metadata by Kaggle reference. |
| `GET /api/users/:owner/jobs/id/:job_id` | `GET` | Fetch job metadata by internal UUID. |
| `DELETE /api/users/:owner/jobs/id/:job_id` | `DELETE` | Delete job metadata by internal UUID. |
| `GET /api/users/:owner/jobs/:job_id` | `GET` | Fetch job metadata (fallback trying internal UUID then Kaggle ref). |
| `PUT /api/users/:owner/jobs/:job_id` | `PUT` | Upsert calculation metadata record with optimistic concurrency (returns 409 on version conflict). |
| `GET /api/users/:owner/jobs` | `GET` | List all calculation jobs for an owner (sorted newest first). |
| `DELETE /api/users/:owner/jobs/:job_id` | `DELETE` | Delete job metadata record. |
| `GET /api/users/:owner/workflows/:id` | `GET` | Fetch multi-step workflow metadata and step statuses. |
| `PUT /api/users/:owner/workflows/:id` | `PUT` | Upsert workflow and all step prerequisite definitions (with optimistic concurrency). |
| `GET /api/users/:owner/workflows` | `GET` | List all workflows for an owner. |
| `POST /api/users/:owner/checkpoints` | `POST` | Record checkpoint bundle hash & verified metadata reference. |

---

## 3. Local Development

### Prerequisites
- Node.js (v18.0.0+) and npm
- Cloudflare Wrangler CLI (`npm install -g wrangler` or `npx wrangler`)

### Step 1: Install Dependencies
```bash
cd cloudflare-control-plane
npm install
```

### Step 2: Configure Local Environment Variables
Copy `.dev.vars.example` to `.dev.vars`:
```bash
cp .dev.vars.example .dev.vars
```

### Step 3: Apply Local D1 Database Migrations
```bash
npx wrangler d1 migrations apply orca-control-plane-db --local
```

### Step 4: Run Local Development Server
```bash
npx wrangler dev
```
The Worker will start locally on `http://localhost:8787` using Miniflare and local SQLite D1.

---

## 4. Production Deployment to Cloudflare

> **Note:** Live D1 `database_id` values remain account- and environment-specific and are never committed to source.

### Step 1: Create the Cloudflare D1 Database
```bash
npx wrangler d1 create orca-control-plane-db
```
*Wrangler will output the unique database ID, for example:*
```text
[[d1_databases]]
binding = "DB"
database_name = "orca-control-plane-db"
database_id = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

### Step 2: Update Canonical `wrangler.jsonc`
Paste your newly generated `database_id` into `wrangler.jsonc`:
```jsonc
"d1_databases": [
  {
    "binding": "DB",
    "database_name": "orca-control-plane-db",
    "database_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "migrations_dir": "migrations"
  }
]
```

### Step 3: Apply Production D1 Migrations
```bash
npx wrangler d1 migrations apply orca-control-plane-db --remote
```

### Step 4: Configure Production Worker Secret
Set the shared API token securely:
```bash
npx wrangler secret put CONTROL_PLANE_API_TOKEN
```
*(When prompted, paste a cryptographically secure token, e.g. 64 random hex characters).*

### Step 5: Deploy the Worker
```bash
npx wrangler deploy
```
*Wrangler will output your live URL (e.g. `https://orca-cloudflare-control-plane.<subdomain>.workers.dev`).*

---

## 5. Configuring ORCA Web Lab (Python Application)

In your Hugging Face Space settings or local `.env`:

```bash
# Enable the Cloudflare Persistent Control Plane
CLOUDFLARE_ENABLED=1

# Cloudflare Worker URL obtained from deployment
CLOUDFLARE_CONTROLLER_URL=https://orca-cloudflare-control-plane.<subdomain>.workers.dev

# Secret API Token (MUST match CONTROL_PLANE_API_TOKEN configured in Worker)
CLOUDFLARE_API_TOKEN=cf_sec_99a8b7c6d5e4f3...

# Project identifier and namespace headers (MUST match Worker configuration)
CLOUDFLARE_PROJECT_ID=orca-web-lab
CLOUDFLARE_NAMESPACE=production

# Network timeout and retry budget
CLOUDFLARE_TIMEOUT_SECONDS=5.0
CLOUDFLARE_MAX_RETRIES=3
```

---

## 6. Verification and Health Checks

```bash
# Health check (unauthenticated)
curl -i https://orca-cloudflare-control-plane.<subdomain>.workers.dev/health

# Authenticated user jobs query with namespace headers
curl -i -H "Authorization: Bearer <YOUR_API_TOKEN>" \
  -H "X-Project-Id: orca-web-lab" \
  -H "X-Namespace: production" \
  https://orca-cloudflare-control-plane.<subdomain>.workers.dev/api/users/chemist_1/jobs
```
