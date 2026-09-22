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

from api.routes import analysis as analysis_routes
from api.routes import compound as compound_routes
from api.routes import health as health_routes
from api.routes import kaggle as kaggle_routes
from api.routes import local_agent as local_agent_routes
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
        version="1.0.4",
        description="Versioned API for ORCA-powered computational chemistry "
                    "workflows (generation, Kaggle execution, local agent, artifacts, analysis).",
        docs_url="/api/v1/docs",
        redoc_url="/api/v1/redoc",
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )

    import os
    from fastapi.middleware.cors import CORSMiddleware
    from api.middleware import NativeApiBodyLimitMiddleware

    max_bytes = int(os.environ.get("CHEMISTRY_LAB_API_MAX_BODY_BYTES", 10 * 1024 * 1024))
    application.add_middleware(NativeApiBodyLimitMiddleware, max_bytes=max_bytes)

    @application.middleware("http")
    async def _csrf_middleware(request: Request, call_next):
        if request.url.path.startswith("/api/v1") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            from services.auth_service import browser_csrf_is_valid, _signed_flask_session_from_request
            session = _signed_flask_session_from_request(request)
            if session and session.get("user") and not browser_csrf_is_valid(request):
                return JSONResponse(
                    status_code=403,
                    content={
                        "ok": False,
                        "error": {
                            "code": "CSRF_FORBIDDEN",
                            "message": "CSRF validation failed: missing or invalid CSRF token.",
                        },
                    },
                )
        return await call_next(request)

    allowed_origins_raw = os.environ.get("CHEMISTRY_LAB_ALLOWED_ORIGINS", "")
    if allowed_origins_raw.strip():
        allowed_origins = [o.strip() for o in allowed_origins_raw.split(",") if o.strip()]
    else:
        allowed_origins = [
            "http://localhost:7860",
            "http://127.0.0.1:7860",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
            "http://localhost:5000",
            "http://127.0.0.1:5000",
        ]

    application.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    application.include_router(health_routes.router, prefix="/api/v1")
    application.include_router(orca_routes.router, prefix="/api/v1")
    application.include_router(kaggle_routes.router, prefix="/api/v1")
    application.include_router(local_agent_routes.router, prefix="/api/v1")
    application.include_router(analysis_routes.router, prefix="/api/v1")
    application.include_router(compound_routes.router, prefix="/api/v1")

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
    if isinstance(exc.detail, dict):
        code = exc.detail.get("code", code)
        detail = exc.detail.get("message", detail)
    return JSONResponse(status_code=exc.status_code, content={
        "ok": False, "error": {"code": code, "message": detail}})


@app.exception_handler(Exception)
async def _unhandled_error(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={
        "ok": False, "error": {"code": "INTERNAL_ERROR",
                               "message": "an internal error occurred; it has been logged"}})


# ── Legacy Flask app (UI pages + legacy /api/* routes) as a WSGI mount ──────
try:
    from app import app as flask_app  # noqa: E402  (runs orchestrator boot once)

    app.mount("/", WSGIMiddleware(flask_app), name="flask-legacy")
except Exception as exc:
    import logging
    logging.getLogger("chemlab.fastapi").exception("Failed to mount Flask WSGI compatibility layer: %s", exc)
    raise RuntimeError(f"Fatal startup failure: could not mount Flask WSGI app: {exc}") from exc
