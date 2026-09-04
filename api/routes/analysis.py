# -*- coding: utf-8 -*-
"""FastAPI endpoints for Quantum Chemistry Analysis."""
from __future__ import annotations

from typing import Any, Dict
from fastapi import APIRouter, HTTPException, status
from api.schemas.analysis import AnalysisResponse, AnalyzeOutputRequest, FrequencyResponse
from services import analysis_service

router = APIRouter(prefix="/analysis", tags=["analysis"])


@router.post("/analyze", response_model=AnalysisResponse, summary="Parse and analyze raw ORCA output text")
def analyze_orca_output(req: AnalyzeOutputRequest) -> Dict[str, Any]:
    res = analysis_service.analyze_output_text(req.output_text)
    if not res.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": res.get("error_code", "ANALYSIS_ERROR"), "message": res.get("error")},
        )
    return res


@router.post("/frequencies", response_model=FrequencyResponse, summary="Extract vibrational frequencies and IR intensities")
def extract_vibrations(req: AnalyzeOutputRequest) -> Dict[str, Any]:
    res = analysis_service.extract_frequencies_and_ir(req.output_text)
    if not res.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": res.get("error_code", "ANALYSIS_ERROR"), "message": res.get("error")},
        )
    return res
