# -*- coding: utf-8 -*-
"""FastAPI endpoints for Local Companion Agent Runtime Sessions & Device Management."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Header, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, JSONResponse

from api.schemas.local_agent import (
    AgentDeviceListResponse,
    AgentRuntimeDeviceResponse,
    PlatformPackageInfo,
    PlatformPackageListResponse,
    ResourceSpec,
    RuntimeClaimRequest,
    RuntimeClaimResponse,
    RuntimeFinalizeRequest,
    RuntimeFinalizeResponse,
    RuntimeInitRequest,
    RuntimeInitResponse,
)
from services import local_agent_service
from services.auth_service import require_authenticated_owner, get_authenticated_owner, AuthenticationError, AuthorizationError

LOGGER = logging.getLogger("chemlab.api.local_agent")
router = APIRouter(prefix="/local-agent", tags=["local-agent"])


def get_current_owner(
    request: Request,
) -> str:
    owner = get_authenticated_owner(request=request, headers=request.headers if request else None)
    if owner:
        return owner
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")


@router.post("/runtime/init", response_model=RuntimeInitResponse, summary="Agent process registers new ephemeral session")
def runtime_init(req: RuntimeInitRequest) -> Dict[str, Any]:
    res = local_agent_service.init_runtime_session(
        installation_id=req.installation_id,
        agent_session_id=req.agent_session_id,
        token_verifiers=req.token_verifiers,
        device_name=req.device_name,
        platform=req.platform,
        backend_kind=req.backend_kind,
        scheduler_type=req.scheduler_type,
        agent_version=req.agent_version,
        protocol_version=req.protocol_version,
        capabilities=req.capabilities,
        installation_secret=req.installation_secret,
    )
    if not res.get("ok"):
        status_code = status.HTTP_403_FORBIDDEN if res.get("error_code") == "INSTALLATION_AUTH_REQUIRED" else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=status_code, detail=res.get("error"))
    return res


@router.post("/runtime/claim", response_model=RuntimeClaimResponse, summary="Website user enters ephemeral Connection API")
def runtime_claim(req: RuntimeClaimRequest, owner_id: str = Depends(get_current_owner)) -> Dict[str, Any]:
    redacted = local_agent_service.redact_token_for_log(req.connection_api)
    LOGGER.info(f"User {owner_id} claiming ephemeral connection API {redacted}")

    res = local_agent_service.claim_runtime_token(
        connection_api=req.connection_api,
        owner_id=owner_id,
        custom_device_name=req.custom_device_name,
    )
    if not res.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": res.get("error_code", "PAIRING_FAILED"), "message": res.get("error")},
        )
    return res


@router.post("/runtime/finalize", response_model=RuntimeFinalizeResponse, summary="Agent retrieves its in-memory runtime secret")
def runtime_finalize(req: RuntimeFinalizeRequest) -> Dict[str, Any]:
    res = local_agent_service.finalize_agent_runtime(
        agent_session_id=req.agent_session_id,
        connection_api=req.connection_api,
    )
    if not res.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": res.get("error_code", "FINALIZE_FAILED"), "message": res.get("error")},
        )
    return res


@router.post("/runtime/end", summary="Agent process signals graceful exit")
def runtime_end(
    agent_session_id: str = Query(...),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    cred = ""
    if authorization and authorization.startswith("Bearer "):
        cred = authorization[7:].strip()
    if not cred:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing runtime credentials to terminate session.")
    ok = local_agent_service.end_agent_runtime_session(agent_session_id, runtime_secret=cred)
    if not ok:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid runtime credentials.")
    return {"ok": True}


@router.get("/devices", response_model=AgentDeviceListResponse, summary="List active execution devices for user")
@router.get("/agents", response_model=AgentDeviceListResponse, summary="Alias for list active devices")
def list_devices(owner_id: str = Depends(get_current_owner)) -> Dict[str, Any]:
    devs = local_agent_service.list_user_runtime_devices(owner_id=owner_id)
    return {"ok": True, "devices": devs, "count": len(devs)}


@router.delete("/devices/{agent_session_id}", summary="Disconnect a runtime device")
def disconnect_device(agent_session_id: str, owner_id: str = Depends(get_current_owner)) -> Dict[str, Any]:
    ok = local_agent_service.disconnect_user_device(agent_session_id=agent_session_id, owner_id=owner_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found or not owned by your account.")
    return {"ok": True}


@router.get("/packages", response_model=PlatformPackageListResponse, summary="List downloadable agent setup packages")
def list_packages() -> Dict[str, Any]:
    from tools.build_local_agent_packages import get_package_manifests
    pkgs = get_package_manifests()
    return {"ok": True, "packages": pkgs}


@router.get("/packages/{package_id}", summary="Download downloadable agent ZIP package")
def download_package(package_id: str):
    from tools.build_local_agent_packages import get_package_file_path
    p = get_package_file_path(package_id)
    if not p or not os.path.isfile(p):
        raise HTTPException(status_code=404, detail=f"Package {package_id} not found.")
    return FileResponse(p, media_type="application/zip", filename=os.path.basename(p))


@router.websocket("/ws")
async def websocket_agent_endpoint(websocket: WebSocket):
    await websocket.accept()
    auth_header = websocket.headers.get("Authorization") or ""
    session_id_param = websocket.query_params.get("agent_session_id") or ""
    cred = ""
    if auth_header.startswith("Bearer "):
        cred = auth_header[7:].strip()

    if not session_id_param or not cred:
        await websocket.send_json({"type": "ERROR", "error": "Missing agent_session_id or runtime_secret"})
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    auth_info = local_agent_service.authenticate_runtime_session(
        agent_session_id=session_id_param,
        runtime_session_secret=cred,
    )
    if not auth_info:
        await websocket.send_json({"type": "AUTH_FAILED", "error": "Invalid agent_session_id or runtime_secret."})
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    local_agent_service._WS_CONNECTIONS[session_id_param] = websocket
    local_agent_service._WS_OWNERS[session_id_param] = auth_info["owner_id"]

    await websocket.send_json({
        "type": "AUTH_OK",
        "agent_session_id": session_id_param,
        "owner_id": auth_info["owner_id"],
        "device_name": auth_info["display_name"],
    })

    try:
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")
            if mtype == "HEARTBEAT":
                local_agent_service.record_heartbeat(session_id_param, capabilities=msg.get("capabilities"))
                await websocket.send_json({"type": "HEARTBEAT_ACK", "timestamp": msg.get("timestamp")})
            elif mtype == "JOB_RESULT":
                job_id = str(msg.get("job_id") or "").strip()
                payload = msg.get("result") or {}
                if not job_id or not isinstance(payload, dict):
                    await websocket.send_json({
                        "type": "JOB_RESULT_NACK",
                        "job_id": job_id,
                        "error": "Invalid job result payload",
                    })
                    continue
                try:
                    exit_code = int(payload.get("exit_code", 0))
                except (TypeError, ValueError):
                    await websocket.send_json({
                        "type": "JOB_RESULT_NACK",
                        "job_id": job_id,
                        "error": "Invalid exit_code",
                    })
                    continue
                result = local_agent_service.complete_agent_job(
                    job_id=job_id,
                    agent_session_id=session_id_param,
                    runtime_session_secret=cred,
                    exit_code=exit_code,
                    stdout_tail=str(payload.get("stdout_tail") or ""),
                    parsed_results=(payload.get("parsed_results")
                                    if isinstance(payload.get("parsed_results"), dict)
                                    else {}),
                    error_message=(str(payload.get("error_message"))
                                   if payload.get("error_message") else None),
                    output_text=(str(payload.get("output_text"))
                                 if payload.get("output_text") is not None else None),
                    xyz_structure=(str(payload.get("xyz_structure"))
                                   if payload.get("xyz_structure") is not None else None),
                )
                if result.get("ok"):
                    await websocket.send_json({
                        "type": "JOB_RESULT_ACK",
                        "job_id": job_id,
                        "status": result.get("status"),
                    })
                else:
                    await websocket.send_json({
                        "type": "JOB_RESULT_NACK",
                        "job_id": job_id,
                        "error": result.get("error", "Result persistence failed"),
                    })
    except WebSocketDisconnect:
        local_agent_service._WS_CONNECTIONS.pop(session_id_param, None)
        local_agent_service._WS_OWNERS.pop(session_id_param, None)
    except Exception as exc:
        LOGGER.warning(f"WebSocket error for {session_id_param}: {exc}")
        local_agent_service._WS_CONNECTIONS.pop(session_id_param, None)
        local_agent_service._WS_OWNERS.pop(session_id_param, None)
