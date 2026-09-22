# -*- coding: utf-8 -*-
"""PBS / Torque Scheduler Adapter."""
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


class PbsAdapter(BaseSchedulerAdapter):
    """PBS Pro / OpenPBS / Torque adapter."""

    def validate_environment(self) -> Dict[str, Any]:
        qsub_cmd = shutil.which("qsub")
        qstat_cmd = shutil.which("qstat")
        return {
            "available": bool(qsub_cmd and qstat_cmd),
            "scheduler": "pbs",
            "qsub_path": qsub_cmd,
            "qstat_path": qstat_cmd,
        }

    def generate_pbs_script(
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
        queue = clean_res.get("queue") or clean_res.get("partition")
        account = clean_res.get("account")

        mem_mb = int(ram_gb * 1024)
        directives = [
            "#!/bin/bash",
            f"#PBS -N chemlab_{clean_job_id}",
            f"#PBS -o {clean_job_id}.out",
            f"#PBS -e {clean_job_id}.err",
            f"#PBS -l select={nodes}:ncpus={cores}:mpiprocs={tasks}:mem={mem_mb}mb",
            f"#PBS -l walltime={walltime}",
        ]
        if queue and queue.lower() not in ("default", "none", "auto"):
            directives.append(f"#PBS -q {queue}")
        if account:
            directives.append(f"#PBS -A {account}")

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
                ["qsub", clean_path],
                cwd=work_dir,
                capture_output=True,
                text=True,
                shell=False,
                timeout=30,
            )
            if res.returncode != 0:
                return {"ok": False, "error": f"qsub failed: {res.stderr.strip()}"}
            raw_sched_id = res.stdout.strip()
            match = re.search(r"^([A-Za-z0-9_.-]+)", raw_sched_id)
            if match:
                sched_id = match.group(1)
                return {"ok": True, "scheduler_job_id": sched_id}
            return {"ok": False, "error": f"Could not parse qsub output: {raw_sched_id}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_job_status(self, scheduler_job_id: str) -> str:
        try:
            clean_id = validate_scheduler_identifier(str(scheduler_job_id), "scheduler_job_id")
            res = subprocess.run(
                ["qstat", clean_id],
                capture_output=True,
                text=True,
                shell=False,
                timeout=15,
            )
            if res.returncode != 0:
                err_lower = (res.stderr or "").lower()
                out_lower = (res.stdout or "").lower()
                # F-028: PBS nonzero exit only means completed if scheduler reports unknown job id (finished)
                if any(phrase in err_lower or phrase in out_lower for phrase in (
                    "unknown job id", "unknown job identifier", "unknown job", "job has finished", "not found"
                )):
                    return "COMPLETED"
                return "UNKNOWN"
            out = res.stdout
            if " R " in out:
                return "RUNNING"
            if " Q " in out or " H " in out:
                return "PENDING"
            if " C " in out or " E " in out:
                return "COMPLETED"
            return "UNKNOWN"
        except Exception:
            return "UNKNOWN"

    def cancel_job(self, scheduler_job_id: str) -> bool:
        try:
            clean_id = validate_scheduler_identifier(str(scheduler_job_id), "scheduler_job_id")
            res = subprocess.run(["qdel", clean_id], capture_output=True, text=True, shell=False, timeout=15)
            return res.returncode == 0
        except Exception:
            return False
