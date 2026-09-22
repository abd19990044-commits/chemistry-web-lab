# -*- coding: utf-8 -*-
"""Base interface for HPC Scheduler Adapters."""
from __future__ import annotations

import abc
import os
import re
from typing import Any, Dict, List, Optional


class HpcValidationError(ValueError):
    """Raised when an HPC scheduler input fails security validation."""
    pass


_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SAFE_WALLTIME_RE = re.compile(r"^(?:(?:\d+-)?\d{1,4}:[0-5]\d:[0-5]\d|(?:[0-5]?\d):[0-5]\d|\d{1,6})$")


def validate_scheduler_identifier(val: Any, field_name: str = "identifier", max_len: int = 128) -> str:
    """Validates scheduler identifiers (job_id, partition, queue, account, qos, scheduler_job_id).
    
    Rejects newlines, carriage returns, null bytes, shell metacharacters, and whitespace.
    Allows only alphanumeric characters, underscores, hyphens, and dots.
    """
    if val is None or not isinstance(val, str):
        raise HpcValidationError(f"{field_name} must be a non-empty string, got {type(val).__name__}")
    s = val.strip()
    if not s:
        raise HpcValidationError(f"{field_name} cannot be empty")
    if len(s) > max_len:
        raise HpcValidationError(f"{field_name} exceeds maximum length of {max_len} characters")
    if any(c in s for c in ("\n", "\r", "\0")):
        raise HpcValidationError(f"{field_name} contains forbidden newline or null characters")
    if not _SAFE_IDENTIFIER_RE.match(s):
        raise HpcValidationError(
            f"{field_name} contains invalid characters: {val!r}. Only [A-Za-z0-9_.-] allowed."
        )
    return s


def validate_filename(val: Any, field_name: str = "filename", max_len: int = 255) -> str:
    """Validates that a filename is a safe relative basename without directory traversal."""
    if val is None or not isinstance(val, str):
        raise HpcValidationError(f"{field_name} must be a non-empty string, got {type(val).__name__}")
    s = val.strip()
    if not s:
        raise HpcValidationError(f"{field_name} cannot be empty")
    if len(s) > max_len:
        raise HpcValidationError(f"{field_name} exceeds maximum length of {max_len} characters")
    if any(c in s for c in ("\n", "\r", "\0", "/", "\\")):
        raise HpcValidationError(f"{field_name} contains directory separators, newlines, or null bytes")
    if ".." in s:
        raise HpcValidationError(f"{field_name} cannot contain path traversal ('..')")
    if not _SAFE_IDENTIFIER_RE.match(s):
        raise HpcValidationError(
            f"{field_name} contains invalid characters: {val!r}. Only [A-Za-z0-9_.-] allowed."
        )
    return s


def validate_walltime(val: Any, field_name: str = "walltime") -> str:
    """Validates walltime string (HH:MM:SS, D-HH:MM:SS, MM:SS, or minutes integer)."""
    if val is None or not isinstance(val, (str, int)):
        raise HpcValidationError(f"{field_name} must be a string or integer, got {type(val).__name__}")
    s = str(val).strip()
    if not s:
        raise HpcValidationError(f"{field_name} cannot be empty")
    if any(c in s for c in ("\n", "\r", "\0", ";", "&", "|", "`", "$")):
        raise HpcValidationError(f"{field_name} contains forbidden control or shell characters")
    if not _SAFE_WALLTIME_RE.match(s):
        raise HpcValidationError(f"Invalid walltime format: {val!r}. Expected HH:MM:SS or D-HH:MM:SS")
    return s


def validate_numeric_resource(
    val: Any,
    field_name: str,
    min_val: int = 1,
    max_val: int = 100000,
    allow_float: bool = False,
) -> int | float:
    """Validates positive numeric resource allocations."""
    if val is None:
        raise HpcValidationError(f"{field_name} cannot be None")
    try:
        if allow_float:
            num = float(val)
        else:
            num = int(val)
    except (ValueError, TypeError):
        raise HpcValidationError(f"{field_name} must be numeric, got {val!r}")
    if num < min_val or num > max_val:
        raise HpcValidationError(f"{field_name} value {num} out of bounds [{min_val}, {max_val}]")
    return num


def validate_resources(resources: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Validates and sanitizes HPC resource specifications."""
    if not isinstance(resources, dict):
        raise HpcValidationError("Resources must be a dictionary")

    cleaned: Dict[str, Any] = {}

    # cpu_cores
    cores = resources.get("cpu_cores", 1)
    cleaned["cpu_cores"] = validate_numeric_resource(cores, "cpu_cores", min_val=1, max_val=1024)

    # nodes
    nodes = resources.get("nodes", 1)
    cleaned["nodes"] = validate_numeric_resource(nodes, "nodes", min_val=1, max_val=256)

    # tasks
    tasks = resources.get("tasks", cleaned["cpu_cores"])
    cleaned["tasks"] = validate_numeric_resource(tasks, "tasks", min_val=1, max_val=1024)

    # ram_gb
    ram_gb = resources.get("ram_gb", 4.0)
    cleaned["ram_gb"] = validate_numeric_resource(ram_gb, "ram_gb", min_val=0.1, max_val=16384, allow_float=True)

    # walltime
    walltime = resources.get("walltime", "02:00:00")
    cleaned["walltime"] = validate_walltime(walltime, "walltime")

    # Optional identifiers: partition, queue, account, qos
    for field in ("partition", "queue", "account", "qos"):
        if field in resources and resources[field] is not None:
            raw_val = str(resources[field]).strip()
            if raw_val:
                # F-033: Normalize 'default', 'none', 'auto' sentinels so adapters don't emit invalid directives
                if field in ("partition", "queue") and raw_val.lower() in ("default", "none", "auto"):
                    continue
                cleaned[field] = validate_scheduler_identifier(raw_val, field)

    return cleaned


def validate_script_path(script_path: str, work_dir: Optional[str] = None) -> str:
    """Validates script file path preventing null bytes, control chars, and shell metacharacters."""
    if not script_path or not isinstance(script_path, str):
        raise HpcValidationError("script_path must be a non-empty string")
    s = script_path.strip()
    if any(c in s for c in ("\0", "\n", "\r", ";", "&", "|", "`", "$", "<", ">")):
        raise HpcValidationError("script_path contains forbidden shell metacharacters or control bytes")
    if work_dir:
        root = os.path.realpath(os.path.abspath(str(work_dir)))
        candidate = os.path.realpath(
            os.path.abspath(s if os.path.isabs(s) else os.path.join(root, s))
        )
        try:
            contained = os.path.commonpath([root, candidate]) == root
        except ValueError:
            contained = False
        if not contained:
            raise HpcValidationError("script_path resolves outside the scheduler work directory")
        return candidate
    return os.path.realpath(os.path.abspath(s))


class BaseSchedulerAdapter(abc.ABC):
    """Abstract base class for cluster workload schedulers (SLURM, PBS, LSF)."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    @abc.abstractmethod
    def validate_environment(self) -> Dict[str, Any]:
        """Checks scheduler availability, commands, and queues."""
        pass

    @abc.abstractmethod
    def submit_job(
        self,
        job_id: str,
        script_path: str,
        resources: Dict[str, Any],
        work_dir: str,
    ) -> Dict[str, Any]:
        """Submits job script to scheduler and returns scheduler_job_id."""
        pass

    @abc.abstractmethod
    def get_job_status(self, scheduler_job_id: str) -> str:
        """Returns normalized status: PENDING, RUNNING, COMPLETED, FAILED, UNKNOWN."""
        pass

    @abc.abstractmethod
    def cancel_job(self, scheduler_job_id: str) -> bool:
        """Cancels a running/pending scheduler job."""
        pass
