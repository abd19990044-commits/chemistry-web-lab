# -*- coding: utf-8 -*-
"""
Assembles the script that Kaggle will run.

The pushed `script.py` is built from three parts:

    1. `ORCA_JOB_HEADER = {...}`     -- this window's parameters
    2. an embedded copy of `orca_artifacts`, exec'd into a real module
    3. the source of `runner/kernel_runner.py`

Reading (2) and (3) from disk rather than storing them as string literals is
the single biggest maintainability change in this rewrite. The previous design
kept the whole in-kernel program inside a triple-quoted string, so it could not
be compiled, linted, imported, or tested -- every bug in it surfaced only in
production, twelve hours at a time. Here both files are ordinary modules that
`python -m compileall` checks on every deploy.

Self-continuation
-----------------
A window has to be able to push its *own* successor with no server involved, so
it must carry the sources it will need to build that successor. Both are
embedded base64 in the header (`artifacts_source_b64`, `runner_body_b64`) and
simply passed along unchanged. A chain started under one deploy therefore keeps
running the runner it was started with, even across a Space redeploy -- which
is the correct behaviour: swapping the runner underneath an in-flight
calculation would be a far worse surprise than running a slightly older one.
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import re
from functools import lru_cache

from ..config import CONFIG
from ..credentials import KaggleCredentials
from ..errors import PayloadTooLargeError
from ..kaggle_api import write_kernel_metadata
from ..logging_ext import get_logger, log_event
from ..models import CheckpointManifest, JobManifest

log = get_logger("orca.builder")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ARTIFACTS_PATH = os.path.join(os.path.dirname(_HERE), "orca_artifacts.py")
_RUNNER_PATH = os.path.join(_HERE, "kernel_runner.py")


_FUTURE_IMPORT = re.compile(r"^\s*from\s+__future__\s+import\s+.*$", re.MULTILINE)


@lru_cache(maxsize=2)
def _read_source(path: str) -> str:
    """Reads a module's source, stripping any `from __future__` import.

    The runner's source is concatenated *after* a generated header, and a
    `__future__` import is only legal as the very first statement of a file.
    One slipping into `kernel_runner.py` would turn every pushed script into an
    immediate SyntaxError -- a failure that would only surface once a real job
    reached Kaggle. Stripping here makes that impossible regardless of what
    anyone later adds to the runner."""
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    stripped, count = _FUTURE_IMPORT.subn(
        "# (__future__ import removed: this source is embedded, not top-of-file)", source)
    if count:
        log.warning("stripped %d __future__ import(s) while embedding %s",
                    count, os.path.basename(path),
                    extra={"event": "future_import_stripped", "path": path})
    return stripped


@lru_cache(maxsize=2)
def _read_source_b64(path: str) -> str:
    return base64.b64encode(_read_source(path).encode("utf-8")).decode("ascii")


def encode_inline_files(files: dict[str, bytes]) -> str:
    """gzip + base64 of a `{name: bytes}` map.

    Compressed because a Kaggle kernel's source is size-limited and the input
    plus any attached files ride inside `script.py`. An oversized push is how a
    chain used to die silently on a large system, so the limit is checked
    explicitly rather than discovered."""
    payload = {name: base64.b64encode(data).decode("ascii") for name, data in files.items()}
    raw = json.dumps(payload).encode("utf-8")
    return base64.b64encode(gzip.compress(raw, 6)).decode("ascii")


def build_header(*, job: JobManifest, epoch: int, creds: KaggleCredentials,
                 inline_files: dict[str, bytes] | None = None,
                 checkpoint: CheckpointManifest | None = None,
                 predecessor_slug: str = "", orca_link: str | None = None) -> dict:
    """Assembles the parameter block injected ahead of the runner body.

    Every key the runner reads has a default on the runner side as well, so a
    successor pushed by an older deploy -- whose header lacks a newer key --
    still runs instead of dying on a KeyError in hour nine of a chain."""
    budgets = {
        "time_limit_seconds": CONFIG.runner.time_limit_seconds,
        "handoff_reserve_seconds": CONFIG.runner.handoff_reserve_seconds,
        "min_free_bytes": CONFIG.runner.min_free_bytes,
        "result_budget_bytes": CONFIG.runner.result_budget_bytes,
        "max_epochs": job.max_epochs,
        "max_disk_epochs": job.max_disk_epochs,
        "max_total_opt_cycles": job.max_total_opt_cycles,
        "per_window_opt_maxiter": CONFIG.runner.per_window_opt_maxiter,
        "heartbeat_seconds": CONFIG.runner.heartbeat_seconds,
        "watchdog_poll_seconds": CONFIG.runner.watchdog_poll_seconds,
        "inline_carry_limit_bytes": CONFIG.runner.inline_carry_limit_bytes,
        "max_checkpoint_file_bytes": CONFIG.runner.max_checkpoint_file_bytes,
        "max_checkpoint_bundle_bytes": CONFIG.runner.max_checkpoint_bundle_bytes,
        "working_quota_bytes": CONFIG.kaggle.working_quota_bytes,
        "scratch_quota_bytes": CONFIG.kaggle.scratch_quota_bytes,
        "implausible_free_bytes": CONFIG.kaggle.implausible_free_bytes,
        "hard_session_seconds": CONFIG.kaggle.hard_session_seconds,
    }

    return {
        "schema_version": CONFIG.manifest_version,
        "job_id": job.job_id,
        "epoch": int(epoch),
        "owner": job.owner,
        "title": job.title,
        "input_filename": job.input_filename,
        "job_kind": job.job_kind,
        "dataset_sources": list(job.dataset_sources),
        "orca_link": orca_link,
        "kaggle_username": creds.username,
        "kaggle_key": creds.key,
        "kaggle_api_token": creds.api_token,
        "inline_files_b64": encode_inline_files(inline_files or {}),
        "checkpoint_manifest": checkpoint.to_dict() if checkpoint else None,
        "predecessor_slug": predecessor_slug,
        "cumulative_opt_cycles": job.cumulative_opt_cycles,
        "disk_epochs_used": job.disk_epochs_used,
        "budgets": budgets,
        "artifacts_source_b64": _read_source_b64(_ARTIFACTS_PATH),
        "runner_body_b64": _read_source_b64(_RUNNER_PATH),
    }


def render_script(header: dict) -> str:
    """Produces the complete `script.py`.

    The embedded module is exec'd into a real `ModuleType` and registered in
    `sys.modules` before the runner body executes, so the runner's ordinary
    `import orca_artifacts` succeeds. That keeps `kernel_runner.py` a normal
    importable module locally -- it does not have to be written in a special
    style to survive embedding.

    The generated header is also self-healing for old or incomplete manifests:
    if `job_kind` is missing or `unknown`, it is inferred from the actual ORCA
    input carried in `inline_files_b64`. This prevents an old/stale server
    manifest from classifying an unconverged optimisation as a single-point
    calculation merely because its header predates the job-kind field.
    """
    script_prefix = (
        "# -*- coding: utf-8 -*-\n"
        "# Generated by orca_orchestrator. Do not edit inside Kaggle: this file is\n"
        "# rebuilt from source on every push, and any local change is discarded.\n"
        f"ORCA_JOB_HEADER = {header!r}\n"
        "\n"
        "import base64 as _b64, gzip as _gzip, json as _json, sys as _sys, types as _types\n"
        "_ARTIFACTS_SRC = _b64.b64decode(ORCA_JOB_HEADER['artifacts_source_b64']).decode('utf-8')\n"
        "_m = _types.ModuleType('orca_artifacts')\n"
        "_m.__file__ = 'orca_artifacts.py'\n"
        "exec(compile(_ARTIFACTS_SRC, 'orca_artifacts.py', 'exec'), _m.__dict__)\n"
        "_sys.modules['orca_artifacts'] = _m\n"
        "\n"
        "if ORCA_JOB_HEADER.get('job_kind') in (None, '', 'unknown'):\n"
        "    try:\n"
        "        _raw = _b64.b64decode(ORCA_JOB_HEADER.get('inline_files_b64') or '')\n"
        "        try: _raw = _gzip.decompress(_raw)\n"
        "        except OSError: pass\n"
        "        _files = _json.loads(_raw.decode('utf-8')) if _raw else {}\n"
        "        _inp = _files.get(ORCA_JOB_HEADER.get('input_filename'))\n"
        "        if _inp:\n"
        "            _input_text = _b64.b64decode(_inp).decode('utf-8', errors='replace')\n"
        "            ORCA_JOB_HEADER['job_kind'] = _m.detect_job_kind(_input_text)\n"
        "    except Exception:\n"
        "        pass\n"
        "\n"
    )
    return script_prefix + _read_source(_RUNNER_PATH)


def build_window_directory(
    target_dir: str,
    *,
    job: JobManifest,
    epoch: int,
    creds: KaggleCredentials,
    checkpoint: CheckpointManifest | None = None,
    inline_files: dict[str, bytes] | None = None,
    orca_link: str | None = None,
    predecessor_slug: str = "",
) -> str:
    """Writes a push-ready directory and returns the slug it targets."""
    os.makedirs(target_dir, exist_ok=True)
    slug = job.slug_for_epoch(epoch)

    if not predecessor_slug and epoch > 0:
        predecessor_slug = job.slug_for_epoch(epoch - 1)

    # A checkpoint's inline files come from the checkpoint itself, which the
    # producing window already encoded. On the server-driven path (a rollback,
    # or a replayed push) the files are not in this process's memory, so the
    # successor is told to fetch the whole bundle from the source window
    # instead. Marking transport explicitly here is what prevents the successor
    # from waiting for an inline payload that was never sent.
    if checkpoint is not None and not inline_files:
        for record in checkpoint.files:
            record.transport = "kaggle_output"
        if not predecessor_slug:
            predecessor_slug = checkpoint.source_kernel_slug

    header = build_header(
        job=job, epoch=epoch, creds=creds, inline_files=inline_files,
        checkpoint=checkpoint, predecessor_slug=predecessor_slug, orca_link=orca_link,
    )
    script = render_script(header)
    size = len(script.encode("utf-8"))
    if size > CONFIG.runner.max_kernel_source_bytes:
        raise PayloadTooLargeError(
            "the generated kernel source exceeds Kaggle's practical size limit. "
            "Put large attachments in a Kaggle Dataset and attach it to the job "
            "instead of shipping them inside the notebook.",
            size=size, limit=CONFIG.runner.max_kernel_source_bytes, slug=slug,
        )

    write_kernel_metadata(target_dir, owner=creds.username, slug=slug,
                          dataset_sources=job.dataset_sources)
    with open(os.path.join(target_dir, "script.py"), "w", encoding="utf-8") as fh:
        fh.write(script)

    log_event(log, "window_built", "assembled a push-ready kernel directory",
              job_id=job.job_id, epoch=epoch, slug=slug, script_bytes=size,
              has_checkpoint=checkpoint is not None,
              inline_files=len(inline_files or {}),
              predecessor=predecessor_slug or None)
    return slug
