# -*- coding: utf-8 -*-
"""Pydantic schemas for Local Agent runtime sessions, claims, and execution devices."""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class RuntimeInitRequest(BaseModel):
    installation_id: str = Field(..., description="Stable non-secret installation UUID")
    agent_session_id: str = Field(..., description="Ephemeral UUID for current process")
    device_name: str = Field("Local Computer", description="Friendly device name")
    token_verifiers: List[str] = Field(..., description="SHA-256 verifiers of generated ephemeral Connection APIs")
    platform: str = Field("unknown", description="Operating system: windows, linux, macos, hpc")
    backend_kind: str = Field("local", description="local or hpc")
    scheduler_type: Optional[str] = Field(None, description="slurm, pbs, lsf")
    agent_version: str = Field("1.0.3", description="Agent version")
    protocol_version: int = Field(1, description="Protocol revision")
    capabilities: Dict[str, Any] = Field(default_factory=dict, description="Hardware & ORCA capabilities")


class RuntimeInitResponse(BaseModel):
    ok: bool = True
    agent_session_id: str
    installation_id: str
    status: str = "UNPAIRED"
    message: str = "Runtime session registered. Awaiting website claim."


class RuntimeClaimRequest(BaseModel):
    connection_api: str = Field(..., description="Ephemeral Connection API printed by the running Agent (e.g. CLA_...)")
    custom_device_name: Optional[str] = Field(None, description="Optional custom name assigned by user")


class RuntimeClaimResponse(BaseModel):
    # CRITICAL: NO runtime_session_secret returned to the browser!
    ok: bool = True
    agent_session_id: str
    installation_id: str
    owner_id: str
    display_name: str
    platform: str
    backend_kind: str
    scheduler_type: Optional[str]
    status: str = "ONLINE"
    paired_at: str


class RuntimeFinalizeRequest(BaseModel):
    agent_session_id: str = Field(..., description="Agent process session ID")
    connection_api: str = Field(..., description="Proof of possession of current Connection API")


class RuntimeFinalizeResponse(BaseModel):
    ok: bool = True
    agent_session_id: str
    owner_id: str
    runtime_session_secret: str = Field(..., description="Delivered ONLY to Agent process for WebSocket authentication")
    status: str = "ONLINE"


class AgentRuntimeDeviceResponse(BaseModel):
    agent_session_id: str
    installation_id: str
    owner_id: str
    display_name: str
    platform: str
    backend_kind: str
    scheduler_type: Optional[str]
    status: str
    process_started_at: str
    paired_at: Optional[str]
    last_seen: Optional[str]
    capabilities: Dict[str, Any]


class AgentDeviceListResponse(BaseModel):
    ok: bool = True
    devices: List[AgentRuntimeDeviceResponse]
    count: int


class ResourceSpec(BaseModel):
    cpu_cores: int = Field(1, ge=1, le=1024)
    ram_gb: float = Field(4.0, ge=0.5, le=4096.0)
    disk_gb: float = Field(20.0, ge=1.0, le=100000.0)
    walltime: Optional[str] = Field(None, description="HH:MM:SS for HPC schedulers")
    nodes: Optional[int] = Field(1, ge=1, le=1000)
    tasks: Optional[int] = Field(None, ge=1, le=10000)
    partition: Optional[str] = Field(None, description="HPC queue or partition")
    account: Optional[str] = Field(None, description="HPC allocation or project account")


class PlatformPackageInfo(BaseModel):
    id: str
    display_name: str
    os_name: str
    filename: str
    size_bytes: int
    sha256: str
    version: str = "1.0.3"
    file_path: Optional[str] = None
    instructions: List[str] = Field(default_factory=list)


class PlatformPackageListResponse(BaseModel):
    ok: bool = True
    packages: List[PlatformPackageInfo]
