# -*- coding: utf-8 -*-
"""ExecutionBackend Abstraction for Chemistry Lab.

Authoritative Execution Model:
- Primary default: LOCAL_AGENT (Companion Agent on user's own computer).
- Secondary options: HPC (University cluster), KAGGLE (Kaggle cloud, disabled by default).
- Restricted admin option: SERVER_LOCAL (Server-host ORCA, admin-only).
- Zero silent fallback: if selected backend/device is offline, submission stops and reports clearly.
"""
from __future__ import annotations

import enum
import logging
from typing import Any, Dict, Optional

from services.auth_service import AuthorizationError, is_admin_user
from services import local_agent_service

LOGGER = logging.getLogger("chemlab.execution")


class ExecutionBackendType(str, enum.Enum):
    LOCAL_AGENT = "local_agent"
    HPC = "hpc"
    KAGGLE = "kaggle"
    SERVER_LOCAL = "server_local"


class ExecutionError(Exception):
    """Raised when calculation execution cannot proceed."""
    def __init__(self, message: str, code: str = "EXECUTION_ERROR", status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


def resolve_execution_target(
    backend_kind: Optional[str] = None,
    agent_session_id: Optional[str] = None,
    owner_id: Optional[str] = None,
    resources: Optional[Dict[str, Any]] = None,
    state_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolves and validates the execution target, ensuring ownership and online status."""
    raw_kind = (backend_kind or "local_agent").strip().lower()

    # Map legacy strings
    if raw_kind in ("local", "agent", "companion", "workstation", "local_agent"):
        kind = ExecutionBackendType.LOCAL_AGENT
    elif raw_kind in ("hpc", "cluster", "supercomputer"):
        kind = ExecutionBackendType.HPC
    elif raw_kind in ("kaggle", "kaggle_cloud"):
        kind = ExecutionBackendType.KAGGLE
    elif raw_kind in ("server_local", "server_host"):
        kind = ExecutionBackendType.SERVER_LOCAL
    else:
        raise ExecutionError(f"Unsupported execution backend '{backend_kind}'.", code="INVALID_BACKEND")

    res_spec = resources or {}
    cpu_cores = int(res_spec.get("cpu_cores") or 4)
    ram_gb = float(res_spec.get("ram_gb") or 8.0)
    disk_gb = float(res_spec.get("disk_gb") or 20.0)

    # 1. Local Companion Agent
    if kind == ExecutionBackendType.LOCAL_AGENT:
        if not owner_id:
            raise ExecutionError("Authentication required for Local ORCA execution.", code="AUTH_REQUIRED", status_code=401)

        sess_id = agent_session_id
        if not sess_id or sess_id in ("server_local", "local_agent", "default"):
            devices = local_agent_service.list_user_runtime_devices(owner_id=owner_id, state_dir=state_dir)
            online_devs = [d for d in devices if d.get("status") == "ONLINE" and d.get("backend_kind") != "hpc"]
            if not online_devs:
                raise ExecutionError(
                    "No online Local Agent computer connected to your account. "
                    "Please start your Local Agent and enter its Connection API.",
                    code="NO_ONLINE_AGENT_DEVICE",
                )
            sess_id = online_devs[0]["agent_session_id"]

        # Verify device ownership and status
        dev = local_agent_service.get_user_runtime_device(agent_session_id=sess_id, owner_id=owner_id, state_dir=state_dir)
        if not dev:
            raise AuthorizationError("Device not found or not owned by your account.", status_code=403)
        if dev.get("status") != "ONLINE":
            raise ExecutionError(f"Selected computer '{dev.get('display_name')}' is offline.", code="DEVICE_OFFLINE")

        return {
            "backend": ExecutionBackendType.LOCAL_AGENT.value,
            "agent_session_id": sess_id,
            "device_name": dev.get("display_name"),
            "platform": dev.get("platform"),
            "resources": {
                "cpu_cores": cpu_cores,
                "ram_gb": ram_gb,
                "disk_gb": disk_gb,
            },
        }

    # 2. HPC Supercomputer Cluster
    if kind == ExecutionBackendType.HPC:
        if not owner_id:
            raise ExecutionError("Authentication required for HPC execution.", code="AUTH_REQUIRED", status_code=401)

        sess_id = agent_session_id
        if not sess_id or sess_id in ("hpc", "cluster"):
            devices = local_agent_service.list_user_runtime_devices(owner_id=owner_id, state_dir=state_dir)
            online_hpc = [d for d in devices if d.get("status") == "ONLINE" and d.get("backend_kind") == "hpc"]
            if not online_hpc:
                raise ExecutionError(
                    "No online HPC cluster connected to your account. "
                    "Please start your HPC Agent on your login node.",
                    code="NO_ONLINE_HPC_DEVICE",
                )
            sess_id = online_hpc[0]["agent_session_id"]

        dev = local_agent_service.get_user_runtime_device(agent_session_id=sess_id, owner_id=owner_id, state_dir=state_dir)
        if not dev:
            raise AuthorizationError("HPC device not found or not owned by your account.", status_code=403)
        if dev.get("status") != "ONLINE":
            raise ExecutionError(f"Selected HPC cluster '{dev.get('display_name')}' is offline.", code="DEVICE_OFFLINE")

        return {
            "backend": ExecutionBackendType.HPC.value,
            "agent_session_id": sess_id,
            "device_name": dev.get("display_name"),
            "scheduler_type": dev.get("scheduler_type") or res_spec.get("scheduler_type") or "slurm",
            "resources": {
                "nodes": int(res_spec.get("hpc_nodes") or res_spec.get("nodes") or 1),
                "cpu_cores": cpu_cores,
                "ram_gb": ram_gb,
                "disk_gb": disk_gb,
                "walltime": str(res_spec.get("hpc_walltime") or res_spec.get("walltime") or "04:00:00"),
                "partition": str(res_spec.get("hpc_partition") or res_spec.get("partition") or "standard"),
            },
        }

    # 3. Kaggle Cloud
    if kind == ExecutionBackendType.KAGGLE:
        return {
            "backend": ExecutionBackendType.KAGGLE.value,
            "resources": {
                "cpu_cores": min(cpu_cores, 4),
                "ram_gb": min(ram_gb, 30.0),
                "disk_gb": min(disk_gb, 20.0),
            },
        }

    # 4. Server-Host Local (Admin only)
    if kind == ExecutionBackendType.SERVER_LOCAL:
        if not is_admin_user(owner_id):
            raise AuthorizationError(
                "Server-host ORCA execution is restricted to trusted administrators. "
                "Please use your Local Companion Agent.",
                status_code=403,
            )
        return {
            "backend": ExecutionBackendType.SERVER_LOCAL.value,
            "resources": {
                "cpu_cores": cpu_cores,
                "ram_gb": ram_gb,
                "disk_gb": disk_gb,
            },
        }

    raise ExecutionError(f"Unknown execution backend '{backend_kind}'.", code="INVALID_BACKEND")
