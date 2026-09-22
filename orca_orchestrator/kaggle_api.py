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
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from typing import Sequence

from .config import CONFIG
from .credentials import KaggleCredentials, kaggle_environment
from .errors import (NotFoundError, OrchestratorError, SubmissionUnknownError,
                     TimeoutError_, TransientError, ValidationError)
from .logging_ext import get_logger, log_event, redact
from .retry import RetryPolicy, classify_subprocess_failure

log = get_logger("orca.kaggle")

_PUSH_URL_RE = re.compile(
    r"https?://(?:www\.)?kaggle\.com/(?:code/)?([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)"
)
_JOB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")

#: Kaggle status word -> our vocabulary.
_STRUCTURED_STATUS_MAP = {
    "NEW_SCRIPT": "queued",
    "QUEUED": "queued",
    "RUNNING": "running",
    "COMPLETE": "complete",
    "ERROR": "error",
    "CANCELLED": "cancelled",
    "CANCELED": "cancelled",
}

KERNEL_ACTIVE_STATUSES = frozenset({"running", "queued"})
KERNEL_STOPPED_STATUSES = frozenset({"complete", "error", "cancelled"})


def is_local_mode() -> bool:
    return bool(CONFIG.local_mode or os.environ.get("ORCA_LOCAL_MODE", "").lower() in ("true", "1", "yes"))


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
    # If Kaggle did not emit the structured status field, the remaining text
    # is not authoritative: titles, slugs and diagnostics can contain words
    # such as "error" or "running".  Treat it as unknown and let the
    # reconciler retry instead of inventing a terminal state.
    if not quoted:
        return "unknown"
    # Kaggle's value is an enum-like token (usually
    # ``KernelWorkerStatus.RUNNING``).  Match the final enum member exactly;
    # substring matching would misclassify values such as INCOMPLETE or an
    # error message embedded in a future status string.
    probe = quoted[-1].strip().rsplit(".", 1)[-1].upper()
    return _STRUCTURED_STATUS_MAP.get(probe, "unknown")


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

    @property
    def is_running(self) -> bool:
        return self.status == "running"

    @property
    def is_queued(self) -> bool:
        return self.status == "queued"

    @property
    def is_complete(self) -> bool:
        return self.status == "complete"

    @property
    def is_error(self) -> bool:
        return self.status == "error"


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
                import kaggle_runner as _kr
                _runner = getattr(_kr, "_run_kaggle_cli", None)
                _orig = getattr(_kr, "_ORIGINAL_RUN_CLI", None)
                if _runner is not None and callable(_runner) and _orig is not None and _runner is not _orig:
                    proc = _runner(list(args), env=env, timeout=timeout)
                else:
                    cmd = list(args)
                    if env is not None:
                        env["PYTHONIOENCODING"] = "utf-8"
                        env["PYTHONUTF8"] = "1"
                        env.setdefault("LC_ALL", "en_US.UTF-8")
                        env.setdefault("LANG", "en_US.UTF-8")

                    kwargs = {
                        "capture_output": True,
                        "text": True,
                        "env": env,
                        "timeout": timeout,
                        "encoding": "utf-8",
                        "errors": "replace",
                    }
                    if cmd and cmd[0] == "kaggle":
                        path = env.get("PATH") if isinstance(env, dict) else os.environ.get("PATH")
                        resolved = shutil.which("kaggle", path=path)
                        if resolved:
                            cmd[0] = resolved
                            if sys.platform == "win32" and resolved.lower().endswith((".bat", ".cmd")):
                                kwargs["shell"] = True
                        else:
                            cmd = [sys.executable, "-c", "import sys; from kaggle.cli import main; sys.argv = ['kaggle'] + sys.argv[1:]; sys.exit(main())"] + cmd[1:]
                    proc = subprocess.run(cmd, **kwargs)
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

    def _mock_dir(self, slug: str) -> str:
        base = os.environ.get("ORCA_STATE_DIR") or CONFIG.store.state_dir
        mock_root = os.path.join(base, "mock_kaggle", slug)
        os.makedirs(mock_root, exist_ok=True)
        return mock_root

    # -- push --------------------------------------------------------------
    def kernel_exists(self, slug: str) -> KernelStatus | None:
        """Probe used for duplicate-launch prevention. Returns None on 404."""
        if not is_valid_slug(slug):
            raise ValidationError("invalid kernel slug", slug=slug)
        if is_local_mode():
            meta_path = os.path.join(self._mock_dir(slug), "_mock_meta.json")
            if not os.path.isfile(meta_path):
                return None
            return self.status(slug)
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
        """Pushes a kernel directory, idempotently."""
        if not is_valid_slug(expected_slug):
            raise ValidationError("invalid kernel slug", slug=expected_slug)
        metadata_path = os.path.join(job_dir, "kernel-metadata.json")
        if not os.path.isfile(metadata_path):
            raise ValidationError("kernel-metadata.json is missing from the push directory",
                                  job_dir=job_dir)

        if is_local_mode():
            target_dir = self._mock_dir(expected_slug)
            for f in os.listdir(job_dir):
                s_path = os.path.join(job_dir, f)
                d_path = os.path.join(target_dir, f)
                if os.path.isfile(s_path):
                    shutil.copy2(s_path, d_path)
            meta = {
                "status": "queued",
                "pushed_at": time.time(),
                "slug": expected_slug,
                "owner": self.creds.username,
                "url": f"http://localhost:7860/code/{self.creds.username}/{expected_slug}",
            }
            with open(os.path.join(target_dir, "_mock_meta.json"), "w", encoding="utf-8") as fh:
                json.dump(meta, fh)
            return PushResult(
                slug=expected_slug, owner=self.creds.username,
                url=meta["url"], requested_slug=expected_slug,
                raw_output="mock push success",
            )

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
            if "error" in (proc.stdout or "").lower() and "successfully" not in (proc.stdout or "").lower():
                raise classify_subprocess_failure(1, combined)
            return combined

        # A kernel push is NOT idempotent across replays: a CLI that dies
        # reading the response AFTER Kaggle accepted the version leaves the
        # push landed, and a blind retry saves a SECOND version, which Kaggle
        # then executes as another run of this window. Verification therefore
        # sits BETWEEN attempts: only a probe that finds no active kernel at
        # the deterministic slug permits the next push.
        combined = None
        started = time.monotonic()
        for number in range(1, self.retry.max_attempts + 1):
            try:
                combined = attempt(number)
                break
            except TransientError as exc:
                try:
                    existing = self.kernel_exists(expected_slug)
                except OrchestratorError as probe_exc:
                    raise SubmissionUnknownError(
                        "the Kaggle push outcome is unknown; refusing a blind retry",
                        slug=expected_slug,
                        push_error=type(exc).__name__,
                        probe_error=type(probe_exc).__name__,
                    ) from exc
                if existing is not None:
                    log_event(log, "push_accepted_despite_error",
                              "the push attempt failed at transport level, but the "
                              "kernel exists at the deterministic "
                              "slug; treating the push as landed instead of pushing a "
                              "duplicate version",
                              slug=expected_slug, kaggle_status=existing.status)
                    combined = "verified existing kernel after transport error"
                    break
                if number >= self.retry.max_attempts:
                    raise
                delay = self.retry.delay_for(
                    number, retry_after=getattr(exc, "retry_after", None))
                if (self.retry.deadline_seconds is not None
                        and (time.monotonic() - started) + delay
                        >= self.retry.deadline_seconds):
                    raise
                log_event(log, "push_retry",
                          "attempt %d/%d failed transiently and the slug is not "
                          "active; pushing again" % (number, self.retry.max_attempts),
                          slug=expected_slug, delay_seconds=round(delay, 2))
                time.sleep(delay)
        assert combined is not None  # the loop either returns output or raises

        match = _PUSH_URL_RE.search(combined)
        owner = (match.group(1) if match else self.creds.username).lower()
        slug = match.group(2) if match else expected_slug

        if slug != expected_slug:
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
        if is_local_mode():
            meta_path = os.path.join(self._mock_dir(slug), "_mock_meta.json")
            if not os.path.isfile(meta_path):
                raise NotFoundError("mock kernel not found", slug=slug)
            try:
                with open(meta_path, "r", encoding="utf-8") as fh:
                    meta = json.load(fh)
            except Exception:
                meta = {"status": "queued", "pushed_at": time.time()}
            elapsed = time.time() - float(meta.get("pushed_at", time.time()))
            target_dir = self._mock_dir(slug)
            if elapsed < 1.0:
                cur_status = "queued"
            elif elapsed < 2.5:
                cur_status = "running"
                hb_path = os.path.join(target_dir, "HEARTBEAT.json")
                if not os.path.exists(hb_path):
                    with open(hb_path, "w", encoding="utf-8") as fh:
                        json.dump({"at": time.time(), "epoch": 0, "run_token": "mock", "detail": {"opt_step": 1}}, fh)
            else:
                cur_status = "complete"
                zip_path = os.path.join(target_dir, "results.zip")
                state_path = os.path.join(target_dir, "STATE.json")
                if not os.path.exists(zip_path):
                    out_text = (
                        "================================================================================\n"
                        "                                ORCA 5.0.4\n"
                        "================================================================================\n"
                        "FINAL SINGLE POINT ENERGY      -76.432100\n"
                        "CARTESIAN COORDINATES (ANGSTROEM)\n"
                        "---------------------------------\n"
                        "  O      0.000000    0.000000    0.117200\n"
                        "  H      0.000000    0.757000   -0.468800\n"
                        "  H      0.000000   -0.757000   -0.468800\n"
                        "---------------------------------\n"
                        "*** OPTIMIZATION RUN DONE ***\n"
                        "VIBRATIONAL FREQUENCIES\n"
                        "-----------------------\n"
                        "   1:      1595.00 cm**-1\n"
                        "   2:      3657.00 cm**-1\n"
                        "   3:      3756.00 cm**-1\n"
                        "****ORCA TERMINATED NORMALLY****\n"
                    )
                    xyz_text = "3\n\nO 0.000000 0.000000 0.117200\nH 0.000000 0.757000 -0.468800\nH 0.000000 -0.757000 -0.468800\n"
                    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                        zf.writestr("molecule.out", out_text)
                        zf.writestr("molecule.xyz", xyz_text)
                    with open(state_path, "w", encoding="utf-8") as fh:
                        json.dump({"job": {"job_id": slug, "state": "FINISHED", "epoch": 0, "last_note": "Mock calculation finished"}}, fh)
            meta["status"] = cur_status
            with open(meta_path, "w", encoding="utf-8") as fh:
                json.dump(meta, fh)
            return KernelStatus(slug=slug, status=cur_status, raw=f'{self.creds.username}/{slug} has status "KernelWorkerStatus.{cur_status.upper()}"')

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
        """Downloads kernel output into a NEW temp directory and returns it."""
        if not is_valid_slug(slug):
            raise ValidationError("invalid kernel slug", slug=slug)
        if is_local_mode():
            self.status(slug)
            out_dir = tempfile.mkdtemp(prefix="mock-kaggle-out-")
            src_dir = self._mock_dir(slug)
            if not os.path.isdir(src_dir):
                raise NotFoundError("mock kernel output not found", slug=slug)
            pat = re.compile(file_pattern, re.IGNORECASE) if file_pattern else None
            for item in os.listdir(src_dir):
                if item.startswith("_"):
                    continue
                if pat and not pat.search(item):
                    continue
                s_path = os.path.join(src_dir, item)
                if os.path.isfile(s_path):
                    shutil.copy2(s_path, os.path.join(out_dir, item))
            return out_dir

        ref = f"{self.creds.username}/{slug}"
        out_dir = tempfile.mkdtemp(prefix="kaggle-out-")

        args = ["kaggle", "kernels", "output", ref, "-p", out_dir]
        if file_pattern:
            args += ["--file-pattern", file_pattern]

        def attempt(_a):
            for entry in os.listdir(out_dir):
                path = os.path.join(out_dir, entry)
                shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.remove(path)
            proc = self._run(args, timeout=timeout, operation="kernels_output", slug=slug, allow_nonzero=True)
            if proc.returncode != 0:
                # A non-zero exit after creating a few files is a torn
                # download, not a successful result.  Accepting those files
                # used to let archive fallback package an incomplete result
                # and mark it durable.  RetryPolicy will clear this directory
                # before the next attempt; only a zero exit is authoritative.
                raise classify_subprocess_failure(proc.returncode, proc.combined)
            return out_dir

        try:
            return self.retry.call("kaggle kernels output", attempt, slug=slug)
        except BaseException:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise

    def fetch_ledger_files(self, slug: str) -> dict[str, bytes]:
        """Fetches only the small control-plane files."""
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
        """Lists this account's kernels."""
        prefix = prefix or CONFIG.job_id_prefix
        if is_local_mode():
            base = os.environ.get("ORCA_STATE_DIR") or CONFIG.store.state_dir
            mock_root = os.path.join(base, "mock_kaggle")
            rows = []
            if os.path.isdir(mock_root):
                for slug in os.listdir(mock_root):
                    if slug.startswith(prefix):
                        rows.append({
                            "slug": slug,
                            "owner": self.creds.username,
                            "ref": f"{self.creds.username}/{slug}",
                            "url": f"http://localhost:7860/code/{self.creds.username}/{slug}",
                            "title": slug,
                            "last_run": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                        })
            return rows

        rows: list[dict] = []
        page = 1
        max_pages = 20
        while page <= max_pages:
            def attempt(_a, p=page):
                cmd = ["kaggle", "kernels", "list", "--mine", "--csv",
                       "--page-size", str(int(page_size)), "--page", str(p)]
                proc = self._run(cmd, timeout=90, operation="kernels_list")
                return proc.stdout or ""

            stdout = self.retry.call("kaggle kernels list", attempt)
            reader = list(csv.DictReader(io.StringIO(stdout)))
            if not reader:
                break
            for row in reader:
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
            if len(reader) < page_size:
                break
            page += 1
        return rows

    # -- delete ------------------------------------------------------------
    def delete_kernel(self, slug: str) -> bool:
        """Idempotent by intent: 'already gone' is the outcome the caller wanted."""
        if not is_valid_slug(slug):
            raise ValidationError("invalid kernel slug", slug=slug)
        if is_local_mode():
            target_dir = self._mock_dir(slug)
            shutil.rmtree(target_dir, ignore_errors=True)
            log_event(log, "kernel_deleted", "mock kernel removed", slug=slug)
            return True
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
