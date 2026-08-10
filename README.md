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

**Chemistry Lab** is a web-based research-software platform for molecular structure exploration, reaction drawing, ORCA 6 input generation, and fault-tolerant execution of long-running ORCA calculations through a user's Kaggle account.

The production calculation lifecycle is implemented by `orca_orchestrator/`. The historical `/api/kaggle/*` endpoints are compatibility aliases only and are routed to the same orchestrator service; the obsolete `kaggle_runner` lifecycle is not used for new jobs.

## Scientific and software contribution

The project addresses a practical reproducibility problem: ORCA calculations can exceed the lifetime of an individual hosted notebook session. The orchestrator therefore treats a calculation as a persistent state machine rather than as one notebook execution. Each continuation window is represented by a verified checkpoint and a new Kaggle kernel. Recovery is driven by persisted state and reconciliation rather than by the browser session.

Important ORCA-specific safeguards include:

- completion is not inferred from `ORCA TERMINATED NORMALLY`; job-type convergence evidence is required;
- restart checkpoints are staged, byte-verified and committed before successor execution;
- corrupted or incomplete binary wavefunction files are not blindly resumed;
- optimization, NEB, scan, MD and frequency calculations use calculation-specific restart artefacts;
- duplicate submissions are controlled by idempotency records;
- watchdog/reconciler logic handles session limits, stalled kernels and resource-limit conditions;
- Kaggle-side state is designed to survive a Hugging Face Space restart or redeploy;
- credentials are kept ephemeral and redacted from application logs.

See `ARCHITECTURE.md` for the state model, recovery matrix and failure assumptions.

## Project structure

```text
app.py                         Flask application and public web routes
chem_core.py                   RDKit/PubChem/reaction/ORCA input generation
orca_orchestrator/             production lifecycle, state, ledger and recovery
  api.py                       canonical /api/orca/* API
  service.py                   orchestration facade
  states.py                    finite-state machine
  reconciler.py                Kaggle/local state reconciliation
  watchdog.py                  stall/resource supervision
  ledger.py                    remote job discovery/reconstruction
  store.py                     persistent local state
  checkpoints.py               checkpoint lifecycle
  orca_artifacts.py            ORCA output/restart validation
  runner/                      Kaggle kernel construction
  legacy_compat.py             compatibility adapter for /api/kaggle/*
kaggle_runner.py               retained legacy utilities; not the production lifecycle
tests/                         regression, integration and simulation tests
Dockerfile                     Hugging Face Docker deployment
requirements.txt               exact Python dependency pins
```

## Reproducible validation

The repository contains deterministic tests that do not require a user's Kaggle credentials and a fake ORCA executable for restart simulations.

Recommended local validation:

```bash
python -m pip install -r requirements.txt
python -m pytest -q tests/test_production_wiring.py
python tests/test_deployment.py
python tests/test_reaction.py
python tests/test_frontend.py
python tests/test_web_routes.py
python tests/test_continuation.py
python tests/test_end_to_end_chain.py
python tests/test_orchestrator.py
python tests/test_lifecycle_simulation.py
```

The CI workflow performs syntax/import checks and production-wiring tests on Python 3.11. Tests that require an external network or a real Kaggle account are explicitly separated from deterministic simulation tests.

## Production deployment on Hugging Face Spaces

The recommended deployment target is Hugging Face Spaces using the Docker SDK. ORCA itself is **not** distributed by this repository and is **not** executed on the web server. The server creates and supervises Kaggle kernels; users must supply their own legally obtained ORCA package through a private/licensed Kaggle Dataset or an appropriate direct archive source.

Set these Space secrets/variables:

```text
SECRET_KEY=<long random production secret>
ORCA_STATE_DIR=/data
```

`SECRET_KEY` should always be explicitly configured in production. `/data` should point to persistent Space storage when available; the Kaggle-side state remains the recovery source for jobs after a web-space restart.

Optional:

```text
GOOGLE_CLIENT_ID=<Google OAuth client id>
```

## Security model

Kaggle credentials are supplied by the user for job operations. The application does not intentionally persist API keys in its database; credentials are cached only in memory and logging filters redact sensitive values. Users should use revocable, least-privilege Kaggle credentials and should not submit credentials belonging to other accounts.

The service assumes the Hugging Face deployment and the user's private Kaggle resources are trusted execution environments. This is a threat-model assumption, not a claim of absolute credential security.

## Reproducibility and publication

For a research-software publication, use a versioned Git tag/commit together with the exact dependency lock, Docker configuration, deterministic test output, representative ORCA input files and anonymised benchmark results. Do not cite a mutable `main` branch as the sole software version.

The software is intended to be described as a fault-tolerant orchestration framework for long-running ORCA calculations. Claims about reliability should be supported by the included failure-injection and lifecycle simulations rather than by successful startup alone.

## License and ORCA notice

This repository does not redistribute the ORCA executable. Users are responsible for obtaining and using ORCA according to its license and distribution terms. The project provides orchestration and web tooling around a separately supplied ORCA installation.
