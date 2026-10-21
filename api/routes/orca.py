# -*- coding: utf-8 -*-
"""ORCA input generation (v1). Calls the SAME service as the Flask route."""
from fastapi import APIRouter, HTTPException

from api.schemas import OrcaGenerateRequest, OrcaGenerateResponse
from services import orca_service

router = APIRouter(tags=["orca"])


@router.post("/orca/generate", response_model=OrcaGenerateResponse)
def generate(req: OrcaGenerateRequest):
    result, status = orca_service.generate_inputs(req.model_dump())
    if status != 200:
        raise HTTPException(status_code=status, detail=result.get("error", "Invalid ORCA input"))
    return result
