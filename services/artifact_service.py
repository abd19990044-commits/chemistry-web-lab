# -*- coding: utf-8 -*-
"""Artifact listing/streaming service (FastAPI v1 artifact API)."""
import hashlib
import os
import zipfile

from orca_orchestrator.result_store import _validate_zip_budget


def validate_archive(archive: zipfile.ZipFile) -> None:
    """Apply the shared traversal, symlink, file-count and size limits."""
    _validate_zip_budget(archive)


def _member_basename(info) -> str:
    return str(info.filename or "").replace("\\", "/").rsplit("/", 1)[-1]


def select_orca_output_info(archive: zipfile.ZipFile, input_filename: str = ""):
    """Select the scientific ORCA output, never an incidental runner log.

    Kaggle archives can contain notebook/runner logs before the actual ORCA
    output.  Archive order is not scientific provenance, so selection is
    deterministic and tied to the input basename whenever possible.
    """
    candidates = [
        info for info in archive.infolist()
        if not info.is_dir()
        and _member_basename(info).lower().endswith((".out", ".log", ".property.txt"))
    ]
    if not candidates:
        return None
    input_stem = os.path.basename(str(input_filename or "")).rsplit(".", 1)[0].lower()

    def output_rank(info):
        base = _member_basename(info).lower()
        stem = base.rsplit(".", 1)[0]
        incidental = any(tag in base for tag in ("runner", "watchdog", "install", "script"))
        return (
            1 if input_stem and stem == input_stem else 0,
            0 if incidental else 1,
            2 if base.endswith(".out") else (1 if base.endswith(".log") else 0),
            int(info.file_size or 0),
            base,
        )

    return max(candidates, key=output_rank)


def select_matching_input_info(archive: zipfile.ZipFile, output_info=None):
    candidates = [
        info for info in archive.infolist()
        if not info.is_dir() and _member_basename(info).lower().endswith(".inp")
    ]
    if not candidates:
        return None
    output_stem = ""
    if output_info is not None:
        output_stem = _member_basename(output_info).rsplit(".", 1)[0].lower()
    return max(
        candidates,
        key=lambda info: (
            1 if output_stem and _member_basename(info).rsplit(".", 1)[0].lower() == output_stem else 0,
            int(info.file_size or 0),
            _member_basename(info).lower(),
        ),
    )


def select_orca_xyz_info(archive: zipfile.ZipFile, input_filename: str = ""):
    """Choose a final geometry candidate while excluding trajectories/inputs."""
    candidates = []
    for info in archive.infolist():
        base = _member_basename(info).lower()
        if info.is_dir() or not base.endswith(".xyz"):
            continue
        if any(tag in base for tag in ("_trj.xyz", "trajectory", "original", "initial", "_input", "_start")):
            continue
        candidates.append(info)
    if not candidates:
        return None
    input_stem = os.path.basename(str(input_filename or "")).rsplit(".", 1)[0].lower()
    return max(
        candidates,
        key=lambda info: (
            1 if input_stem and _member_basename(info).rsplit(".", 1)[0].lower() == input_stem else 0,
            1 if any(tag in _member_basename(info).lower() for tag in ("final", "opt")) else 0,
            int(info.file_size or 0),
            _member_basename(info).lower(),
        ),
    )


def read_text_member(archive: zipfile.ZipFile, info, *, max_bytes: int) -> str:
    """Read one validated text member with a hard decoded-memory budget."""
    if info is None:
        return ""
    if int(info.file_size or 0) > max_bytes:
        raise ValueError(
            "archive member %r exceeds the parse budget (%d bytes)"
            % (info.filename, max_bytes)
        )
    with archive.open(info, "r") as member:
        payload = member.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError("archive member exceeds the parse budget")
    return payload.decode("utf-8", errors="replace")


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
            _validate_zip_budget(zf)
            for info in zf.infolist():
                if info.is_dir():
                    continue
                digest = hashlib.sha256()
                with zf.open(info, "r") as member:
                    while True:
                        chunk = member.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                artifacts.append({
                    "filename": os.path.basename(info.filename),
                    "artifact_type": _classify(info.filename),
                    "size": info.file_size,
                    "sha256": digest.hexdigest(),
                })
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return {"ok": False, "error": {"code": "ARCHIVE_UNREADABLE",
                                       "message": str(exc)}}, 502
    return {"ok": True, "artifacts": artifacts, "count": len(artifacts)}, 200


def read_artifact(zip_path: str, wanted: str):
    """Returns (bytes, status_code, filename) for one artifact by basename."""
    base = os.path.basename(wanted)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            _validate_zip_budget(zf)
            for info in zf.infolist():
                if os.path.basename(info.filename) == base and not info.is_dir():
                    return zf.read(info), 200, os.path.basename(info.filename)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return None, 502, str(exc)
    return None, 404, base


def resolve_artifact(zip_path: str, wanted: str):
    """Resolve one safe, unambiguous ZIP member without reading its payload."""
    base = os.path.basename(wanted)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            _validate_zip_budget(zf)
            matches = [
                info for info in zf.infolist()
                if not info.is_dir() and os.path.basename(info.filename) == base
            ]
            if not matches:
                return None, 404, "artifact not found in this job's archive"
            if len(matches) > 1:
                return None, 409, "artifact name is ambiguous in this archive"
            info = matches[0]
            return {
                "member_name": info.filename,
                "filename": base,
                "size": int(info.file_size or 0),
            }, 200, ""
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return None, 502, "archive unreadable: %s" % exc
