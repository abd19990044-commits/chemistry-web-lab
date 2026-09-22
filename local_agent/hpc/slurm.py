# -*- coding: utf-8 -*-
"""SLURM Workload Manager Adapter."""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from typing import Any, Dict, Optional

from .base import (
    BaseSchedulerAdapter,
    HpcValidationError,
    validate_filename,
    validate_resources,
    validate_scheduler_identifier,
    validate_script_path,
)


class SlurmAdapter(BaseSchedulerAdapter):
    """SLURM scheduler adapter executing sbatch, squeue, sacct, and scancel with shell=False."""

    def validate_environment(self) -> Dict[str, Any]:
        sbatch_cmd = shutil.which("sbatch")
        squeue_cmd = shutil.which("squeue")
        return {
            "available": bool(sbatch_cmd and squeue_cmd),
            "scheduler": "slurm",
            "sbatch_path": sbatch_cmd,
            "squeue_path": squeue_cmd,
        }

    def generate_sbatch_script(
        self,
        job_id: str,
        orca_executable: str,
        input_filename: str,
        resources: Dict[str, Any],
        work_dir: str,
    ) -> str:
        clean_job_id = validate_scheduler_identifier(job_id, "job_id")
        clean_input = validate_filename(input_filename, "input_filename")
        clean_res = validate_resources(resources)

        cores = clean_res["cpu_cores"]
        nodes = clean_res["nodes"]
        tasks = clean_res["tasks"]
        ram_gb = clean_res["ram_gb"]
        walltime = clean_res["walltime"]
        partition = clean_res.get("partition")
        account = clean_res.get("account")
        qos = clean_res.get("qos")

        directives = [
            "#!/bin/bash",
            f"#SBATCH --job-name=chemlab_{clean_job_id}",
            f"#SBATCH --output={clean_job_id}.out",
            f"#SBATCH --error={clean_job_id}.err",
            f"#SBATCH --nodes={nodes}",
            f"#SBATCH --ntasks={tasks}",
            f"#SBATCH --cpus-per-task={max(1, cores // tasks)}",
            f"#SBATCH --mem={int(ram_gb * 1024)}M",
            f"#SBATCH --time={walltime}",
        ]
        if partition and partition.lower() not in ("default", "none", "auto"):
            directives.append(f"#SBATCH --partition={partition}")
        if account:
            directives.append(f"#SBATCH --account={account}")
        if qos:
            directives.append(f"#SBATCH --qos={qos}")

        module_cmd = self.config.get("module_load_command", "")
        if module_cmd and any(c in module_cmd for c in ("\n", "\r", "\0")):
            raise HpcValidationError("module_load_command contains forbidden newline characters")

        body = [
            "",
            "set -euo pipefail",
            f"cd -- {shlex.quote(str(work_dir))}",
        ]
        if module_cmd:
            body.append(module_cmd)

        body.append(
            f"{shlex.quote(str(orca_executable))} {shlex.quote(clean_input)} "
            f"> {shlex.quote(clean_job_id + '.out')} 2>&1"
        )
        return "\n".join(directives + body) + "\n"

    def submit_job(
        self,
        job_id: str,
        script_path: str,
        resources: Dict[str, Any],
        work_dir: str,
    ) -> Dict[str, Any]:
        try:
            clean_job_id = validate_scheduler_identifier(job_id, "job_id")
            clean_path = validate_script_path(script_path, work_dir)
            _ = validate_resources(resources)

            res = subprocess.run(
                ["sbatch", clean_path],
                cwd=work_dir,
                capture_output=True,
                text=True,
                shell=False,
                timeout=30,
            )
            if res.returncode != 0:
                return {"ok": False, "error": f"sbatch failed: {res.stderr.strip()}"}
            # Output format: "Submitted batch job 123456"
            match = re.search(r"Submitted batch job (\d+)", res.stdout)
            if match:
                sched_id = match.group(1)
                return {"ok": True, "scheduler_job_id": sched_id}
            return {"ok": False, "error": f"Could not parse sbatch output: {res.stdout}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_job_status(self, scheduler_job_id: str) -> str:
        try:
            clean_id = validate_scheduler_identifier(str(scheduler_job_id), "scheduler_job_id")
            res = subprocess.run(
                ["squeue", "-j", clean_id, "-h", "-o", "%T"],
                capture_output=True,
                text=True,
                shell=False,
                timeout=15,
            )
            if res.returncode == 0:
                state = res.stdout.strip().upper()
                if "RUNNING" in state or "COMPLETING" in state:
                    return "RUNNING"
                if "PENDING" in state or "CONFIGURING" in state:
                    return "PENDING"

            # Check sacct if not in active queue
            sacct_res = subprocess.run(
                ["sacct", "-j", clean_id, "-n", "-X", "-o", "State"],
                capture_output=True,
                text=True,
                shell=False,
                timeout=15,
            )
            if sacct_res.returncode == 0:
                state = sacct_res.stdout.strip().upper()
                if "COMPLETED" in state:
                    return "COMPLETED"
                if "FAILED" in state or "CANCELLED" in state or "TIMEOUT" in state:
                    return "FAILED"
                if "RUNNING" in state:
                    return "RUNNING"
                if "PENDING" in state:
                    return "PENDING"
            return "UNKNOWN"
        except Exception:
            return "UNKNOWN"

    def cancel_job(self, scheduler_job_id: str) -> bool:
        try:
            clean_id = validate_scheduler_identifier(str(scheduler_job_id), "scheduler_job_id")
            res = subprocess.run(
                ["scancel", clean_id],
                capture_output=True,
                text=True,
                shell=False,
                timeout=15,
            )
            return res.returncode == 0
        except Exception:
            return False
