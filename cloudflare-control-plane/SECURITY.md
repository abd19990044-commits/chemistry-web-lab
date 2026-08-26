# Security Policy: ORCA Web Lab Cloudflare Control Plane

## 1. Supported Versions

| Version | Supported | Status |
| :--- | :--- | :--- |
| `1.0.x` | Yes | Current Stable Production |
| `< 1.0.0` | No | Legacy Development Snapshots |

---

## 2. Reporting a Vulnerability

If you discover a security vulnerability within the Cloudflare Control Plane, please report it privately:

- **Security Disclosure Email:** `abd.19990044@gmail.com`
- **Response SLA:** Initial acknowledgment within 24 hours; severity assessment and fix timeline within 72 hours.
- **Responsible Disclosure:** Please do not disclose vulnerabilities publicly until a fix and security release have been published.

---

## 3. Threat Model and Security Architecture

### A. Fail-Closed Authentication
- The Cloudflare Worker requires bearer token authentication (`CONTROL_PLANE_API_TOKEN`) for all endpoints except `/health`.
- If `CONTROL_PLANE_API_TOKEN` is missing or unconfigured on the Worker, it fails closed with HTTP `503 CONFIG_ERROR`.
- `X-Project-Id` and `X-Namespace` headers are strictly enforced to prevent cross-tenant and cross-environment leakage.

### B. Zero Plaintext Persistence in D1 (AES-256-GCM AEAD)
- Cloudflare D1 stores only high-entropy ciphertext, 12-byte random initialization vectors (nonces), and 16-byte AEAD authentication tags.
- The Cloudflare Worker has no master decryption key and never decrypts records.
- Plaintext Kaggle API credentials never touch Cloudflare storage or Cloudflare logs.

### C. Multi-Tenant Owner Isolation
- All database queries in `src/db.ts` are strictly parameterized and scoped by `owner`.
- Cross-tenant record queries or mutations return HTTP `404 NOT_FOUND`.
