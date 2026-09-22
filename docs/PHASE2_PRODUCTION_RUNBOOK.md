# Phase 2 production runbook

The web process does not own a server-local ORCA subprocess. Run the durable
worker as a separate long-lived service against the same `CHEMISTRY_LAB_STATE_DIR`
used by the web process.

## Windows PowerShell

```powershell
$env:CHEMISTRY_LAB_STATE_DIR = '.\.state-prod'
$env:PYTHONUTF8 = '1'
python -m pip install -r requirements.txt

# Web/API process (use Python 3.11-3.13; the project excludes Python 3.14)
python -m uvicorn api.main:app --host 0.0.0.0 --port 7860 --workers 1

# Separate durable local worker (run as a Windows service, not in the web request)
python -m services.local_orca_worker --state-dir $env:CHEMISTRY_LAB_STATE_DIR --poll-seconds 1

# Optional companion agent on a user's workstation.  Use HTTPS for a remote
# server; plain HTTP is accepted only for loopback deployments.
python -m local_agent.agent --server https://your-space.example --data-dir C:\ChemistryLabAgent --device-name "Workstation A"
```

For a Windows service, install the worker command with NSSM or Task Scheduler
using the same virtual-environment interpreter and state directory. Configure
automatic restart on non-zero exit. Do not run two workers against the same
state directory unless deliberately testing lease/fencing recovery.

## Linux/systemd

Run the web process and worker as separate units. The worker unit can be based
on the following template (replace the paths and user):

```ini
[Unit]
Description=ORCA Web Lab durable local worker
After=network-online.target

[Service]
Type=simple
User=orca
WorkingDirectory=/opt/orca-web-lab
Environment=CHEMISTRY_LAB_STATE_DIR=/var/lib/orca-web-lab
Environment=PYTHONUTF8=1
ExecStart=/opt/orca-web-lab/.venv/bin/python -m services.local_orca_worker --state-dir /var/lib/orca-web-lab --poll-seconds 1
Restart=always
RestartSec=5
KillMode=control-group
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

The web unit should use:

```bash
/opt/orca-web-lab/.venv/bin/python -m uvicorn api.main:app --host 0.0.0.0 --port 7860 --workers 1
```

The companion Local Agent remains optional and is a separate user/workstation
process. It is needed only when a job targets `local_agent`; it is not needed
for `server_local` jobs.

The agent must be run as its own Windows service/task (or a systemd user unit
on Linux) when unattended execution is required. Do not start it from the web
request process. The pairing TXT is a short-lived bootstrap artifact and is
removed after the browser claims the connection API.

## State and recovery

Keep the state directory on durable local storage and back it up before moving
or upgrading the deployment. The worker creates/migrates its SQLite schema
non-destructively, enables WAL/foreign keys/busy timeout, and reconciles
non-terminal jobs at startup. A stale PID is never trusted without process
creation-time and executable/workspace verification.

Configure `KAGGLE_API_TOKEN` (preferred) or `KAGGLE_KEY` as a Kaggle User
Secret for notebook self-continuation. Server-side credentials are never
embedded in uploaded notebook source.
