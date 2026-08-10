# -*- coding: utf-8 -*-
"""Compatibility adapter for the pre-orchestrator browser API.

The UI historically called /api/kaggle/*.  The production orchestrator lives
behind /api/orca/*.  This adapter keeps the old browser contract stable while
routing every calculation operation through OrchestratorService.  It is
intentionally small and contains no orchestration logic of its own.
"""
from __future__ import annotations

import os
import shutil

from flask import after_this_request, jsonify, request, send_file

from .credentials import parse as parse_credentials
from .errors import OrchestratorError, ValidationError
from .service import get_service


def _creds(source):
    return parse_credentials(
        (source.get("kaggle_username") or "").strip(),
        (source.get("kaggle_key") or "").strip(),
    )


def _legacy_status(state: str) -> str:
    state = str(state or "").upper()
    if state == "FINISHED":
        return "complete"
    if state == "FAILED":
        return "error"
    if state == "CANCELLED":
        return "cancelled"
    if state in {"RUNNING"}:
        return "running"
    if state in {"CHECKPOINTING", "DOWNLOADING", "VERIFYING", "RESTORING", "ROLLING_BACK", "RESTARTING"}:
        return "restarting"
    return "queued"


def _legacy_job(job: dict) -> dict:
    return {
        "job_id": job.get("job_id"),
        "title": job.get("title") or job.get("job_id"),
        "job_title": job.get("title") or job.get("job_id"),
        "kaggle_url": job.get("kaggle_url") or "",
        "status": _legacy_status(job.get("state")),
        "state": job.get("state"),
        "epoch": job.get("epoch", 0),
        "restarts": job.get("epoch", 0),
        "chain_ids": job.get("chain_slugs") or [job.get("current_slug") or job.get("job_id")],
        "chain_slugs": job.get("chain_slugs") or [],
        "current_slug": job.get("current_slug") or job.get("job_id"),
        "last_run": job.get("updated_at"),
        "warning": job.get("note") or (job.get("error") or {}).get("message"),
    }


def login():
    creds = _creds(request.get_json(force=True, silent=True) or {})
    service = get_service()
    service.authenticate(creds.username, creds.key or creds.api_token)
    jobs = [_legacy_job(j) for j in service.list_jobs(creds)]
    return jsonify({"ok": True, "jobs": jobs, "owner": creds.username})


def submit():
    form = request.form
    creds = _creds(form)
    input_filename = (form.get("input_filename") or "molecule.inp").strip()
    input_content = form.get("input_content") or ""
    upload = request.files.get("input_file")
    if upload and upload.filename:
        input_filename = os.path.basename(upload.filename)
        raw = upload.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValidationError("the .inp file is too large", limit_bytes=2 * 1024 * 1024)
        input_content = raw.decode("utf-8", errors="replace")
    if not input_content.strip():
        raise ValidationError("There is no .inp content to submit.")

    aux = {}
    allowed = (".xyz", ".allxyz", ".hess", ".inp", ".pdb", ".mdrestart")
    for uploaded in request.files.getlist("aux_files"):
        if not uploaded.filename:
            continue
        name = os.path.basename(uploaded.filename)
        if name.lower().endswith(allowed):
            aux[name] = uploaded.read()

    datasets = [s.strip() for s in (form.get("dataset_sources") or "").split(",") if s.strip()]
    orca_link = (form.get("orca_link") or "").strip() or None
    if not datasets and not orca_link:
        raise ValidationError("Provide an ORCA source: a Kaggle Dataset identifier or a direct download link.")

    result = get_service().submit(
        creds,
        input_filename=input_filename,
        input_content=input_content,
        job_name=(form.get("job_name") or "").strip(),
        aux_files=aux,
        dataset_sources=datasets,
        orca_link=orca_link,
        idempotency_key=request.headers.get("Idempotency-Key"),
    )
    return jsonify({
        "ok": True,
        "kaggle_url": result.url,
        "job_id": result.job_id,
        "job_title": result.title,
        "title": result.title,
        "replayed": result.replayed,
        "message": "Job submitted to the ORCA orchestrator and will continue automatically across Kaggle sessions.",
    })


def status():
    data = request.get_json(force=True, silent=True) or {}
    creds = _creds(data)
    job_id = (data.get("job_id") or "").strip()
    if not job_id:
        raise ValidationError("Missing job id.")
    job = get_service().status(creds, job_id)
    legacy = _legacy_job(job)
    # The orchestrator keeps one stable job id across every continuation, so
    # there is no need for the browser to replace its id when a new window is
    # created. The current URL and chain are always returned instead.
    legacy.update({
        "note": job.get("note"),
        "warning": job.get("note") or (job.get("error") or {}).get("message"),
        "next_job_id": None,
        "next_kaggle_url": job.get("kaggle_url"),
        "chain_ids": job.get("chain_slugs") or [],
    })
    return jsonify({"ok": True, **legacy})


def download():
    data = request.get_json(force=True, silent=True) or {}
    creds = _creds(data)
    job_id = (data.get("job_id") or "").strip()
    if not job_id:
        raise ValidationError("Missing job id.")
    zip_path, cleanup_dir = get_service().fetch_results(creds, job_id)
    if not zip_path:
        shutil.rmtree(cleanup_dir, ignore_errors=True)
        raise ValidationError("No output files exist for this job yet.")

    @after_this_request
    def _cleanup(response):
        shutil.rmtree(cleanup_dir, ignore_errors=True)
        return response

    return send_file(zip_path, as_attachment=True, download_name=f"{job_id}_results.zip")


def delete():
    data = request.get_json(force=True, silent=True) or {}
    creds = _creds(data)
    job_id = (data.get("job_id") or "").strip()
    if not job_id:
        raise ValidationError("Missing job id.")
    return jsonify({"ok": True, **get_service().delete(creds, job_id)})


def cancel():
    data = request.get_json(force=True, silent=True) or {}
    creds = _creds(data)
    job_id = (data.get("job_id") or "").strip()
    return jsonify({"ok": True, "job": get_service().cancel(creds, job_id)})


def resume():
    data = request.get_json(force=True, silent=True) or {}
    creds = _creds(data)
    job_id = (data.get("job_id") or "").strip()
    return jsonify({"ok": True, "job": get_service().resume(creds, job_id)})


LEGACY_ROUTES = {
    "/api/kaggle/login": login,
    "/api/kaggle/submit": submit,
    "/api/kaggle/status": status,
    "/api/kaggle/download": download,
    "/api/kaggle/delete": delete,
    "/api/kaggle/cancel": cancel,
    "/api/kaggle/resume": resume,
}


def install_legacy_route_adapter() -> None:
    """Make legacy route declarations resolve to the orchestrator handlers.

    app.py still declares the historical endpoints. Replacing their view
    function at registration time avoids duplicate Flask rules and, crucially,
    prevents the old kaggle_runner from ever handling a calculation request.
    """
    from flask import Flask

    if getattr(Flask.add_url_rule, "_orca_legacy_adapter", False):
        return

    original = Flask.add_url_rule

    def add_url_rule(self, rule, endpoint=None, view_func=None, **options):
        replacement = LEGACY_ROUTES.get(str(rule))
        if replacement is not None:
            view_func = replacement
        return original(self, rule, endpoint=endpoint, view_func=view_func, **options)

    add_url_rule._orca_legacy_adapter = True
    Flask.add_url_rule = add_url_rule
