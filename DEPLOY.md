# Deploying to Hugging Face Spaces

A checklist for getting this running and confirming it actually works. The
smoke tests in §4 matter more than the upload itself — a Space that boots is
not the same as a Space that can carry a calculation across two Kaggle
sessions.

---

## 1. Files to upload

Upload **everything except `__pycache__`**. That is 37 files, about 836K.

```
ARCHITECTURE.md          DEPLOY.md            LICENSE.txt
README.md                .gitignore
Dockerfile               requirements.txt

app.py                   chem_core.py         kaggle_runner.py
index.html               app.js               style.css

orca_orchestrator/
    __init__.py          api.py               checkpoints.py
    config.py            credentials.py       errors.py
    hashing.py           kaggle_api.py        ledger.py
    logging_ext.py       models.py            orca_artifacts.py
    reconciler.py        retry.py             service.py
    states.py            store.py             watchdog.py
    runner/
        __init__.py      builder.py           kernel_runner.py

tests/
    test_orchestrator.py test_lifecycle_simulation.py
```

**Do not delete `kaggle_runner.py`.** `app.py` still imports it to serve the
legacy `/api/kaggle/*` routes. Any ORCA chain already running on Kaggle right
now was started by that code and still hands off through it; removing the file
strands those calculations mid-flight.

**Do not upload `__pycache__/`.** Those `.pyc` files were compiled by a
different Python version than the container's, and at best they are ignored.

`tests/` is optional for the Space but costs 56 KB and lets you re-run the
verification suite inside the real container, which is the only place it runs
against the exact Python and library versions that will serve users.

### Uploading

Either drag the whole folder into the Space's **Files** tab, or:

```bash
git clone https://huggingface.co/spaces/<user>/<space>
cd <space>
# copy the files in
git add -A && git commit -m "Fault-tolerant checkpoint/restart subsystem"
git push
```

---

## 2. Required setting

In **Settings → Variables and secrets**, add one secret:

| Name | Value | Why |
|---|---|---|
| `SECRET_KEY` | any long random string | Without it each gunicorn worker derives its own session key, and with `--workers 2` a signed-in user is silently signed out on roughly half their requests |
| `ORCA_STATE_DIR` | `/app/.state` | Optional but recommended. Pins every worker to one database explicitly, so the leases and idempotency keys that prevent duplicate work definitely coordinate across workers |

Generate one with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Everything else has a working default. `ARCHITECTURE.md §7` lists the full set
if you want to tune budgets later.

### Optional: persistent storage

If you enable paid persistent storage, also set `ORCA_STATE_DIR=/data`. This is
a **performance** setting, not a correctness one. The orchestrator treats its
local database as a disposable cache; the authoritative record of every job is
the `STATE.json` each Kaggle window writes into its own saved output, so a
wiped Space rebuilds every job from Kaggle on the next status poll. Without
persistent storage that rebuild simply happens more often.

---

## 3. Confirming the build

Watch the build log. Expect roughly two minutes, most of it installing RDKit.

Once running, the container prints one JSON object per line. A healthy boot
looks like:

```json
{"event":"api_registered","url_prefix":"/api/orca"}
{"event":"store_ready","db_path":"/app/.state/orchestrator.sqlite3","journal_mode":"wal","pid":7}
{"event":"startup_recovery","expired_leases_cleared":0, ...}
{"event":"watchdog_started","interval_seconds":120, ...}
```

**Every one of those lines must appear twice — once per gunicorn worker**, with
different `pid` values. Both workers run their own watchdog; the fenced leases
in the shared SQLite file stop them from doing the same work twice.

**Compare the two `db_path` values — they must be identical.** If the workers
report different paths (one `/app/.state/...`, the other `/tmp/orca-state-...`),
they are using separate databases, and the leases and idempotency keys that stop
duplicate work coordinate nothing between them. `store_ready` now reports
`state_dir_shared`; if it is `false`, an ERROR line follows explaining the
consequence, and `/api/orca/health` reports it under `state_dir_diagnostic`.
Setting `ORCA_STATE_DIR` explicitly removes any doubt.

Check that `store_ready` reports `"journal_mode": "wal"`. If it reports
`"delete"` instead, the database fell back to rollback-journal mode: correct,
but slower under concurrency. If you see `wal_unavailable`, the two workers
could not agree on the mode — report it, since it should not happen.

**The watchdog is quiet on purpose.** After boot you will *not* see a sweep
line every two minutes. An uneventful sweep logs at DEBUG; INFO is reserved for
sweeps that actually did something, so an INFO line from `orca.watchdog` always
means something worth reading. A `watchdog_alive` summary still appears
periodically, so silence and death remain distinguishable.

If `store_ready` appears only **once**, one worker failed to initialise. That is
no longer fatal — the routes are registered regardless and the service is built
on the first request that needs it — but check `/api/orca/health` and look for
`"ready": false` before assuming the deploy is healthy.

---

## 4. Smoke tests

### 4.1 The Space is alive

```
GET https://<your-space>.hf.space/health
```

Expect `"ok": true` and `"orchestrator": true`. If `orchestrator` is `false`,
the package failed to import and the reason is in the build log — the site
falls back to the legacy routes rather than going down entirely.

### 4.2 The state machine loaded

```
GET https://<your-space>.hf.space/api/orca/state-machine
```

Expect 14 states. This response is generated from the live transition table, so
if it renders, the FSM is intact.

### 4.3 Credentials and job discovery

```bash
curl -X POST https://<your-space>.hf.space/api/orca/login \
  -H 'Content-Type: application/json' \
  -d '{"kaggle_username":"YOU","kaggle_key":"YOUR_TOKEN"}'
```

Expect `ok: true` and a `jobs` array. Any job you submitted with the *old* code
should appear here — that array is rebuilt from Kaggle, not from your browser,
so it is also the test that legacy adoption works.

### 4.4 Re-run the verification suite inside the container

From the Space's terminal, if you have one:

```bash
python tests/test_orchestrator.py
python tests/test_lifecycle_simulation.py
```

Expect `136 passed, 0 failed` and `29 passed, 0 failed`.

---

## 5. The real test: a job that must restart

Everything above proves the plumbing. Only this proves the subsystem.

Submit a geometry optimisation **deliberately too large to converge in one
session** — a molecule of 40–60 atoms at, say, B3LYP/def2-TZVP with `Opt`. You
want it to hit `%geom MaxIter` or the session budget, because that is the exact
path that failed before.

Then watch for these events in the Kaggle notebook's log, in order:

| Event | Means |
|---|---|
| `disk_baseline` | Disk accounting established. **Check `accounting_mode`** |
| `orca_outcome` | The verdict. `MAXITER_EXHAUSTED` is a *success* for this test |
| `in_session_continue` | It continued in the same session instead of spending a restart |
| `checkpoint_verified` | Written, read back, re-hashed, structurally validated |
| `successor_pushed` | The next window was accepted by Kaggle |
| `handoff_complete` | The chain advanced |

### What to look at first

In `disk_baseline`, the field **`accounting_mode`** is the direct test of the
disk fix. On Kaggle it should read `"quota"`, accompanied by a `statvfs_note`
explaining that the reported free space is the host overlay and is being
ignored. If it instead reads `"min(quota, statvfs)"` and
`statvfs_free_bytes` is around 1 TB, the plausibility threshold needs lowering
— but that combination should not occur.

In `orca_outcome`, the field **`opt_converged`** paired with **`outcome`** is
the direct test of the MaxIter fix. The failure you reported was
`opt_converged: false` with the job marked finished. You should now never see
`outcome: COMPLETE` alongside `opt_converged: false` for an `opt` job.

### Expected timeline

The second window starts within a few minutes of the first ending. Poll
`/api/orca/status` and watch `epoch` go from `0` to `1` and
`cumulative_opt_cycles` keep climbing rather than resetting. A reset to zero
would mean the successor restarted from the original geometry — the exact bug
the lifecycle simulation now guards against.

---

## 6. If something goes wrong

Every failure line carries four fields, always: `failure_what`, `failure_why`,
`recovery_attempted`, `next_action`. Read `next_action` first — it tells you
whether the system is already handling it.

To trace one job's whole life, filter the Space log by its `job_id`. To trace
one request's fan-out, filter by `correlation_id`.

Inside a Kaggle window, `RUN_LOG.jsonl` is saved with the notebook's output, so
a window that finished days ago still has a complete machine-readable trace you
can download.

A job that appears stuck: `POST /api/orca/status` forces a reconciliation, and
`POST /api/orca/sweep` forces a full watchdog pass over every job whose owner
has signed in recently. Both are safe to call repeatedly — every operation in
this system is idempotent.
