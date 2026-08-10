# -*- coding: utf-8 -*-
"""
The Kaggle-side ledger: the system's actual source of truth.

Rationale
---------
Neither of the two obvious places to keep state survives the failures this
system must tolerate:

  * The **Flask process** is restarted by every Hugging Face redeploy, every
    OOM, and every Space sleep/wake. Its filesystem is wiped on the free tier.
  * The **Kaggle kernel** exists for at most twelve hours and has no identity
    that outlives it.

The third place does survive both: a kernel's *saved output* persists in the
owner's Kaggle account indefinitely, is readable through the API with the
owner's own credentials, and is written by the one actor that always knows the
truth -- the kernel that is actually running the calculation.

So each window writes `STATE.json` into `/kaggle/working`, and that file is the
authoritative record. The SQLite store is a cache in front of it. Losing the
cache costs a few API calls; losing the ledger would require the user to delete
their own notebooks.

Staleness is the hard part
--------------------------
`/kaggle/working` persists across *runs of the same kernel*, so a second run
sees the previous run's files. A naive reader can therefore pick up a
`NEXT_JOB_ID.txt` written by an earlier run and follow a chain that is not
advancing -- one of the reported "job appears stuck" symptoms. Every record
here is stamped with `epoch`, `run_token` and `written_at`, and
`is_stale_relative_to()` refuses to accept a record that describes an older
epoch than the one being asked about.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .config import CONFIG
from .errors import ChecksumMismatchError, NotFoundError, ValidationError
from .hashing import sha256_bytes, stable_json
from .kaggle_api import KaggleClient
from .logging_ext import get_logger, log_event, log_failure
from .models import CheckpointManifest, Event, JobManifest, now
from .states import JobState

log = get_logger("orca.ledger")

STATE_FILE = "STATE.json"
CHECKPOINT_FILE = "CHECKPOINT.json"
HEARTBEAT_FILE = "HEARTBEAT.json"
LEGACY_NEXT_ID_FILE = "NEXT_JOB_ID.txt"
LEGACY_NEXT_URL_FILE = "NEXT_JOB_URL.txt"
NOTE_FILE = "JOB_NOTE.txt"


@dataclass
class LedgerRecord:
    """One window's self-report, as read back from Kaggle."""

    slug: str
    job: JobManifest | None = None
    checkpoint: CheckpointManifest | None = None
    heartbeat: dict = field(default_factory=dict)
    note: str = ""
    #: Set when only the pre-orchestrator marker files were found, i.e. this
    #: window was produced by the previous generation of the runner. Chains
    #: that are already in flight during a deploy land here, and must keep
    #: working rather than being abandoned.
    legacy_next_slug: str | None = None
    raw_files: dict[str, int] = field(default_factory=dict)
    read_at: float = field(default_factory=now)

    @property
    def has_state(self) -> bool:
        return self.job is not None

    @property
    def epoch(self) -> int:
        if self.job is not None:
            return self.job.epoch
        return int(self.heartbeat.get("epoch", -1))

    @property
    def run_token(self) -> str:
        if self.heartbeat.get("run_token"):
            return str(self.heartbeat["run_token"])
        return self.job.run_token if self.job else ""

    @property
    def heartbeat_age_seconds(self) -> float | None:
        ts = self.heartbeat.get("at")
        try:
            return max(0.0, time.time() - float(ts))
        except (TypeError, ValueError):
            return None

    def is_stale_relative_to(self, epoch: int) -> bool:
        """True when this record describes a window older than `epoch`.

        The guard against `/kaggle/working` persistence: a leftover file from a
        previous run of the same kernel describes an epoch that has already
        been superseded, and acting on it re-runs work or follows a dead
        pointer."""
        return self.epoch >= 0 and self.epoch < epoch


# ---------------------------------------------------------------------------
# Writing (used by the in-kernel runner via the embedded copy)
# ---------------------------------------------------------------------------
def serialise_state(job: JobManifest, *, run_token: str,
                    checkpoint: CheckpointManifest | None = None,
                    disk_report: dict | None = None,
                    extra: dict | None = None) -> dict:
    """Builds the STATE.json document.

    Includes its own digest under `_digest`, computed over the document with
    that field removed. A reader can therefore detect a truncated or partially
    flushed file without a second sidecar file that could itself go missing."""
    document: dict[str, Any] = {
        "schema_version": CONFIG.manifest_version,
        "written_at": now(),
        "run_token": run_token,
        "job": job.to_dict(),
        "checkpoint": checkpoint.to_dict() if checkpoint else None,
        "disk_report": disk_report or job.disk_report or {},
    }
    if extra:
        document["extra"] = extra
    document["_digest"] = sha256_bytes(stable_json(
        {k: v for k, v in document.items() if k != "_digest"}
    ).encode("utf-8"))
    return document


def serialise_heartbeat(*, job_id: str, epoch: int, run_token: str, state: str,
                        detail: dict | None = None) -> dict:
    """The liveness signal.

    Deliberately tiny and written with an atomic replace, because it is
    rewritten every 45 seconds for up to twelve hours and must never be the
    thing that fills the output quota or that a reader catches half-written."""
    return {
        "schema_version": CONFIG.manifest_version,
        "job_id": job_id,
        "epoch": epoch,
        "run_token": run_token,
        "state": state,
        "at": now(),
        "detail": detail or {},
    }


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def _parse_document(raw: bytes, filename: str) -> dict | None:
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        log_failure(
            log,
            what=f"parsing {filename} from the Kaggle ledger",
            why=f"the file is not valid JSON ({exc}); it was most likely truncated by a "
                f"kill during the write or by the output quota filling up",
            recovery="the record is discarded rather than partially trusted",
            next_action="falling back to the previous window's ledger record, and to the "
                        "local cache if that is also unreadable",
            filename=filename, size=len(raw),
        )
        return None


def _verify_digest(document: dict, filename: str) -> bool:
    recorded = document.get("_digest")
    if not recorded:
        return True  # written by an older schema; accept but do not claim verified
    actual = sha256_bytes(stable_json(
        {k: v for k, v in document.items() if k != "_digest"}
    ).encode("utf-8"))
    if actual != recorded:
        log_failure(
            log,
            what=f"verifying the digest of {filename}",
            why="the recorded digest does not match the document content",
            recovery="the record is rejected; a corrupt ledger entry is never trusted",
            next_action="reading the previous window's ledger record instead",
            filename=filename, expected=recorded[:16], actual=actual[:16],
        )
        return False
    return True


def parse_ledger_files(slug: str, files: dict[str, bytes]) -> LedgerRecord:
    """Turns raw downloaded control files into a typed record.

    Tolerant by design: any individual file may be absent, empty or corrupt,
    and the record simply reports less. A ledger reader that raises on a
    missing file makes the whole recovery path fragile in exactly the
    situations recovery exists for."""
    record = LedgerRecord(slug=slug,
                          raw_files={name: len(data) for name, data in files.items()})

    if STATE_FILE in files:
        document = _parse_document(files[STATE_FILE], STATE_FILE)
        if document and _verify_digest(document, STATE_FILE):
            try:
                record.job = JobManifest.from_dict(document.get("job") or {})
                if document.get("checkpoint"):
                    record.checkpoint = CheckpointManifest.from_dict(document["checkpoint"])
                if document.get("disk_report"):
                    record.job.disk_report = document["disk_report"]
                record.job.run_token = document.get("run_token", record.job.run_token)
            except (TypeError, ValueError) as exc:
                log.warning("STATE.json had an unreadable shape", extra={
                    "slug": slug, "error": str(exc)})

    if CHECKPOINT_FILE in files and record.checkpoint is None:
        document = _parse_document(files[CHECKPOINT_FILE], CHECKPOINT_FILE)
        if document:
            try:
                record.checkpoint = CheckpointManifest.from_dict(
                    document.get("checkpoint", document))
            except (TypeError, ValueError):
                pass

    if HEARTBEAT_FILE in files:
        document = _parse_document(files[HEARTBEAT_FILE], HEARTBEAT_FILE)
        if isinstance(document, dict):
            record.heartbeat = document

    if NOTE_FILE in files:
        record.note = files[NOTE_FILE].decode("utf-8", errors="replace").strip()

    # Backwards compatibility with chains already in flight when this
    # orchestrator is deployed. Those kernels only know how to write the old
    # marker files, and abandoning them would strand real calculations.
    if LEGACY_NEXT_ID_FILE in files:
        candidate = files[LEGACY_NEXT_ID_FILE].decode("utf-8", errors="replace").strip()
        if candidate:
            record.legacy_next_slug = candidate

    return record


def read_window(client: KaggleClient, slug: str) -> LedgerRecord:
    """Fetches and parses one window's ledger."""
    files = client.fetch_ledger_files(slug)
    record = parse_ledger_files(slug, files)
    log_event(log, "ledger_read", "read a window's ledger from Kaggle",
              slug=slug, has_state=record.has_state, epoch=record.epoch,
              files=sorted(record.raw_files), note_len=len(record.note))
    return record


def newest_window_slug(job_id: str, known_slugs: list[str]) -> str:
    """Given every slug belonging to a chain, returns the newest window.

    Ordering is by *epoch parsed from the slug*, never by Kaggle's
    `lastRunTime`. A re-run of an older window updates its timestamp and would
    otherwise make it look like the newest one, sending every subsequent poll
    to a window that has already been superseded."""
    def epoch_of(slug: str) -> int:
        if slug == job_id:
            return 0
        suffix = slug[len(job_id):]
        if suffix.startswith("-r") and suffix[2:].isdigit():
            return int(suffix[2:])
        return -1

    ranked = sorted(((epoch_of(s), s) for s in known_slugs if epoch_of(s) >= 0), reverse=True)
    return ranked[0][1] if ranked else job_id


def group_chains(kernels: list[dict], prefix: str | None = None) -> dict[str, list[dict]]:
    """Groups a flat kernel list into chains keyed by base slug.

    An auto-restarted job owns several kernels -- `<base>`, `<base>-r1`,
    `<base>-r2` -- but they are one job to the person who submitted it, and
    deleting the job must delete all of them or orphans accumulate in the
    account forever."""
    import re as _re

    prefix = prefix or CONFIG.job_id_prefix
    chains: dict[str, list[dict]] = {}
    for kernel in kernels:
        slug = kernel.get("slug", "")
        if not slug.startswith(prefix):
            continue
        match = _re.match(r"^(.*?)-r(\d+)$", slug)
        base, epoch = (match.group(1), int(match.group(2))) if match else (slug, 0)
        entry = dict(kernel)
        entry["epoch"] = epoch
        entry["base"] = base
        chains.setdefault(base, []).append(entry)
    for windows in chains.values():
        windows.sort(key=lambda w: w["epoch"])
    return chains


# ---------------------------------------------------------------------------
# Reconstruction
# ---------------------------------------------------------------------------
def rebuild_from_kaggle(client: KaggleClient, job_id: str,
                        *, max_windows_to_probe: int = 4) -> JobManifest:
    """Rebuilds a complete job manifest using only Kaggle as input.

    This is the function that makes the whole 'no persistent server storage'
    position tenable. It is exercised on three real paths:

      * a Hugging Face restart or redeploy wiped the SQLite cache;
      * the user signed in from a different browser or device;
      * a job was submitted before this orchestrator existed and must be
        adopted rather than abandoned.

    Windows are probed newest-first and the walk stops at the first one with a
    readable `STATE.json`, because that document already contains the whole
    job. The bounded probe exists because a long chain would otherwise mean
    twenty API calls just to answer 'what is the status'.
    """
    kernels = client.list_kernels()
    chains = group_chains(kernels)
    windows = chains.get(job_id)
    if not windows:
        raise NotFoundError(
            "no kernel belonging to this job exists on the Kaggle account",
            job_id=job_id,
        )

    slugs = [w["slug"] for w in windows]
    probed = 0
    for window in reversed(windows):
        if probed >= max_windows_to_probe:
            break
        probed += 1
        try:
            record = read_window(client, window["slug"])
        except NotFoundError:
            continue
        if record.has_state and record.job is not None:
            job = record.job
            # Chain membership comes from the listing, which is authoritative
            # about what exists; the manifest may predate the newest window.
            job.chain_slugs = slugs
            job.current_slug = newest_window_slug(job_id, slugs)
            job.owner = client.creds.username
            if record.heartbeat:
                job.last_heartbeat_at = record.heartbeat.get("at")
                job.heartbeat_detail = record.heartbeat.get("detail", {})
            log_event(log, "job_rebuilt_from_kaggle",
                      "reconstructed the job manifest from the Kaggle-side ledger",
                      job_id=job_id, epoch=job.epoch, state=job.state.value,
                      windows=len(slugs), probed=probed,
                      source_window=window["slug"])
            return job

    # No orchestrator-era ledger anywhere in the chain: adopt it conservatively.
    newest = windows[-1]
    job = JobManifest.create(
        job_id=job_id,
        owner=client.creds.username,
        title=newest.get("title") or job_id,
        input_filename="",
        original_input_sha256="",
    )
    job.epoch = int(newest.get("epoch", 0))
    job.chain_slugs = slugs
    job.current_slug = newest["slug"]
    job.current_url = newest.get("url", "")
    job.state = JobState.QUEUED
    job.last_note = (
        "Adopted from Kaggle without an orchestrator ledger. This job was submitted by an "
        "earlier version of the runner, so its checkpoint history is not available. It is "
        "tracked from here on, but a failure inside the current window cannot be rolled "
        "back to a verified checkpoint until this window writes one."
    )
    job.record_event(Event.create(
        job_id=job_id, epoch=job.epoch, trigger="ADOPTED",
        from_state=JobState.CREATED, to_state=JobState.QUEUED, actor="system",
        reason="legacy chain adopted from the Kaggle kernel listing",
        windows=len(slugs),
    ))
    log_event(log, "job_adopted_legacy",
              "adopted a pre-orchestrator chain from Kaggle",
              job_id=job_id, windows=len(slugs), epoch=job.epoch)
    return job


def discover_jobs(client: KaggleClient) -> list[dict]:
    """Every job this site has ever created on the account, grouped into chains.

    Used on sign-in. This is what makes the job list survive clearing browser
    data or moving to a new device: the list is derived from Kaggle, which owns
    the notebooks, rather than from the browser, which owns nothing."""
    chains = group_chains(client.list_kernels())
    jobs = []
    for base, windows in chains.items():
        newest = windows[-1]
        jobs.append({
            "job_id": base,
            "current_slug": newest["slug"],
            "kaggle_url": newest["url"],
            "epoch": newest["epoch"],
            "chain_slugs": [w["slug"] for w in windows],
            "last_run": max((w.get("last_run") or "") for w in windows),
            "title": pretty_title(base),
        })
    jobs.sort(key=lambda j: j["last_run"], reverse=True)
    return jobs


def pretty_title(slug: str, fallback: str = "") -> str:
    """Turns `chem-tools-co2-opt-1a2b3c4d` back into `co2 opt`, so a job list
    rebuilt from Kaggle reads like the name the person chose rather than an
    internal identifier."""
    import re as _re

    slug = (slug or "").strip()
    core = slug[len(CONFIG.job_id_prefix):] if slug.startswith(CONFIG.job_id_prefix) else slug
    core = _re.sub(r"-r\d+$", "", core)
    core = _re.sub(r"-[0-9a-f]{8}$", "", core)
    core = core.replace("-", " ").strip()
    return core or (fallback or slug)
