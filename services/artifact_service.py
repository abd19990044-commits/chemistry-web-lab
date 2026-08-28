# -*- coding: utf-8 -*-
"""Artifact listing/streaming service (FastAPI v1 artifact API)."""
import hashlib
import os
import zipfile


def _classify(filename: str) -> str:
    low = filename.lower()
    if low.endswith(".molden.input") or low.endswith(".molden"):
        return "molden"
    if low.endswith(".gbw"):
        return "wavefunction"
    if low.endswith((".xyz", ".allxyz")):
        return "geometry"
    if low.endswith(".hess"):
        return "hessian"
    if low.endswith(".engrad"):
        return "engrad"
    if low.endswith(".mdrestart"):
        return "md_restart"
    if low.endswith((".out", ".log")):
        return "output"
    if low.endswith(".inp"):
        return "input"
    if low.endswith(".zip"):
        return "archive"
    return "other"


def list_artifacts(zip_path: str):
    """Returns (artifacts, status_code). Each entry: filename, artifact_type,
    size, sha256. Never exposes filesystem paths."""
    artifacts = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                data = zf.read(info)
                artifacts.append({
                    "filename": os.path.basename(info.filename),
                    "artifact_type": _classify(info.filename),
                    "size": info.file_size,
                    "sha256": hashlib.sha256(data).hexdigest(),
                })
    except (OSError, zipfile.BadZipFile) as exc:
        return {"ok": False, "error": {"code": "ARCHIVE_UNREADABLE",
                                       "message": str(exc)}}, 502
    return {"ok": True, "artifacts": artifacts, "count": len(artifacts)}, 200


def read_artifact(zip_path: str, wanted: str):
    """Returns (bytes, status_code, filename) for one artifact by basename."""
    base = os.path.basename(wanted)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if os.path.basename(info.filename) == base and not info.is_dir():
                    return zf.read(info), 200, os.path.basename(info.filename)
    except (OSError, zipfile.BadZipFile) as exc:
        return None, 502, str(exc)
    return None, 404, base
