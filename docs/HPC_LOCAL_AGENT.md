# Chemistry Lab University HPC & Supercomputer Integration Guide

The Chemistry Lab HPC Agent runs on cluster login or service nodes and interfaces with workload managers (SLURM, PBS Pro / Torque, IBM LSF) to run distributed ORCA calculations.

## Architecture

```
Chemistry Lab Server
        │
        ▼ (Secure outbound WSS)
HPC Agent (on Login / Service Node)
        │
        ▼ (Scheduler Adapter)
SLURM (sbatch) / PBS (qsub) / LSF (bsub)
        │
        ▼ (Compute Nodes)
ORCA Multi-Node / Multi-Core Execution
```

## Supported Schedulers
- **SLURM**: Generates sbatch submission scripts with `#SBATCH` directives for nodes, tasks, cpus-per-task, memory, walltime, partitions, and accounts.
- **PBS / Torque**: Generates qsub scripts with `#PBS -l` resource specifications.
- **LSF**: Generates bsub batch scripts.

## Environment Modules
Clusters using environment modules can specify fixed module load commands (e.g. `module load orca/6.0.0`) in the local agent configuration.

## Resource Allocation & Safety
- **No Arbitrary Remote Shell**: The Chemistry Lab server never sends arbitrary bash scripts; only structured scientific parameters and validated resource specs are transmitted.
- **Restart Reconciliation**: Upon agent restart, active jobs are queried via `sacct` / `qstat` rather than blindly resubmitted.
