# -*- coding: utf-8 -*-
"""Compatibility adapter for the historical ``/api/kaggle/*`` browser API.

The adapter translates the old HTTP contract to OrchestratorService. It never
runs the legacy Kaggle runner. ``install_legacy_route_adapter`` is called while
app.py is declaring its historical routes, so the instance-local route hook
safely replaces those view functions without modifying Flask globally.
"""
from __future__ import annotations
import os, re, shutil
from types import MethodType
from flask import after_this_request, jsonify, request, send_file
from .credentials import parse as parse_credentials
from .errors import ValidationError
from .service import get_service


def _creds(source):
    return parse_credentials((source.get("kaggle_username") or "").strip(),
                             (source.get("kaggle_key") or "").strip())


def _legacy_status(state):
    state = str(state or "").upper()
    if state == "FINISHED": return "complete"
    if state == "FAILED": return "error"
    if state == "CANCELLED": return "cancelled"
    if state == "RUNNING": return "running"
    if state in {"CHECKPOINTING", "DOWNLOADING", "VERIFYING", "RESTORING", "ROLLING_BACK", "RESTARTING"}: return "restarting"
    return "queued"


def _legacy_job(job):
    chain = job.get("chain_slugs") or [job.get("current_slug") or job.get("job_id")]
    return {"job_id": job.get("job_id"), "title": job.get("title") or job.get("job_id"),
            "job_title": job.get("title") or job.get("job_id"), "kaggle_url": job.get("kaggle_url") or "",
            "status": _legacy_status(job.get("state")), "state": job.get("state"),
            "epoch": job.get("epoch", 0), "restarts": job.get("epoch", 0),
            "chain_ids": chain, "chain_slugs": chain, "current_slug": job.get("current_slug") or job.get("job_id"),
            "last_run": job.get("updated_at"),
            "warning": job.get("note") or (job.get("error") or {}).get("message")}


def login():
    creds = _creds(request.get_json(force=True, silent=True) or {}); service = get_service()
    service.authenticate(creds.username, creds.key or creds.api_token)
    return jsonify({"ok": True, "jobs": [_legacy_job(j) for j in service.list_jobs(creds)], "owner": creds.username})


def submit():
    form = request.form; creds = _creds(form)
    filename = os.path.basename((form.get("input_filename") or "molecule.inp").strip()); content = form.get("input_content") or ""
    upload = request.files.get("input_file")
    if upload and upload.filename:
        filename = os.path.basename(upload.filename); raw = upload.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024: raise ValidationError("the .inp file is too large", limit_bytes=2 * 1024 * 1024)
        content = raw.decode("utf-8", errors="replace")
    if not content.strip(): raise ValidationError("There is no .inp content to submit.")
    aux = {}
    for uploaded in request.files.getlist("aux_files"):
        if not uploaded.filename: continue
        name = os.path.basename(uploaded.filename)
        if name.lower().endswith((".xyz", ".allxyz", ".hess", ".inp", ".pdb", ".mdrestart")): aux[name] = uploaded.read()
    datasets = [x.strip() for x in (form.get("dataset_sources") or "").split(",") if x.strip()]
    link = (form.get("orca_link") or "").strip() or None
    if not datasets and not link: raise ValidationError("Provide an ORCA source: a Kaggle Dataset identifier or a direct download link.")
    result = get_service().submit(creds, input_filename=filename, input_content=content,
                                  job_name=(form.get("job_name") or "").strip(), aux_files=aux,
                                  dataset_sources=datasets, orca_link=link,
                                  idempotency_key=request.headers.get("Idempotency-Key"))
    return jsonify({"ok": True, "kaggle_url": result.url, "job_id": result.job_id,
                    "job_title": result.title, "title": result.title, "replayed": result.replayed,
                    "message": "Job submitted to the ORCA orchestrator and will continue automatically across Kaggle sessions."})


def status():
    data = request.get_json(force=True, silent=True) or {}; creds = _creds(data); job_id = (data.get("job_id") or "").strip()
    if not job_id: raise ValidationError("Missing job id.")
    job = get_service().status(creds, job_id); out = _legacy_job(job)
    out.update({"note": job.get("note"), "warning": job.get("note") or (job.get("error") or {}).get("message"),
                "next_job_id": None, "next_kaggle_url": job.get("kaggle_url")})
    return jsonify({"ok": True, **out})


def download():
    data = request.get_json(force=True, silent=True) or {}; creds = _creds(data); job_id = (data.get("job_id") or "").strip()
    if not job_id: raise ValidationError("Missing job id.")
    path, cleanup = get_service().fetch_results(creds, job_id)
    if not path: shutil.rmtree(cleanup, ignore_errors=True); raise ValidationError("No output files exist for this job yet.")
    @after_this_request
    def _cleanup(response): shutil.rmtree(cleanup, ignore_errors=True); return response
    return send_file(path, as_attachment=True, download_name=f"{job_id}_results.zip")


def delete():
    data = request.get_json(force=True, silent=True) or {}; creds = _creds(data); supplied = (data.get("job_id") or "").strip()
    if not supplied: raise ValidationError("Missing job id.")
    return jsonify({"ok": True, **get_service().delete(creds, re.sub(r"-r\d+$", "", supplied))})


def cancel():
    data = request.get_json(force=True, silent=True) or {}; creds = _creds(data)
    return jsonify({"ok": True, "job": get_service().cancel(creds, (data.get("job_id") or "").strip())})


def resume():
    data = request.get_json(force=True, silent=True) or {}; creds = _creds(data)
    return jsonify({"ok": True, "job": get_service().resume(creds, (data.get("job_id") or "").strip())})


LEGACY_ROUTES = {"/api/kaggle/login": login, "/api/kaggle/submit": submit, "/api/kaggle/status": status,
                 "/api/kaggle/download": download, "/api/kaggle/delete": delete,
                 "/api/kaggle/cancel": cancel, "/api/kaggle/resume": resume}


def install_legacy_route_adapter(app):
    """Bind legacy route declarations to orchestrator handlers on this app only.

    Flask 3's ``route`` decorator delegates to ``add_url_rule``. The previous
    implementation patched only the instance's ``add_url_rule`` and was fragile
    during the package/app import ordering used by this project. Wrapping the
    route decorator itself makes the interception explicit and deterministic.
    """
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
