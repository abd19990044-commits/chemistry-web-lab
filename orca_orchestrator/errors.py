# -*- coding: utf-8 -*-
"""
Typed error hierarchy.

Design rule: **every failure must be classifiable without parsing a string.**
The old code decided whether to retry by substring-matching CLI output in
`_looks_transient()`. That is fragile in both directions -- a new Kaggle error
message silently becomes "permanent" (chain dies), and a job whose *name*
contains "connection reset" silently becomes "transient" (infinite retry).

Here, classification is a property of the exception type:

    OrchestratorError
      +-- TransientError      -> retry with backoff, same operation id
      |     +-- NetworkError
      |     +-- RateLimitError          (carries retry_after)
      |     +-- KaggleUnavailableError
      |     +-- LeaseLostError
      +-- PermanentError      -> do NOT retry; transition to FAILED
      |     +-- AuthenticationError
      |     +-- NotFoundError
      |     +-- ValidationError
      |     +-- PayloadTooLargeError
      |     +-- IllegalTransitionError
      |     +-- QuotaExhaustedError
      +-- IntegrityError      -> do NOT retry the *same* artefact; roll back
            +-- ChecksumMismatchError
            +-- IncompleteArtifactError
            +-- MissingRequiredFileError

`IntegrityError` is deliberately a third branch rather than a kind of
PermanentError: a corrupt checkpoint does not doom the job, it dooms *that
checkpoint*. The correct response is rollback to the previous verified
checkpoint, which is a different action from "fail the job".
"""
from __future__ import annotations

from typing import Any


class OrchestratorError(Exception):
    """Base class. Carries structured context for the log record."""

    #: Stable, machine-readable code used in logs, the event ledger and the API.
    code = "orchestrator_error"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "error_class": type(self).__name__,
            "message": self.message,
            "retryable": isinstance(self, TransientError),
            "context": self.context,
        }

    def __str__(self) -> str:  # pragma: no cover - trivial
        if not self.context:
            return self.message
        rendered = " ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
        return f"{self.message} [{rendered}]"


# --------------------------------------------------------------------------
# Transient: the same operation, replayed later, may well succeed.
# --------------------------------------------------------------------------
class TransientError(OrchestratorError):
    code = "transient"


class NetworkError(TransientError):
    code = "network"


class RateLimitError(TransientError):
    code = "rate_limited"

    def __init__(self, message: str, retry_after: float | None = None, **context: Any) -> None:
        super().__init__(message, retry_after=retry_after, **context)
        self.retry_after = retry_after


class KaggleUnavailableError(TransientError):
    code = "kaggle_unavailable"


class LeaseLostError(TransientError):
    """Another worker fenced us out. Abandon the action; do not undo anything --
    every action in this system is idempotent, so the new holder will redo it
    safely."""

    code = "lease_lost"


class TimeoutError_(TransientError):  # noqa: N801 - avoid shadowing builtins
    code = "timeout"


# --------------------------------------------------------------------------
# Permanent: replaying the identical operation cannot change the outcome.
# --------------------------------------------------------------------------
class PermanentError(OrchestratorError):
    code = "permanent"


class AuthenticationError(PermanentError):
    code = "auth_failed"


class NotFoundError(PermanentError):
    code = "not_found"


class ValidationError(PermanentError):
    code = "validation_failed"


class PayloadTooLargeError(PermanentError):
    code = "payload_too_large"


class IllegalTransitionError(PermanentError):
    """A state transition that the FSM does not define. Raised rather than
    tolerated: an undefined transition means the code holds a belief about the
    world that the model says is impossible, and continuing from there is how
    a job ends up in a state nobody can reason about."""

    code = "illegal_transition"


class QuotaExhaustedError(PermanentError):
    """A budget (restarts, disk restarts, cumulative optimisation cycles,
    wall-clock) was spent. Distinct from a resource *failure*: nothing broke,
    the job simply needs a human decision."""

    code = "quota_exhausted"


class ConcurrencyError(PermanentError):
    """Two actors tried to own the same resource and the loser is reporting it.
    Permanent for *this* attempt: the winner is already doing the work."""

    code = "concurrency_conflict"


# --------------------------------------------------------------------------
# Integrity: the artefact is bad; the job is not.
# --------------------------------------------------------------------------
class IntegrityError(OrchestratorError):
    code = "integrity"


class ChecksumMismatchError(IntegrityError):
    code = "checksum_mismatch"


class IncompleteArtifactError(IntegrityError):
    """Structurally truncated ORCA artefact (half-written .hess, torn .allxyz,
    trajectory cut mid-frame). Hash-correct files can still be semantically
    incomplete when the producer was killed mid-write, so structure is checked
    in addition to, not instead of, the hash."""

    code = "incomplete_artifact"


class MissingRequiredFileError(IntegrityError):
    code = "missing_required_file"
