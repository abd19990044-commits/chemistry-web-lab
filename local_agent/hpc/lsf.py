# -*- coding: utf-8 -*-
"""IBM Spectrum LSF Scheduler Adapter."""
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


class LsfAdapter(BaseSchedulerAdapter):
    """LSF adapter."""

    def validate_environment(self) -> Dict[str, Any]:
        bsub_cmd = shutil.which("bsub")
        bjobs_cmd = shutil.which("bjobs")
        return {
            "available": bool(bsub_cmd and bjobs_cmd),
            "scheduler": "lsf",
            "bsub_path": bsub_cmd,
            "bjobs_path": bjobs_cmd,
        }

    def generate_lsf_script(
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
        ram_gb = clean_res["ram_gb"]
        walltime = clean_res["walltime"]
        queue = clean_res.get("queue") or clean_res.get("partition")
        account = clean_res.get("account")

        mem_mb = int(ram_gb * 1024)
        directives = [
            "#!/bin/bash",
            f"#BSUB -J chemlab_{clean_job_id}",
            f"#BSUB -o {clean_job_id}.out",
            f"#BSUB -e {clean_job_id}.err",
            f"#BSUB -n {cores}",
            f"#BSUB -M {mem_mb}MB",
            f"#BSUB -W {walltime}",
        ]
        if queue and queue.lower() not in ("default", "none", "auto"):
            directives.append(f"#BSUB -q {queue}")
        if account:
            directives.append(f"#BSUB -P {account}")

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

            with open(clean_path, "r", encoding="utf-8") as batch_file:
                res = subprocess.run(
                    ["bsub"],
                    stdin=batch_file,
                    cwd=work_dir,
                    capture_output=True,
                    text=True,
                    shell=False,
                    timeout=30,
                )
            if res.returncode != 0:
                return {"ok": False, "error": f"bsub failed: {res.stderr.strip()}"}
            # Typical LSF output: "Job <12345> is submitted to queue <normal>."
            match = re.search(r"Job <(\d+)>", res.stdout)
            if match:
                sched_id = match.group(1)
                return {"ok": True, "scheduler_job_id": sched_id}
            num_match = re.search(r"\b(\d+)\b", res.stdout)
            if num_match:
                return {"ok": True, "scheduler_job_id": num_match.group(1)}
            return {"ok": False, "error": f"Could not parse bsub output: {res.stdout}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_job_status(self, scheduler_job_id: str) -> str:
        try:
            clean_id = validate_scheduler_identifier(str(scheduler_job_id), "scheduler_job_id")
            res = subprocess.run(
                ["bjobs", clean_id],
                capture_output=True,
                text=True,
                shell=False,
                timeout=15,
            )
            if res.returncode != 0:
                err_lower = (res.stderr or "").lower()
                out_lower = (res.stdout or "").lower()
                # F-028: LSF nonzero exit only maps to completed if bjobs confirms job finished
                if any(phrase in err_lower or phrase in out_lower for phrase in (
                    "not found", "is not found", "no such job", "illegal job id"
                )):
                    try:
                        res_a = subprocess.run(
                            ["bjobs", "-a", clean_id],
                            capture_output=True,
                            text=True,
                            shell=False,
                            timeout=15,
                        )
                        if res_a.returncode == 0:
                            out_a = res_a.stdout.upper()
                            if "DONE" in out_a:
                                return "COMPLETED"
                            if "EXIT" in out_a:
                                return "FAILED"
                    except Exception:
                        pass
                    return "COMPLETED"
                return "UNKNOWN"
            out = res.stdout.upper()
            if "RUN" in out:
                return "RUNNING"
            if "PEND" in out:
                return "PENDING"
            if "DONE" in out:
                return "COMPLETED"
            if "EXIT" in out:
                return "FAILED"
            return "UNKNOWN"
        except Exception:
            return "UNKNOWN"

    def cancel_job(self, scheduler_job_id: str) -> bool:
        try:
            clean_id = validate_scheduler_identifier(str(scheduler_job_id), "scheduler_job_id")
            res = subprocess.run(
                ["bkill", clean_id],
                capture_output=True,
                text=True,
                shell=False,
                timeout=15,
            )
            return res.returncode == 0
        except Exception:
            return False
