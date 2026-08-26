# Security Policy: ORCA Web Lab

## 1. Supported Versions

Security updates and critical patches are actively provided for the following releases:

| Version | Supported | Status |
| :--- | :--- | :--- |
| `1.0.x` | Yes | Current Stable Production |
| `< 1.0.0` | No | Legacy Development Snapshots |

---

## 2. Reporting a Vulnerability

If you discover a security vulnerability within ORCA Web Lab, please report it privately:

- **Security Disclosure Email:** `abd.19990044@gmail.com` (or repository security advisory)
- **Response SLA:** Initial acknowledgment within 24 hours; severity assessment and fix timeline within 72 hours.
- **Responsible Disclosure:** Please do not disclose vulnerabilities publicly in issues or forums until a fix and security release have been published.

---

## 3. Threat Model and Security Architecture

### A. ORCA Package Distribution Archive Security
- **Format Enforcement:** Only compressed `.tar.xz` (`.txz`) archives are accepted for custom ORCA binary distribution packages.
- **Path Traversal & Tar-Slip Protection:** Every member inside uploaded archives is strictly inspected before extraction or storage. Any member with absolute paths (`/`, `C:\`), relative parent traversal (`..`), Windows UNC shares (`\\`), null bytes (`\0`), control characters, device nodes, FIFOs, or symlinks/hardlinks pointing outside the designated extraction directory is rejected immediately.
- **Decompression Bomb Mitigation:** Maximum upload size is enforced (500 MB upload limit, 2 GB uncompressed limit, 25,000 maximum member count, 100x maximum compression ratio).
- **Per-User Quotas:** Individual accounts are constrained to 10 stored distribution archives and 2 GB total quota.

### B. Access Control and Ownership Scoping
- **Archive Scoping:** Every uploaded archive is bound to the authenticated owner (`owner_id` via Google Auth session, Kaggle identity, or authenticated request context).
- **Horizontal Privilege Isolation:** User A cannot access (`GET /api/orca/archive/<id>`), list, or delete (`DELETE /api/orca/archive/<id>`) User B's archive. Unauthorized requests return `403 Forbidden`.
- **Result Artifact Protection:** Calculation results and Molden files in `ResultArtifactStore` are partitioned by `(owner, job_id)`. Non-owners cannot download or inspect results.

### C. Subprocess Execution Security
- Subprocess execution is performed with argument vectors (`list[str]`), avoiding unescaped shell concatenation.
- Explicit execution timeouts and isolated working directories are strictly enforced.

### D. Secret Protection and Credential Sanitization
- API keys (Kaggle API tokens, Cloudflare tokens, Google OAuth client secrets) are never committed to git, logged in plain text, or serialized into canonical scientific records.
- Canonical export layers (`tools/normalize_scientific_json.py`, `tools/export_training_jsonl.py`) automatically scrub authentication fields via fail-closed sanitizers.
- Local configuration files (`.dev.vars`, `kaggle.json`, `.state/`, `.pytest_cache/`) are ignored in `.gitignore`.

### E. Encrypted Kaggle Credential Persistence (AES-256-GCM AEAD)
- **Zero Plaintext Persistence in Cloudflare:** Cloudflare D1 stores only high-entropy ciphertext, 12-byte random IVs (nonces), and 16-byte AEAD authentication tags. Plaintext Kaggle credentials never touch Cloudflare storage or Cloudflare logs.
- **Fail-Closed Master Key:** The encryption key is a 256-bit (32-byte) secret passed via the environment variable `KAGGLE_CREDENTIALS_ENCRYPTION_KEY`. The Cloudflare Worker never possesses this master key and cannot decrypt stored records.
- **Owner-Bound Associated Authenticated Data (AAD):** Encryption binds the user's canonical owner ID into the AEAD authenticated payload (`owner:<owner_id>`). Any cross-tenant substitution, record swapping, or tampering triggers immediate cryptographic authentication tag verification failure (`InvalidTag`).
- **Memory-Only In-Process Decryption:** Decryption occurs strictly in process RAM on Hugging Face / ORCA Web Lab backend. When a Space sleeps or restarts, credentials are reconstituted in memory on demand for background reconciliation and watchdog sweeps.
- **Strict Multi-Tenant Isolation:** When User B visits or wakes a shared Space, background recovery processes User A's stalled jobs without exposing User A's credentials, tokens, or job outputs to User B's session or browser.

---

## 4. Release Integrity and CI Quality Gate

- All releases must pass the comprehensive CI quality gate (`.github/workflows/ci.yml`) including static analysis, full test matrix (Python 3.11, 3.12, 3.13 on Linux and Windows), security scanning, and clean-clone verification.
- Release manifests report cryptographic SHA-256 digests for all generated datasets and artifacts.

