# -*- coding: utf-8 -*-
"""Pydantic schemas for Compound and Structure APIs."""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class CompoundQueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=256, description="Compound name, formula, or SMILES")


class Clean3DRequest(BaseModel):
    xyz_text: str = Field(..., max_length=10 * 1024 * 1024, description="XYZ coordinate block to optimize with force-field")
    force_field: str = Field(default="UFF", max_length=16, description="Force field to use: UFF or MMFF94")
