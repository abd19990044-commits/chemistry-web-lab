# -*- coding: utf-8 -*-
"""
Retry policy with exponential backoff and full jitter.

Three properties the previous ad-hoc retry loops did not have:

  1. **The delay is computed before the failure is logged**, so the log line
     can state the concrete next delay ("retrying in 8.4s, attempt 3/6")
     rather than a vague "will retry". The brief requires this.
  2. **A wall-clock deadline bounds the whole call**, not just the attempt
     count. Inside a Kaggle kernel the difference is critical: five retries at
     90 s each will happily run past the point where there is no longer time
     to package results, which is how a chain loses a whole window's work.
  3. **Classification comes from the exception type**, not from substring
     matching, so a Kaggle error message this code has never seen is handled
     by its type rather than by luck.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from .config import CONFIG, RetryConfig
from .errors import OrchestratorError, RateLimitError, TransientError
from .logging_ext import get_logger, log_failure

T = TypeVar("T")
log = get_logger("orca.retry")


@dataclass
class Attempt:
    number: int
    max_attempts: int
    delay_before: float
    elapsed: float

    @property
    def is_last(self) -> bool:
        return self.number >= self.max_attempts


class RetryPolicy:
    def __init__(
        self,
        config: RetryConfig | None = None,
        *,
        max_attempts: int | None = None,
        base_delay: float | None = None,
        max_delay: float | None = None,
        deadline_seconds: float | None = None,
    ) -> None:
        cfg = config or CONFIG.retry
        self.max_attempts = max_attempts or cfg.max_attempts
        self.base_delay = base_delay if base_delay is not None else cfg.base_delay_seconds
        self.max_delay = max_delay if max_delay is not None else cfg.max_delay_seconds
        self.jitter = cfg.jitter
        self.deadline_seconds = deadline_seconds

    def delay_for(self, attempt: int, *, retry_after: float | None = None) -> float:
        """Full jitter: `uniform(0, min(cap, base * 2**(n-1)))`.

        Equal jitter and no jitter both leave clients synchronised after a
        shared outage; full jitter is what actually spreads a fleet out. A
        server-supplied Retry-After always wins, since it is authoritative."""
        if retry_after is not None and retry_after > 0:
            return min(float(retry_after), self.max_delay)
        ceiling = min(self.max_delay, self.base_delay * (2 ** max(0, attempt - 1)))
        return random.uniform(0, ceiling) if self.jitter else ceiling

    def call(
        self,
        operation: str,
        fn: Callable[[Attempt], T],
        *,
        on_retry: Callable[[Attempt, BaseException], None] | None = None,
        **log_fields,
    ) -> T:
        """Runs `fn` until it succeeds, the attempt budget is spent, the
        deadline passes, or a non-transient error is raised.

        `fn` receives the Attempt so it can shrink its own sub-timeouts as the
        deadline approaches -- the in-kernel push does exactly this."""
        started = time.monotonic()
        last_exc: BaseException | None = None

        for number in range(1, self.max_attempts + 1):
            elapsed = time.monotonic() - started
            attempt = Attempt(number, self.max_attempts, 0.0, elapsed)
            try:
                return fn(attempt)
            except TransientError as exc:
                last_exc = exc
                retry_after = getattr(exc, "retry_after", None)
                delay = self.delay_for(number, retry_after=retry_after)

                budget_exhausted = number >= self.max_attempts
                deadline_exceeded = (
                    self.deadline_seconds is not None
                    and (time.monotonic() - started) + delay >= self.deadline_seconds
                )
                if budget_exhausted or deadline_exceeded:
                    reason = ("attempt budget exhausted" if budget_exhausted
                              else "retry would exceed the wall-clock deadline")
                    log_failure(
                        log,
                        what=operation,
                        why=f"{exc.code}: {exc.message}",
                        recovery=f"retried {number} time(s) with exponential backoff and full jitter",
                        next_action=f"giving up -- {reason}; the caller decides the recovery path",
                        exc=exc,
                        attempt=number, max_attempts=self.max_attempts,
                        elapsed_seconds=round(time.monotonic() - started, 2),
                        **log_fields,
                    )
                    raise

                log_failure(
                    log,
                    what=operation,
                    why=f"{exc.code}: {exc.message}",
                    recovery=f"attempt {number}/{self.max_attempts} failed; the operation is "
                             f"idempotent so replay is safe",
                    next_action=f"retrying in {delay:.1f}s (attempt {number + 1}/{self.max_attempts})",
                    exc=exc,
                    attempt=number, retry_delay_seconds=round(delay, 2), **log_fields,
                )
                if on_retry is not None:
                    on_retry(attempt, exc)
                time.sleep(delay)
            except OrchestratorError as exc:
                log_failure(
                    log,
                    what=operation,
                    why=f"{exc.code}: {exc.message}",
                    recovery="none -- the error is classified as non-transient, so an identical "
                             "replay cannot produce a different result",
                    next_action="propagating to the caller for a state transition",
                    exc=exc,
                    attempt=number, **log_fields,
                )
                raise

        assert last_exc is not None  # pragma: no cover - unreachable
        raise last_exc


def classify_subprocess_failure(returncode: int, combined_output: str) -> OrchestratorError:
    """Maps a `kaggle` CLI invocation onto the typed hierarchy.

    Substring matching still happens here -- it has to, the CLI's only
    machine-readable channel is its exit code, which is 1 for everything -- but
    it happens exactly once, in one place, and produces a *typed* result that
    the rest of the system reasons about. That is the difference between
    'string matching as a classification boundary' and the old design, where
    string matching was scattered through the control flow."""
    from .errors import (AuthenticationError, KaggleUnavailableError, NetworkError,
                         NotFoundError, PayloadTooLargeError, RateLimitError as _RL,
                         ValidationError)

    text = (combined_output or "").lower()

    transient_markers = (
        "sslerror", "ssl:", "eof occurred", "max retries exceeded", "connection reset",
        "connection aborted", "remote end closed", "read timed out", "timed out",
        "connection refused", "temporary failure in name resolution", "bad handshake",
        "connection broken", "502 bad gateway", "503 service", "504 gateway",
        "internal server error", "500 server error", "connection error",
    )
    if any(m in text for m in transient_markers):
        return NetworkError("transport-level failure talking to Kaggle",
                            returncode=returncode, detail=combined_output[-400:])

    if "429" in text or "too many requests" in text or "rate limit" in text:
        return _RL("Kaggle rate limited the request", retry_after=30.0,
                   returncode=returncode, detail=combined_output[-400:])

    if any(m in text for m in ("401", "unauthorized", "authentication required", "403", "forbidden")):
        return AuthenticationError(
            "Kaggle rejected the credentials",
            returncode=returncode, detail=combined_output[-400:],
        )

    if any(m in text for m in ("404", "not found", "does not exist", "doesn't exist")):
        return NotFoundError("Kaggle has no resource at that address",
                             returncode=returncode, detail=combined_output[-400:])

    if "too large" in text or "413" in text or "request entity" in text:
        return PayloadTooLargeError("Kaggle rejected the payload as too large",
                                    returncode=returncode, detail=combined_output[-400:])

    if any(m in text for m in ("invalid", "bad request", "400", "must be", "title")):
        return ValidationError("Kaggle rejected the request as invalid",
                               returncode=returncode, detail=combined_output[-400:])

    # Unknown failures are treated as transient *once*: an unrecognised message
    # is far more often a new transport error than a new permanent one, and the
    # attempt budget bounds the cost of being wrong.
    return KaggleUnavailableError("unclassified Kaggle CLI failure",
                                  returncode=returncode, detail=combined_output[-400:])
