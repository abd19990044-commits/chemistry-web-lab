# Reproducibility protocol

This document defines the minimum evidence package for evaluating Chemistry Lab as research software.

## 1. Environment

- Python: 3.11 (the production CI target)
- Deployment: Docker SDK on Hugging Face Spaces
- Chemistry engine: ORCA 6.x supplied separately by the user under its own license
- Remote execution: user-owned Kaggle account
- Local chemistry toolkit: RDKit pinned in `requirements.txt`

Record the exact Git commit and the Docker image digest for every published benchmark. Do not use a mutable `main` branch as the sole software identifier.

## 2. Deterministic tests

The repository includes unit/component and failure-injection tests for the FSM, store, checkpoints, continuation logic, frontend contract, reaction generation, and deployment wiring. `tests/fake_orca.py` provides a deterministic executable for restart simulations without a licensed ORCA installation.

Run:

```bash
python -m pytest -q tests/test_production_wiring.py
python tests/test_deployment.py
python tests/test_reaction.py
python tests/test_frontend.py
python tests/test_continuation.py
python tests/test_end_to_end_chain.py
python tests/test_orchestrator.py
python tests/test_lifecycle_simulation.py
```

If a test requires network access, a real Kaggle account, or ORCA itself, report that dependency explicitly rather than silently treating a skipped test as a pass.

## 3. Failure-injection matrix

At minimum, validate these transitions:

| Failure | Expected behaviour |
|---|---|
| web process restart | active jobs are reconstructed/reconciled |
| duplicate submission | same idempotency record is replayed; no second kernel |
| concurrent submit | one request owns the idempotency claim |
| corrupted checkpoint | verification rejects it and a previous verified checkpoint is selected |
| interrupted checkpoint transfer | incomplete transaction is recovered without trusting partial data |
| Kaggle session expiration | successor kernel is created from a verified checkpoint |
| heartbeat loss | watchdog marks the window for recovery |
| disk/resource pressure | window is stopped/recovered within configured budget |
| ORCA normal exit without convergence | job is not declared scientifically complete |
| genuine ORCA fatal error | job fails with an actionable diagnostic |
| deleted remote kernel | reconciliation records the remote deletion and does not resurrect a phantom run |
| invalid credentials | HTTP 401; infrastructure failures are not misreported as credential errors |

## 4. Scientific validation

Use at least one representative input for each supported restart class (optimization, OptTS, NEB, scan, MD, and frequency where applicable). Record:

1. ORCA version and input file.
2. Method, basis, charge, multiplicity and relevant resource limits.
3. Initial geometry checksum.
4. Epoch/checkpoint sequence.
5. Final convergence criteria and ORCA termination evidence.
6. Final output checksum.
7. Whether the result obtained through continuation matches a control run executed without interruption.

The strongest validation is a paired experiment: run the same calculation once uninterrupted and once with injected session boundaries. Compare final geometry, energy, convergence indicators and relevant output artefacts within documented numerical tolerances.

## 5. Publication evidence

For a SoftwareX submission, archive:

- the exact Git commit/tag;
- source code and dependency pins;
- Docker configuration;
- deterministic test output;
- representative input files;
- benchmark logs and machine specifications;
- failure-injection results;
- a data/code availability statement;
- the software citation metadata in `CITATION.cff`.

Do not claim that a feature is validated merely because its implementation exists. Separate implementation claims from experimentally demonstrated claims in the manuscript.
