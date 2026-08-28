# -*- coding: utf-8 -*-
"""ORCA input generation (v1). Calls the SAME service as the Flask route."""
from fastapi import APIRouter

from api.schemas import OrcaGenerateRequest, OrcaGenerateResponse
from services import orca_service

router = APIRouter(tags=["orca"])


@router.post("/orca/generate", response_model=OrcaGenerateResponse)
def generate(req: OrcaGenerateRequest):
    result, status = orca_service.generate_inputs(req.model_dump())
    return result
