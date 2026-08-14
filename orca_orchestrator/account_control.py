# -*- coding: utf-8 -*-
"""Account-level controls for Kaggle-backed ORCA jobs.

A Kaggle notebook chain may span several 12-hour windows.  The public limit is
therefore applied to *logical jobs*, not to individual ``-rN`` windows.
Stopping a logical job removes every known Kaggle window in that chain and then
marks the local manifest CANCELLED, preventing the watchdog from continuing it.
"""
from __future__ import annotations

import os
from collections import defaultdict

from . import ledger as ledger_mod
from .errors import OrchestratorError, RateLimitError
from .kaggle_api import KERNEL_ACTIVE_STATUSES, KaggleClient


def max_active_jobs_per_account() -> int:
    raw = os.environ.get("ORCA_MAX_ACTIVE_JOBS_PER_ACCOUNT", "5").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 5
    return max(1, value)


def _remote_chains(client: KaggleClient) -> dict[str, list[dict]]:
    return ledger_mod.group_chains(client.list_kernels())


def active_remote_job_ids(client: KaggleClient) -> set[str]:
    """Return logical jobs whose newest Kaggle window is queued or running."""
    active: set[str] = set()
    for job_id, windows in _remote_chains(client).items():
        if not windows:
            continue
        newest = max(windows, key=lambda item: int(item.get("epoch", 0)))
        try:
            status = client.status(newest["slug"])
        except OrchestratorError:
            # A transient status failure must not be treated as free capacity.
            # The caller can retry the submission after the normal API error.
            raise
        if status.status in KERNEL_ACTIVE_STATUSES:
            active.add(job_id)
    return active


def enforce_capacity(service, creds) -> int:
    """Raise 429 when the account already has the configured number of jobs.

    Local non-terminal manifests are included because a freshly accepted submit
    can exist between persistence and the first Kaggle status becoming visible.
    Remote chains cover restarts and browser changes where the local cache is
    incomplete.  A job is counted once even when it has many continuation
    windows.
    """
    client = KaggleClient(creds)
    active = active_remote_job_ids(client)
    for job in service.store.list_jobs(creds.username):
        if not job.is_terminal:
            active.add(job.job_id)
    limit = max_active_jobs_per_account()
    if len(active) >= limit:
        raise RateLimitError(
            "this Kaggle account already has the maximum number of active ORCA jobs",
            active_jobs=len(active), max_active_jobs=limit,
        )
    return len(active)


def stop_job_chain(service, creds, job_id: str) -> dict:
    """Stop all known Kaggle windows for one logical job.

    Kaggle's CLI does not expose a stable non-destructive cancel command across
    supported versions.  ``kernels delete`` is therefore used as the reliable
    hard-stop primitive: it terminates the remote notebook by removing the
    kernel.  The API returns exactly which windows were removed or failed.
    """
    client = KaggleClient(creds)
    slugs: set[str] = set()
    job = service.store.get_job(job_id)
    if job is not None:
        slugs.update(job.chain_slugs)
        if job.current_slug:
            slugs.add(job.current_slug)
    for window in _remote_chains(client).get(job_id, []):
        slugs.add(window["slug"])

    stopped, failed = [], []
    for slug in sorted(slugs):
        try:
            client.delete_kernel(slug)
            stopped.append(slug)
        except OrchestratorError as exc:
            failed.append({"slug": slug, "error": exc.code})

    local = None
    if job is not None:
        local = service.cancel(creds, job_id)
    return {"job_id": job_id, "stopped": stopped, "failed": failed, "job": local}


def stop_all_active(service, creds) -> dict:
    """Hard-stop every logical job belonging to the authenticated account."""
    client = KaggleClient(creds)
    chains = _remote_chains(client)
    results = []
    # Also include locally known jobs that may not have reached Kaggle yet.
    ids = set(chains)
    ids.update(job.job_id for job in service.store.list_jobs(creds.username) if not job.is_terminal)
    for job_id in sorted(ids):
        try:
            results.append(stop_job_chain(service, creds, job_id))
        except OrchestratorError as exc:
            results.append({"job_id": job_id, "stopped": [], "failed": [{"error": exc.code}]})
    return {"owner": creds.username, "results": results}
