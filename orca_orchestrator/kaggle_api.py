# -*- coding: utf-8 -*-
"""
Idempotent adapter over the `kaggle` CLI.

Every method here is safe to call twice. That is not incidental -- it is the
property the whole recovery design rests on. A worker that crashes mid-action
is recovered by simply replaying the action, and replay is only safe because
each operation is either naturally idempotent or made so here:

  * `push_kernel`  -- the slug is deterministic (`<base>` / `<base>-r<epoch>`),
    so pushing twice creates one kernel with two versions rather than two
    kernels. Before pushing, a status probe detects that a *running* instance
    already exists, which is what prevents the genuinely harmful case: two
    concurrent runs of the same epoch writing to the same output directory.
  * `fetch_output` -- downloads into a fresh temp dir every time and never
    resumes into an existing file, so a torn download cannot be silently
    stitched onto a previous one.
  * `delete_kernel` -- treats 404 as success, because the caller's intent is
    "make it not exist".

Timeouts are mandatory on every invocation. A `kaggle` call that hangs inside a
gunicorn worker consumes that worker until the request timeout, and with two
workers it takes exactly two hung calls to make the entire site unresponsive.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Sequence

from .config import CONFIG
from .credentials import KaggleCredentials, kaggle_environment
from .errors import (NotFoundError, OrchestratorError, TimeoutError_, ValidationError)
from .logging_ext import get_logger, log_event, redact
from .retry import RetryPolicy, classify_subprocess_failure

log = get_logger("orca.kaggle")

_PUSH_URL_RE = re.compile(
    r"https?://(?:www\.)?kaggle\.com/(?:code/)?([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)"
)
_JOB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")

#: Kaggle status word -> our vocabulary.
_STATUS_WORDS = (
    ("complete", "complete"),
    ("error", "error"),
    ("running", "running"),
    ("queued", "queued"),
    ("queue", "queued"),
    ("cancel", "cancelled"),
    ("new_script", "queued"),
)

KERNEL_ACTIVE_STATUSES = frozenset({"running", "queued"})
KERNEL_STOPPED_STATUSES = frozenset({"complete", "error", "cancelled"})


def is_valid_slug(slug: str) -> bool:
    """Guards every value that reaches the CLI as an argument. The slug comes
    from a browser, so this is a security boundary, not a convenience check."""
    return bool(_JOB_ID_RE.match((slug or "").strip()))


def classify_status(text: str) -> str:
    """`kaggle kernels status` prints:
        `owner/slug has status "KernelWorkerStatus.COMPLETE"`

    Only the quoted value is examined. Matching against the whole line lets the
    *slug* decide the status -- a notebook called `chem-tools-error-test` would
    read as an errored job forever."""
    quoted = re.findall(r'status\s+"([^"]+)"', text or "", flags=re.IGNORECASE)
    probe = (quoted[-1] if quoted else (text or "")).lower()
    for needle, status in _STATUS_WORDS:
        if needle in probe:
            return status
    return "unknown"


@dataclass
class PushResult:
    slug: str
    owner: str
    url: str
    requested_slug: str
    raw_output: str = ""

    @property
    def slug_matches_request(self) -> bool:
        return self.slug == self.requested_slug


@dataclass
class KernelStatus:
    slug: str
    status: str
    raw: str = ""

    @property
    def is_active(self) -> bool:
        return self.status in KERNEL_ACTIVE_STATUSES

    @property
    def is_stopped(self) -> bool:
        return self.status in KERNEL_STOPPED_STATUSES


class KaggleClient:
    def __init__(self, creds: KaggleCredentials, *, retry: RetryPolicy | None = None) -> None:
        if not creds.is_valid:
            raise ValidationError("incomplete Kaggle credentials")
        self.creds = creds
        self.retry = retry or RetryPolicy()

    # -- low level ---------------------------------------------------------
    def _run(self, args: Sequence[str], *, timeout: float, operation: str,
             allow_nonzero: bool = False, **log_fields) -> subprocess.CompletedProcess:
        """One CLI invocation, with a typed failure and a hard timeout.

        Retries live in the caller so that the retry *policy* -- attempt count,
        deadline, whether a replay is safe at all -- is a decision made with
        knowledge of the operation, not a blanket rule applied to every call."""
        with kaggle_environment(self.creds) as env:
            try:
                proc = subprocess.run(
                    list(args), capture_output=True, text=True, env=env, timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError_(
                    f"the kaggle CLI did not return within {timeout:.0f}s",
                    operation=operation, timeout=timeout, **log_fields,
                ) from exc

        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if proc.returncode != 0 and not allow_nonzero:
            raise classify_subprocess_failure(proc.returncode, combined)
        proc.combined = combined  # type: ignore[attr-defined]
        return proc

    # -- push --------------------------------------------------------------
    def kernel_exists(self, slug: str) -> KernelStatus | None:
        """Probe used for duplicate-launch prevention. Returns None on 404."""
        if not is_valid_slug(slug):
            raise ValidationError("invalid kernel slug", slug=slug)
        ref = f"{self.creds.username}/{slug}"
        try:
            proc = self._run(["kaggle", "kernels", "status", ref],
                             timeout=45, operation="kernels_status",
                             allow_nonzero=True, slug=slug)
        except OrchestratorError:
            raise
        combined = proc.combined  # type: ignore[attr-defined]
        if proc.returncode != 0:
            error = classify_subprocess_failure(proc.returncode, combined)
            if isinstance(error, NotFoundError):
                return None
            raise error
        return KernelStatus(slug=slug, status=classify_status(combined), raw=combined.strip())

    def push_kernel(self, job_dir: str, *, expected_slug: str,
                    skip_if_active: bool = True) -> PushResult:
        """Pushes a kernel directory, idempotently.

        `skip_if_active` is the duplicate-launch guard. Pushing a new version of
        a kernel that is *already running* makes Kaggle schedule a second run,
        and both runs then write to the same `/kaggle/working`. That is a
        genuine data race between two ORCA processes, and it is the most
        damaging duplicate this system can produce, so it is checked for
        explicitly rather than being left to chance.
        """
        if not is_valid_slug(expected_slug):
            raise ValidationError("invalid kernel slug", slug=expected_slug)
        metadata_path = os.path.join(job_dir, "kernel-metadata.json")
        if not os.path.isfile(metadata_path):
            raise ValidationError("kernel-metadata.json is missing from the push directory",
                                  job_dir=job_dir)

        if skip_if_active:
            existing = self.kernel_exists(expected_slug)
            if existing is not None and existing.is_active:
                log_event(log, "push_skipped_already_active",
                          "a run of this exact epoch is already active on Kaggle; "
                          "not pushing a second one",
                          slug=expected_slug, kaggle_status=existing.status)
                return PushResult(
                    slug=expected_slug, owner=self.creds.username,
                    url=f"https://www.kaggle.com/code/{self.creds.username}/{expected_slug}",
                    requested_slug=expected_slug,
                    raw_output="skipped: an active run already exists",
                )

        def attempt(_a):
            proc = self._run(["kaggle", "kernels", "push", "-p", job_dir],
                             timeout=240, operation="kernels_push", slug=expected_slug)
            combined = proc.combined  # type: ignore[attr-defined]
            # The CLI exits 0 while printing an error in some versions, so the
            # exit code alone is not a sufficient success signal.
            if "error" in (proc.stdout or "").lower() and "successfully" not in (proc.stdout or "").lower():
                raise classify_subprocess_failure(1, combined)
            return combined

        combined = self.retry.call("kaggle kernels push", attempt, slug=expected_slug)

        match = _PUSH_URL_RE.search(combined)
        owner = (match.group(1) if match else self.creds.username).lower()
        slug = match.group(2) if match else expected_slug

        if slug != expected_slug:
            # Kaggle derives the slug from the title when the two disagree. The
            # notebook then exists at an address nobody is polling, so every
            # later status check, download and delete targets something that is
            # not there. Following the URL Kaggle actually reported is the only
            # safe response.
            log_event(log, "push_slug_diverged",
                      "Kaggle created the kernel under a different slug than requested; "
                      "following the address Kaggle reported",
                      requested=expected_slug, actual=slug)

        result = PushResult(slug=slug, owner=owner,
                            url=f"https://www.kaggle.com/code/{owner}/{slug}",
                            requested_slug=expected_slug, raw_output=redact(combined[-800:]))
        log_event(log, "kernel_pushed", "kernel accepted by Kaggle",
                  slug=result.slug, url=result.url, diverged=not result.slug_matches_request)
        return result

    # -- status ------------------------------------------------------------
    def status(self, slug: str) -> KernelStatus:
        if not is_valid_slug(slug):
            raise ValidationError("invalid kernel slug", slug=slug)
        ref = f"{self.creds.username}/{slug}"

        def attempt(_a):
            proc = self._run(["kaggle", "kernels", "status", ref],
                             timeout=45, operation="kernels_status", slug=slug)
            return proc.combined  # type: ignore[attr-defined]

        combined = self.retry.call("kaggle kernels status", attempt, slug=slug)
        return KernelStatus(slug=slug, status=classify_status(combined), raw=combined.strip())

    # -- output ------------------------------------------------------------
    def fetch_output(self, slug: str, *, file_pattern: str | None = None,
                     timeout: float = 600.0, page_size: int = 200) -> str:
        """Downloads kernel output into a NEW temp directory and returns it.

        Always a fresh directory: reusing one would let a previous, possibly
        partial, download masquerade as this one's result -- exactly the
        'interrupted download' failure the brief calls out. The caller owns the
        directory and must remove it.
        """
        if not is_valid_slug(slug):
            raise ValidationError("invalid kernel slug", slug=slug)
        ref = f"{self.creds.username}/{slug}"
        out_dir = tempfile.mkdtemp(prefix="kaggle-out-")

        args = ["kaggle", "kernels", "output", ref, "-p", out_dir,
                "--page-size", str(int(page_size))]
        if file_pattern:
            args += ["--file-pattern", file_pattern]

        def attempt(_a):
            # Each retry starts from an empty directory so a partial transfer
            # from a failed attempt can never be mistaken for a complete one.
            for entry in os.listdir(out_dir):
                path = os.path.join(out_dir, entry)
                shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.remove(path)
            self._run(args, timeout=timeout, operation="kernels_output", slug=slug)
            return out_dir

        try:
            return self.retry.call("kaggle kernels output", attempt, slug=slug)
        except BaseException:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise

    def fetch_ledger_files(self, slug: str) -> dict[str, bytes]:
        """Fetches only the small control-plane files.

        Downloading a job's full output on every status poll is what makes a
        finished job look stuck: a real results bundle runs to hundreds of
        megabytes, the request blows past the web server's timeout, and from
        the browser's side that is indistinguishable from a job that never
        finished. The control files are a few kilobytes."""
        pattern = r"(STATE|CHECKPOINT|HEARTBEAT|NEXT_JOB_ID|NEXT_JOB_URL|JOB_NOTE)\.(json|txt)$"
        out_dir = self.fetch_output(slug, file_pattern=pattern, timeout=90, page_size=50)
        try:
            files: dict[str, bytes] = {}
            for name in os.listdir(out_dir):
                path = os.path.join(out_dir, name)
                if os.path.isfile(path) and os.path.getsize(path) <= 4 << 20:
                    with open(path, "rb") as fh:
                        files[name] = fh.read()
            return files
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    # -- listing -----------------------------------------------------------
    def list_kernels(self, prefix: str | None = None, page_size: int = 100) -> list[dict]:
        """Lists this account's kernels.

        `--mine` already scopes to the authenticated account; the explicit
        owner check below is a second, independent confirmation rather than a
        substitute for it. The prefix deliberately contains no username: an
        earlier design embedded one, sanitised differently on each side, so any
        username containing a hyphen produced two strings that never matched
        and the job list came back empty -- which looks exactly like data loss.
        """
        prefix = prefix or CONFIG.job_id_prefix

        def attempt(_a):
            proc = self._run(
                ["kaggle", "kernels", "list", "--mine", "--csv",
                 "--page-size", str(int(page_size))],
                timeout=90, operation="kernels_list",
            )
            return proc.stdout or ""

        stdout = self.retry.call("kaggle kernels list", attempt)

        rows: list[dict] = []
        for row in csv.DictReader(io.StringIO(stdout)):
            ref = (row.get("ref") or "").strip()
            if "/" not in ref:
                continue
            owner, slug = ref.split("/", 1)
            if owner.strip().lower() != self.creds.username or not slug.startswith(prefix):
                continue
            rows.append({
                "slug": slug,
                "owner": owner.strip().lower(),
                "ref": ref,
                "url": f"https://www.kaggle.com/code/{ref}",
                "title": (row.get("title") or "").strip(),
                "last_run": (row.get("lastRunTime") or "").strip(),
            })
        return rows

    # -- delete ------------------------------------------------------------
    def delete_kernel(self, slug: str) -> bool:
        """Idempotent by intent: 'already gone' is the outcome the caller
        wanted, so a duplicate delete is a success, not a confusing error."""
        if not is_valid_slug(slug):
            raise ValidationError("invalid kernel slug", slug=slug)
        ref = f"{self.creds.username}/{slug}"
        proc = self._run(["kaggle", "kernels", "delete", ref, "--yes"],
                         timeout=60, operation="kernels_delete",
                         allow_nonzero=True, slug=slug)
        if proc.returncode == 0:
            log_event(log, "kernel_deleted", "kernel removed from Kaggle", slug=slug)
            return True
        error = classify_subprocess_failure(proc.returncode, proc.combined)  # type: ignore[attr-defined]
        if isinstance(error, NotFoundError):
            log_event(log, "kernel_delete_noop", "kernel was already gone", slug=slug)
            return True
        raise error


def write_kernel_metadata(job_dir: str, *, owner: str, slug: str,
                          dataset_sources: list[str] | None = None,
                          enable_internet: bool = True) -> str:
    """Writes `kernel-metadata.json`.

    `title` is set identical to the slug on purpose. When the two disagree,
    Kaggle derives the slug from the title, the CLI warns about it, and the
    notebook is created at an address the caller is not tracking. Making them
    the same string removes that failure mode for every job name in every
    script, including ones that lose characters to slug sanitisation."""
    if not is_valid_slug(slug):
        raise ValidationError("invalid kernel slug", slug=slug)
    metadata = {
        "id": f"{owner.lower()}/{slug}",
        "title": slug,
        "code_file": "script.py",
        "language": "python",
        "kernel_type": "script",
        "is_private": True,
        "enable_gpu": False,
        # Required so the kernel can push its own successor. If the account is
        # not phone-verified, Kaggle accepts the push but the run fails with no
        # network -- surfaced explicitly by the runner's preflight check.
        "enable_internet": bool(enable_internet),
        "dataset_sources": list(dataset_sources or []),
        "competition_sources": [],
        "kernel_sources": [],
    }
    path = os.path.join(job_dir, "kernel-metadata.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2)
    return path
