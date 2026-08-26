# -*- coding: utf-8 -*-
"""
ORCA Package Archive Validator and Storage Manager.

Provides security-hardened validation for uploaded .tar.xz ORCA distribution packages,
enforcing archive format compliance, path traversal (tar-slip) prevention,
decompression bomb mitigation, per-user ownership scoping, and storage quotas.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import tarfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO

log = logging.getLogger("orca.archive_validator")

# Maximum reasonable size for an ORCA package upload (500 MB)
MAX_ARCHIVE_SIZE_BYTES = 500 * 1024 * 1024
# Maximum allowed uncompressed size for extracted content (2 GB)
MAX_EXTRACTED_SIZE_BYTES = 2 * 1024 * 1024 * 1024
# Maximum member count in archive (decompression bomb protection)
MAX_FILE_COUNT = 25000
# Maximum allowed compression ratio (uncompressed / compressed)
MAX_COMPRESSION_RATIO = 100.0
# Per-user quota limits
MAX_USER_ARCHIVES = 10
MAX_USER_STORAGE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB total per user


class ArchiveValidationError(ValueError):
    """Raised when an uploaded archive fails security or format validation."""
    pass


class ArchiveQuotaExceededError(ArchiveValidationError):
    """Raised when an archive upload exceeds storage or quota limits."""
    pass


class ArchiveAuthorizationError(PermissionError):
    """Raised when an unauthorized user attempts to access or delete an archive."""
    pass


@dataclass
class OrcaArchiveMetadata:
    archive_id: str
    filename: str
    size_bytes: int
    sha256: str
    has_orca_binary: bool
    created_at: float
    owner_id: str | None = None
    extracted_size_bytes: int = 0
    file_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _is_safe_tar_member(member: tarfile.TarInfo, base_target_dir: str | Path | None = None) -> tuple[bool, str]:
    """
    Validates that a tar member does not perform path traversal (tar-slip),
    reference special device nodes, or target sensitive system locations.
    """
    name = member.name
    
    # 0. Reject null bytes or control characters
    if "\0" in name or any(ord(c) < 32 for c in name if c not in ("\t", "\n", "\r")):
        return False, f"Member '{name}' contains null bytes or illegal control characters"

    # 1. Reject absolute paths, Windows drive letters, or UNC paths
    if name.startswith(("/", "\\")) or re.match(r"^[a-zA-Z]:", name) or name.startswith("\\\\"):
        return False, f"Member '{name}' has an absolute, drive-rooted, or UNC path"

    # 2. Reject path traversal segments ('..' components)
    norm = os.path.normpath(name).replace("\\", "/")
    if norm == ".." or norm.startswith("../") or "/../" in norm or norm.endswith("/.."):
        return False, f"Member '{name}' contains path traversal ('..')"

    # 3. Reject special device files (FIFOs, character/block devices)
    if member.isdev() or member.ischr() or member.isblk() or member.isfifo():
        return False, f"Member '{name}' is a special device file (character, block, or FIFO)"

    # 4. Check link targets for symlinks and hardlinks
    if member.issym() or member.islnk():
        link_target = member.linkname
        if "\0" in link_target:
            return False, f"Link '{name}' contains null bytes in target"
        if link_target.startswith(("/", "\\")) or re.match(r"^[a-zA-Z]:", link_target) or link_target.startswith("\\\\"):
            return False, f"Link '{name}' -> '{link_target}' points to an absolute, drive-rooted, or UNC path"
        norm_link = os.path.normpath(link_target).replace("\\", "/")
        if norm_link == ".." or norm_link.startswith("../") or "/../" in norm_link or norm_link.endswith("/.."):
            return False, f"Link '{name}' -> '{link_target}' contains path traversal ('..')"

    # 5. If base_target_dir is provided, verify resolved path containment
    if base_target_dir:
        base_path = Path(base_target_dir).resolve()
        candidate = (base_path / name).resolve()
        try:
            candidate.relative_to(base_path)
        except ValueError:
            return False, f"Member '{name}' resolves outside target directory '{base_target_dir}'"

    return True, ""


def validate_orca_tar_xz(
    file_source: str | BinaryIO | bytes,
    filename: str = "orca.tar.xz",
) -> tuple[bool, str, list[str], int, int]:
    """
    Inspects a .tar.xz archive without extracting to disk.
    Returns (is_valid, error_message, list_of_members, total_extracted_size, file_count).
    """
    low_name = filename.lower()
    if not low_name.endswith(".tar.xz") and not low_name.endswith(".txz"):
        return False, f"Invalid archive extension '{filename}'. Only .tar.xz archives are accepted.", [], 0, 0

    try:
        compressed_size = 0
        if isinstance(file_source, (str, os.PathLike)):
            if not os.path.isfile(file_source):
                return False, f"File not found: {file_source}", [], 0, 0
            compressed_size = os.path.getsize(file_source)
            if compressed_size > MAX_ARCHIVE_SIZE_BYTES:
                return False, f"Archive exceeds maximum upload size limit of {MAX_ARCHIVE_SIZE_BYTES // (1024*1024)} MB", [], 0, 0
            tf = tarfile.open(file_source, mode="r:xz")
        elif isinstance(file_source, bytes):
            compressed_size = len(file_source)
            if compressed_size > MAX_ARCHIVE_SIZE_BYTES:
                return False, f"Archive exceeds maximum upload size limit of {MAX_ARCHIVE_SIZE_BYTES // (1024*1024)} MB", [], 0, 0
            tf = tarfile.open(fileobj=io.BytesIO(file_source), mode="r:xz")
        else:
            tf = tarfile.open(fileobj=file_source, mode="r:xz")

        with tf:
            members = tf.getmembers()
            if not members:
                return False, "Archive is empty", [], 0, 0

            if len(members) > MAX_FILE_COUNT:
                return False, f"Archive contains {len(members)} files, exceeding limit of {MAX_FILE_COUNT}", [], 0, 0

            member_names = []
            total_uncompressed = 0
            has_orca_exe = False

            for m in members:
                is_safe, err = _is_safe_tar_member(m)
                if not is_safe:
                    return False, f"Security validation failed: {err}", [], 0, 0

                total_uncompressed += m.size
                if total_uncompressed > MAX_EXTRACTED_SIZE_BYTES:
                    return False, f"Archive uncompressed size exceeds limit of {MAX_EXTRACTED_SIZE_BYTES // (1024*1024)} MB (possible decompression bomb)", [], 0, 0

                base = os.path.basename(m.name).lower()
                if base in ("orca", "orca.exe"):
                    has_orca_exe = True
                member_names.append(m.name)

            # Check compression ratio if compressed size is known
            if compressed_size > 0:
                ratio = total_uncompressed / max(compressed_size, 1)
                if ratio > MAX_COMPRESSION_RATIO:
                    return False, f"Archive compression ratio ({ratio:.1f}x) exceeds safety threshold of {MAX_COMPRESSION_RATIO}x", [], 0, 0

        return True, "", member_names, total_uncompressed, len(member_names)
    except tarfile.ReadError as exc:
        return False, f"Corrupted or invalid .tar.xz archive: {exc}", [], 0, 0
    except Exception as exc:
        return False, f"Failed to validate archive: {exc}", [], 0, 0

