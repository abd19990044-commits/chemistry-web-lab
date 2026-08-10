---
title: Chemistry Lab
emoji: 🧪
colorFrom: green
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# Chemistry Lab 🧪

Created by **Abdulsalam S. Hasan** — Mosul, Iraq. A free, charitable
initiative to give researchers accessible computational-chemistry tools.

An always-on computational chemistry site with two workspaces:

- **Draw Chemistry** — look up a compound (name / SMILES / InChI) and get a
  clean 2D structure plus its properties, or draw a full reaction scheme.
  Includes `.mol` / `.rxn` downloads.
- **ORCA Program** — a guided, step-by-step wizard that builds an ORCA 6
  `.inp` file exactly matching your choices, then an optional launcher that
  packages and runs it on your own Kaggle account, with automatic
  session-limit restarts and a job tracker that surfaces a results download
  link once a run finishes.

Built with Flask + RDKit, packaged for **Hugging Face Spaces (Docker SDK)**
so it stays online continuously (no cold-start "sleep" behavior).

## Checkpoint/restart subsystem

> **Which code actually runs a job today.** The browser submits through
> `POST /api/kaggle/submit`, so every job currently launched from the site is
> driven by `kaggle_runner.py`. `orca_orchestrator/` is a second, more capable
> implementation mounted at `/api/orca/*` and covered by its own test suites,
> but **no page in the UI calls it yet** — the client half of that migration
> was never finished. Read the section below as a description of
> `orca_orchestrator/`, not of the path a job takes right now. Anything you
> change about restart behaviour has to be changed in *both* runners until the
> UI is switched over.

Long ORCA calculations survive many time-limited Kaggle sessions through
`orca_orchestrator/`, a fault-tolerant orchestration layer. **See
[ARCHITECTURE.md](ARCHITECTURE.md)** for the full design, the flaw analysis
that motivated it, and the recovery matrix.

The short version: each Kaggle window writes a signed `STATE.json` into its
saved output, and *that* is the source of truth — it outlives both the web
application and any individual kernel, so a job is fully reconstructible after
a Space restart, a redeploy, or a move to a different browser. Every state
change goes through an explicit finite-state machine; a checkpoint is a
transaction that is staged, verified by re-reading every byte, and only then
committed by pushing a successor; and every operation is idempotent, so crash
recovery is a matter of running the loop again.

Two bugs that this subsystem exists to fix are worth knowing about, because
both produced silent wrong behaviour rather than visible errors:

ORCA prints `ORCA TERMINATED NORMALLY` **even when a geometry optimisation has
not converged** — its own manual warns against treating that line as a success
signal. The previous code did exactly that, so a run that exhausted
`%geom MaxIter` after six hours was reported as finished, unconverged, with no
restart. Completion now requires the job-type convergence marker.

`shutil.disk_usage()` inside a Kaggle container reports the **host overlay
filesystem** (over 1 TB free), not the enforced per-notebook quota (~20 GB
output, ~60 GiB scratch). The free-space watchdog compared 1006.8 GB against a
5 GB floor and could therefore never fire, so the entire disk-recovery path was
dead code. Disk headroom is now measured as consumption against the quota,
cross-checked with a real periodic write probe.

```bash
python tests/test_deployment.py              #  26 checks: the image can be built and run
python tests/test_reaction.py                # 101 checks: equations, coefficients, RXN files
python tests/test_frontend.py                #  21 checks: app.js loads and behaves
python tests/test_web_routes.py             #  69 checks on the Flask routes
python tests/test_continuation.py           #  86 checks on the LIVE runner
python tests/test_end_to_end_chain.py       #  46 checks: whole chains, every job type
python tests/test_orchestrator.py           # 136 component checks
python tests/test_lifecycle_simulation.py    #  29 end-to-end checks
```

Set `SECRET_KEY` in the Space's environment. Without it, each gunicorn worker
derives its own session key and signed-in users are randomly signed out.

## Kaggle launcher notes

- **Job addresses come from Kaggle, not from a guess.** A kernel's slug is
  decided by Kaggle at push time, and when the requested id and the kernel
  title disagree Kaggle may create the notebook under a slug derived from the
  *title* — the `kaggle kernels push` CLI warns about exactly this ("your
  kernel title does not resolve to the specified id … this may result in
  surprising behavior"). That is what made the link shown after a submission
  fail to open, and made every later status poll/download/delete address a
  notebook that did not exist. Two fixes, belt and braces: the slug and the
  Kaggle-side title are now the *same* string (`chem-tools-<name>-<random>`,
  built with `make_job_base_id`), and `push_job` reads the real URL back out
  of the CLI's own output and uses that. "My Jobs" keeps showing the pretty
  name you typed.
- **Running out of disk no longer ends a run.** ORCA's scratch files
  (integrals, densities, per-rank temporaries) are routinely tens of GB, and
  the job used to run inside `/kaggle/working` — the auto-saved output
  directory, capped at 20 GB — so a large calculation filled it and died. Now:
    - ORCA runs in Kaggle's much larger scratch space (`/kaggle/temp`), and
      only curated results are copied back into `/kaggle/working`.
    - A watchdog thread tracks free space alongside the clock. When either the
      session-time budget or the free-space floor is crossed, the ORCA process
      *group* is stopped cleanly (so its MPI children die with it) and the run
      continues in a fresh session from the same text checkpoints used for a
      time-limit restart.
    - The continuation kernel is pushed **before** results are packaged, and
      regenerable scratch is purged first. Packaging is the step that runs out
      of room, and it used to take the whole restart chain down with it.
    - An ORCA abort whose output carries a disk/IO fingerprint ("no space left
      on device", `Errno 28`, …) counts as a resource limit, not a broken
      input, so it is continued too — up to `MAX_DISK_RESTARTS` (6), after
      which the job stops with concrete advice (RIJCOSX, RI-MP2/DLPNO, smaller
      basis, fewer TD-DFT roots, r2SCAN-3c) instead of looping.
    - `results.zip` is written straight from scratch under a size budget, with
      a `MANIFEST.txt` listing anything left out, so the output stays inside
      Kaggle's 20 GB cap. Every packaging step is best-effort: on a full disk
      the `.out` and the job note still get through.
- **Failures that used to waste a whole session.** Before launching, the kernel
  clamps `%pal nprocs` to the cores it actually has, clamps `%maxcore ×
  nprocs` to ~70 % of real RAM (an out-of-memory kill looks like a mysterious
  "stopped without finishing"), and checks that an `mpirun` exists at all —
  ORCA 6 needs an external OpenMPI for any parallel run. If none is found it
  tries a quick install and otherwise falls back to a serial run, so the
  calculation finishes slowly instead of failing instantly. A parallel start-up
  that still fails with an MPI error is retried serially in the same session.
- This site never distributes the ORCA binary. You need your own private
  Kaggle Dataset containing your own licensed ORCA package (downloaded from
  the official ORCA forum after your own registration). The Kaggle Launcher
  tab now has two expandable **"How do I get…"** panels that walk a visitor
  through both of these end-to-end (including *why* the ORCA download must
  specifically be the Linux build — Kaggle Notebooks run on Linux, so a
  Windows/macOS binary simply won't execute there).
- Your Kaggle username and API key are sent to the server only for the
  moment of submitting/polling a job (`kaggle_runner.py`) and are never
  written anywhere persistent server-side. If you tick "remember", they are
  saved only in your own browser's local storage.
- Signing in re-fetches this account's job list straight from Kaggle
  (`list_jobs()`), so it's recoverable after clearing browser data or
  switching devices/browsers. **Fixed:** an earlier version built the
  submitted kernel's slug from a differently-sanitized copy of the Kaggle
  username than the one this lookup searched for (e.g. a hyphen in a
  real username like `chem-lab-99` silently became an underscore), so the
  two could drift apart and this recovery silently found nothing for any
  username needing sanitizing at all — jobs only ever showed up in the
  browser they were submitted from. Job ids no longer embed the username
  at all (ownership is already established by whichever account's
  credentials are used), and the username is also lower-cased consistently
  everywhere it's used, since Kaggle usernames are canonically lowercase.
- The auto-restart chain runs entirely inside your Kaggle kernel and is
  **corruption-proof by design**: it never resumes from the binary `.gbw`
  wavefunction (a force-killed session can leave that half-written, which is
  exactly what makes a naive restart abort). Instead each calculation type is
  continued from a plain-text checkpoint that survives a hard kill, and every
  continuation adds `! NoAutoStart` so a stray `.gbw` is ignored:
    - **Opt / Opt Freq / OptTS** → the last *complete* frame of the append-only
      `basename_trj.xyz` trajectory (a truncated final frame is detected and
      dropped), fed back via `* xyzfile`. A fully-written ASCII Hessian
      (`basename.hess`) is validated and reused via `%geom InHess Read` so
      OptTS doesn't recompute it every window; once the geometry has converged
      the restart drops `Opt` and finishes the frequencies only.
    - **NEB** → a validated `*_MEP.allxyz` snapshot via `Restart_ALLXYZFile`.
    - **Relaxed surface scan** → resumes at the last completed scan point.
    - **Ab-initio MD (`%md`)** → `Restart IfExists` + `basename.mdrestart`.
    - **NumFreq** → `%freq Restart true` + the carried `basename.res.*` columns.
  Only the minimal set of restart files is shipped to the next kernel (not the
  whole scratch dir), and a run is auto-restarted **only** when *our* watchdog
  stopped it for the session limit — a genuine ORCA error, or a single-shot job
  (plain SP / TD-DFT) that simply can't be continued from text, finalizes with
  a clear note instead of silently looping through all your restarts.
- Once a job finishes, the kernel zips its output files as part of its own
  kernel output — no third-party file host involved. Clicking **Download
  results** in **My Jobs** fetches that zip fresh from Kaggle (via `kaggle
  kernels output`) and streams it straight to your browser.
- A job's name — shown here in **My Jobs** and as the notebook's title on
  Kaggle itself — comes from the optional "Job name" field on the launcher
  form, or from the input file's own name when that field is left blank.
  This stays consistent across an auto-restart too, instead of the
  continuation kernel showing its raw slug.
- Deleting a job from **My Jobs** permanently deletes its notebook(s) from
  Kaggle as well (`kaggle kernels delete`), including any auto-restart
  continuation kernels — this can't be undone.

## Legal, Privacy & Disclaimer page

The footer's **"Legal, Privacy & Disclaimer"** link opens an in-app page
(`#legal-view`) covering the disclaimer/no-warranty/liability language, an
ORCA/Kaggle licensing notice, and privacy notices written for GDPR (EU/EEA/
UK) and CCPA/CPRA-style (US) visitors, so the site can be run for a global
audience. It's a solid starting template, **not a substitute for legal
advice** — before relying on it, replace every `[bracketed placeholder]`
(operator name, contact email, jurisdiction, last-updated date) with real
values and have it reviewed by a qualified lawyer, especially once the site
handles data at real scale or for money.

## Google Sign-In

Optional, identity display only — set the `GOOGLE_CLIENT_ID` environment
variable (from a Google Cloud OAuth client) to enable the "Sign in with
Google" button in the header. Without it, the button is simply hidden and
everything else works normally.

## Project layout

```
app.py                 API routes + page rendering
chem_core.py            Chemistry logic (PubChem, RDKit, ORCA input builder)
kaggle_runner.py         Kaggle job packaging, submission, and status polling
templates/index.html     Single-page UI: landing screen, two workspaces, legal page
static/css/style.css      Design system
static/js/app.js           Frontend logic, wizard, jobs tracker
Dockerfile                For deployment on Hugging Face Spaces
```

Either layout works: the folders above, **or** every file side by side in one
flat directory (`index.html`, `style.css`, `app.js` next to `app.py`). `app.py`
detects which one is present at start-up, so uploading the folder as-is to a
Space cannot fail with a bare `TemplateNotFound`.

## Deploying on Hugging Face Spaces

1. Create a new Space with the **Docker** SDK.
2. Upload everything in this folder to the Space's root (including
   `Dockerfile` and this `README.md` — Spaces reads its SDK settings from
   the header above).
3. Optionally set `GOOGLE_CLIENT_ID` and `SECRET_KEY` as Space secrets.
4. No secrets are required for the Draw Chemistry / Input Generator tools;
   the Kaggle Launcher takes credentials from each visitor's own form.

## Running locally

```bash
pip install -r requirements.txt
python app.py   # listens on port 7860
```
