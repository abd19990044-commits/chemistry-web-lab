# -*- coding: utf-8 -*-
"""Chemistry Lab API - FastAPI v1 layer.

Architecture (gradual migration, see FASTAPI_MIGRATION_ARCHITECTURE.md):

    UI pages (/ /lab /calculations /analysis)  ─┐
                                                ├─ served by the legacy
    legacy /api/* routes                        ─┘   Flask app (WSGI mount)
    /api/v1/*                                   ─── FastAPI (this app)

Both API layers call the SAME service modules in `services/`; there is no
second business implementation. Long-running ORCA/Kaggle work stays in the
existing orchestrator - never inside HTTP request handlers.

Run:  uvicorn api.main:app
"""
from contextlib import asynccontextmanager

from a2wsgi.wsgi import WSGIMiddleware
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from api.routes import health as health_routes
from api.routes import kaggle as kaggle_routes
from api.routes import orca as orca_routes


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # The legacy Flask app is imported below; importing it runs the
    # orchestrator's boot reconciliation exactly once (startup recovery,
    # logging, credential vault) - the same process serves both layers.
    yield


def _create_app() -> FastAPI:
    application = FastAPI(
        title="Chemistry Lab API",
        version="1.0.3",
        description="Versioned API for ORCA-powered computational chemistry "
                    "workflows (generation, Kaggle execution, artifacts, analysis).",
        docs_url="/api/v1/docs",
        redoc_url="/api/v1/redoc",
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )

    application.include_router(health_routes.router, prefix="/api/v1")
    application.include_router(orca_routes.router, prefix="/api/v1")
    application.include_router(kaggle_routes.router, prefix="/api/v1")

    return application


app = _create_app()


# ── Centralized error contract (Phase 6) ────────────────────────────────────
# Every /api/v1 error answers {ok: false, error: {code, message}} with no
# stack traces and no secrets.
@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    first = (exc.errors() or [{}])[0]
    loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
    return JSONResponse(status_code=422, content={
        "ok": False,
        "error": {"code": "VALIDATION_ERROR",
                  "message": "%s: %s" % (loc or "request", first.get("msg", "invalid input"))},
    })


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    codes = {400: "BAD_REQUEST", 401: "UNAUTHORIZED", 403: "FORBIDDEN",
             404: "NOT_FOUND", 409: "CONFLICT", 413: "PAYLOAD_TOO_LARGE",
             422: "VALIDATION_ERROR", 429: "RATE_LIMITED", 502: "BAD_GATEWAY",
             503: "SERVICE_UNAVAILABLE", 504: "GATEWAY_TIMEOUT"}
    code = codes.get(exc.status_code, "ERROR")
    detail = exc.detail if isinstance(exc.detail, str) else "request failed"
    return JSONResponse(status_code=exc.status_code, content={
        "ok": False, "error": {"code": code, "message": detail}})


@app.exception_handler(Exception)
async def _unhandled_error(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={
        "ok": False, "error": {"code": "INTERNAL_ERROR",
                               "message": "an internal error occurred; it has been logged"}})


# ── Legacy Flask app (UI pages + legacy /api/* routes) as a WSGI mount ──────
# Mounted LAST so the /api/v1 routes above always win. This keeps the three
# primary pages (/ /lab /calculations /analysis) and every legacy route
# working in the same process/port during the migration.
try:
    from app import app as flask_app  # noqa: E402  (runs orchestrator boot once)

    app.mount("/", WSGIMiddleware(flask_app), name="flask-legacy")
except Exception:  # noqa: BLE001  - pragma: no cover
    # The API can still boot without the legacy UI mounted (e.g. API-only
    # deployments); the mount is what makes the single-port deployment work.
    pass
