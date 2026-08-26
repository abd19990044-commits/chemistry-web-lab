# -*- coding: utf-8 -*-
"""
Checkpoint transactions: stage, verify, commit, roll back.

The rule the brief asks for -- "no checkpoint is valid until verification
succeeds" -- is enforced structurally here rather than by convention. A
checkpoint moves through exactly three states:

    staged  ->  verified  ->  committed
       |            |
       +--> rejected +--> rejected

and `JobManifest.verified_checkpoint_id` is only ever advanced by
`commit_checkpoint()`, which refuses to run on anything that is not already
`verified`. There is deliberately no code path that lets a caller mark a
checkpoint good without re-reading every byte of it.

Why verify twice
----------------
Verification runs on the producer (before the successor is pushed) and again
on the consumer (before ORCA is started). Producer-side verification catches a
kill mid-write. Consumer-side verification catches corruption introduced by
the transfer itself: a truncated `kaggle kernels output`, a gzip stream cut
short, a base64 blob clipped by a kernel-source size limit. Only checking one
side leaves the other class of failure undetectable.

Why hashes are not enough on their own
--------------------------------------
A truncated `.hess` has a perfectly valid SHA-256 -- of its truncated self. The
hash proves the bytes survived the journey; it says nothing about whether the
producer finished writing them. So every file is validated *structurally* as
well, using the same `orca_artifacts` validators that the kernel runs.
"""
from __future__ import annotations

import glob
import os
import shutil
from typing import Callable, Iterable

from . import orca_artifacts as art
from .config import CONFIG
from .errors import (ChecksumMismatchError, IncompleteArtifactError,
                     MissingRequiredFileError, ValidationError)
from .hashing import atomic_write_bytes, digest_fileset, sha256_bytes, sha256_file
from .logging_ext import get_logger, log_event, log_failure
from .models import CheckpointManifest, CheckpointStatus, FileRecord, JobManifest, new_id, now

log = get_logger("orca.checkpoints")


# ---------------------------------------------------------------------------
# Role assignment
# ---------------------------------------------------------------------------
#: Which artefacts a given job kind *cannot* restart without. Getting this
#: table wrong in either direction is expensive: too strict and a perfectly
#: resumable checkpoint is rejected, too loose and the successor starts from a
#: state ORCA will refuse, burning a whole window before anyone notices.
REQUIRED_ROLES: dict[str, tuple[str, ...]] = {
    "opt": ("geometry",),
    "opt_freq": ("geometry",),
    "freq": ("geometry",),
    "scan": ("geometry",),
    "neb": ("neb_path",),
    "md": ("md_restart",),
    "irc": ("geometry",),
    "sp": (),
    "unknown": (),
}

ROLE_BY_VALIDATOR = {
    "xyz": "geometry",
    "trajectory": "trajectory",
    "hessian": "hessian",
    "allxyz": "neb_path",
    "engrad": "gradient",
    "mdrestart": "md_restart",
    "input": "input",
    "auxiliary": "auxiliary",
    "text": "auxiliary",
}


def role_for(filename: str) -> str:
    return ROLE_BY_VALIDATOR.get(art.validator_for(filename), "auxiliary")


# ---------------------------------------------------------------------------
# Building a checkpoint
# ---------------------------------------------------------------------------
def build_file_record(path: str, *, required: bool = False,
                      transport: str = "inline") -> FileRecord:
    """Hashes and structurally validates one file in a single pass.

    Raises rather than returning a degraded record: a file that does not pass
    its validator must never end up inside a bundle wearing a 'verified' label,
    because everything downstream trusts that label."""
    name = os.path.basename(path)
    result = art.validate_file(path)
    if not result.ok:
        raise IncompleteArtifactError(
            f"{name} failed structural validation: {result.reason}",
            filename=name, validator=result.validator, detail=result.detail,
        )
    size = os.path.getsize(path)
    if size > CONFIG.runner.max_checkpoint_file_bytes:
        raise ValidationError(
            f"{name} is larger than the per-file checkpoint limit",
            filename=name, size=size, limit=CONFIG.runner.max_checkpoint_file_bytes,
        )
    return FileRecord(
        name=name,
        sha256=sha256_file(path),
        size=size,
        role=role_for(name),
        required=required,
        transport=transport,
        structural_check=result.validator,
        verified_at=now(),
    )


def stage_checkpoint(
    *,
    job: JobManifest,
    source_dir: str,
    candidate_files: Iterable[str],
    next_input_text: str,
    orca_phase: str,
    completed_opt_cycles: int = 0,
    scan_points_done: int = 0,
    opt_converged: bool = False,
    last_energy: float | None = None,
    source_kernel_slug: str = "",
) -> CheckpointManifest:
    """Phase 1: collect and describe. Nothing is trusted yet.

    Files that fail validation are *dropped with a logged reason* rather than
    aborting the whole checkpoint -- a torn optional `.hess` should cost the
    successor a Hessian recomputation, not the entire window's progress. Files
    whose role is required for this job kind are different: if one of those is
    missing or bad, the checkpoint is invalid and must be rejected, because a
    successor started from it would fail in a way that looks like a mystery.
    """
    required_roles = set(REQUIRED_ROLES.get(job.job_kind, ()))
    records: list[FileRecord] = []
    dropped: list[dict] = []

    for path in candidate_files:
        if not os.path.isfile(path):
            continue
        name = os.path.basename(path)
        role = role_for(name)
        try:
            records.append(build_file_record(path, required=role in required_roles))
        except (IncompleteArtifactError, ValidationError) as exc:
            dropped.append({"file": name, "role": role, "reason": exc.message})
            log_event(log, "checkpoint_file_dropped",
                      "excluded an unusable artefact from the checkpoint",
                      job_id=job.job_id, epoch=job.epoch, file=name, role=role,
                      reason=exc.message,
                      consequence=("the checkpoint will be REJECTED: this role is required"
                                   if role in required_roles else
                                   "the successor recomputes this artefact"))

    next_input_bytes = (next_input_text or "").encode("utf-8")
    checkpoint = CheckpointManifest(
        checkpoint_id=new_id("ckpt_"),
        job_id=job.job_id,
        epoch=job.epoch,
        created_at=now(),
        status=CheckpointStatus.STAGED,
        files=records,
        next_input_text=next_input_text,
        next_input_sha256=sha256_bytes(next_input_bytes),
        orca_phase=orca_phase,
        completed_opt_cycles=completed_opt_cycles,
        cumulative_opt_cycles=job.cumulative_opt_cycles + completed_opt_cycles,
        scan_points_done=scan_points_done,
        opt_converged=opt_converged,
        last_energy=last_energy,
        source_kernel_slug=source_kernel_slug or job.current_slug,
        parent_checkpoint_id=job.verified_checkpoint_id,
    )
    checkpoint.bundle_digest = digest_fileset(
        [(f.name, f.sha256, f.size) for f in records]
        + [("__next_input__", checkpoint.next_input_sha256, len(next_input_bytes))]
    )
    checkpoint._extra["dropped_files"] = dropped
    checkpoint._extra["source_dir"] = source_dir

    log_event(log, "checkpoint_staged", "checkpoint staged, awaiting verification",
              job_id=job.job_id, epoch=job.epoch, checkpoint_id=checkpoint.checkpoint_id,
              bundle_digest=checkpoint.bundle_digest[:16], files=len(records),
              dropped=len(dropped), total_bytes=checkpoint.total_bytes)
    return checkpoint


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
def verify_checkpoint(checkpoint: CheckpointManifest, directory: str,
                      *, strict: bool = True) -> CheckpointManifest:
    """Phase 2: re-read every byte and re-run every validator.

    Deliberately does not trust anything computed during staging. Re-hashing
    from disk is what catches the file that was still being flushed when it was
    hashed, and re-validating is what catches the file that was complete when
    hashed and truncated afterwards by a disk-full condition.

    Mutates and returns the checkpoint with `status` set to `verified` or
    `rejected`. Raises only when `strict` and the checkpoint is unusable, so
    the caller can choose between "this must work" (producer side) and "tell me
    whether this works" (rollback evaluation).
    """
    # Delegated to the shared implementation so the producer (this process) and
    # the consumer (the in-kernel runner, which embeds `orca_artifacts`) run
    # byte-identical checks. A divergence between the two would let a bundle
    # pass on one side and fail on the other, which is the worst possible
    # outcome: a checkpoint that is committed and then rejected.
    outcome = art.verify_bundle(
        [f.to_dict() for f in checkpoint.files],
        directory,
        next_input_text=checkpoint.next_input_text or None,
        next_input_sha256=checkpoint.next_input_sha256 if checkpoint.next_input_text else None,
    )
    problems: list[dict] = list(outcome["problems"])
    verified_names = set(outcome["verified"])
    for record in checkpoint.files:
        if record.name in verified_names:
            record.verified_at = now()

    present = {f.name for f in checkpoint.files
               if os.path.exists(os.path.join(directory, f.name))}
    recomputed = digest_fileset(
        [(f.name, f.sha256, f.size) for f in checkpoint.files if f.name in present]
        + [("__next_input__", checkpoint.next_input_sha256,
            len((checkpoint.next_input_text or "").encode("utf-8")))]
    )
    bundle_intact = recomputed == checkpoint.bundle_digest

    blocking = [p for p in problems if p["required"]]
    if not blocking and not bundle_intact and strict:
        blocking.append({"file": "<bundle>", "role": "bundle", "required": True,
                         "problem": "the bundle digest does not match the file set present"})

    if blocking:
        checkpoint.status = CheckpointStatus.REJECTED
        checkpoint.rejection_reason = "; ".join(
            f"{p['file']}: {p['problem']}" for p in blocking
        )
        checkpoint._extra["verification_problems"] = problems
        log_failure(
            log,
            what="checkpoint verification",
            why=checkpoint.rejection_reason,
            recovery="the checkpoint is marked rejected and will never be used as a "
                     "restart source or a rollback target",
            next_action="rolling back to the previous verified checkpoint",
            job_id=checkpoint.job_id, epoch=checkpoint.epoch,
            checkpoint_id=checkpoint.checkpoint_id, problems=problems,
        )
        if strict:
            first = blocking[0]
            if "missing" in first["problem"]:
                raise MissingRequiredFileError(checkpoint.rejection_reason,
                                               checkpoint_id=checkpoint.checkpoint_id,
                                               problems=problems)
            if "sha256" in first["problem"] or "digest" in first["problem"]:
                raise ChecksumMismatchError(checkpoint.rejection_reason,
                                            checkpoint_id=checkpoint.checkpoint_id,
                                            problems=problems)
            raise IncompleteArtifactError(checkpoint.rejection_reason,
                                          checkpoint_id=checkpoint.checkpoint_id,
                                          problems=problems)
        return checkpoint

    # Non-blocking problems mean optional artefacts were lost. The checkpoint
    # is still valid; it is just cheaper to restart from than it could be.
    if problems:
        checkpoint.files = [f for f in checkpoint.files
                            if f.name not in {p["file"] for p in problems}]
        checkpoint.bundle_digest = digest_fileset(
            [(f.name, f.sha256, f.size) for f in checkpoint.files]
            + [("__next_input__", checkpoint.next_input_sha256,
                len((checkpoint.next_input_text or "").encode("utf-8")))]
        )
        checkpoint._extra["degraded"] = problems

    checkpoint.status = CheckpointStatus.VERIFIED
    checkpoint.verified_at = now()
    checkpoint.rejection_reason = None
    log_event(log, "checkpoint_verified",
              "checkpoint passed hash and structural verification",
              job_id=checkpoint.job_id, epoch=checkpoint.epoch,
              checkpoint_id=checkpoint.checkpoint_id,
              files=len(checkpoint.files), degraded=len(problems),
              bundle_digest=checkpoint.bundle_digest[:16])
    return checkpoint


def commit_checkpoint(checkpoint: CheckpointManifest) -> CheckpointManifest:
    """Phase 3. Only a `verified` checkpoint may be committed.

    Committing means: a successor carrying this checkpoint has been accepted by
    Kaggle. It is the point of no return for the epoch, and it is why the
    successor push happens *after* verification and *before* result packaging
    -- packaging is the step that runs out of disk or time, and it used to take
    the whole chain down with it."""
    if checkpoint.status != CheckpointStatus.VERIFIED:
        raise ValidationError(
            "refusing to commit a checkpoint that has not passed verification",
            checkpoint_id=checkpoint.checkpoint_id, status=checkpoint.status,
        )
    checkpoint.status = CheckpointStatus.COMMITTED
    checkpoint.committed_at = now()
    log_event(log, "checkpoint_committed", "checkpoint committed to a successor window",
              job_id=checkpoint.job_id, epoch=checkpoint.epoch,
              checkpoint_id=checkpoint.checkpoint_id)
    return checkpoint


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------
def select_rollback_target(
    *,
    job: JobManifest,
    load_checkpoint: Callable[[str], CheckpointManifest | None],
    find_latest_verified: Callable[[str, int | None], CheckpointManifest | None],
    failed_epoch: int | None = None,
) -> CheckpointManifest | None:
    """Chooses what to fall back to after a failed restart.

    The search is strictly *backwards* past the epoch that just failed. Without
    that constraint the system re-selects the checkpoint that just poisoned the
    run and loops -- the classic "automatic recovery that never recovers".

    Preference order:
      1. the job's recorded previous checkpoint, if it still verifies;
      2. the newest verified checkpoint strictly older than the failed epoch;
      3. nothing -- which means the job genuinely cannot continue and must
         fail loudly rather than restart from zero and silently repeat work.
    """
    epoch_bound = failed_epoch if failed_epoch is not None else job.epoch

    if job.previous_checkpoint_id:
        candidate = load_checkpoint(job.previous_checkpoint_id)
        if candidate is not None and candidate.is_usable and candidate.epoch < epoch_bound:
            log_event(log, "rollback_target_selected",
                      "falling back to the job's recorded previous checkpoint",
                      job_id=job.job_id, checkpoint_id=candidate.checkpoint_id,
                      target_epoch=candidate.epoch, failed_epoch=epoch_bound)
            return candidate

    candidate = find_latest_verified(job.job_id, epoch_bound)
    if candidate is not None:
        log_event(log, "rollback_target_selected",
                  "falling back to the newest verified checkpoint before the failed window",
                  job_id=job.job_id, checkpoint_id=candidate.checkpoint_id,
                  target_epoch=candidate.epoch, failed_epoch=epoch_bound)
        return candidate

    log_failure(
        log,
        what="rollback target selection",
        why="no verified checkpoint exists strictly before the failed window",
        recovery="searched the recorded previous checkpoint and the full checkpoint history",
        next_action="failing the job -- restarting from zero would silently repeat work "
                    "already paid for and would very likely fail the same way",
        job_id=job.job_id, failed_epoch=epoch_bound,
    )
    return None


# ---------------------------------------------------------------------------
# Bundle materialisation
# ---------------------------------------------------------------------------
def materialise_bundle(checkpoint: CheckpointManifest, files: dict[str, bytes],
                       target_dir: str) -> None:
    """Writes a received bundle to disk atomically, then verifies it in place.

    Writing straight into the working directory is what makes a half-restored
    checkpoint possible: ORCA starts, finds three of five files, and fails in a
    way nobody can diagnose. Here every file lands in a staging directory
    first, the whole set is verified, and only then is it moved across -- so
    the working directory transitions from "empty" to "complete", never through
    "partly restored".
    """
    staging = os.path.join(target_dir, ".ckpt-staging")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)
    try:
        for name, data in files.items():
            atomic_write_bytes(os.path.join(staging, os.path.basename(name)), data)

        verify_checkpoint(checkpoint, staging, strict=True)

        for record in checkpoint.files:
            src = os.path.join(staging, record.name)
            if os.path.exists(src):
                os.replace(src, os.path.join(target_dir, record.name))
        if checkpoint.next_input_text:
            atomic_write_bytes(
                os.path.join(target_dir, "__continuation__.inp"),
                checkpoint.next_input_text.encode("utf-8"),
            )
        log_event(log, "bundle_materialised",
                  "checkpoint bundle verified in staging and moved into the working directory",
                  job_id=checkpoint.job_id, epoch=checkpoint.epoch,
                  checkpoint_id=checkpoint.checkpoint_id, files=len(checkpoint.files))
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# Candidate discovery
# ---------------------------------------------------------------------------
#: Ordered by restart value. Position matters when a bundle has to be trimmed
#: to fit a transport limit: the geometry is what makes a restart possible at
#: all, the Hessian only makes the next step cheaper.
#:
#: The binary `.gbw` wavefunction is deliberately absent and must stay absent.
#: A force-killed ORCA leaves it half-written with no structural marker that
#: says so, the successor AutoStarts or MOReads it, and the whole chain dies on
#: "GBWFile is corrupt / I/O OPERATION FAILED" -- after the good ASCII
#: checkpoints were already in hand. Everything here is ASCII, append-only or
#: rewritten whole, and therefore verifiable before it is trusted.
CHECKPOINT_PATTERNS: tuple[tuple[str, int], ...] = (
    ("{base}.xyz", 10),
    ("{base}_trj.xyz", 15),
    ("*_MEP.allxyz", 10),
    ("{base}.allxyz", 12),
    ("{base}.mdrestart", 10),
    ("{base}.[0-9][0-9][0-9].xyz", 12),
    ("{base}.res.*", 15),
    ("{base}.hess", 30),
    ("{base}.opt", 35),
    ("{base}.engrad", 40),
)


def discover_checkpoint_candidates(work_dir: str, basename: str) -> list[str]:
    """Returns restart-relevant files in descending order of importance."""
    seen: set[str] = set()
    scored: list[tuple[int, str]] = []
    for pattern, priority in CHECKPOINT_PATTERNS:
        for path in sorted(glob.glob(os.path.join(work_dir, pattern.format(base=basename)))):
            name = os.path.basename(path)
            if name in seen or not os.path.isfile(path):
                continue
            seen.add(name)
            scored.append((priority, path))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [path for _priority, path in scored]
