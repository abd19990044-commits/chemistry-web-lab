# -*- coding: utf-8 -*-
"""Thin Flask HTTP surface over :class:`OrchestratorService`."""
from __future__ import annotations

import os
from flask import Blueprint, after_this_request, jsonify, request, send_file
from .credentials import parse as parse_credentials
from .errors import (ConcurrencyError, IntegrityError, OrchestratorError,
                     PermanentError, RateLimitError, TransientError, ValidationError)
from .logging_ext import get_logger, log_context, log_failure, new_correlation_id
from .service import get_service

log = get_logger("orca.api")
bp = Blueprint("orchestrator", __name__, url_prefix="/api/orca")
_MAX_INPUT_BYTES = 2 * 1024 * 1024
_ALLOWED_AUX_EXTENSIONS = (".xyz", ".allxyz", ".hess", ".inp", ".pdb", ".mdrestart")


def _error(exc, status):
    response = jsonify({"ok": False, **exc.to_dict()})
    response.status_code = status
    retry_after = getattr(exc, "retry_after", None)
    if retry_after: response.headers["Retry-After"] = str(int(retry_after))
    return response


@bp.errorhandler(OrchestratorError)
def _handle_orchestrator_error(exc):
    from .errors import AuthenticationError, NotFoundError, PayloadTooLargeError
    if isinstance(exc, AuthenticationError): return _error(exc, 401)
    if isinstance(exc, NotFoundError): return _error(exc, 404)
    if isinstance(exc, PayloadTooLargeError): return _error(exc, 413)
    if isinstance(exc, ConcurrencyError): return _error(exc, 409)
    if isinstance(exc, ValidationError): return _error(exc, 400)
    if isinstance(exc, IntegrityError): return _error(exc, 422)
    if isinstance(exc, RateLimitError): return _error(exc, 429)
    if isinstance(exc, TransientError):
        response = _error(exc, 503); response.headers.setdefault("Retry-After", "15"); return response
    if isinstance(exc, PermanentError): return _error(exc, 400)
    return _error(exc, 500)


def _credentials_from(source):
    return parse_credentials((source.get("kaggle_username") or "").strip(),
                             (source.get("kaggle_key") or "").strip())


def _json(): return request.get_json(force=True, silent=True) or {}


@bp.before_request
def _bind_correlation_id():
    request.environ["orca.correlation_id"] = request.headers.get("X-Correlation-Id") or new_correlation_id()


@bp.after_request
def _emit_correlation_id(response):
    response.headers["X-Correlation-Id"] = request.environ.get("orca.correlation_id", "-")
    return response


@bp.route("/login", methods=["POST"])
def login():
    creds = _credentials_from(_json()); service = get_service()
    with log_context(correlation_id=request.environ["orca.correlation_id"]):
        service.authenticate(creds.username, creds.key or creds.api_token)
        return jsonify({"ok": True, "owner": creds.username, "jobs": service.list_jobs(creds)})


@bp.route("/jobs", methods=["POST"])
def list_jobs():
    creds = _credentials_from(_json())
    return jsonify({"ok": True, "jobs": get_service().list_jobs(creds)})


@bp.route("/submit", methods=["POST"])
def submit():
    form = request.form; creds = _credentials_from(form)
    input_filename = (form.get("input_filename") or "molecule.inp").strip()
    input_content = form.get("input_content") or ""
    upload = request.files.get("input_file")
    if upload and upload.filename:
        input_filename = os.path.basename(upload.filename)
        raw = upload.read(_MAX_INPUT_BYTES + 1)
        if len(raw) > _MAX_INPUT_BYTES:
            raise ValidationError("the .inp file is too large", limit_bytes=_MAX_INPUT_BYTES)
        input_content = raw.decode("utf-8", errors="replace")
    if not input_content.strip():
        raise ValidationError("There is no .inp content to submit. Build one, paste it, or upload a ready-made .inp file.")
    aux_files = {}
    for uploaded in request.files.getlist("aux_files"):
        if not uploaded.filename: continue
        name = os.path.basename(uploaded.filename)
        if name.lower().endswith(_ALLOWED_AUX_EXTENSIONS): aux_files[name] = uploaded.read()
    datasets = [s.strip() for s in (form.get("dataset_sources") or "").split(",") if s.strip()]
    orca_link = (form.get("orca_link") or "").strip() or None
    if not datasets and not orca_link:
        raise ValidationError("Provide an ORCA source: a licensed Kaggle Dataset identifier or a direct ORCA archive link.")
    result = get_service().submit(
        creds, input_filename=input_filename, input_content=input_content,
        job_name=(form.get("job_name") or "").strip(), aux_files=aux_files,
        dataset_sources=datasets, orca_link=orca_link,
        idempotency_key=request.headers.get("Idempotency-Key"))
    return jsonify({"ok": True, **result.to_dict(),
                    "message": "Job submitted. It will continue automatically across Kaggle sessions until it finishes."})


@bp.route("/status", methods=["POST"])
def status():
    payload = _json(); creds = _credentials_from(payload); job_id = (payload.get("job_id") or "").strip()
    return jsonify({"ok": True, "job": get_service().status(creds, job_id)})


@bp.route("/cancel", methods=["POST"])
def cancel():
    payload = _json(); creds = _credentials_from(payload)
    return jsonify({"ok": True, "job": get_service().cancel(creds, (payload.get("job_id") or "").strip())})


@bp.route("/resume", methods=["POST"])
def resume():
    payload = _json(); creds = _credentials_from(payload)
    return jsonify({"ok": True, "job": get_service().resume(creds, (payload.get("job_id") or "").strip())})


@bp.route("/delete", methods=["POST"])
def delete():
    payload = _json(); creds = _credentials_from(payload)
    return jsonify({"ok": True, **get_service().delete(creds, (payload.get("job_id") or "").strip())})


@bp.route("/results", methods=["POST"])
def results():
    payload = _json(); creds = _credentials_from(payload)
    job_id = (payload.get("job_id") or "").strip(); slug = (payload.get("slug") or "").strip() or None
    zip_path, cleanup_dir = get_service().fetch_results(creds, job_id, slug=slug)
    if not zip_path:
        import shutil; shutil.rmtree(cleanup_dir, ignore_errors=True)
        raise ValidationError("No output files exist for this window yet.")
    @after_this_request
    def _cleanup(response):
        import shutil; shutil.rmtree(cleanup_dir, ignore_errors=True); return response
    return send_file(zip_path, as_attachment=True, download_name=f"{slug or job_id}_results.zip")


@bp.route("/health", methods=["GET"])
def health():
    from .service import service_is_ready
    try:
        return jsonify({"ok": True, "ready": True, **get_service().health()})
    except Exception as exc:
        return jsonify({"ok": False, "ready": service_is_ready(),
                        "error": f"{type(exc).__name__}: {exc}",
                        "note": "The orchestrator service could not be constructed; the next request will retry."}), 503


@bp.route("/sweep", methods=["POST"])
def sweep():
    return jsonify({"ok": True, "sweep": get_service().sweep_now()})


@bp.route("/state-machine", methods=["GET"])
def state_machine():
    from .states import TRANSITIONS, JobState
    return jsonify({"ok": True, "mermaid": TRANSITIONS.as_mermaid(),
                    "states": {state.value: {"terminal": state.is_terminal,
                    "handoff": state.is_handoff,
                    "triggers": [t.value for t in TRANSITIONS.allowed_triggers(state)],
                    "reachable": [s.value for s in TRANSITIONS.reachable_states(state)]} for state in JobState}})


def register(app, *, start_watchdog=True):
    """Register canonical routes and install the legacy adapter before app.py
    declares its historical /api/kaggle/* routes.

    The ordering is intentional: the adapter patches only this Flask instance's
    ``add_url_rule`` while the remaining app routes are being declared. No Flask
    global is modified. This guarantees that the browser's existing endpoints
    execute OrchestratorService rather than the obsolete kaggle_runner.
    """
    from .legacy_compat import install_legacy_route_adapter
    install_legacy_route_adapter(app)
    app.register_blueprint(bp)
    log.info("orchestrator API registered", extra={"event": "api_registered", "url_prefix": bp.url_prefix})
    try:
        get_service(start_watchdog=start_watchdog)
    except Exception as exc:
        log_failure(log, what="warming the orchestrator service during startup",
                    why=f"{type(exc).__name__}: {exc}",
                    recovery="API routes remain registered and service construction is retried lazily",
                    next_action="retry on the first request", exc=exc)
