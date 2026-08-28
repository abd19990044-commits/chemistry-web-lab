# -*- coding: utf-8 -*-
"""Health (liveness) and ready (readiness) endpoints."""
from fastapi import APIRouter

from api.dependencies import orca_engine_available, orchestrator_available
from services import health_service

router = APIRouter(tags=["health"])


@router.get("/health")
def health():
    return health_service.build_health(
        orchestrator_available=orchestrator_available(),
        orca_engine_available=orca_engine_available())


@router.get("/ready")
def ready():
    return health_service.build_ready(
        orchestrator_available=orchestrator_available(),
        orca_engine_available=orca_engine_available())
