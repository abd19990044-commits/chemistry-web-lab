# -*- coding: utf-8 -*-
"""Kaggle job routes (v1): submit, status, artifacts, opt-coordinate import.

All routes delegate to services.kaggle_service - the SAME implementation the
Flask routes call. Idempotency is enforced by the orchestrator store (shared
across workers/processes); the Idempotency-Key header is honored as the
caller's key.
"""
import os
import zipfile

from fastapi import APIRouter, Header, HTTPException

from api.schemas import ExtractOptCoordsRequest, JobStatusRequest, JobSubmitRequest
from services import artifact_service, kaggle_service

router = APIRouter(prefix="/kaggle", tags=["kaggle"])


def _error_message(payload):
    """The service may return {'error': 'msg'} OR {'error': {'code', 'message'}}.
    Both are safe here - never let the transport crash on the error shape."""
    err = payload.get("error")
    if isinstance(err, dict):
        return str(err.get("message", "request failed"))
    return str(err or "request failed")


def _payload_or_http(payload, status):
    if status != 200:
        raise HTTPException(status_code=status, detail=_error_message(payload))
    return payload


@router.get("/config")
def get_kaggle_config():
    required_passcode = (
        os.environ.get("KAGGLE_EXECUTION_PASSCODE")
        or os.environ.get("KAGGLE_ACCESS_CODE")
        or os.environ.get("KAGGLE_PASSCODE")
        or ""
    ).strip()
    return {
        "ok": True,
        "passcode_required": bool(required_passcode),
    }


@router.post("/verify-passcode")
def verify_kaggle_passcode(req: dict | None = None,
                           x_kaggle_passcode: str | None = Header(default=None)):
    required_passcode = (
        os.environ.get("KAGGLE_EXECUTION_PASSCODE")
        or os.environ.get("KAGGLE_ACCESS_CODE")
        or os.environ.get("KAGGLE_PASSCODE")
        or ""
    ).strip()
    provided = ""
    if req and isinstance(req, dict):
        provided = req.get("passcode") or req.get("kaggle_passcode") or ""
    if not provided and x_kaggle_passcode:
        provided = x_kaggle_passcode
    provided = provided.strip()
    if required_passcode:
        import hmac
        if not provided or not hmac.compare_digest(provided, required_passcode):
            raise HTTPException(status_code=403, detail="Invalid execution passcode. On cloud/domain deployments, please enter the administrator secret passcode.")
    return {
        "ok": True,
        "message": "Passcode verified successfully.",
        "passcode_required": bool(required_passcode),
    }


@router.post("/jobs")
def submit_job(req: JobSubmitRequest,
               idempotency_key: str | None = Header(default=None),
               x_kaggle_passcode: str | None = Header(default=None)):
    """Submits a Kaggle calculation. Authoritative idempotency lives in the
    orchestrator store (SQLite, shared across workers/processes): the same
    Idempotency-Key - or the same payload - can never create a second kernel."""
    required_passcode = (
        os.environ.get("KAGGLE_EXECUTION_PASSCODE")
        or os.environ.get("KAGGLE_ACCESS_CODE")
        or os.environ.get("KAGGLE_PASSCODE")
        or ""
    ).strip()
    if required_passcode:
        provided = (req.kaggle_passcode or x_kaggle_passcode or "").strip()
        import hmac
        if not provided or not hmac.compare_digest(provided, required_passcode):
            raise HTTPException(
                status_code=403,
                detail="Invalid or missing Kaggle execution passcode. A valid access code configured for this site is required to run Kaggle calculations."
            )
    payload, status = kaggle_service.submit_job(
        kaggle_username=req.kaggle_username,
        kaggle_key=req.kaggle_key,
        dataset_sources_raw=req.dataset_sources,
        orca_link=req.orca_link,
        input_filename=req.input_filename,
        input_content=req.input_content,
        job_name=req.job_name,
        maxdisk_mb=req.maxdisk_mb,
        idem_key=idempotency_key,
    )
    return _payload_or_http(payload, status)


@router.post("/jobs/{job_id}/status")
def job_status(job_id: str, req: JobStatusRequest):
    payload, status = kaggle_service.check_status(
        req.kaggle_username, req.kaggle_key, job_id)
    return _payload_or_http(payload, status)


@router.post("/jobs/{job_id}/extract-opt-coords")
def extract_opt_coords(job_id: str, req: ExtractOptCoordsRequest):
    """Imports the FINAL optimized geometry - only from a genuinely completed
    (converged) Opt/OptTS calculation. Comment text can never change the
    classification; unconverged/aborted/killed runs are refused."""
    payload, status = kaggle_service.extract_opt_coords(
        req.kaggle_username, req.kaggle_key, job_id)
    return _payload_or_http(payload, status)


@router.get("/jobs/{job_id}/artifacts")
def list_artifacts(job_id: str, kaggle_username: str, kaggle_key: str):
    """Artifact manifest: filename, artifact_type, size, sha256 - never
    filesystem paths. Molden appears as artifact_type=molden with filename
    <base>.molden.input."""
    zip_path, cleanup_dir, payload, status = kaggle_service.fetch_archive(
        kaggle_username, kaggle_key, job_id)
    if status != 200:
        # Deterministic denials (owner isolation, missing archive) surface here
        # with their proper status - never by listing a None archive.
        raise HTTPException(status_code=status, detail=_error_message(payload))
    try:
        listing, list_status = artifact_service.list_artifacts(zip_path)
    finally:
        import shutil
        if cleanup_dir:
            shutil.rmtree(cleanup_dir, ignore_errors=True)
    if list_status != 200:
        raise HTTPException(status_code=list_status, detail=listing.get("error", "unreadable archive"))
    return listing


@router.get("/jobs/{job_id}/artifacts/{artifact_name}")
def download_artifact(job_id: str, artifact_name: str, kaggle_username: str,
                      kaggle_key: str):
    """Streams ONE artifact from the job's results archive.

    Path-traversal safety: the requested name is reduced to its basename and
    matched against ZIP MEMBER basenames - the archive's own stored path is
    never concatenated onto a filesystem path, so nothing outside the archive
    can be read and archive members cannot escape it.
    """
    import re as _re

    # Basename-safe resolution: reject any separator/parent-navigation up front.
    if (not artifact_name
            or "/" in artifact_name or "\\" in artifact_name
            or ".." in artifact_name
            or artifact_name.startswith(".")):
        raise HTTPException(status_code=400, detail="invalid artifact name")
    safe_name = os.path.basename(artifact_name)
    if safe_name != artifact_name:
        raise HTTPException(status_code=400, detail="invalid artifact name")

    zip_path, cleanup_dir, payload, status = kaggle_service.fetch_archive(
        kaggle_username, kaggle_key, job_id)
    if status != 200:
        raise HTTPException(status_code=status, detail=payload.get("error", "archive unavailable"))

    try:
        with zipfile.ZipFile(zip_path) as zf:
            match = None
            for info in zf.infolist():
                if info.is_dir():
                    continue
                member_base = os.path.basename(info.filename)
                if member_base == safe_name:
                    match = info
                    break
            if match is None:
                raise HTTPException(status_code=404, detail="artifact not found in this job's archive")
            data = zf.read(match)
    except HTTPException:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise HTTPException(status_code=502, detail="archive unreadable: %s" % exc)
    finally:
        import shutil
        if cleanup_dir:
            shutil.rmtree(cleanup_dir, ignore_errors=True)

    low = safe_name.lower()
    if low.endswith((".molden.input", ".molden", ".out", ".log", ".txt", ".inp")):
        media = "text/plain"
    elif low.endswith(".xyz"):
        media = "chemical/x-xyz"
    elif low.endswith(".zip"):
        media = "application/zip"
    else:
        media = "application/octet-stream"

    from fastapi.responses import Response
    return Response(content=data, media_type=media,
                    headers={"Content-Disposition": 'attachment; filename="%s"' % safe_name})
