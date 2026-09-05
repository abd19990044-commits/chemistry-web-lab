# -*- coding: utf-8 -*-
"""Compatibility adapter for the historical ``/api/kaggle/*`` browser API.

The adapter translates the old HTTP contract to OrchestratorService. It never
runs the legacy Kaggle runner. ``install_legacy_route_adapter`` is called while
app.py is declaring its historical routes, so the instance-local route hook
safely replaces those view functions without modifying Flask globally.
"""
from __future__ import annotations
import io, os, re, shutil, zipfile
from types import MethodType
from flask import after_this_request, jsonify, request, send_file
from .account_control import enforce_capacity
from .credentials import parse as parse_credentials
from .errors import PayloadTooLargeError, ValidationError
from .service import get_service


def _creds(source):
    return parse_credentials((source.get("kaggle_username") or "").strip(),
                             (source.get("kaggle_key") or "").strip())


def _legacy_status(state, job_dict=None):
    if hasattr(state, "value"):
        state = state.value
    state = str(state or "").upper()
    if "." in state:
        state = state.split(".")[-1]
    if state in {"FINISHED", "COMPLETED", "COMPLETE", "DONE"}: return "complete"
    if state in {"FAILED", "ERROR"}: return "error"
    if state in {"CANCELLED", "CANCELED"}: return "cancelled"
    if job_dict and (job_dict.get("current_slug") != job_dict.get("job_id") or job_dict.get("epoch", 0) > 0):
        return "restarting"
    if state in {"RUNNING", "READY"}: return "running"
    if state in {"CHECKPOINTING", "DOWNLOADING", "VERIFYING", "RESTORING", "ROLLING_BACK", "RESTARTING", "RESUMING", "CONTINUED"}: return "restarting"
    return "queued"


def _legacy_job(job):
    chain = job.get("chain_slugs") or [job.get("current_slug") or job.get("job_id")]
    return {
        "job_id": job.get("job_id"),
        "title": job.get("title") or job.get("job_id"),
        "job_title": job.get("title") or job.get("job_id"),
        "kaggle_url": job.get("kaggle_url") or "",
        "status": _legacy_status(job.get("state"), job),
        "state": job.get("state"),
        "epoch": job.get("epoch", 0),
        "restarts": job.get("epoch", 0),
        "chain_ids": chain,
        "chain_slugs": chain,
        "current_slug": job.get("current_slug") or job.get("job_id"),
        "last_run": job.get("updated_at"),
        "warning": job.get("note") or (job.get("error") or {}).get("message"),
        "workflow_id": job.get("workflow_id"),
        "step_name": job.get("step_name"),
        "step_index": job.get("step_index"),
        "step_count": job.get("step_count"),
        "resume_required": job.get("resume_required", False),
        "resume_reason": job.get("resume_reason"),
        "local_state": job.get("local_state"),
        "remote_state": job.get("remote_state"),
        "deleted_on_kaggle": job.get("deleted_on_kaggle", False),
        "result_state": job.get("result_state", "REMOTE_ONLY"),
        "storage_durability": job.get("storage_durability", "ephemeral_local"),
        "is_durable": job.get("is_durable", False),
        "cf_sync_status": job.get("cf_sync_status", "DEGRADED_UNSYNCED"),
        "result_available": job.get("result_available", False),
        "result_sha256": job.get("result_sha256"),
        "result_size_bytes": job.get("result_size_bytes"),
        "result_archived_at": job.get("result_archived_at"),
        "result_storage_reference": job.get("result_storage_reference"),
        "result_manifest_id": job.get("result_manifest_id"),
    }


def login():
    creds = _creds(request.get_json(force=True, silent=True) or {})
    service = get_service()
    service.authenticate(creds.username, creds.key or creds.api_token)
    return jsonify({
        "ok": True,
        "jobs": [_legacy_job(j) for j in service.list_jobs(creds)],
        "workflows": service.list_workflows(creds),
        "owner": creds.username,
        "username": creds.username,
    })


def sync():
    creds = _creds(request.get_json(force=True, silent=True) or {})
    service = get_service()
    return jsonify({
        "ok": True,
        "jobs": [_legacy_job(j) for j in service.list_jobs(creds)],
        "workflows": service.list_workflows(creds),
        "owner": creds.username,
        "username": creds.username,
    })


def submit():
    required_passcode = (
        os.environ.get("KAGGLE_EXECUTION_PASSCODE")
        or os.environ.get("KAGGLE_ACCESS_CODE")
        or os.environ.get("KAGGLE_PASSCODE")
        or ""
    ).strip()
    if required_passcode:
        provided = (
            request.form.get("kaggle_passcode")
            or (request.get_json(silent=True) or {}).get("kaggle_passcode")
            or request.headers.get("X-Kaggle-Passcode")
            or ""
        ).strip()
        import hmac
        if not provided or not hmac.compare_digest(provided, required_passcode):
            return jsonify({
                "ok": False,
                "error": "INVALID_KAGGLE_PASSCODE",
                "message": "Invalid or missing Kaggle execution passcode. A valid access code configured for this site is required to run Kaggle calculations.",
            }), 403

    form = request.form; creds = _creds(form)
    raw_datasets = form.get("dataset_sources") or ""
    try:
        from kaggle_runner import clean_dataset_sources
        datasets = clean_dataset_sources(raw_datasets)
    except Exception:
        datasets = [x.strip() for x in raw_datasets.split(",") if x.strip()]
    link = (form.get("orca_link") or "").strip() or None
    if not datasets and not link: raise ValidationError("Provide an ORCA source: a Kaggle Dataset identifier or a direct download link.")

    filename = os.path.basename((form.get("input_filename") or "molecule.inp").strip()); content = form.get("input_content") or ""
    upload = request.files.get("input_file")
    if upload and upload.filename:
        filename = os.path.basename(upload.filename); raw = upload.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024: raise PayloadTooLargeError("the .inp file is larger than the 2 MB upload limit", limit_bytes=2 * 1024 * 1024)
        content = raw.decode("utf-8", errors="replace")
    if not content.strip(): raise ValidationError("submit with an empty .inp is refused: no .inp content to submit.")
    enforce_capacity(get_service(), creds)
    aux = {}
    for uploaded in request.files.getlist("aux_files"):
        if not uploaded.filename: continue
        name = os.path.basename(uploaded.filename)
        if name.lower().endswith((".xyz", ".allxyz", ".hess", ".inp", ".pdb", ".mdrestart")): aux[name] = uploaded.read()
    result = get_service().submit(creds, input_filename=filename, input_content=content,
                                  job_name=(form.get("job_name") or "").strip(), aux_files=aux,
                                  dataset_sources=datasets, orca_link=link,
                                  idempotency_key=request.headers.get("Idempotency-Key"))
    return jsonify({"ok": True, "kaggle_url": result.url, "job_id": result.job_id,
                    "kaggle_owner": creds.username,
                    "job_title": result.title, "title": result.title, "replayed": result.replayed,
                    "message": "Job submitted to the ORCA orchestrator and will continue automatically across Kaggle sessions."})


def status():
    data = request.get_json(force=True, silent=True) or {}; creds = _creds(data); job_id = (data.get("job_id") or "").strip()
    if not job_id: raise ValidationError("Missing job id.")
    if not job_id.startswith("chem-tools-") or ".." in job_id or "/" in job_id or "\\" in job_id:
        raise ValidationError("That job id does not belong to this application.")
    job = get_service().status(creds, job_id)
    out = _legacy_job(job)
    curr_slug = job.get("current_slug") or job.get("job_id")
    next_slug = curr_slug if curr_slug != job_id else None
    note_text = job.get("note") or (job.get("error") or {}).get("message") or ""
    out.update({
        "note": note_text,
        "warning": note_text,
        "next_job_id": next_slug,
        "next_kaggle_url": job.get("kaggle_url"),
    })
    return jsonify({"ok": True, **out})


def download():
    if request.method == "GET":
        data = request.args.to_dict()
    else:
        data = request.get_json(force=True, silent=True) or {}
    creds = _creds(data); job_id = (data.get("job_id") or "").strip()
    mode = (data.get("mode") or "essential").strip().lower()
    if not job_id: raise ValidationError("Missing job id.")
    if not job_id.startswith("chem-tools-") or ".." in job_id or "/" in job_id or "\\" in job_id:
        raise ValidationError("That job id does not belong to this application.")
    path, cleanup = get_service().fetch_results(creds, job_id)
    if not path:
        if cleanup: shutil.rmtree(cleanup, ignore_errors=True)
        raise ValidationError("No output files exist for this job yet.")
    
    # 1. Essential Mode: bundle lightweight outputs (.out, .xyz, .property.txt, etc.)
    ESSENTIAL_EXTS = (".out", ".log", ".xyz", ".inp", ".property.txt", ".txt", ".png", ".svg", ".json")
    EXCLUDED_EXTS = (".gbw", ".tmp", ".densities", ".hess", ".scfp", ".int", ".ges", ".prop")

    if mode == "essential" and cleanup:
        essential_zip = os.path.join(cleanup, "_essential_results.zip")
        bundled = False
        added_names = set()
        with zipfile.ZipFile(essential_zip, "w", zipfile.ZIP_DEFLATED) as ezf:
            if os.path.exists(path) and zipfile.is_zipfile(path):
                with zipfile.ZipFile(path, "r") as src_zf:
                    for item in src_zf.infolist():
                        fname_lower = item.filename.lower()
                        if any(fname_lower.endswith(ext) for ext in ESSENTIAL_EXTS) and not any(fname_lower.endswith(ext) for ext in EXCLUDED_EXTS):
                            if item.filename not in added_names:
                                ezf.writestr(item.filename, src_zf.read(item))
                                added_names.add(item.filename)
                                bundled = True
            for root, _, files in os.walk(cleanup):
                for f in files:
                    full_p = os.path.join(root, f)
                    if full_p in (essential_zip, path):
                        continue
                    fname_lower = f.lower()
                    if any(fname_lower.endswith(ext) for ext in ESSENTIAL_EXTS) and not any(fname_lower.endswith(ext) for ext in EXCLUDED_EXTS):
                        if not any(f.startswith(sec) for sec in ("__results__", "__script__", "__notebook__", "script.py")):
                            if f not in added_names:
                                ezf.write(full_p, f)
                                added_names.add(f)
                                bundled = True
        if bundled and os.path.exists(essential_zip) and zipfile.is_zipfile(essential_zip):
            path = essential_zip

    download_name = f"{job_id}_results.zip" if mode == "essential" else f"{job_id}_full_results.zip"
    file_size = os.path.getsize(path) if os.path.exists(path) else 0
    if file_size <= 100 * 1024 * 1024:
        with open(path, "rb") as fh:
            data_bytes = io.BytesIO(fh.read())
        if cleanup: shutil.rmtree(cleanup, ignore_errors=True)
        return send_file(data_bytes, as_attachment=True, download_name=download_name, mimetype="application/zip")
    else:
        response = send_file(path, as_attachment=True, download_name=download_name, mimetype="application/zip")
        if cleanup:
            response.call_on_close(lambda: shutil.rmtree(cleanup, ignore_errors=True))
        return response


def delete():
    data = request.get_json(force=True, silent=True) or {}; creds = _creds(data); supplied = (data.get("job_id") or "").strip()
    if not supplied: raise ValidationError("Missing job id.")
    if not supplied.startswith("chem-tools-") or ".." in supplied or "/" in supplied or "\\" in supplied:
        raise ValidationError("That job id does not belong to this application.")
    return jsonify({"ok": True, **get_service().delete(creds, re.sub(r"-r\d+$", "", supplied))})


def cancel():
    data = request.get_json(force=True, silent=True) or {}; creds = _creds(data)
    return jsonify({"ok": True, "job": get_service().cancel(creds, (data.get("job_id") or "").strip())})


def resume():
    data = request.get_json(force=True, silent=True) or {}; creds = _creds(data)
    return jsonify({"ok": True, "job": get_service().resume(creds, (data.get("job_id") or "").strip())})


def config():
    required_passcode = (
        os.environ.get("KAGGLE_EXECUTION_PASSCODE")
        or os.environ.get("KAGGLE_ACCESS_CODE")
        or os.environ.get("KAGGLE_PASSCODE")
        or ""
    ).strip()
    return jsonify({
        "ok": True,
        "passcode_required": bool(required_passcode),
    })


def verify_passcode():
    required_passcode = (
        os.environ.get("KAGGLE_EXECUTION_PASSCODE")
        or os.environ.get("KAGGLE_ACCESS_CODE")
        or os.environ.get("KAGGLE_PASSCODE")
        or ""
    ).strip()
    data = request.get_json(silent=True) or request.form or {}
    provided = (
        data.get("passcode")
        or data.get("kaggle_passcode")
        or request.headers.get("X-Kaggle-Passcode")
        or ""
    ).strip()
    if required_passcode:
        import hmac
        if not provided or not hmac.compare_digest(provided, required_passcode):
            return jsonify({
                "ok": False,
                "error": "INVALID_KAGGLE_PASSCODE",
                "message": "Invalid execution passcode. On cloud/domain deployments, please enter the administrator secret passcode.",
            }), 403
    return jsonify({
        "ok": True,
        "message": "Passcode verified successfully.",
        "passcode_required": bool(required_passcode),
    })


from functools import wraps
from .errors import (
    AuthenticationError, ConcurrencyError, KaggleUnavailableError, NetworkError,
    PayloadTooLargeError, RateLimitError, TimeoutError_, ValidationError
)


def _wrap_handler(fn):
    @wraps(fn)
    def _wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValidationError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except PayloadTooLargeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 413
        except AuthenticationError as exc:
            return jsonify({
                "ok": False, "status": "error",
                "error": f"Could not sign in to Kaggle: {exc}. Please verify your username and API key on kaggle.com.",
                "note": "Kaggle username or API key rejected.", "warning": str(exc),
            }), 401
        except RateLimitError as exc:
            return jsonify({
                "ok": False, "status": "queued",
                "note": "Kaggle output files rate-limited; will retry on next poll.",
                "warning": str(exc), "error": str(exc),
            }), 429
        except (KaggleUnavailableError, NetworkError, TimeoutError_) as exc:
            msg = str(exc)
            if "No module named" in msg or "not found" in msg.lower() or "is not recognized" in msg.lower() or "not installed" in msg.lower():
                err_msg = "The kaggle CLI is not installed correctly on this server. Do not regenerate your Kaggle API token; the token is not the problem."
            else:
                err_msg = f"kaggle.com could not be reached from this server: {exc}. Do not regenerate your Kaggle API token; the token is not the problem."
            return jsonify({"ok": False, "error": err_msg}), 503
        except ConcurrencyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
        except Exception as exc:
            import traceback; traceback.print_exc()
            return jsonify({"ok": False, "error": f"Internal server error: {exc}"}), 500
    return _wrapped


LEGACY_ROUTES = {
    "/api/kaggle/login": _wrap_handler(login),
    "/api/kaggle/sync": _wrap_handler(sync),
    "/api/kaggle/submit": _wrap_handler(submit),
    "/api/kaggle/status": _wrap_handler(status),
    "/api/kaggle/download": _wrap_handler(download),
    "/api/kaggle/delete": _wrap_handler(delete),
    "/api/kaggle/cancel": _wrap_handler(cancel),
    "/api/kaggle/resume": _wrap_handler(resume),
    "/api/kaggle/config": _wrap_handler(config),
    "/api/kaggle/verify-passcode": _wrap_handler(verify_passcode),
}


def install_legacy_route_adapter(app):
    """Bind legacy route declarations to orchestrator handlers on this app only."""
    if getattr(app, "_orca_legacy_adapter_installed", False):
        return

    original_route = app.route

    def route(self, rule, **options):
        replacement = LEGACY_ROUTES.get(str(rule))
        if replacement is None:
            return original_route(rule, **options)

        def decorator(view_func):
            endpoint = options.pop("endpoint", None) or view_func.__name__
            self.add_url_rule(rule, endpoint=endpoint, view_func=replacement, **options)
            return view_func

        return decorator

    app.route = MethodType(route, app)
    app._orca_legacy_adapter_installed = True

    # app.py still declares login/submit/status/download/delete. It no longer
    # declares cancel/resume, although the browser contract and old clients do.
    # Register those two missing endpoints directly so the compatibility surface
    # is complete and both operations reach OrchestratorService.
    app.add_url_rule("/api/kaggle/cancel", endpoint="legacy_cancel", view_func=_wrap_handler(cancel), methods=["POST"])
    app.add_url_rule("/api/kaggle/resume", endpoint="legacy_resume", view_func=_wrap_handler(resume), methods=["POST"])
    app.add_url_rule("/api/kaggle/config", endpoint="legacy_config", view_func=_wrap_handler(config), methods=["GET"])
    app.add_url_rule("/api/kaggle/verify-passcode", endpoint="legacy_verify_passcode", view_func=_wrap_handler(verify_passcode), methods=["POST"])
