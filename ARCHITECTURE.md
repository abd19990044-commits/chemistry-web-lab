# ORCA Checkpoint/Restart Subsystem — Architecture

**Version 2.0** · `orca_orchestrator`

This document explains why the previous checkpoint/restart design failed, what
replaced it, and why each choice was made. It is written to be read by someone
who has to operate this system at 3 a.m. without the author present.

---

## 1. The two bugs in the production log

Your Kaggle log is the right place to start, because both failures it shows are
symptoms of the same underlying disease: **the system inferred state instead of
recording it.**

### 1.1 A six-hour calculation was declared finished while unconverged

```
23022.6s  done=True opt_converged=False stopped_by=None orca_error=False disk_failure=False
23022.6s  [done] ORCA terminated normally.
```

`kaggle_runner.py:1157` decided completion like this:

```python
if orca_normal_end and not orca_error and stop_reason["why"] is None:
    needs_continue = False      # "finished cleanly"
```

`opt_converged` was computed on line 927 and then **never used in the decision.**
The ORCA 6 manual warns about exactly this inference:

> Even if the optimization does not converge, the ORCA output may still end with
> `****ORCA TERMINATED NORMALLY****`. Therefore do not rely on the presence of
> this line as an indicator of whether the geometry optimization is converged!

What happened: the optimisation hit `%geom MaxIter` (ORCA's default is 200
cycles), ORCA printed *"The optimization did not converge but reached the
maximum number of optimization cycles"*, then terminated normally with exit code
0. After **6 h 23 m 54 s** the system reported success, packaged partial results,
and stopped. No restart, no warning, no checkpoint.

Note also that ORCA wraps that warning across a line break, so even a naive
substring search for the whole sentence would have missed it. Matching now
happens after whitespace normalisation.

**Why it is dangerous.** This is the worst failure class a scientific pipeline
can have: it produces a wrong answer *confidently*. A researcher takes the
"finished" geometry, publishes an energy computed at a non-stationary point, and
nothing anywhere indicates a problem. Silent incorrectness beats loud failure
every time, in the bad sense.

**Fix.** `orca_artifacts.classify_outcome()` requires the job-type convergence
marker. Normal termination without it is `MAXITER_EXHAUSTED` — a *continuable*
outcome. `Opt Freq` additionally requires `VIBRATIONAL FREQUENCIES` to be
present; a converged geometry with no frequencies continues with the frequency
stage only, rather than redoing the optimisation.

### 1.2 Free disk space was measured against the wrong filesystem

```
23022.6s  ... free=1006.8 GB
```

Your observation is exactly right. `_free_bytes()` called
`shutil.disk_usage(path).free`, which reports the **host overlay filesystem**
the container is layered on. Kaggle enforces its quota separately, on top of
that: roughly **20 GB** for `/kaggle/working` and about **60 GiB** of scratch.

The watchdog's test was `free < MIN_FREE_BYTES`, i.e. `1006.8 GB < 5 GB`. That
is unsatisfiable. **The disk watchdog could never fire, on any job, ever.**

There was a second, subtler consequence. This clamp:

```python
_floor_cap = max(128 * 1024 * 1024, SCRATCH_FREE // 4)
```

computed a "safety" cap of ~250 GB from the bogus figure, so the code that was
supposed to protect against a smaller-than-expected machine also did nothing.

**Why it is dangerous.** The entire disk-pressure recovery path — the one the
README describes at length as the fix for large calculations — was dead code in
production. A job that fills the scratch quota does not checkpoint cleanly; it
dies on `ENOSPC` mid-write, which is precisely the condition that produces
half-written `.hess` and `.gbw` files. Your "restart files are incomplete"
symptom is downstream of this bug.

**Fix.** `orca_artifacts.DiskAccountant` takes headroom as the **minimum** of
three independent estimates: quota minus measured consumption, statvfs free
(used only as an upper bound, and discarded entirely when it exceeds a
plausibility threshold), and a **real periodic write probe**, because a quota
can be enforced purely by the write path returning `ENOSPC`/`EDQUOT` while
statvfs keeps reporting a terabyte. All three numbers are logged side by side,
always, so this can never again be invisible.

### 1.3 The compounding waste

Both bugs together produced the specific shape of your log. ORCA exited at
MaxIter after 6 h 24 m of an 11 h 45 m budget. The old runner invoked ORCA
exactly once per session, so **five hours of paid-for compute sat idle** and a
whole restart would have been spent to do what could have been done in the same
session. The new runner loops *within* the window: no push, no queue wait, no
re-extraction of a multi-gigabyte ORCA package.

---

## 2. The root cause behind all of it

The old design had **no state**. It had *inferences*.

`check_job_status()` asked Kaggle for a status word and re-derived, from
scratch, on every poll, what that implied. The only durable record of a job's
existence was `localStorage` in one browser. Nothing recorded what had already
been decided or acted upon.

Every symptom you listed follows mechanically from that:

- *State becomes inconsistent* — because two pollers could reach different
  conclusions from the same kernel and neither wrote anything down.
- *The application loses track of the current checkpoint* — because there was no
  checkpoint object, only files that happened to be in a directory.
- *Duplicated work* — because "did I already push the successor?" had no answer
  other than looking at Kaggle and guessing.
- *Jobs become stuck* — because a missed transition was unrecoverable; there was
  no loop that would notice and retry.
- *Restarting requires manual intervention* — for the same reason.

---

## 3. Complete flaw catalogue

Ordered by severity. "Old" refers to `kaggle_runner.py` / `app.py` as they
stood; "New" names the component that addresses it.

| # | Flaw | Why it is dangerous | Fix |
|---|------|--------------------|-----|
| 1 | `ORCA TERMINATED NORMALLY` treated as completion | Silently produces scientifically wrong results at a non-stationary point | `classify_outcome()` requires a job-type convergence marker |
| 2 | `shutil.disk_usage()` measures the host overlay | The entire disk-recovery path is dead code; jobs die on `ENOSPC` instead of checkpointing | `DiskAccountant`: quota accounting + write probe |
| 3 | No server-side state at all; job list lives in `localStorage` | Clearing browser data destroys the job list; nothing can recover an unwatched chain | Kaggle-side `STATE.json` ledger + SQLite cache |
| 4 | No finite-state machine; state inferred per poll | Two observers disagree; no transition is recorded; stuck jobs are undetectable | `states.py` — explicit table, illegal transitions raise |
| 5 | Checkpoints never verified | A truncated `.hess` is passed to the successor, which aborts an hour later for no visible reason | Three-phase transaction: staged → verified → committed |
| 6 | No rollback path | One bad checkpoint ends the chain permanently | `select_rollback_target()`, searching strictly backwards |
| 7 | Restart payload silently trimmed at 400 KB | Large Hessians and trajectories discarded without a word; work already paid for is thrown away | Two transports: inline, or bundle fetched from the predecessor's Kaggle output |
| 8 | Re-pushing a kernel that is already running | Kaggle schedules a **second concurrent run** writing to the same `/kaggle/working` — two ORCA processes interleaving writes | Status probe before push + in-kernel `run_token` heartbeat guard |
| 9 | `NEXT_JOB_ID.txt` read without epoch stamping | `/kaggle/working` persists across runs, so a leftover pointer from a previous run is followed; the chain appears to advance but does not | Every ledger record carries `epoch` + `run_token`; `is_stale_relative_to()` |
| 10 | No idempotency on submit | A double-clicked button or refreshed POST launches two 12-hour notebooks | `Idempotency-Key` + SQLite claim table |
| 11 | No mutual exclusion between workers | 2 gunicorn workers × 4 threads can all act on one job | Fenced leases (`BEGIN IMMEDIATE` + monotonic fence token) |
| 12 | Retry classification by substring matching, scattered | An unrecognised Kaggle message becomes "permanent" and kills a chain; a job *named* `error-test` becomes an errored job | Typed exception hierarchy; classification happens once, at one boundary |
| 13 | Retries unbounded by wall clock | Retries consume the time reserved for packaging; the window's work is lost | `RetryPolicy(deadline_seconds=…)`; the in-kernel push reserves 25 min |
| 14 | 1300-line in-kernel program inside a string literal | Cannot be compiled, linted, imported or tested; every bug is found in production, 12 hours at a time | `runner/kernel_runner.py` is a real module, embedded at push time |
| 15 | Single ORCA invocation per session | A MaxIter exit at hour 6 wastes the remaining 5 hours *and* a restart | In-session continuation loop |
| 16 | Non-atomic writes of control files | An interrupted write leaves JSON that the next reader parses as garbage | `atomic_write_*`: temp file → fsync → `os.replace` → fsync dir |
| 17 | Restart budget inherits the same `MaxIter` | A long optimisation makes one window of progress and then stalls in the same place forever | `set_geom_maxiter()` per window + a cumulative-cycle budget |
| 18 | No cumulative progress budget | A non-converging system consumes 20 × 11 h of the user's Kaggle quota for nothing | `max_total_opt_cycles`, tracked across all windows |
| 19 | `app.secret_key = os.environ.get(...) or secrets.token_hex(32)` | Gunicorn runs without `--preload`, so **each worker generates a different key**; signed-in users are randomly signed out on ~half of requests | `_resolve_secret_key()`: env → shared persisted key → warn |
| 20 | Every Kaggle failure returned HTTP 502 | The front end cannot distinguish "retry in 15 s" from "your credentials are wrong" | Typed errors → 400/401/404/409/413/422/429/503 with `Retry-After` |
| 21 | Deleting a job deleted only the newest window | Older windows survive, `list_jobs` regroups them, the deleted job reappears | `delete()` removes the whole chain |
| 22 | Credentials embedded in kernel source, unscrubbed | A traceback prints module globals into saved notebook output, persisting the API token in readable form | `sys.excepthook` scrubber, log redaction filter, credential files removed after push |
| 23 | Status poll downloaded full output | A finished job looks permanently stuck because the request exceeds the web-server timeout | Control files only (`fetch_ledger_files`, ~KB); results are a separate endpoint |
| 24 | No preflight for `enable_internet` | Push succeeds, the run has no network, and the successor push fails at hour 12 | Preflight check at minute one |
| 25 | No heartbeat | A window that dies silently is indistinguishable from one that is working | 45 s heartbeat + watchdog grace period |
| 26 | No structured logging | "Why did this fail and what happens next?" is unanswerable from the logs | `log_failure()` requires *what / why / recovery / next action* |

---

## 4. The new architecture

### 4.1 Where truth lives

Neither obvious option survives the failures this system must tolerate:

- The **Flask process** is restarted by every Hugging Face redeploy, every OOM,
  every sleep/wake. On the free tier its filesystem is wiped.
- The **Kaggle kernel** lives at most twelve hours and has no identity that
  outlives it.

The third option survives both: **a kernel's saved output persists in the
owner's Kaggle account indefinitely**, is readable through the API with the
owner's own credentials, and is written by the only actor that always knows the
truth — the kernel actually running the calculation.

```
┌──────────────────────────────┐
│  Browser (a view, never truth)│
└──────────────┬───────────────┘
               │ HTTPS
┌──────────────▼───────────────┐        ┌───────────────────────────┐
│  Flask + orca_orchestrator   │        │  SQLite (WAL)             │
│  · reconciler (level-driven) │◄──────►│  cache · leases · events  │
│  · watchdog (stall sweeper)  │        │  DISPOSABLE               │
└──────────────┬───────────────┘        └───────────────────────────┘
               │ kaggle CLI
┌──────────────▼─────────────────────────────────────────────┐
│  Kaggle kernel  (epoch N)                                  │
│  /kaggle/working/                                          │
│    STATE.json             ← SOURCE OF TRUTH                │
│    CHECKPOINT.json        ← manifest, per-file SHA-256     │
│    CHECKPOINT_BUNDLE.zip  ← files too large to ride inline │
│    HEARTBEAT.json         ← liveness, every 45 s           │
│    RUN_LOG.jsonl          ← full structured trace          │
│    results.zip                                             │
└────────────────────────────────────────────────────────────┘
```

**Losing SQLite costs API calls, never correctness.**
`ledger.rebuild_from_kaggle()` reconstructs a complete manifest from Kaggle
alone. It runs on three real paths: a Space restart wiped the cache, the user
signed in from another device, or the job predates this orchestrator entirely.

### 4.2 The state machine

Fourteen states. Thirteen are yours; `ROLLING_BACK` was added so that automatic
rollback is an *explicit transition* rather than something that happens
implicitly inside an error handler.

```
CREATED ──SUBMIT──► UPLOADING ──PUSH_ACK──► QUEUED
                         │                     │
                    PUSH_RETRY ↺               ├─KERNEL_BOOT_FRESH──► READY
                                               └─KERNEL_BOOT_RESUME─► DOWNLOADING
                                                                          │
                                                          BUNDLE_FETCHED  ▼
                                                                     VERIFYING
                                                                     │       │
                                          BUNDLE_VERIFIED ───────────┘       │
                                                    │          VERIFICATION_FAILED
                                                    ▼                        ▼
                                                RESTORING              ROLLING_BACK
                                                    │                        │
                                          RESTORE_COMPLETE          ROLLBACK_SELECTED
                                                    ▼                        │
                                                  READY ──ORCA_STARTED──► RUNNING
                                                                             │
        ┌────────────────────────────────────────────────────────────────────┤
        │ WINDOW_EXPIRING · DISK_PRESSURE · MAXITER_EXHAUSTED                 │
        │ ORCA_EXIT_INCOMPLETE · HEARTBEAT_LOST          ORCA_COMPLETE ──► FINISHED
        ▼                                                ORCA_FATAL    ──► FAILED
   CHECKPOINTING ──CHECKPOINT_STAGED──► VERIFYING ──CHECKPOINT_VERIFIED──► RESTARTING
                                                                             │
                                              SUCCESSOR_PUSHED (epoch += 1)  ▼
                                                                          QUEUED
```

Three properties are enforced structurally, not by convention:

**Every transition is explicit.** There is no assignment to `job.state` anywhere
outside `Reconciler.transition()`. An undefined `(state, trigger)` pair raises
`IllegalTransitionError` rather than being tolerated — because an undefined
transition means the caller holds a belief the model says is impossible, and
continuing from there is how a job reaches a state nobody can reason about.

**The table validates itself at import.** `TransitionTable.validate()` proves
that no non-terminal state is a dead end, that every state is reachable, and
that every active state can terminate in one hop. That last one matters: a job
must always be cancellable without waiting for a multi-step dance to finish.

**Exactly one transition advances the epoch** — `RESTARTING → QUEUED`. That is
what makes the epoch a trustworthy monotonic clock for the whole chain, which
in turn is what makes stale-ledger detection possible.

`VERIFYING` is deliberately entered from two directions and exits to two:

```
CHECKPOINTING → VERIFYING → RESTARTING    (outbound: sealing a checkpoint)
DOWNLOADING   → VERIFYING → RESTORING     (inbound: accepting one)
```

That symmetry is the point. Producer-side verification catches a kill mid-write.
Consumer-side verification catches corruption introduced by the *transfer* — a
truncated download, a clipped base64 blob, a torn zip. Checking only one side
leaves the other class of failure undetectable.

### 4.3 Checkpoints are transactions

```
   stage ──────► verify ──────► commit
     │              │
     └──► rejected ◄┘
```

- **Stage.** Collect candidate artefacts, hash each one, run its structural
  validator, write `CHECKPOINT.json` + `CHECKPOINT_BUNDLE.zip`. Status:
  `staged`. **A staged checkpoint is never a rollback target**, so a crash
  between staging and verification cannot poison the recovery path.
- **Verify.** Re-read every byte *from disk*, re-hash, re-validate structurally.
  Nothing computed during staging is trusted — the question is precisely whether
  what was written survives being read back. Status: `verified` or `rejected`.
- **Commit.** Only a `verified` checkpoint may be committed, and commit means "a
  successor carrying it was accepted by Kaggle".

**Why hashes alone are insufficient.** A truncated `.hess` has a perfectly valid
SHA-256 — of its truncated self. The hash proves the bytes survived the journey;
it says nothing about whether the producer finished writing them. So every file
gets a structural validator as well:

| Artefact | Check |
|---|---|
| `*_trj.xyz` | Every frame complete; a torn tail is discarded, not rejected — trajectories are append-only, so a hard kill can only damage the final frame |
| `*.hess` | Every row `0..N-1` accumulates exactly `N` values across all column blocks |
| `*.allxyz` | Every `>`-separated image parses; ≥ 2 images; consistent atom count |
| `*.xyz` | Atom count matches, all coordinates numeric |
| `*.gbw` | Size plausibility only — **never required for restart** (see below) |

**The `.gbw` is deliberately never trusted.** A force-killed ORCA can leave the
binary wavefunction half-written; AutoStart then `MORead`s it and the successor
aborts with *"GBWFile is corrupt"*. Restart is driven entirely from ASCII
artefacts, `! NoAutoStart` is forced, and `MOREAD` is stripped from every
continuation input. The `.gbw` is carried only as an SCF-guess optimisation, and
any doubt drops it.

**Rollback searches strictly backwards.** `select_rollback_target()` excludes the
epoch that just failed. Without that constraint the system re-selects the
checkpoint that just poisoned the run and loops — the classic "automatic
recovery that never recovers". If nothing older is verified, the job **fails
loudly** rather than restarting from zero, because restarting from zero silently
repeats work the user already paid for and will very likely fail identically.

### 4.4 Idempotency

| Operation | Mechanism |
|---|---|
| Submit | `Idempotency-Key` header, or a key derived from credentials + input hash. In-flight duplicates return `409`; completed ones replay the stored response |
| Push window *N* | Slug is deterministic (`<base>` / `<base>-r<N>`). Pushing twice is an **upsert of one kernel**, not two kernels |
| Concurrent run of one kernel | In-kernel `run_token` + heartbeat. A second run sees a live heartbeat under a different token and **exits without touching anything** |
| Two workers, one job | Fenced lease. The loser does nothing; because every action is idempotent, there is no partial state to clean up |
| State write | Optimistic concurrency (`version` column). A stale write raises rather than clobbering an unseen decision |
| Download | Always into a fresh temp directory; a retry can never stitch a torn transfer onto a previous one |
| Delete | 404 is success — the caller's intent was "make it not exist" |

**On fencing tokens.** A TTL alone is not enough. A worker can stall (GC pause,
a blocked CLI call), have its lease expire, be replaced, then wake up and
complete its write against state the new owner has already moved past. The
monotonic fence makes that write *rejectable*. This is Kleppmann's argument, and
it is the difference between a lock that usually works and one that is correct.

### 4.5 Crash safety: intent before effect

Every effect is preceded by a persisted record of the intent to perform it.

```
1. write STATE = RESTARTING, checkpoint = verified     ← persisted
2. push successor                                       ← the effect
3. write STATE = QUEUED, checkpoint = committed         ← persisted
```

A crash between 1 and 2 leaves `RESTARTING` with a verified checkpoint and no
newer kernel. The reconciler observes exactly that and replays the push — safe,
because the slug is deterministic. A crash between 2 and 3 leaves `RESTARTING`
with a *newer kernel that exists*; the reconciler observes the successor and
adopts it. **Both crash windows have a defined, tested recovery.**

The old code had the opposite ordering in one critical place: results were
packaged before the continuation was pushed. Packaging is the step that runs out
of disk or time — so when it died, it took the whole restart chain with it.

### 4.6 The reconciler

A **level-triggered controller**, like a Kubernetes controller. It does not react
to events; it repeatedly drives observed state toward desired state.

```
observe(client, job)  →  Observation      pure I/O, no mutation
decide(job, obs)      →  Decision         PURE: no I/O, no mutation
act(job, decision)    →  JobManifest      effects, under lease + fence
```

`decide()` being pure is the single most valuable property here. Every routing
decision the system makes is reproducible from a recorded observation and
unit-testable without a Kaggle account — which is why the test suite can cover
"a running kernel with a dead heartbeat", "a network blip must not change
state", and "a stale ledger entry must not be believed" in milliseconds.

Level-triggered logic is also what survives *missed events*, and every failure in
your brief — a crashed worker, a closed browser, a Space that restarted
mid-transition — is a missed event.

### 4.7 Watchdog

Per-state deadlines, because a single global timeout is always wrong in one
direction: short enough to catch a dead handoff is short enough to interrupt a
healthy twelve-hour calculation.

| State | Deadline | Action |
|---|---|---|
| `QUEUED` | 1 h | Re-push (idempotent) |
| `RUNNING` | 15 min since last heartbeat | `HEARTBEAT_LOST` → roll back |
| Any handoff state | 45 min | Roll back and re-drive |
| Any active state | 14 h with no epoch advance | Escalate to an operator |

An escalation **does not fail the job**. It means the automation has run out of
ideas, which is not the same as the calculation being doomed — the Kaggle window
may still be making progress the server cannot observe. Failing it there would
destroy real work to satisfy a timeout.

---

## 5. Recovery matrix

Every failure in your brief, and what now happens.

| Failure | Detection | Recovery |
|---|---|---|
| Hugging Face restart / redeploy | Startup: `recover_after_restart()` | Expired leases cleared, stale idempotency claims released. **Nothing is repaired blindly** — each job is reconciled against the Kaggle ledger, because the correct action depends on what Kaggle did while the server was down |
| SQLite cache wiped (free tier) | `get_job()` returns `None` | `rebuild_from_kaggle()` reconstructs the full manifest from `STATE.json` |
| Kaggle session timeout | In-kernel watchdog at `TIME_LIMIT − reserve` | Clean checkpoint → verify → push successor, with 25 min reserved for the handoff |
| Kaggle kills a session without warning | Heartbeat goes stale | `HEARTBEAT_LOST` → roll back to the last verified checkpoint |
| Network failure (server) | Typed `TransientError` | Exponential backoff + full jitter; the job's state is **not** changed, because a failure to *look* is not a failure of the job |
| Network failure (in-kernel push) | Retry loop bounded by a wall-clock deadline | Checkpoint stays verified in the output; the server replays the push from outside |
| Partial upload | Push returns non-zero or the kernel is absent | Replay against the deterministic slug |
| Interrupted download | Zip CRC + per-file SHA-256 | Fresh directory, full re-download; never a resume |
| Corrupted restart file | Hash + structural validator | Optional file → dropped, work redone. Required file → checkpoint **rejected**, roll back |
| Duplicate submit / browser refresh | Idempotency key | `409` if in flight, stored response if complete |
| Duplicate notebook launch | Status probe before push | Skipped; the existing run is adopted |
| Two concurrent runs of one kernel | `run_token` heartbeat | The second run exits immediately, touching nothing |
| Two workers on one job | Fenced lease | The loser no-ops; the winner's work is idempotent anyway |
| Worker crash mid-transition | Job sits in a handoff state | Watchdog handoff deadline → reconcile → roll forward or back |
| Unexpected ORCA termination | `classify_outcome()` | OOM/resource → continuable. Input error → `FAILED` with the last 20 output lines |
| **MaxIter exhaustion** | Convergence marker absent | **Continue** — in-session if time remains, otherwise a new window with a fresh `MaxIter` |
| **Disk quota exhaustion** | Quota accounting + write probe | Purge regenerable scratch, checkpoint, continue. Budget: 6 disk-driven windows |
| Notebook deleted on kaggle.com | `kernel_exists()` → `None` | `CANCEL`; reported as `deleted_on_kaggle` in the job list |
| Job predates this orchestrator | No `STATE.json` in the chain | Adopted conservatively; legacy `NEXT_JOB_ID.txt` handoff still followed |

---

## 6. Security posture

**Credentials are never written to persistent server storage.** They arrive with
a request, live in RAM under a TTL (`CredentialBroker`), and construct a
throwaway `$HOME` for a single CLI invocation, removed in `finally`. A
compromise of the Space's disk yields no user secrets.

**The stated cost.** The server can only act on a job while some recent request
has supplied credentials for its owner. Between those moments the chain is
carried by the Kaggle kernel, which self-continues. `sweep()` reports this
honestly as `skipped_no_credentials` and a `reachability` string, rather than
quietly pretending full coverage. This was your explicit choice, and it is the
right one for a public site — but an operator should be able to see the boundary.

**Residual exposure, stated plainly.** The in-kernel runner must authenticate to
Kaggle in order to push its own successor, so the credential is embedded in the
pushed kernel source. It lives inside a private notebook belonging to the user
for as long as that notebook exists. This is inherent to *any* self-continuing
kernel, not a consequence of this design — but it is mitigated:

- kernels are pushed `is_private: true`;
- `sys.excepthook` scrubs every traceback, because a traceback can print module
  globals and the rendered output is saved with the kernel;
- `logging_ext.RedactingFilter` scrubs `KGAT_*` and 32-hex patterns from every
  log record, including those from third-party libraries;
- credential files are deleted from the kernel filesystem immediately after the
  successor push;
- users should use a dedicated, revocable token.

---

## 7. Operations

### Configuration

Everything is an environment variable with a sane default; see `config.py`. The
ones that matter most:

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | *(derived)* | **Set this.** See flaw #19 |
| `ORCA_STATE_DIR` | `/data` → `./.state` → tmp | A wiped directory is survivable by design |
| `ORCA_TIME_LIMIT_SECONDS` | 39600 (11 h) | Our self-restart point, well inside Kaggle's 12 h |
| `ORCA_HANDOFF_RESERVE_SECONDS` | 1500 (25 min) | Reserved for checkpoint + verify + push |
| `KAGGLE_SCRATCH_QUOTA_GB` | 60 | Raise only with evidence |
| `ORCA_MAX_EPOCHS` | 24 | ≈ 11 days of wall clock |
| `ORCA_MAX_TOTAL_OPT_CYCLES` | 1500 | The brake on a non-converging system |
| `ORCA_WATCHDOG_ENABLED` | `true` | Disable for single-shot deploys |

### Endpoints

```
POST /api/orca/submit          Idempotency-Key supported
POST /api/orca/status          reconciles, then reports
POST /api/orca/jobs            merges Kaggle + local cache
POST /api/orca/login           verifies credentials AND rebuilds the job list
POST /api/orca/results         streams a window's archive
POST /api/orca/cancel | resume | delete
GET  /api/orca/health          store stats, last sweep, startup recovery
POST /api/orca/sweep           force a watchdog pass
GET  /api/orca/state-machine   live Mermaid diagram, generated from the table
```

`/api/kaggle/*` routes are retained so that chains already in flight at deploy
time keep working. An ORCA calculation can be days old; cutting it off at a
redeploy would destroy real work.

> **Status check (read this before trusting the rest of the document).** The
> browser still submits every new job to `POST /api/kaggle/submit`. `app.js`
> calls `/api/orca/coords`, `/api/orca/coords/file` and `/api/orca/generate` —
> all three of which are defined in `app.py` itself — and never calls
> `/api/orca/submit`, `/api/orca/status`, `/api/orca/results` or any other
> orchestrator route. So the state machine, the ledger, the transactional
> checkpoints and the watchdog described here are exercised only by the test
> suites; in production the job is run by `kaggle_runner.py`.
>
> This is a real hazard rather than a cosmetic one: a restart bug fixed in
> `orca_orchestrator/runner/kernel_runner.py` does not reach any user. That is
> exactly what happened with the MaxIter false-completion bug — the new runner
> handled it from the start, the shipped runner did not, and jobs stopped after
> one window for months. Until the UI is migrated, treat `kaggle_runner.py` as
> the production runner and fix both.

### Diagnosing a job

`GET /api/orca/health` for the fleet. `POST /api/orca/status` for one job — it
returns `state`, a human `phase`, `stall` (with the age and the deadline it is
measured against), `disk_report` (all three disk numbers side by side),
`verified_checkpoint`, and the last fifteen events with triggers and reasons.

Logs are one JSON object per line. Filter by `job_id` for a job's whole life, or
by `correlation_id` for one request's fan-out. Every failure record carries
`failure_what`, `failure_why`, `recovery_attempted`, and `next_action` — the
four fields `log_failure()` makes mandatory.

Inside a window, `RUN_LOG.jsonl` lands in the saved Kaggle output, so a window
that has long since disappeared still has a complete machine-readable trace.

### Tests

```bash
python tests/test_orchestrator.py            # 136 checks — components
python tests/test_lifecycle_simulation.py    #  29 checks — full lifecycle
```

Neither needs pytest, so both run inside the Docker image without adding a
dependency. Both production bugs are reproduced as regression tests using the
exact ORCA output text and the exact 1006.8 GB disk figure from your log.

The lifecycle suite replaces only `KaggleClient`. Everything below it is real —
including `build_window_directory`, so the generated Kaggle script is compiled
on every simulated push. It drives a job through submission, a MaxIter handoff,
a duplicate reconcile, an already-running successor, a silent window death, a
stale ledger entry, genuine completion, and both budget exhaustions.

---

## 8. Production incident: the first deploy

Recorded in full because it is the most instructive failure in this document —
the bug was not in the checkpoint logic at all, and it produced a symptom that
would have been very hard to diagnose from user reports.

**What happened.** On the first boot, one of the two gunicorn workers logged:

```
orchestrator failed to load; falling back to the legacy Kaggle routes only:
  File "orca_orchestrator/store.py", line 156, in _connect
    conn.execute("PRAGMA journal_mode=WAL")
sqlite3.OperationalError: database is locked
```

The other worker started normally.

**Root cause.** Switching a SQLite database into WAL mode requires a brief
EXCLUSIVE lock, and for *that specific operation* SQLite returns `SQLITE_BUSY`
**immediately, without consulting the busy-timeout handler**. The connection
already had an 8-second `busy_timeout` configured — it simply does not apply
here. Two workers booting in the same second against a brand-new database file
both issued the pragma, and one lost. Reproduced deterministically: with two
workers, 1 of 2 succeeded.

**Why the symptom was worse than the bug.** `register()` warmed the service
*before* registering the blueprint, so the exception propagated into `app.py`,
which caught it and set `ORCHESTRATOR_AVAILABLE = False` for the life of that
process. The site came up looking healthy. But gunicorn round-robins requests,
so roughly **half of all `/api/orca/*` requests returned 404** — intermittently,
with no error anywhere. To a user that reads as the site randomly forgetting
their jobs; to an operator it reads as nothing at all, because the failure was
logged once at boot and never again.

**Three fixes, at three layers.**

`_enable_wal()` retries with jittered backoff and tolerates losing the race
outright. WAL is a persistent property of the *file*, not the connection, so if
the other worker wins we simply read back the mode it set. If the switch never
succeeds, the database still works correctly in rollback-journal mode —
concurrency drops, nothing is lost. Degrading beats refusing to start. The
pragmas were also removed from `_SCHEMA`, where they had no retry path, and
schema creation itself now retries on lock.

`register()` attaches the routes **first** and warms the service on a
best-effort basis, and `get_service()` never caches a failure. A boot race is
now self-healing: the first request that needs the service constructs it.

`/api/orca/health` distinguishes `ready: false` (not constructed yet, will
retry) from a genuinely broken deploy, and returns 503 rather than 500 for the
former.

**The general lesson**, which applies beyond this bug: a `try/except` that
disables a subsystem permanently converts a transient fault into a permanent
one, and does it silently. The exception handler was more damaging than the
exception. Regression tests now boot 2, 4 and 8 workers simultaneously against
a fresh database and assert that all of them initialise and converge on WAL.

---

## 8b. The watchdog was blind to the jobs people were watching

Found immediately after the first successful deploy, while reducing log noise.
It is the most serious defect in this rewrite, and it would have reproduced the
original complaint — *jobs become stuck* — with all the new machinery in place.

**The bug.** `assess()` measured how long a job had been stalled using
`job.updated_at`. But `updated_at` means *last write of any kind*, and a status
poll is a write: the reconciler calls `touch()` even on a no-op pass, and
`put_job()` touches unconditionally. So a job stuck in `QUEUED` for three hours
while a browser polled it every 45 seconds had its timestamp refreshed 240
times and never aged past its one-hour grace period.

The failure mode is precisely inverted from what you would want. A job nobody
was watching would eventually be recovered. **A job someone was actively
waiting on could never be.** The more attention a stuck job received, the more
invisible it became.

**The fix.** `state_entered_at` is a separate field, advanced only by
`enter_state()` — that is, only by a real transition. Re-entering the same state
does not restart it, and no amount of observation moves it. `assess()` measures
against that clock, falling back to `updated_at` only for manifests written
before the field existed.

Verified: a job stuck in `QUEUED` for two hours, then polled 160 times through
a full write/read cycle, still reports `stalled=True` with an accurate age of
120 minutes.

**The general lesson**, and the reason this is worth six paragraphs: *last
modified* and *time in current state* are different quantities, and using one
for the other is invisible until the moment it matters. Any timeout measured
from a field that unrelated code also writes is not a timeout — it is a timeout
that silently resets whenever the system is busy, which is exactly when the
timeout was supposed to fire.

A smaller change went in alongside it. An uneventful watchdog sweep now logs at
DEBUG rather than INFO, with a periodic liveness summary so that *"the watchdog
has nothing to say"* stays distinguishable from *"the watchdog is dead"*. Two
workers sweeping every two minutes produced roughly 1,440 INFO lines a day
saying nothing happened, which is 1,440 lines an operator scrolls past while
looking for the one event that explains a stopped calculation. An INFO line
from `orca.watchdog` now always means something worth reading.

---

## 8c. The two workers were using different databases

Third production finding, visible in the boot log only if you compared two
lines that looked almost identical:

```
"db_path": "/app/.state/orchestrator.sqlite3",        "pid": 7
"db_path": "/tmp/orca-state-nfdcujnc/orchestrator...", "pid": 8
```

**The bug.** `_default_state_dir()` tested writability by creating a probe file
with a **fixed name**, `.write-probe`. Two workers booting together both created
it; the first removed it; the second's `os.remove()` raised `FileNotFoundError`
— a subclass of `OSError`, caught by the same handler that means *"this
directory is not writable"*. A cleanup collision was read as a permissions
failure, and that worker fell through to a private temp directory.

**Why it matters far more than it looks.** The local database is not the source
of truth, so at first glance a split is harmless. But it is where the **leases,
the fencing tokens and the idempotency keys** live, and those coordinate
nothing unless every worker opens the same file. Two databases means:

- two workers can hold "the" lease on the same job simultaneously, so both
  reconcile it and both may push a successor;
- `Idempotency-Key` deduplicates only against the worker that happens to
  receive the retry, so a double-clicked submit can launch two notebooks;
- a job submitted through worker 7 is invisible to worker 8's cache, so its
  status poll adopts it from Kaggle as if it were a stranger's.

In other words, the entire concurrency-control design in §4.4 was switched off
for one of the two workers — and nothing said so.

**The fix, in three parts.** The probe filename now includes the pid and a
random suffix, so two processes cannot collide. Cleanup moved into a `finally`
with a swallowed error, because removing the probe is housekeeping and says
nothing about writability. And the fallback is no longer silent: choosing a
private directory logs at ERROR, is recorded in `STATE_DIR_DIAGNOSTIC`, and is
reported by `/api/orca/health` as `state_dir_diagnostic.shared: false`.

**The lesson**, which is the same one as §8 in different clothing: the
dangerous part was not the race, it was the *silent degradation*. A system that
quietly does something weaker than you asked, without saying so, is worse than
one that fails — because the failure would have been fixed in minutes, whereas
this would have surfaced weeks later as "sometimes a job gets submitted twice"
with no way to reproduce it.

Regression tests boot 2, 4 and 8 workers concurrently and assert they all pick
the same directory, and hammer the probe with a thread deleting files
underneath it to confirm a concurrent deleter cannot make a writable directory
look unwritable.

---

## 9. What verification actually caught

Recorded because "we wrote tests" is worth less than "here is what they found".
All four of these were bugs in *this* rewrite, found before deployment.

**A `__future__` import would have broken every pushed script.** The runner's
source is concatenated after a generated header, and `from __future__ import
annotations` is only legal as the first statement of a file. Every push would
have produced an immediate `SyntaxError` — discovered at hour zero of a real
job, but only after a user submitted one. The runner now contains none, and
`builder._read_source` strips them defensively so no future edit can
reintroduce the failure.

**A stale ledger entry could still finish a job.** The staleness guard was
applied to the fresh-ledger fast path but not to the stopped-window branch
below it. A `STATE.json` left in `/kaggle/working` by a *previous run of the
same kernel* would have been read as this window's result and marked the job
FINISHED at the wrong epoch — the exact class of bug the guard exists to
prevent, reintroduced twenty lines further down. `ledger_fresh` is now computed
once and used everywhere.

**A crash-recovery path would have silently discarded all progress.** When a
window verified a checkpoint and then died before its successor was accepted,
the reconciler pushed the successor — but never adopted the checkpoint from the
window's ledger first. The successor would have been built with
`checkpoint=None` and restarted from the original geometry, throwing away every
optimisation cycle the window had paid for, while reporting success.
`_adopt_checkpoint()` now runs before the push.

**Credentials passed via `extra=` were written to logs in clear text.**
`RedactingFilter` only inspects `record.msg` and `record.args`, but structured
logging puts the interesting data in `extra=`, which lands in `record.__dict__`
and reaches the formatter untouched. A single `log_failure(..., kaggle_key=...)`
would have printed a live API token to a Space's stdout. Redaction moved to
`JsonFormatter`, the one point every record must pass through.

There is also a modelling gap worth naming, because the state machine is what
found it: completing a handoff from `RUNNING` required transitions the server
had never observed. Rather than assigning the state directly, `_find_path()`
performs a breadth-first search over the transition table and replays the
transitions the window really performed — so the path stays correct if the
machine changes, and the event ledger records an honest causal history instead
of a fabricated jump.

---

## 10. Migration

Nothing is deleted. `kaggle_runner.py` and the `/api/kaggle/*` routes remain
mounted, because an ORCA chain in flight at deploy time can be days old and
cutting it off would destroy real work. Three things make the transition safe:

The new runner still writes `NEXT_JOB_ID.txt` and `NEXT_JOB_URL.txt`, so a
browser or server still speaking the old protocol keeps following the chain.
`ledger.parse_ledger_files()` reads those same files, so a *legacy* window's
handoff is followed by the *new* reconciler. And `rebuild_from_kaggle()` adopts
a chain with no orchestrator ledger anywhere, tracking it from that point on
while stating plainly in `last_note` that it has no checkpoint history to roll
back to.

Point the front end at `/api/orca/*` when convenient. New submissions get the
full machinery; old ones finish on the path they started.

---

## 11. Known limitations

Stated because a design document that claims no limits is not describing a real
system.

**Watchdog reachability is bounded by credential caching.** Consequence of the
no-stored-credentials choice, reported in every sweep result. If you later want
true 24/7 unattended recovery, the change is an opt-in encrypted credential
vault; nothing else in the architecture needs to move.

**A `run_token` heartbeat is a lease, not a distributed lock.** If a Kaggle
process is paused for longer than the liveness threshold and then resumed, a
second run could in principle take over. The threshold is 6× the heartbeat
interval (~4.5 min) to make that improbable, but it is not impossible.

**`decide()` trusts the ledger when it is fresh.** A window that writes a
*wrong* `STATE.json` will be believed. Mitigated by the digest, the epoch stamp
and the run token, but a Byzantine window is out of scope.

**The cumulative-cycle budget is a heuristic.** 1500 cycles is generous for most
systems and too small for a few. It exists to stop a runaway from consuming
someone's whole Kaggle quota; raise it deliberately when a system warrants it.

**Kaggle's quota figures are documented, not queryable.** There is no API that
reports remaining quota, which is why the write probe exists as a third,
independent signal. If Kaggle changes its limits, update
`KAGGLE_*_QUOTA_GB` — the plausibility threshold will keep the system safe in
the meantime, but the accounting will be conservative.
