# -*- coding: utf-8 -*-
"""
Credential handling.

Policy: **Kaggle credentials are never written to persistent server storage.**
They arrive with a request, live in RAM under a short TTL, and are used to
construct a throwaway `$HOME` for a single CLI invocation. Nothing survives a
process restart, so a compromise of the Space's disk yields no user secrets.

The cost of that policy is stated plainly rather than hidden: the server can
only act on a job while *some* recent request has supplied credentials for its
owner. Between such requests the chain is carried forward by the Kaggle kernel
itself, which self-continues. `watchdog.py` is therefore best-effort by
construction, and the boundary is documented in ARCHITECTURE.md instead of
being papered over.

Known residual exposure, stated honestly
----------------------------------------
The in-kernel runner must authenticate to Kaggle in order to push its own
successor, so the credential is embedded in the pushed kernel source. It
therefore lives inside a private Kaggle notebook belonging to the user, for as
long as that notebook exists. This design does not create that exposure -- any
self-continuing kernel has it -- but it does mitigate it: the token is written
only into `is_private` kernels, is scrubbed from every log record by
`logging_ext.RedactingFilter`, is removed from the kernel's filesystem
immediately after the successor push, and the runner installs an excepthook so
a traceback can never print it. Users are advised in the UI to use a dedicated
token they can revoke.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from .config import CONFIG
from .errors import ValidationError
from .logging_ext import get_logger

log = get_logger("orca.credentials")

_LEGACY_KEY_RE = re.compile(r"^[0-9a-f]{32}$")


def _clean(value: str) -> str:
    """Strips whitespace and accidental surrounding quotes -- the single most
    common cause of a silent 401 when a value is copy-pasted out of a JSON
    file."""
    return (value or "").strip().strip('"').strip("'").strip()


@dataclass(frozen=True)
class KaggleCredentials:
    username: str
    key: str | None = None          # legacy 32-hex API key
    api_token: str | None = None    # new single API token (KGAT_...)

    @property
    def is_valid(self) -> bool:
        return bool(self.username) and bool(self.key or self.api_token)

    @property
    def fingerprint(self) -> str:
        """A stable, non-reversible handle for logging and cache keys.

        Logging the username alone would be enough to key a cache but would not
        distinguish a rotated token, so a rotation would silently reuse a stale
        cache entry. Hashing the secret alongside the username fixes that
        without ever recording the secret."""
        import hashlib
        material = f"{self.username}:{self.key or self.api_token or ''}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()[:16]

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"KaggleCredentials(username={self.username!r}, fingerprint={self.fingerprint})"

    __str__ = __repr__


def parse(username: str, key_or_token: str) -> KaggleCredentials:
    """Accepts either credential shape, and tolerates the most common paste
    mistake: dropping the whole downloaded `kaggle.json` into one field.

    The username is lower-cased because Kaggle usernames always are. Without
    that, the same account typed with different capitalisation on a phone and a
    laptop produces two different owner keys, and the job list simply appears
    empty on one of them -- indistinguishable from data loss."""
    username = _clean(username)
    secret = _clean(key_or_token)

    for candidate in (secret, username):
        if candidate.startswith("{"):
            try:
                obj = json.loads(candidate)
                if "username" in obj and "key" in obj:
                    username = _clean(obj["username"])
                    secret = _clean(obj["key"])
                    break
            except (ValueError, TypeError):
                pass

    username = username.lower()
    if not username or not secret:
        raise ValidationError("a Kaggle username and API key/token are both required")

    # Anything that is not the strict, unchanging legacy shape is treated as a
    # new-style token. Guessing at the new format's prefix would break the day
    # Kaggle changes it; this rule cannot.
    if _LEGACY_KEY_RE.match(secret):
        return KaggleCredentials(username=username, key=secret)
    return KaggleCredentials(username=username, api_token=secret)


@contextmanager
def kaggle_environment(creds: KaggleCredentials) -> Iterator[dict]:
    """Yields an env dict whose HOME and KAGGLE_CONFIG_DIR point at a
    throwaway directory containing one-off credentials.

    The directory is removed in `finally`, including on exception, so a crashed
    request never leaves a credential file behind in the container. Env vars
    are exported in addition to the files because the kaggle CLI reads env
    first, and that path is stable across CLI versions in a way that its
    config-dir resolution is not."""
    if not creds.is_valid:
        raise ValidationError("incomplete Kaggle credentials")

    tmp_home = tempfile.mkdtemp(prefix="kaggle-home-")
    try:
        cfg_dir = os.path.join(tmp_home, ".kaggle")
        os.makedirs(cfg_dir, exist_ok=True)

        env = os.environ.copy()
        env["HOME"] = tmp_home
        env["KAGGLE_CONFIG_DIR"] = cfg_dir
        env["KAGGLE_USERNAME"] = creds.username

        if creds.api_token:
            env["KAGGLE_API_TOKEN"] = creds.api_token
            env.pop("KAGGLE_KEY", None)
            token_path = os.path.join(cfg_dir, "access_token")
            with open(token_path, "w") as fh:
                fh.write(creds.api_token)
            os.chmod(token_path, 0o600)
        else:
            env["KAGGLE_KEY"] = creds.key
            env.pop("KAGGLE_API_TOKEN", None)
            json_path = os.path.join(cfg_dir, "kaggle.json")
            with open(json_path, "w") as fh:
                json.dump({"username": creds.username, "key": creds.key}, fh)
            os.chmod(json_path, 0o600)

        yield env
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)


class CredentialBroker:
    """In-RAM, TTL-bounded credential cache.

    Exists so that one browser poll can enable a full reconciliation sweep
    across every job that user owns, instead of only the single job the poll
    named. That is the difference between "the watchdog can help when you
    happen to look at the page" and "the watchdog can only ever fix the one job
    you clicked on".

    Never persisted. Cleared on process exit by virtue of being a dict.
    """

    def __init__(self, ttl_seconds: float | None = None) -> None:
        self._ttl = ttl_seconds if ttl_seconds is not None else CONFIG.store.credential_ttl_seconds
        self._entries: dict[str, tuple[KaggleCredentials, float]] = {}
        self._lock = threading.RLock()

    def remember(self, creds: KaggleCredentials) -> None:
        if not creds.is_valid:
            return
        with self._lock:
            self._entries[creds.username] = (creds, time.time() + self._ttl)

    def get(self, username: str) -> KaggleCredentials | None:
        key = (username or "").lower()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            creds, expires_at = entry
            if time.time() >= expires_at:
                self._entries.pop(key, None)
                return None
            return creds

    def forget(self, username: str) -> None:
        with self._lock:
            self._entries.pop((username or "").lower(), None)

    def known_owners(self) -> list[str]:
        now_ts = time.time()
        with self._lock:
            expired = [k for k, (_c, exp) in self._entries.items() if now_ts >= exp]
            for k in expired:
                self._entries.pop(k, None)
            return sorted(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


BROKER = CredentialBroker()
