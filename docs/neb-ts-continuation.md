# NEB-TS continuation

The production Kaggle runner treats NEB-TS as a calculation with separate
milestones: the NEB path, the subsequent transition-state optimization, and any
explicitly requested `Freq` or `NumFreq`. Normal termination or a converged NEB
band alone cannot finish a `NEB-TS` job. A TS convergence certificate must follow
the latest band/TS-phase announcement. Requested frequencies must follow that
certificate. Endpoint pre-optimization, input comments, and echoed input do not
provide those certificates. FAST/LOOSE/TIGHT/ZOOM-NEB-TS use the same checks;
FAST-NEB-TS may switch to TS optimization without a converged-band certificate.

## What survives a session boundary

Before a controlled time/disk handoff, the runner carries the complete
`basename_MEP.allxyz` path and available endpoint XYZ files. The checkpoint is
staged, checksummed and verified before a successor is submitted. Restart uses
`%neb Restart_ALLXYZFile`, with an existing reference replaced by the verified
current snapshot. A torn/non-finite path, inconsistent atom order, or a path
that cannot fit the checkpoint is refused rather than silently restarting from
the original endpoints. Restoring an earlier verified checkpoint can lose work
performed after that checkpoint; a sudden platform kill cannot guarantee saving
the latest iteration.

NEB normally retains a fixed number of images. The progress guard therefore
compares path atom identities and coordinates, ignoring comment timestamps and
numeric formatting. A changed band with the same image count is real progress;
an identical band is not. This guard runs both inside one session and before
handing work to another session.

This is a **path restart**, not a bit-for-bit restoration of the entire ORCA
process. In particular, the subsequent OptTS work may repeat from the saved
band. The runner does not silently replace NEB-TS with a standalone OptTS, which
would discard the NEB tangent/mode selection. When TS iteration exhaustion is
reported, its geometry-iteration allowance increases within the configured
budget. Numerical frequency columns are not independently resumed through this
NEB path-restart branch. These distinctions matter when sizing a long job.

## Browser and server availability

Closing the browser does not stop ORCA or the server-side workflow supervisor.
Kaggle User Secrets can authorize an in-notebook successor push. The running
application can reconcile and retry supported handoffs using its owner's
encrypted credentials. A sleeping/stopped application cannot do that server-side
work until it wakes. Submission still depends on Kaggle capacity, quota, working
network access, and authorized credentials. Explicit cancellation does not
request an automatic continuation.

Continuation is bounded: `ORCA_MAX_EPOCHS`, `ORCA_MAX_TOTAL_OPT_CYCLES`, disk
restart limits and no-progress checks prevent endless relaunches. A normal
termination with missing milestones remains incomplete, not scientific success.
Converging an optimizer also does not independently prove a first-order saddle
point or its chemical connectivity; inspect frequencies/modes and IRC as needed.

Generated notebooks embed their runner source. Updating the website changes
newly generated notebooks; an already-running notebook retains its embedded
code. Do not assume a running old notebook gained this fix automatically.

## Validation

Regression tests execute the production runner with a scripted ORCA stand-in:
three successive passes retain four images while their coordinates move, and
the job finishes only when TS convergence and requested frequencies appear.
A simulated session timeout stages a verified path checkpoint and pushes one
successor. Unit cases cover variants, premature completion, comments, stale
restart references, filename/block parsing, malformed paths and stable coordinate
identity. These tests do not constitute a live licensed ORCA/Kaggle NEB-TS run.

The ORCA 6.1 reference explains the NEB-to-OptTS transition, tangent-based mode
selection, output files and `Restart_ALLXYZFile`:
https://www.faccts.de/docs/orca/6.1/manual/contents/structurereactivity/neb.html
