# -*- coding: utf-8 -*-
"""Pydantic schemas for Quantum Chemical Analysis API."""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class AnalyzeOutputRequest(BaseModel):
    output_text: str = Field(..., description="Raw ORCA calculation output text")
    job_id: Optional[str] = Field(None, description="Optional job identifier")


class AnalysisJobSummary(BaseModel):
    job_index: int
    energy_hartree: Optional[float] = None
    converged: bool = False
    frequencies: List[float] = Field(default_factory=list)


class AnalysisResponse(BaseModel):
    ok: bool = True
    job_count: int
    final_energy_hartree: Optional[float] = None
    converged: bool = False
    jobs: List[Dict[str, Any]] = Field(default_factory=list)


class FrequencyResponse(BaseModel):
    ok: bool = True
    frequencies: List[float] = Field(default_factory=list)
    ir_intensities: List[float] = Field(default_factory=list)
    has_imaginary: bool = False
    zero_point_energy_hartree: Optional[float] = None
