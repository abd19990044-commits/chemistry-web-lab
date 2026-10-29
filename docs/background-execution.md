# Background calculations and local ORCA execution

Calculation chains are registered on the application server before the UI reports
that they were accepted. Their settings, dependency order, execution attempts,
and backend job identities survive browser closure. The application supervisor
checks them every 30 seconds by default, collects completed outputs, validates
convergence, and submits the next stage with the latest valid geometry.
Failed or cancelled predecessors block subsequent stages. Active cancellation
remains pending until the execution backend confirms it.

## Local computer: one application command

Install the project dependencies, then run:

```bash
python app.py
```

Open `http://127.0.0.1:7860`. Configure the ORCA executable and input/output
directories in the server-host ORCA settings. In a generated chained workflow,
select **This application host / local computer**, then **Run workflow in
background**. The application starts its local execution worker internally;
no companion script, connection token, or second terminal is required for ORCA
installed on that same computer. The application and ORCA installation must
remain running; the browser can be closed.

The same lifecycle is supported by `uvicorn api.main:app`. Standalone Flask
startup binds to localhost by default. A deployment on a different computer
executes ORCA on that application host, not on the browser's computer.
Server-host execution on shared deployments remains restricted to configured
administrators. A separate companion remains available for a different computer.

## Kaggle

Sign in to Kaggle, select the ORCA dataset or archive link, and submit the chain.
The complete chain is recorded on the server. Calculations waiting for Kaggle's
capacity are submitted later by the supervisor. Initial submission still checks
the site's configured Kaggle execution passcode; the passcode is not copied into
the saved scientific stage configuration. Refreshing or reopening the UI reloads
the server-owned calculation list.

The notebook's existing ORCA-window checkpoint/restart logic remains separate
from the chain of different calculation stages. The notebook can use Kaggle User
Secrets for self-continuation; the server-side supervisor can also reconcile
and resume supported windows while the application is online.

## Encrypted connection storage

- Kaggle API credentials use AES-256-GCM with the owner identity authenticated
  as associated data. A local SQLite mirror holds ciphertext, nonce, tag, and
  non-secret account metadata. Cloudflare D1 receives the same encrypted records
  when configured. A fresh process can recover without the old RAM broker.
- Local ORCA connection settings are encrypted as authenticated JSON envelopes.
  Existing plaintext settings are migrated when read successfully.
- Key selection is an explicit `KAGGLE_CREDENTIALS_ENCRYPTION_KEY`, then a
  domain-separated key derived from environment `SECRET_KEY` or
  `FLASK_SECRET_KEY`. An invalid explicit encryption key fails closed.
- Standalone local installations without those environment secrets generate a
  private `connection_vault.key` in the state directory. Keep this installation
  key when updating the application. Protect backups with the operating system's
  access controls; access to both the key and database permits decryption.
- Keep production keys stable in hosting secrets. Changing them without key
  migration makes old records unreadable. Keys, runtime settings, and credential
  databases are excluded from Git, Docker context, and release packages.
- Browser APIs return only masked credential status. Forgetting a saved account
  clears its local cache and records a deletion tombstone; an old Cloudflare
  record cannot silently restore it after a failed remote deletion.

## Hosting and operation

Use a persistent `CHEMISTRY_LAB_STATE_DIR` for workflow state and encrypted
connection records. Browser independence does not keep a sleeping hosting plan
awake. A stopped application cannot submit another stage until it starts again.
If the platform wipes its ephemeral filesystem, unsubmitted workflow settings
on that filesystem are lost; Cloudflare credential replication alone does not
back up complete scientific workflows. Use retained storage for unattended jobs.

The Docker entrypoint continues to supervise its local worker process and sets
`CHEMISTRY_LAB_EXTERNAL_LOCAL_WORKER=1`; the web lifecycle therefore starts only
the workflow supervisor. Standalone local startup embeds the local worker.
`CHEMISTRY_LAB_EMBEDDED_LOCAL_WORKER=0` explicitly disables embedded execution;
`CHEMISTRY_LAB_BACKGROUND_ENABLED=0` disables lifecycle-managed background work.
`CHEMISTRY_LAB_WORKFLOW_POLL_SECONDS` changes the default 30-second observation
interval. Health/readiness includes the required background runtime and tests
the actual state volume, not only the temporary directory.

Tests use a scripted ORCA stand-in to exercise local execution and synthetic
ORCA-format outputs for convergence/geometry handoff. They do not substitute
for validating a licensed ORCA installation or a live Kaggle account.
