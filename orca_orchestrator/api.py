# -*- coding: utf-8 -*-
"""
Flask blueprint: a thin HTTP surface over `OrchestratorService`.

Deliberately thin. Every handler parses input, calls exactly one service
method, and serialises the result. No orchestration decision is made here,
because anything decided in a request handler is unavailable to the watchdog,
untestable without a WSGI stack, and re-implemented slightly differently the
next time it is needed.

Two cross-cutting behaviours are handled once, for every route:

  * **Typed errors map to HTTP status codes.** `TransientError` becomes 503
    with `Retry-After` so a client backs off correctly; `PermanentError`
    becomes 4xx; `IntegrityError` becomes 422. The old API returned 502 for
    everything, which made every failure look identical to a browser and gave
    the front end nothing to reason about.
  * **Credentials never touch a log or a response.** They arrive per-request,
    are parsed once, cached in RAM, and are scrubbed from every log record by
    `logging_ext.RedactingFilter`.
"""
from __future__ import annotations

import base64
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
# Restart-capable, verifiable, ASCII. `.gbw` is deliberately not here: it
# cannot be checked for truncation and a corrupt one aborts the run that reads
# it (see orca_artifacts.validate_gbw).
_ALLOWED_AUX_EXTENSIONS = (".xyz", ".allxyz", ".hess", ".inp", ".pdb", ".mdrestart")


def _error(exc: OrchestratorError, status: int):
    payload = {"ok": False, **exc.to_dict()}
    response = jsonify(payload)
    response.status_code = status
    retry_after = getattr(exc, "retry_after", None)
    if retry_after:
        response.headers["Retry-After"] = str(int(retry_after))
    return response


@bp.errorhandler(OrchestratorError)
def _handle_orchestrator_error(exc: OrchestratorError):
    """Single mapping from the typed hierarchy to HTTP.

    The status code is derived from the *class*, so a new error type is handled
    correctly the moment it is defined, without anyone having to remember to
    add a branch here."""
    from .errors import AuthenticationError, NotFoundError, PayloadTooLargeError

    if isinstance(exc, AuthenticationError):
        return _error(exc, 401)
    if isinstance(exc, NotFoundError):
        return _error(exc, 404)
    if isinstance(exc, PayloadTooLargeError):
        return _error(exc, 413)
    if isinstance(exc, ConcurrencyError):
        return _error(exc, 409)
    if isinstance(exc, ValidationError):
        return _error(exc, 400)
    if isinstance(exc, IntegrityError):
        return _error(exc, 422)
    if isinstance(exc, RateLimitError):
        return _error(exc, 429)
    if isinstance(exc, TransientError):
        # 503 + Retry-After: a transport failure says nothing about the job, and
        # the client should come back rather than treat it as a job failure.
        response = _error(exc, 503)
        response.headers.setdefault("Retry-After", "15")
        return response
    if isinstance(exc, PermanentError):
        return _error(exc, 400)
    return _error(exc, 500)


def _credentials_from(source: dict):
    return parse_credentials(
        (source.get("kaggle_username") or "").strip(),
        (source.get("kaggle_key") or "").strip(),
    )


def _json() -> dict:
    return request.get_json(force=True, silent=True) or {}


@bp.before_request
def _bind_correlation_id():
    request.environ["orca.correlation_id"] = (
        request.headers.get("X-Correlation-Id") or new_correlation_id()
    )


@bp.after_request
def _emit_correlation_id(response):
    response.headers["X-Correlation-Id"] = request.environ.get("orca.correlation_id", "-")
    return response


# ---------------------------------------------------------------------------
# Auth / listing
# ---------------------------------------------------------------------------
@bp.route("/login", methods=["POST"])
def login():
    """Verifies credentials and returns the account's full job list.

    Sign-in and job-list recovery are one call on purpose: the job list is
    rebuilt from Kaggle, not from the browser, so signing in from a new device
    or after clearing site data restores everything."""
    creds = _credentials_from(_json())
    service = get_service()
    with log_context(correlation_id=request.environ["orca.correlation_id"]):
        service.authenticate(creds.username, creds.key or creds.api_token)
        return jsonify({"ok": True, "owner": creds.username,
                        "jobs": service.list_jobs(creds)})


@bp.route("/jobs", methods=["POST"])
def list_jobs():
    creds = _credentials_from(_json())
    with log_context(correlation_id=request.environ["orca.correlation_id"]):
        return jsonify({"ok": True, "jobs": get_service().list_jobs(creds)})


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------
@bp.route("/submit", methods=["POST"])
def submit():
    """Creates a job.

    Accepts an `Idempotency-Key` header. Without one, a key is derived from the
    credentials plus a hash of the input, which already makes an accidental
    double-submit of identical content a no-op -- the common case for a
    double-clicked button or a refreshed POST. A client that genuinely wants to
    run the same input twice supplies distinct keys."""
    form = request.form
    creds = _credentials_from(form)

    input_filename = (form.get("input_filename") or "molecule.inp").strip()
    input_content = form.get("input_content") or ""

    upload = request.files.get("input_file")
    if upload and upload.filename:
        input_filename = os.path.basename(upload.filename)
        raw = upload.read(_MAX_INPUT_BYTES + 1)
        if len(raw) > _MAX_INPUT_BYTES:
            raise ValidationError("the .inp file is too large",
                                  limit_bytes=_MAX_INPUT_BYTES)
        input_content = raw.decode("utf-8", errors="replace")

    if not input_content.strip():
        raise ValidationError(
            "There is no .inp content to submit. Build one with the Input Generator, "
            "paste it directly, or upload a ready-made .inp file."
        )

    aux_files: dict[str, bytes] = {}
    for uploaded in request.files.getlist("aux_files"):
        if not uploaded.filename:
            continue
        name = os.path.basename(uploaded.filename)
        if not name.lower().endswith(_ALLOWED_AUX_EXTENSIONS):
            continue
        aux_files[name] = uploaded.read()

    datasets = [s.strip() for s in (form.get("dataset_sources") or "").split(",") if s.strip()]
    orca_link = (form.get("orca_link") or "").strip() or None
    if not datasets and not orca_link:
        raise ValidationError(
            "Provide an ORCA source: either a Kaggle Dataset identifier holding your own "
            "licensed ORCA package (for example username/orca-6-1-0), or a direct download "
            "link to the ORCA archive."
        )

    with log_context(correlation_id=request.environ["orca.correlation_id"]):
        result = get_service().submit(
            creds,
            input_filename=input_filename,
            input_content=input_content,
            job_name=(form.get("job_name") or "").strip(),
            aux_files=aux_files,
            dataset_sources=datasets,
            orca_link=orca_link,
            idempotency_key=request.headers.get("Idempotency-Key"),
        )
        return jsonify({"ok": True, **result.to_dict(),
                        "message": "Job submitted. It will continue automatically across "
                                   "Kaggle sessions until it finishes."})


# ---------------------------------------------------------------------------
# Status / control
# ---------------------------------------------------------------------------
@bp.route("/status", methods=["POST"])
def status():
    payload = _json()
    creds = _credentials_from(payload)
    job_id = (payload.get("job_id") or "").strip()
    with log_context(correlation_id=request.environ["orca.correlation_id"], job_id=job_id):
        return jsonify({"ok": True, "job": get_service().status(creds, job_id)})


@bp.route("/cancel", methods=["POST"])
def cancel():
    payload = _json()
    creds = _credentials_from(payload)
    job_id = (payload.get("job_id") or "").strip()
    return jsonify({"ok": True, "job": get_service().cancel(creds, job_id)})


@bp.route("/resume", methods=["POST"])
def resume():
    payload = _json()
    creds = _credentials_from(payload)
    job_id = (payload.get("job_id") or "").strip()
    return jsonify({"ok": True, "job": get_service().resume(creds, job_id)})


@bp.route("/delete", methods=["POST"])
def delete():
    payload = _json()
    creds = _credentials_from(payload)
    job_id = (payload.get("job_id") or "").strip()
    return jsonify({"ok": True, **get_service().delete(creds, job_id)})


@bp.route("/results", methods=["POST"])
def results():
    """Streams a window's output archive.

    Kept off the status path deliberately: a real results bundle runs to
    hundreds of megabytes, and downloading it on every poll is what made a
    finished job look permanently stuck -- the request exceeded the web
    server's timeout, which from the browser is indistinguishable from a job
    that never finished."""
    payload = _json()
    creds = _credentials_from(payload)
    job_id = (payload.get("job_id") or "").strip()
    slug = (payload.get("slug") or "").strip() or None

    zip_path, cleanup_dir = get_service().fetch_results(creds, job_id, slug=slug)
    if not zip_path:
        import shutil

        shutil.rmtree(cleanup_dir, ignore_errors=True)
        raise ValidationError(
            "No output files exist for this window yet. It may still be finishing -- try "
            "again shortly, or open the notebook on kaggle.com."
        )

    @after_this_request
    def _cleanup(response):
        # Unlinking a file that is still open is safe on POSIX: the inode
        # survives until the last descriptor closes, so the stream completes.
        import shutil

        shutil.rmtree(cleanup_dir, ignore_errors=True)
        return response

    return send_file(zip_path, as_attachment=True,
                     download_name=f"{slug or job_id}_results.zip")


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------
@bp.route("/health", methods=["GET"])
def health():
    """Reports readiness, and distinguishes it from liveness.

    A service that has not been constructed yet is reported as `ready: false`
    with the reason, rather than raising a 500. That distinction matters during
    a boot race: the routes are up and the next request will very likely
    succeed, which is a different situation from a genuinely broken deploy."""
    from .service import service_is_ready

    try:
        return jsonify({"ok": True, "ready": True, **get_service().health()})
    except Exception as exc:  # noqa: BLE001
        return jsonify({
            "ok": False,
            "ready": service_is_ready(),
            "error": f"{type(exc).__name__}: {exc}",
            "note": "the orchestrator service could not be constructed. The API routes "
                    "are registered, so this will be retried on the next request.",
        }), 503


@bp.route("/sweep", methods=["POST"])
def sweep():
    """Triggers a watchdog sweep immediately.

    Useful when the background sweeper is disabled (single-worker deploys,
    tests) and as an operator escape hatch. Safe to call repeatedly: every
    action it can take is idempotent."""
    return jsonify({"ok": True, "sweep": get_service().sweep_now()})


@bp.route("/state-machine", methods=["GET"])
def state_machine():
    """Returns the live transition table as a Mermaid diagram.

    Generated from the same table the runtime dispatches on, so the published
    diagram cannot drift away from the implemented behaviour."""
    from .states import TRANSITIONS, JobState

    return jsonify({
        "ok": True,
        "mermaid": TRANSITIONS.as_mermaid(),
        "states": {
            state.value: {
                "terminal": state.is_terminal,
                "handoff": state.is_handoff,
                "triggers": [t.value for t in TRANSITIONS.allowed_triggers(state)],
                "reachable": [s.value for s in TRANSITIONS.reachable_states(state)],
            }
            for state in JobState
        },
    })


def register(app, *, start_watchdog: bool = True) -> None:
    """Attaches the blueprint, then warms the service on a best-effort basis.

    The ordering is the fix for a production incident. Registration used to warm
    the service *first*, so a transient failure there -- two gunicorn workers
    racing to initialise the same fresh SQLite file -- propagated out of
    `register()`, `app.py` caught it, and that worker served the site with the
    orchestrator permanently disabled. With two workers, roughly half of all
    `/api/orca/*` requests then returned 404, intermittently, in a way that
    would look to a user like the site randomly forgetting their jobs.

    Registering the routes first makes the failure recoverable: `get_service()`
    retries on the first request that needs it. Warming is still attempted here
    because startup recovery is worth running during boot, where its log output
    sits next to the deploy rather than inside some unlucky user's request.
    """
    app.register_blueprint(bp)
    log.info("orchestrator API registered", extra={
        "event": "api_registered", "url_prefix": bp.url_prefix})

    try:
        get_service(start_watchdog=start_watchdog)
    except Exception as exc:  # noqa: BLE001
        log_failure(
            log,
            what="warming the orchestrator service during startup",
            why=f"{type(exc).__name__}: {exc}",
            recovery="the API routes are registered regardless, so no endpoint is lost",
            next_action="the service will be constructed on the first request that needs "
                        "it; if the cause was a transient boot race between workers, that "
                        "retry will succeed",
            exc=exc,
        )
