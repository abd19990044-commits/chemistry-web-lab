# -*- coding: utf-8 -*-
"""
Content addressing and crash-safe file writes.

Two primitives the old code lacked entirely, and whose absence explains
several of the reported symptoms:

  * `sha256_file` / `digest_fileset` -- "incomplete restart files" cannot be
    detected without a hash taken by the producer and re-checked by the
    consumer.
  * `atomic_write_*` -- a manifest written with a plain `open(..., 'w')` and
    then interrupted leaves a half-written JSON file that the next reader
    parses as garbage. Every file that participates in a state decision is
    written to a temp file in the same directory, fsync'd, then `os.replace`d,
    which is atomic on POSIX. A reader therefore sees either the whole old
    file or the whole new one, never a torn one.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import Any, Iterable

_CHUNK = 1 << 20


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def digest_fileset(entries: Iterable[tuple[str, str, int]]) -> str:
    """A single hash covering a whole set of files, order-independent.

    Each entry is (name, sha256, size). Sorting by name before hashing makes
    the digest a pure function of *content*, so two independently built
    checkpoints containing the same files produce the same id -- which is what
    makes "push the successor twice" provably a no-op rather than a hopeful
    claim."""
    h = hashlib.sha256()
    for name, digest, size in sorted(entries, key=lambda e: e[0]):
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(digest.encode("ascii"))
        h.update(b"\0")
        h.update(str(int(size)).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def stable_json(obj: Any) -> str:
    """Canonical JSON: sorted keys, no incidental whitespace. Required for any
    value that is hashed or compared for equality across processes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def content_id(obj: Any) -> str:
    return sha256_bytes(stable_json(obj).encode("utf-8"))


def _fsync_dir(path: str) -> None:
    """Renaming is atomic, but the rename itself is only durable once the
    *directory* entry is flushed. Without this a power loss can leave the new
    file invisible despite os.replace having returned."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: str, data: bytes, *, fsync: bool = True) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            if fsync:
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp, path)
        if fsync:
            _fsync_dir(directory)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: str, text: str, *, fsync: bool = True) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), fsync=fsync)


def atomic_write_json(path: str, obj: Any, *, fsync: bool = True) -> str:
    """Writes `obj` atomically and returns its sha256, so the caller can record
    the digest alongside without re-reading the file."""
    payload = stable_json(obj).encode("utf-8")
    atomic_write_bytes(path, payload, fsync=fsync)
    return sha256_bytes(payload)


def read_json_verified(path: str, expected_sha256: str | None = None) -> Any:
    """Reads JSON and, when a digest is supplied, refuses to return content
    that does not match it."""
    from .errors import ChecksumMismatchError

    with open(path, "rb") as fh:
        raw = fh.read()
    if expected_sha256:
        actual = sha256_bytes(raw)
        if actual != expected_sha256:
            raise ChecksumMismatchError(
                "manifest digest does not match the recorded value",
                path=path, expected=expected_sha256, actual=actual,
            )
    return json.loads(raw.decode("utf-8"))
