# Project review: 29 October 2026

Scope: application startup, Flask/FastAPI routes, authentication and tenant
isolation, upload/archive boundaries, scientific analysis and thermochemistry,
Kaggle/Cloudflare orchestration, desktop ORCA execution, browser chemistry,
publication exports, dependency security, packaging, and deployment.

## Confirmed findings and changes

| Priority | Finding | Resulting behavior |
| --- | --- | --- |
| High | Generator chains and capacity-overflow submissions were stored in a browser-owned queue. Closing the page could strand later stages. | Complete chains are registered in the durable server workflow ledger before acceptance. A lifecycle supervisor observes outputs and dispatches dependency-ready stages without UI polling. |
| High | Cloudflare's in-memory fallback could acknowledge credential saves without surviving a server restart. Application-owner identities also differ from Kaggle usernames. | An AES-256-GCM SQLite mirror persists encrypted records; internal recovery resolves the verified account alias. Cloudflare remains an encrypted replica. |
| Medium | Local ORCA settings were saved in plaintext. Same-computer execution required a separately started worker. | Connection settings use authenticated encrypted envelopes with legacy migration. Desktop startup embeds the existing durable local worker. Docker retains its explicit external worker. |
| High | A reaction execution route could bypass the site's Kaggle submission passcode. | Initial reaction and calculation-chain execution verify the configured passcode. Credentials/passcodes are not copied into scientific stage snapshots. |
| Medium | Reaction cancellation could change a local label while leaving backend execution active. | Tracked attempts enter cancellation pending; the supervisor requests backend cancellation and retries unavailable backends. Future stages do not start. |
| Medium | Readiness omitted missing analyzer/failed orchestration state and checked only temporary storage. | Readiness fails for unavailable core components, failed required background services, or an unwritable actual state volume. |
| Medium | Extreme equilibrium constants emitted Infinity or rounded to zero; missing temperatures/entropy/ZPE could produce misleading results or break UI formatting. | JSON remains finite; lnK/log10K and explicit range notes represent extreme constants. Missing values remain unavailable. Non-finite inputs are rejected, zero values remain visible, and missing temperature is not borrowed from another species. |
| High in development tools | npm audit found three affected dependency nodes through an outdated undici in the Wrangler/Miniflare toolchain. | Updated Wrangler and lockfile; subsequent npm audit reports zero known vulnerabilities. This was a tooling dependency finding, not proof of an exploited deployed Worker. |
| Tooling | The installed local pip had published advisories. | Updated the audit environment and made the Docker build upgrade pip before dependency installation. |
| Low | The release verifier accepted Python 3.14 despite the declared supported range. | Verification now agrees with the Python 3.11-3.13 project requirement. |

## Verification evidence

- Initial complete Python run: **1241 passed, 9 failed, 11 skipped** of 1261.
  Four failures came from multiprocessing-manager Unix sockets rejected by this
  execution environment. Five concerned process identity/adoption: a live child
  PID was not visible through this environment's `/proc`/psutil view. A separate
  probe confirmed `Popen.poll() is None` while psutil reported that PID missing.
  These failures were retained and reported; production code was not weakened
  to bypass process-identity checks.
- Added 26 focused regression cases for the findings and new execution paths.
  A combined 100-test security, background-execution, analyzer, packaging, and
  ownership run passed. Separate scientific/workflow and UI suites were run
  during implementation.
- A local embedded worker completed a two-stage chain using a scripted ORCA
  stand-in. A mocked Kaggle transport completed a chain across fresh supervisor
  instances, passed optimized coordinates onward, and did not duplicate stages.
- Concurrency, credential restart recovery, owner isolation, rejected invalid
  keys, failed replacement preservation, cancellation/pause guards, release
  exclusion of keys, strict scientific JSON, and real DOM-table formatting
  logic were tested.
- The pinned Open Babel WASM worker passed actual UFF/MMFF94 convergence,
  progress, stop, invalid-input, and molecular bond/charge preservation checks.
- Cloudflare TypeScript validation and six Vitest streaming-body tests passed.
- Python compilation and blocking undefined-name checks passed. JavaScript
  syntax checks passed; the initial HTML scan found no duplicate IDs or missing
  local script sources.
- Direct pinned Python dependencies and the installed audit environment passed
  pip-audit after updating pip. npm audit passed after the tooling update.
- The repository-wide initial Ruff inventory contained 1262 findings across
  application code, tests, and nested engine settings. These include import/style
  debt and are not equivalent to 1262 runtime failures. The CI's blocking
  undefined-name checks remain enabled; no bulk cosmetic rewrite was attempted.

GitHub's full Linux/Windows matrix and release/deployment checks are the final
cross-platform gate for the changed revision. Their outcome should be read from
the corresponding commit's Actions runs rather than inferred from the local
environment's process restrictions.

## Practical limits

The local execution test uses a scripted stand-in, not a licensed quantum
calculation. No live Kaggle job was submitted during this review. The latest
interactive production-browser inspection remains unavailable after the browser
approval service rejected access because of its usage limit. Local tests and
deployment checks do not certify the deployed UI by visual inspection.

Browser independence requires the application host to remain online. A sleeping
Space cannot dispatch another stage until resumed; persistent state and a stable
encryption key are required across restarts. Cloudflare credential replication
does not back up complete unsubmitted workflow settings. No multi-user load test
or claim of uninterrupted hosting availability is included.

Operation and key management: [background execution](background-execution.md).
