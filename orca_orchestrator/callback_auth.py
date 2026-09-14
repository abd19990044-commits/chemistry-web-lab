# -*- coding: utf-8 -*-
"""Stateless authentication for Kaggle -> site callbacks.

The token is bound to a single ORCA job with HMAC-SHA256. The server can verify
it after a Space restart without SQLite, Cloudflare, or any other persistent
control plane. Kaggle only receives the opaque per-job token, never the server
secret.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from .errors import ValidationError

_ENV = "ORCA_CALLBACK_SECRET"
_FALLBACK_ENV = "SECRET_KEY"
_CONTEXT = b"orca-kaggle-callback-v1\x00"


def _secret() -> bytes:
    raw = (os.environ.get(_ENV) or os.environ.get(_FALLBACK_ENV) or "").strip()
    # Fail closed in production. Development/local tests may explicitly set a
    # deterministic secret in the test environment.
    if len(raw.encode('utf-8')) < 32:
        raise ValidationError(
            "ORCA callback authentication is not configured. Set ORCA_CALLBACK_SECRET "
            "to a random value of at least 32 bytes in the deployment secrets."
        )
    return raw.encode('utf-8')


def is_configured() -> bool:
    try:
        _secret()
        return True
    except ValidationError:
        return False


def issue(job_id: str) -> str:
    mac = hmac.new(_secret(), _CONTEXT + job_id.encode('utf-8'), hashlib.sha256).digest()
    sig = base64.urlsafe_b64encode(mac).decode('ascii').rstrip('=')
    return f"v1.{job_id}.{sig}"


def verify(job_id: str, token: str) -> bool:
    try:
        version, token_job, sig = str(token or '').split('.', 2)
    except ValueError:
        return False
    if version != 'v1' or token_job != job_id:
        return False
    try:
        expected = issue(job_id)
    except ValidationError:
        return False
    return hmac.compare_digest(expected, str(token))


def fingerprint(token: str) -> str:
    return hashlib.sha256(str(token or '').encode('utf-8')).hexdigest()
