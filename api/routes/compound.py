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


@router.post("/export-cdxml", summary="Export single compound to ChemDraw XML (.cdxml)")
def export_compound_cdxml(req: CompoundQueryRequest) -> Response:
    from fastapi.responses import Response
    import chem_core as core
    smiles = core.resolve_compound_to_smiles(req.query)
    if not smiles:
        raise HTTPException(status_code=404, detail="Compound not found")
    cdxml_bytes = core.generate_single_compound_cdxml(smiles, title=req.query)
    if not cdxml_bytes:
        raise HTTPException(status_code=422, detail="Failed to generate CDXML")
    filename = f"{core.safe_filename(req.query)}.cdxml"
    return Response(
        content=cdxml_bytes,
        media_type="chemical/x-cdxml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

