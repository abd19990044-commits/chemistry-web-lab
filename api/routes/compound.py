# -*- coding: utf-8 -*-
"""FastAPI endpoints for Compound Resolution and 3D Coordinates."""
from __future__ import annotations

from typing import Any, Dict
from fastapi import APIRouter, HTTPException, status
from api.schemas.compound import CompoundQueryRequest
from services import compound_service

router = APIRouter(prefix="/compound", tags=["compound"])


@router.post("/resolve", summary="Resolve compound name or SMILES via PubChem")
def resolve_compound(req: CompoundQueryRequest) -> Dict[str, Any]:
    res = compound_service.resolve_pubchem_compound(req.query)
    if not res.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": res.get("error_code", "COMPOUND_NOT_FOUND"), "message": res.get("error")},
        )
    return res
