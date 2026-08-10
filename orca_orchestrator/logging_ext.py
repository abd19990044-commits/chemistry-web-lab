# -*- coding: utf-8 -*-
"""
Structured logging.

Requirements this satisfies, from the brief:
  * every action generates a structured log record;
  * every failure states what failed, why, what recovery was attempted, and
    what the next retry will be.

The last point is the one that ordinary logging never gets right, so it is not
left to the caller's discretion -- `log_failure()` takes those four things as
*required* arguments. You cannot log a failure here without saying what
happens next.

Two other properties matter in this specific system:

  1. **Redaction.** Kaggle credentials pass through this process and are
     embedded in the kernel source. A traceback or a debug dump that prints a
     job manifest must not leak them. `RedactingFilter` scrubs known secret
     shapes from every record, including ones produced by third-party
     libraries.
  2. **Correlation.** One user action fans out into CLI calls, retries and
     state transitions across threads. Every record carries job_id, epoch and
     a correlation id pulled from a contextvar, so a single job's whole
     lifetime can be reconstructed with one grep.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")
_log_context: contextvars.ContextVar[dict] = contextvars.ContextVar("log_context", default={})

# Legacy Kaggle key: 32 lowercase hex. New token: KGAT_ prefixed.
_SECRET_PATTERNS = (
    re.compile(r"\bKGAT_[A-Za-z0-9_\-]{8,}", re.IGNORECASE),
    re.compile(r"\b[0-9a-f]{32}\b"),
    re.compile(r'(?i)("?(?:kaggle_)?(?:key|api_token|token|secret|password)"?\s*[:=]\s*")([^"]{4,})(")'),
    re.compile(r"(?i)((?:kaggle_)?(?:key|api_token|token|secret)\s*=\s*')([^']{4,})(')"),
)


def redact(text: str) -> str:
    """Best-effort scrub of credential-shaped substrings.

    Deliberately aggressive: a false positive costs an unreadable hex string in
    a log line, a false negative costs a leaked API token in a public Space's
    log stream. The asymmetry is not close."""
    if not text:
        return text
    out = text
    out = _SECRET_PATTERNS[0].sub("KGAT_<redacted>", out)
    out = _SECRET_PATTERNS[1].sub("<redacted:32hex>", out)
    for pattern in _SECRET_PATTERNS[2:]:
        out = pattern.sub(lambda m: m.group(1) + "<redacted>" + m.group(3), out)
    return out


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: redact(v) if isinstance(v, str) else v
                                   for k, v in record.args.items()}
                else:
                    record.args = tuple(redact(a) if isinstance(a, str) else a
                                        for a in record.args)
        except Exception:  # pragma: no cover - logging must never raise
            pass
        return True


def redact_structure(value, _depth: int = 0):
    """Recursively scrubs credential-shaped strings out of a nested structure.

    `RedactingFilter` alone is not sufficient, and this is not a theoretical
    gap: it was found by running the verification suite. The filter only sees
    `record.msg` and `record.args`, but structured logging puts most of the
    interesting data in `extra=`, which lands directly in `record.__dict__` and
    goes straight to the formatter untouched. A call like

        log_failure(log, ..., kaggle_key=creds.key)

    would therefore have written the API token to stdout in clear text, on a
    Space whose logs may be visible to anyone with access to the deployment.

    Redaction now happens at the formatter -- the single point every record
    must pass through -- so no future call site can bypass it by choosing the
    wrong argument.
    """
    if _depth > 6:
        return value
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_structure(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_structure(v, _depth + 1) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Hugging Face Spaces surfaces stdout verbatim,
    so this is directly greppable and directly ingestible."""

    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + ".%03dZ" % (record.msecs),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlation_id": _correlation_id.get(),
        }
        ctx = _log_context.get()
        if ctx:
            payload.update(ctx)
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        try:
            # Applied to the WHOLE payload, including everything that arrived
            # via `extra=`. See redact_structure for why this must be here and
            # not only in the filter.
            return json.dumps(redact_structure(payload), default=str, ensure_ascii=False)
        except Exception:  # pragma: no cover
            return json.dumps({"ts": payload["ts"], "level": payload["level"],
                               "msg": str(record.getMessage())})


_configured = False


def configure(level: str | int | None = None, stream=None) -> None:
    """Idempotent. Safe to call from every gunicorn worker and from tests."""
    global _configured
    if _configured:
        return
    level = level or os.environ.get("ORCA_LOG_LEVEL", "INFO")
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(
        JsonFormatter() if os.environ.get("ORCA_LOG_FORMAT", "json") == "json"
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    configure()
    return logging.getLogger(name)


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Binds fields onto every record emitted inside the block, across await
    points and thread-local work started within it."""
    token_ctx = _log_context.set({**_log_context.get(), **fields})
    token_corr = None
    if "correlation_id" in fields:
        token_corr = _correlation_id.set(str(fields["correlation_id"]))
    try:
        yield
    finally:
        _log_context.reset(token_ctx)
        if token_corr is not None:
            _correlation_id.reset(token_corr)


def log_failure(
    logger: logging.Logger,
    *,
    what: str,
    why: str,
    recovery: str,
    next_action: str,
    exc: BaseException | None = None,
    **fields: Any,
) -> None:
    """The only sanctioned way to log a failure.

    Every argument is mandatory and maps onto one of the four questions the
    brief requires an operator to be able to answer from the log alone:

      what        -- which operation failed, named precisely
      why         -- the classified cause, not just the exception text
      recovery    -- what was already attempted automatically
      next_action -- what happens next, including the concrete delay, or an
                     explicit statement that nothing further will be attempted
    """
    extra = {
        "event": "failure",
        "failure_what": what,
        "failure_why": why,
        "recovery_attempted": recovery,
        "next_action": next_action,
        **fields,
    }
    if exc is not None:
        extra["error_class"] = type(exc).__name__
        to_dict = getattr(exc, "to_dict", None)
        if callable(to_dict):
            extra["error"] = to_dict()
        else:
            extra["error"] = {"message": redact(str(exc))}
    # Pass the exception instance, not True: `exc_info=True` looks up the
    # *currently handled* exception, and log_failure is frequently called
    # outside an except block, which produced a useless "NoneType: None"
    # traceback in place of the real one.
    logger.error("%s failed: %s", what, why, extra=extra, exc_info=exc)


def log_event(logger: logging.Logger, event: str, message: str = "", **fields: Any) -> None:
    """Structured success/progress record. `event` is a stable machine-readable
    verb (`checkpoint_verified`, `successor_pushed`, ...) that dashboards and
    alerts key off, independent of the human-readable message."""
    logger.info(message or event, extra={"event": event, **fields})
