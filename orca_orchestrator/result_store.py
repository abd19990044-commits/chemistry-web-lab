# -*- coding: utf-8 -*-
"""
Scientific Result Durability Layer.

Guarantees that a completed ORCA scientific calculation is not considered
durably preserved until its artifacts are verified, written to independent
storage, checksummed, and indexed by manifest.

Cloudflare holds lightweight metadata and references, while ResultArtifactStore
manages the physical verified artifacts.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from .config import CONFIG
from .hashing import sha256_bytes

log = logging.getLogger("orca.result_store")

MAX_ARCHIVE_FILES = int(os.environ.get("ORCA_MAX_ARCHIVE_FILES", "10000"))
MAX_ARCHIVE_EXTRACTED_BYTES = int(os.environ.get("ORCA_MAX_ARCHIVE_EXTRACTED_BYTES", str(4 * 1024 ** 3)))


def _safe_zip_member(name: str) -> bool:
    raw = str(name or "").replace("\\", "/")
    clean = raw.rstrip("/")
    if not clean or "\x00" in clean or clean.startswith("/"):
        return False
    if len(clean) >= 2 and clean[1] == ":":
        return False
    return all(part not in ("", ".", "..") for part in clean.split("/"))


def _validate_zip_budget(zf: zipfile.ZipFile) -> None:
    infos = zf.infolist()
    if len(infos) > MAX_ARCHIVE_FILES:
        raise ValueError(f"archive contains too many files ({len(infos)} > {MAX_ARCHIVE_FILES})")
    total = 0
    for info in infos:
        if not _safe_zip_member(info.filename):
            raise ValueError(f"unsafe archive member: {info.filename!r}")
        # Unix symlink entries are represented in external_attr even though
        # ZipInfo.is_dir() is false.  Never extract them into a job workspace.
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise ValueError(f"symlink archive member is not allowed: {info.filename!r}")
        total += int(info.file_size or 0)
        if total > MAX_ARCHIVE_EXTRACTED_BYTES:
            raise ValueError("archive extracted-size budget exceeded")


class ResultDurabilityState(str, Enum):
    """Lifecycle states for scientific result durability."""

    REMOTE_ONLY = "REMOTE_ONLY"
    DOWNLOAD_PENDING = "DOWNLOAD_PENDING"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    VALIDATING = "VALIDATING"
    PARSED = "PARSED"
    ARCHIVING = "ARCHIVING"
    ARCHIVED_LOCAL = "ARCHIVED_LOCAL"
    ARCHIVED_PERSISTENT = "ARCHIVED_PERSISTENT"
    ARCHIVED = "ARCHIVED"  # General alias
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    ARCHIVE_FAILED = "ARCHIVE_FAILED"
    RESULT_UNAVAILABLE = "RESULT_UNAVAILABLE"
    REMOTE_DELETED = "REMOTE_DELETED"

    @property
    def is_durable(self) -> bool:
        """True ONLY if stored in persistent volume guaranteed to survive environment recreation."""
        return self == ResultDurabilityState.ARCHIVED_PERSISTENT


def _clean_owner(owner: str) -> str:
    return (owner or "").strip().lower()


def _file_sha256(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class ArtifactRecord:
    """Metadata record for a single durable file artifact."""

    name: str
    size_bytes: int
    sha256: str
    artifact_type: str  # e.g. "archive_zip", "orca_output", "geometry_xyz", "hessian", "property_txt"
    storage_ref: str
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArtifactRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


@dataclass
class ResultManifest:
    """Durable manifest documenting the scientific result package."""

    schema_version: int = 1
    manifest_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    job_id: str = ""
    kaggle_job_ref: str = ""
    owner: str = ""
    status: str = ResultDurabilityState.ARCHIVED.value
    archived_at: float = field(default_factory=time.time)
    total_size_bytes: int = 0
    bundle_sha256: str = ""
    artifacts: list[ArtifactRecord] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.owner = _clean_owner(self.owner)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["artifacts"] = [a.to_dict() for a in self.artifacts]
        # Never leak secrets in manifests
        d.pop("kaggle_key", None)
        d.pop("api_token", None)
        d.pop("password", None)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResultManifest":
        clean = dict(data or {})
        clean.pop("kaggle_key", None)
        clean.pop("api_token", None)
        clean.pop("password", None)
        raw_artifacts = clean.pop("artifacts", []) or []
        artifacts = [ArtifactRecord.from_dict(a) for a in raw_artifacts]
        known = {f for f in cls.__dataclass_fields__}
        init_kwargs = {k: v for k, v in clean.items() if k in known}
        init_kwargs["artifacts"] = artifacts
        if "manifest_id" not in init_kwargs or not init_kwargs["manifest_id"]:
            init_kwargs["manifest_id"] = str(uuid.uuid4())
        return cls(**init_kwargs)


class ResultArtifactStore:
    """Durable local/volume storage manager for verified ORCA scientific results."""

    def __init__(self, base_dir: str | None = None) -> None:
        if base_dir:
            self.base_dir = base_dir
        else:
            state_dir = getattr(CONFIG.store, "state_dir", os.path.join(os.getcwd(), ".state"))
            self.base_dir = os.environ.get("ORCA_RESULTS_DIR", os.path.join(state_dir, "results"))
        os.makedirs(self.base_dir, exist_ok=True)

        # Check if the backing storage is guaranteed persistent across environment recreations
        norm_base = os.path.abspath(self.base_dir).replace("\\", "/")
        is_data_vol = norm_base.startswith("/data") or norm_base.startswith("/persistent")
        is_env_persistent = os.environ.get("ORCA_RESULTS_PERSISTENT", "").lower() in ("1", "true", "yes", "on")
        self.is_persistent: bool = is_data_vol or is_env_persistent

    @property
    def storage_durability(self) -> str:
        return "persistent_volume" if self.is_persistent else "ephemeral_local"

    def _job_dir(self, owner: str, job_id: str) -> str:
        owner_clean = _clean_owner(owner) or "anonymous"
        # Sanitize job_id to prevent path traversal
        safe_job_id = os.path.basename(job_id.strip())
        return os.path.join(self.base_dir, owner_clean, safe_job_id)

    def store(
        self,
        job_id: str,
        owner: str,
        raw_zip_or_dir_path: str,
        provenance: dict[str, Any] | None = None,
    ) -> ResultManifest:
        """Stores result artifacts durably, computes cryptographic hashes, and creates manifest."""
        if not raw_zip_or_dir_path or not os.path.exists(raw_zip_or_dir_path):
            raise FileNotFoundError(f"Source results artifact not found: {raw_zip_or_dir_path}")

        target_dir = self._job_dir(owner, job_id)
        staging_dir = tempfile.mkdtemp(prefix="orca-archive-stage-", dir=self.base_dir)

        try:
            artifacts: list[ArtifactRecord] = []
            target_zip = os.path.join(staging_dir, "results.zip")

            if os.path.isfile(raw_zip_or_dir_path) and zipfile.is_zipfile(raw_zip_or_dir_path):
                with zipfile.ZipFile(raw_zip_or_dir_path, "r") as source_zip:
                    _validate_zip_budget(source_zip)
                shutil.copy2(raw_zip_or_dir_path, target_zip)
            elif os.path.isdir(raw_zip_or_dir_path):
                # Bundle directory into results.zip
                existing_zip = os.path.join(raw_zip_or_dir_path, "results.zip")
                if os.path.isfile(existing_zip) and zipfile.is_zipfile(existing_zip):
                    with zipfile.ZipFile(existing_zip, "r") as source_zip:
                        _validate_zip_budget(source_zip)
                    shutil.copy2(existing_zip, target_zip)
                else:
                    file_count = 0
                    raw_total = 0
                    with zipfile.ZipFile(target_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                        for root, _, files in os.walk(raw_zip_or_dir_path):
                            for f in files:
                                p = os.path.join(root, f)
                                rel_p = os.path.relpath(p, raw_zip_or_dir_path)
                                if not _safe_zip_member(rel_p):
                                    raise ValueError(f"unsafe result path: {rel_p!r}")
                                file_count += 1
                                raw_total += os.path.getsize(p)
                                if file_count > MAX_ARCHIVE_FILES or raw_total > MAX_ARCHIVE_EXTRACTED_BYTES:
                                    raise ValueError("result directory exceeds archive safety budget")
                                zf.write(p, rel_p)

            if not os.path.isfile(target_zip) or not zipfile.is_zipfile(target_zip):
                raise ValueError("result archive is missing or invalid")
            with zipfile.ZipFile(target_zip, "r") as verified_zip:
                _validate_zip_budget(verified_zip)

            # Record results.zip artifact
            zip_size = os.path.getsize(target_zip)
            zip_sha = _file_sha256(target_zip)
            artifacts.append(
                ArtifactRecord(
                    name="results.zip",
                    size_bytes=zip_size,
                    sha256=zip_sha,
                    artifact_type="archive_zip",
                    storage_ref="results.zip",
                )
            )

            # Extract key essential artifacts (e.g. .out, .xyz, .hess, .property.txt) for direct access
            total_size = zip_size
            try:
                with zipfile.ZipFile(target_zip, "r") as zf:
                    _validate_zip_budget(zf)
                    for item in zf.infolist():
                        if item.is_dir():
                            continue
                        name_lower = item.filename.lower()
                        art_type = None
                        if name_lower.endswith(".out") or name_lower.endswith(".log"):
                            art_type = "orca_output"
                        elif name_lower.endswith(".xyz"):
                            art_type = "geometry_xyz"
                        elif name_lower.endswith(".hess"):
                            art_type = "hessian"
                        elif name_lower.endswith(".property.txt"):
                            art_type = "property_txt"
                        elif name_lower.endswith(".molden") or name_lower.endswith(".molden.input") or ".molden" in name_lower:
                            art_type = "molden"

                        if art_type:
                            extracted_path = os.path.join(staging_dir, os.path.basename(item.filename))
                            with open(extracted_path, "wb") as out_f:
                                out_f.write(zf.read(item))
                            f_size = os.path.getsize(extracted_path)
                            f_sha = _file_sha256(extracted_path)
                            total_size += f_size
                            artifacts.append(
                                ArtifactRecord(
                                    name=os.path.basename(item.filename),
                                    size_bytes=f_size,
                                    sha256=f_sha,
                                    artifact_type=art_type,
                                    storage_ref=os.path.basename(item.filename),
                                )
                            )
            except ValueError:
                raise
            except Exception as exc:
                log.warning("Could not extract sub-artifacts from results.zip for job %s: %s", job_id, exc)

            archive_status = (
                ResultDurabilityState.ARCHIVED_PERSISTENT.value
                if self.is_persistent
                else ResultDurabilityState.ARCHIVED_LOCAL.value
            )
            manifest = ResultManifest(
                job_id=job_id,
                kaggle_job_ref=job_id,
                owner=_clean_owner(owner),
                status=archive_status,
                archived_at=time.time(),
                total_size_bytes=total_size,
                bundle_sha256=zip_sha,
                artifacts=artifacts,
                provenance=provenance or {},
            )

            manifest_path = os.path.join(staging_dir, "manifest.json")
            with open(manifest_path, "w", encoding="utf-8") as mf:
                json.dump(manifest.to_dict(), mf, indent=2)

            # Atomic promotion
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir, ignore_errors=True)
            os.makedirs(os.path.dirname(target_dir), exist_ok=True)
            shutil.move(staging_dir, target_dir)

            log.info(
                "Archived scientific results durably",
                extra={
                    "event": "results_archived",
                    "job_id": job_id,
                    "owner": owner,
                    "bundle_sha256": zip_sha,
                    "size_bytes": total_size,
                },
            )
            return manifest
        finally:
            if os.path.exists(staging_dir):
                shutil.rmtree(staging_dir, ignore_errors=True)

    def retrieve(self, job_id: str, owner: str) -> tuple[str | None, ResultManifest | None]:
        """Retrieves verified archive zip and manifest if present and intact."""
        target_dir = self._job_dir(owner, job_id)
        manifest_path = os.path.join(target_dir, "manifest.json")
        zip_path = os.path.join(target_dir, "results.zip")

        if not os.path.exists(manifest_path) or not os.path.exists(zip_path):
            return None, None

        try:
            with open(manifest_path, "r", encoding="utf-8") as mf:
                manifest_data = json.load(mf)
            manifest = ResultManifest.from_dict(manifest_data)

            # Verify checksum on results.zip
            current_sha = _file_sha256(zip_path)
            if current_sha != manifest.bundle_sha256:
                log.error("Checksum mismatch for archived results.zip for job %s", job_id)
                return None, None

            return zip_path, manifest
        except Exception as exc:
            log.warning("Failed to retrieve archived result for job %s: %s", job_id, exc)
            return None, None

    def exists(self, job_id: str, owner: str) -> bool:
        """Returns True if the result is durably archived and verified."""
        path, manifest = self.retrieve(job_id, owner)
        return path is not None and manifest is not None

    def verify_checksum(self, job_id: str, owner: str) -> bool:
        """Performs complete re-verification of all artifact checksums."""
        target_dir = self._job_dir(owner, job_id)
        manifest_path = os.path.join(target_dir, "manifest.json")
        if not os.path.exists(manifest_path):
            return False
        try:
            with open(manifest_path, "r", encoding="utf-8") as mf:
                manifest_data = json.load(mf)
            manifest = ResultManifest.from_dict(manifest_data)
            for art in manifest.artifacts:
                art_path = os.path.join(target_dir, art.storage_ref)
                if not os.path.exists(art_path):
                    return False
                if _file_sha256(art_path) != art.sha256:
                    return False
            return True
        except Exception:
            return False

    def get_manifest(self, job_id: str, owner: str) -> ResultManifest | None:
        """Returns the ResultManifest without reading binary payloads."""
        target_dir = self._job_dir(owner, job_id)
        manifest_path = os.path.join(target_dir, "manifest.json")
        if not os.path.exists(manifest_path):
            return None
        try:
            with open(manifest_path, "r", encoding="utf-8") as mf:
                manifest_data = json.load(mf)
            return ResultManifest.from_dict(manifest_data)
        except Exception:
            return None

    def delete(self, job_id: str, owner: str) -> bool:
        """Removes the archived directory."""
        target_dir = self._job_dir(owner, job_id)
        if os.path.exists(target_dir):
            shutil.rmtree(target_dir, ignore_errors=True)
            return True
        return False
