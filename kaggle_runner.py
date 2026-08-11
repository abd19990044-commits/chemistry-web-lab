# -*- coding: utf-8 -*-
"""
kaggle_runner.py
================
Packages and pushes an ORCA job to Kaggle, reusing the auto-restart design
from the original ORCA Telegram bot but triggered from a web form instead
of a chat command.

Licensing note: ORCA is proprietary academic software. This site never
distributes the ORCA binary. The user must supply their own licensed ORCA
package (downloaded directly from the official ORCA forum after their own
registration), either as:
  (a) a private Kaggle Dataset attached to the kernel, or
  (b) a direct download / Google Drive link, which the kernel downloads
      and extracts into /tmp at job start (same approach as the original
      Telegram bot's Kaggle script).

Kaggle authentication note: Kaggle's Settings -> API page now issues a
single "API token" by default (looks like 'KGAT_xxxxxxxx...'); the old
username+key pair is still available under "Legacy API Credentials" on
that same page. This module accepts *either* — whatever the person pastes
into the "API key / token" field is auto-detected and used correctly, so a
person who copies the new-style token no longer gets a silent 401.

Job lifecycle exposed to the website:
  1. build_job_dir() + push_job()      -> submit the first kernel
  2. list_jobs()                       -> called on Kaggle sign-in; asks
                                           Kaggle for every kernel this site
                                           has ever created under that
                                           account, so the job list survives
                                           clearing browser data or moving
                                           to a new device/browser.
  3. check_job_status()                -> polled periodically by the browser
       - "running" / "queued"          -> still working
       - "restarting"                  -> hit the session time limit and
                                           auto-pushed a continuation kernel
                                           (next_job_id is returned so the
                                           frontend can keep following it)
       - "complete"                    -> finished; call fetch_job_results()
                                           to stream the zipped output back
       - "error"                       -> something went wrong; see `note`
  4. fetch_job_results()               -> called on demand (e.g. when the
                                           person clicks "Download results"),
                                           fetches the kernel's own output
                                           fresh via `kaggle kernels output`.
                                           No third-party upload host is
                                           involved — that used to be
                                           file.io, which occasionally
                                           returned empty/non-JSON responses
                                           and broke the download silently.
  5. delete_job()                      -> called when the person deletes a
                                           job from "My Jobs"; permanently
                                           removes the kernel from Kaggle
                                           (`kaggle kernels delete`) so it
                                           cannot reappear via list_jobs()
                                           the next time this account signs
                                           back in.
"""
from __future__ import annotations

import base64
import glob
import gzip
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from contextlib import contextmanager


TIME_LIMIT_DEFAULT = 41400      # 11h30m
MAX_RESTARTS_DEFAULT = 20

# How many times a single job may be restarted specifically because the Kaggle
# scratch disk filled up (separate budget from MAX_RESTARTS, which counts every
# session window). A calculation that needs more temporary space than a session
# can offer must eventually stop with advice instead of looping forever.
MAX_DISK_RESTARTS_DEFAULT = 6

# The in-kernel watchdog stops-and-continues the run when free scratch space
# falls below this. ORCA writes its scratch in ~GB-sized chunks, so the floor
# has to leave room for the file being written plus the restart hand-off.
MIN_FREE_GB_DEFAULT = 5.0

# Upper bound on what the finished job packs into results.zip. Kaggle caps a
# notebook's saved output at 20 GB; anything above this budget is listed in
# MANIFEST.txt as left out rather than silently breaking the whole output.
RESULT_BUDGET_GB_DEFAULT = 10.0

# Every job slug this site creates starts with this, which is what makes
# "sign in from another browser and get my job list back" possible.
JOB_ID_PREFIX = "chem-tools-"

# The plain `NAME = value` assignments prepended to the in-kernel script. The
# same tuple is mirrored inside KAGGLE_RUNNER_BODY (as HEADER_VARS) so a
# continuation kernel writes exactly the same header for its own successor;
# keeping one list on each side is what stops the two from drifting apart.
HEADER_VARS = (
    "ENCODED_FILES_JSON", "INPUT_FILE", "KAGGLE_USERNAME", "KAGGLE_KEY",
    "KAGGLE_API_TOKEN", "JOB_BASE_ID", "JOB_TITLE", "DATASET_SOURCES", "ORCA_LINK",
    "RESTART_COUNT", "MAX_RESTARTS", "DISK_RESTART_COUNT", "MAX_DISK_RESTARTS",
    "TIME_LIMIT", "MIN_FREE_GB", "RESULT_BUDGET_GB",
    "GEOM_MAXITER", "SCF_MAXITER", "SCAN_TOTAL_POINTS", "SCAN_POINTS_BEFORE",
    "HISTORY_B64", "STATIC_BODY_B64",
)

# Kaggle's own documented hard cap for a notebook session: 12h for CPU/GPU
# (https://www.kaggle.com/docs/notebooks#technical-specifications). This is
# deliberately a *separate* constant from TIME_LIMIT_DEFAULT above — that
# one is just the point at which *we* choose to trigger a self-restart,
# comfortably earlier than this real platform cutoff, and it's the
# caller-configurable `time_limit` build_job_dir() accepts. Keeping the
# real hard limit as its own named constant means the in-kernel restart
# retry loop (see the mirrored HARD_SESSION_LIMIT_SECONDS in
# KAGGLE_RUNNER_BODY below) always has an accurate reference for "how much
# runway is really left", independent of whatever time_limit a given job
# was configured with.
KAGGLE_HARD_SESSION_LIMIT_SECONDS = 43200


_TRANSIENT_KAGGLE_ERROR_MARKERS = (
    # Server-side throttling and gateway errors are the failures a shared Space
    # hits most, and treating them as permanent is what turned one busy minute
    # on Kaggle's side into a job the browser stopped polling.
    "429", "too many requests", "rate limit", "quota exceeded",
    "500", "502", "503", "504", "service unavailable", "bad gateway",
    "gateway timeout", "internal server error",
    "sslerror", "eof occurred", "max retries exceeded", "connection reset",
    "connection aborted", "remote end closed", "read timed out",
    "connection refused", "temporary failure in name resolution",
    "bad handshake", "connection broken",
)


def _looks_transient(text: str) -> bool:
    """True if `text` (combined stdout+stderr from a `kaggle` CLI call)
    looks like a network/SSL-level hiccup rather than a real, retry-proof
    failure (bad credentials, a validation error, a genuine 404, ...).
    This kind of transient failure is confirmed to happen in practice
    against api.kaggle.com — an SSLEOFError right as a 12-hour session
    wraps up is exactly what silently killed an entire auto-restart chain
    before this retry logic existed (see the loop in KAGGLE_RUNNER_BODY)."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _TRANSIENT_KAGGLE_ERROR_MARKERS)


def _run_cli_cmd(args, **kwargs):
    cmd = list(args)
    if sys.platform == "win32":
        env = kwargs.get("env")
        path = env.get("PATH") if isinstance(env, dict) else os.environ.get("PATH")
        resolved = shutil.which(cmd[0], path=path)
        if resolved:
            cmd[0] = resolved
            if resolved.lower().endswith((".bat", ".cmd")):
                kwargs["shell"] = True
    return subprocess.run(cmd, **kwargs)


def _run_kaggle_cli(args, *, env=None, timeout=60, attempts=4, base_delay=3.0):
    """subprocess.run for a `kaggle` CLI invocation, with automatic retries
    when a failure looks transient (see _looks_transient). A clean
    non-zero exit that ISN'T a transport-level error (bad auth, a 404, a
    validation error) is returned on the very first attempt without
    retrying, since retrying wouldn't change that outcome and would only
    delay a response someone may be actively waiting on (e.g. mid
    form-submit).

    Mirrors subprocess.run's contract: returns the CompletedProcess from
    the last attempt made. Only raises if every attempt times out."""
    result = None
    for attempt in range(1, attempts + 1):
        try:
            result = _run_cli_cmd(args, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            if attempt == attempts:
                raise
            time.sleep(base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1))
            continue
        if result.returncode == 0:
            return result
        combined = (result.stdout or "") + (result.stderr or "")
        if not _looks_transient(combined) or attempt == attempts:
            return result
        time.sleep(base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1))
    return result


#: Output that means the CLI itself is broken or absent, not that the
#: credentials were rejected. Telling someone their sign-in failed when the
#: server has no working `kaggle` command sends them to regenerate their API
#: token -- which does not help, and which kills every job already running,
#: because each running kernel carries the old token to push its successor.
_CLI_BROKEN_MARKERS = (
    "modulenotfounderror", "no module named", "traceback (most recent call last)",
    "command not found", "no such file or directory", "importerror",
    "cannot import name", "is not recognized as an internal or external command",
)


#: Output that means kaggle.com could not be reached at all. Distinct from a
#: rejected credential for the same reason as above: the remedy is different,
#: and "check your API key" is actively harmful advice when the key is fine.
_UNREACHABLE_MARKERS = (
    "max retries exceeded", "connection refused", "name or service not known",
    "temporary failure in name resolution", "network is unreachable",
    "failed to establish a new connection", "connectionerror", "connection aborted",
    "read timed out", "connect timeout", "nodename nor servname",
)


class KaggleCliUnavailable(RuntimeError):
    """The `kaggle` command is missing or broken on this server."""


class KaggleUnreachable(RuntimeError):
    """kaggle.com could not be reached from this server."""


def _raise_if_unreachable(text: str) -> None:
    low = (text or "").lower()
    if any(marker in low for marker in _UNREACHABLE_MARKERS):
        raise KaggleUnreachable(
            "kaggle.com could not be reached from this server, so your credentials were "
            "never checked. Your username and API key are probably fine — this is a "
            "network problem at the site's end. Please do not regenerate your token; that "
            "would also stop any jobs you have running. Try again in a few minutes.")


def cli_health() -> dict:
    """Is the Kaggle CLI actually usable? Runs it rather than looking for a file.

    `shutil.which` finds the wrapper script even when the package behind it is
    not importable, which is exactly the state a failed image build leaves -- so
    the file test reported healthy while every call raised."""
    path = shutil.which("kaggle")
    if not path:
        return {"ok": False, "path": None,
                "detail": "the `kaggle` command is not on PATH"}
    try:
        proc = subprocess.run(["kaggle", "--version"], capture_output=True,
                              text=True, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "path": path, "detail": "could not be run: %s" % exc}
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0 or _looks_like_broken_cli(output):
        return {"ok": False, "path": path,
                "detail": output.splitlines()[-1][:200] if output else "exited non-zero"}
    return {"ok": True, "path": path, "version": output.splitlines()[-1][:80]}


def _looks_like_broken_cli(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in _CLI_BROKEN_MARKERS)


def _raise_if_cli_broken(text: str) -> None:
    if _looks_like_broken_cli(text):
        raise KaggleCliUnavailable(
            "The Kaggle command-line tool is not installed correctly on this server, so no "
            "request could be sent to Kaggle at all. This is a problem with the site, not "
            "with your username or API key — please do not regenerate your token, which "
            "would also stop any jobs you already have running. Report this to whoever "
            "deploys the site; /health shows the same diagnosis.")


def _clean_credential(value: str) -> str:
    """Strips whitespace and accidental surrounding quotes — a common
    source of silent 401s when a value is copy-pasted from a JSON file."""
    return (value or "").strip().strip('"').strip("'").strip()


def clean_kaggle_credentials(username: str, key: str) -> tuple[str, str]:
    """Defends against the most common paste mistake: dropping the whole
    downloaded kaggle.json (e.g. {"username":"foo","key":"bar"}) into one
    of the two fields instead of just its value.

    The username is also lower-cased. Kaggle account usernames are always
    lowercase, so this guarantees sign-in, job submission, and job lookup
    all key off the exact same string even if someone types or pastes it
    with different capitalization on different occasions/devices (e.g. a
    phone's autocapitalize) — otherwise the exact-string ownership check in
    list_jobs()/delete_job() would silently treat it as a different account
    and simply find nothing, which looks identical to "my jobs disappeared"."""
    username = _clean_credential(username)
    key = _clean_credential(key)
    for candidate in (key, username):
        if candidate.startswith("{"):
            try:
                obj = json.loads(candidate)
                if "username" in obj and "key" in obj:
                    return _clean_credential(obj["username"]).lower(), _clean_credential(obj["key"])
            except Exception:  # noqa: BLE001
                pass
    return username.lower(), key


_LEGACY_KEY_RE = re.compile(r"^[0-9a-f]{32}$")


def _looks_like_new_api_token(value: str) -> bool:
    """Kaggle's legacy API key has always been a 32-character lowercase hex
    string (e.g. '1567b3980e493ca3640f3400530c55a3'). The new single API
    token issued by default from Settings -> API has a different, longer
    shape (commonly prefixed, e.g. 'KGAT_...'). Rather than guessing at the
    new format's exact prefix (which Kaggle could change), anything that
    does NOT match the strict, unchanging legacy shape is treated as the
    new-style token — so this keeps working even if that format evolves."""
    if not value:
        return False
    return not bool(_LEGACY_KEY_RE.match(value))


def resolve_kaggle_auth(username: str, key_or_token: str) -> dict:
    """Cleans + classifies the pasted secret as either a new-style single
    API token or a legacy key that must be paired with a username."""
    username, key_or_token = clean_kaggle_credentials(username, key_or_token)
    if _looks_like_new_api_token(key_or_token):
        return {"username": username, "api_token": key_or_token, "key": None}
    return {"username": username, "api_token": None, "key": key_or_token}


_MIN_TITLE_LEN = 6
_MAX_TITLE_LEN = 50


def kaggle_safe_title(raw: str, fallback: str = "") -> str:
    """Keeps a kernel display title inside Kaggle's own length rule: a
    kernel title must be more than 5 characters and at most 50, or
    `kernels push` fails outright (see
    github.com/Kaggle/kaggle-cli/pull/179). Short filenames are common in
    chemistry ('co2.inp', 'hf.inp', ...), so this pads them instead of
    letting an otherwise-valid submission fail; a very long custom job
    name is trimmed instead of being rejected. Applying this twice is
    harmless — an already-compliant title is returned unchanged."""
    title = (raw or "").strip() or (fallback or "").strip() or "Chemistry Job"
    while len(title) < _MIN_TITLE_LEN:
        title = f"{title} job"
    if len(title) > _MAX_TITLE_LEN:
        title = title[:_MAX_TITLE_LEN].strip()
    return title


_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
_JOB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")


def slugify_for_kaggle(raw: str) -> str:
    """Lowercase ASCII slug: exactly the character set a Kaggle kernel slug is
    allowed to use. Non-ASCII names (Arabic, accented Latin, ...) simply lose
    the characters that cannot appear in a URL slug, which is fine — the slug
    is an identifier, and the pretty name the person typed is what the site
    shows in "My Jobs"."""
    slug = _SLUG_STRIP_RE.sub("-", (raw or "").strip().lower()).strip("-")
    return re.sub(r"-{2,}", "-", slug)


def make_job_base_id(job_name: str = "", input_filename: str = "") -> str:
    """Builds this job's Kaggle kernel slug: `chem-tools-<name>-<random>`.

    Two rules drive the shape:

    * It must start with JOB_ID_PREFIX, because that prefix is how list_jobs()
      recognises this site's own kernels when someone signs in from a browser
      that has no local job list.
    * It is also used verbatim as the kernel's *title*. Kaggle derives a new
      kernel's slug from the title when the two disagree — the `kaggle kernels
      push` CLI even warns "your kernel title does not resolve to the specified
      id ... this may result in surprising behavior". The surprise is that the
      notebook is created at a different address than the one requested, so the
      link the site shows 404s and every later status poll, results download and
      delete targets a kernel that does not exist. Making title == slug removes
      that failure mode completely, for any language of job name.
    """
    stem = slugify_for_kaggle(job_name) or \
        slugify_for_kaggle(os.path.splitext(os.path.basename(input_filename or ""))[0])
    stem = stem[:24].strip("-")
    suffix = os.urandom(4).hex()
    return f"{JOB_ID_PREFIX}{stem}-{suffix}" if stem else f"{JOB_ID_PREFIX}{suffix}"


def pretty_job_title(slug: str, fallback: str = "") -> str:
    """Turns `chem-tools-co2-opt-1a2b3c4d` back into `co2 opt` for display, so
    a job list rebuilt from Kaggle still reads like the name the person chose
    rather than an internal identifier."""
    slug = (slug or "").strip()
    core = slug[len(JOB_ID_PREFIX):] if slug.startswith(JOB_ID_PREFIX) else slug
    core = re.sub(r"-r\d+$", "", core)              # continuation suffix
    core = re.sub(r"-[0-9a-f]{8}$", "", core)       # uniqueness suffix
    core = core.replace("-", " ").strip()
    return core or (fallback or slug)


def is_valid_job_id(job_id: str) -> bool:
    """Guards every place a job id arrives from the browser and is then handed
    to the `kaggle` CLI as an argument.

    The prefix check is not cosmetic: /api/kaggle/delete performs a real,
    irreversible `kernels delete` on the user's account, and without it a
    corrupted or hand-edited localStorage entry could name any notebook the
    user owns. Every id this site creates starts with JOB_ID_PREFIX."""
    job_id = (job_id or "").strip()
    return bool(_JOB_ID_RE.match(job_id)) and job_id.startswith(JOB_ID_PREFIX)


_PUSH_URL_RE = re.compile(r"https?://(?:www\.)?kaggle\.com/(?:code/)?([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)")


def parse_pushed_kernel(stdout: str) -> tuple[str | None, str | None]:
    """`kaggle kernels push` prints the URL Kaggle actually created, e.g.
    "Kernel version 1 successfully pushed.  Please check progress at
    https://www.kaggle.com/code/someone/chem-tools-abc". That URL is the only
    authoritative answer to "where did my notebook end up", so it is what the
    site links to and polls — never a URL reassembled from local guesses.

    Returns (owner, slug), either of which may be None if nothing recognisable
    was printed (older CLI builds, unusual output)."""
    match = _PUSH_URL_RE.search(stdout or "")
    if not match:
        return None, None
    return match.group(1), match.group(2)


def _build_header(values: dict) -> str:
    """Renders the `NAME = value` prelude for the in-kernel script from
    HEADER_VARS, so a missing key is a loud KeyError here rather than a
    NameError twelve hours into a calculation."""
    return "".join(f"{name} = {values[name]!r}\n" for name in HEADER_VARS)


# A Kaggle kernel's source is limited to about 1 MB, and the input plus any
# attached .xyz/.hess files ride inside script.py as one encoded blob. Refusing
# an oversized bundle up front with a clear message beats a cryptic push
# failure — or, worse, a chain that silently stops continuing later on.
MAX_PUSH_PAYLOAD_BYTES = 800 * 1024


def encode_files_payload(files_payload: dict[str, str]) -> str:
    """gzip + base64 of the {filename: base64content} map. The kernel decodes
    either this or the older plain-base64 form, and compressing it keeps the
    pushed script comfortably under Kaggle's source-size limit."""
    raw = json.dumps(files_payload).encode("utf-8")
    return base64.b64encode(gzip.compress(raw, 6)).decode("ascii")


# ─────────────────────────────────────────────────────────────
# The script that actually runs *inside* the Kaggle kernel.
# A header of plain `NAME = value` assignments is prepended before push
# (see build_job_dir below).
# ─────────────────────────────────────────────────────────────
KAGGLE_RUNNER_BODY = r'''
import base64, glob, gzip, json, os, re, random, shutil, signal, subprocess, sys, tarfile, threading, time, zipfile

START_TIME = time.time()


def log(msg):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        safe_msg = str(msg).encode("ascii", errors="replace").decode("ascii")
        print(safe_msg, flush=True)


# ── Header defaults ────────────────────────────────────────────────────────
# Every value below is normally injected as a plain `NAME = value` assignment
# ahead of this body (see _build_header in kaggle_runner.py). They are
# re-declared here with defaults so an in-flight continuation kernel pushed by
# an OLDER version of the site — whose header lacks the newer names — still
# runs instead of dying on a NameError halfway through a long chain.
DISK_RESTART_COUNT = globals().get("DISK_RESTART_COUNT", 0)
MAX_DISK_RESTARTS = globals().get("MAX_DISK_RESTARTS", 6)
MIN_FREE_GB = globals().get("MIN_FREE_GB", 5.0)
RESULT_BUDGET_GB = globals().get("RESULT_BUDGET_GB", 10.0)
# 0 means "this window has not had its ORCA cycle budget forced yet". A
# continuation raises them; see section 3a.
GEOM_MAXITER = int(globals().get("GEOM_MAXITER", 0) or 0)
SCF_MAXITER = int(globals().get("SCF_MAXITER", 0) or 0)
# A scan is rewritten each window to cover only the points that are left, so
# the ORIGINAL extent and the number already done have to travel with the chain.
# Reading them back out of the shrunken input is how a job 8 points into a
# 20-point scan came to report "point 4 of 15".
SCAN_TOTAL_POINTS = int(globals().get("SCAN_TOTAL_POINTS", 0) or 0)
SCAN_POINTS_BEFORE = int(globals().get("SCAN_POINTS_BEFORE", 0) or 0)
# Base64 of the chain's cumulative history. Each window appends one line, so the
# final archive can state what the earlier windows did -- otherwise the results
# the user downloads describe only the last leg of a multi-day calculation.
HISTORY_B64 = globals().get("HISTORY_B64", "") or ""

MIN_FREE_BYTES = int(float(MIN_FREE_GB) * (1 << 30))
RESULT_BUDGET_BYTES = int(float(RESULT_BUDGET_GB) * (1 << 30))

OUTPUT_DIR = "/kaggle/working"          # Kaggle's auto-saved output dir (20 GB cap)
# Kaggle's own documented hard cap for a notebook session: 12 h. TIME_LIMIT
# (header var) is only ever *our* earlier self-restart trigger; the retry
# budgets below are measured against the real platform cutoff so they can
# never run the kernel past the point Kaggle force-kills it.
HARD_SESSION_LIMIT_SECONDS = 43200
RESTART_PUSH_SAFETY_MARGIN = 300        # keep >= 5 min in reserve for packaging
CARRY_PAYLOAD_LIMIT = 400 * 1024        # max compressed restart payload inside script.py
CARRY_FILE_RAW_LIMIT = 24 * 1024 * 1024  # never even consider carrying a file bigger than this

try:
    import requests
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "--quiet"])
    import requests

# Best-effort only. Kaggle's kernel image can ship an older `kaggle` CLI than
# what's current, and that copy is what performs the self-continuation push
# later in this script. Doing it now, with ~12 h of slack ahead, means it is
# already done by the time the restart push is urgent; a failed upgrade is
# swallowed so it can never block or crash the actual computation.
try:
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "kaggle"],
        timeout=180, capture_output=True, text=True,
    )
except Exception as _exc:
    log("[setup] Could not pre-upgrade the kaggle CLI (continuing anyway): %s" % _exc)


# ── Disk helpers ───────────────────────────────────────────────────────────
def _free_bytes(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _gb(nbytes):
    if nbytes is None:
        return "?"
    for unit, scale in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if nbytes >= scale:
            return "%.1f %s" % (nbytes / float(scale), unit)
    return "%d B" % nbytes


def _dir_size(path):
    total = 0
    for root, _dirs, fnames in os.walk(path):
        for fn in fnames:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def _pick_scratch_root():
    # ORCA's scratch files (integrals, densities, per-rank temporaries) are
    # routinely tens of GB. /kaggle/working is auto-saved as the notebook's
    # output and capped at 20 GB, so running the calculation there is what
    # makes a big job die with "no space left on device" — and it also drags
    # every scratch byte into the saved output. Kaggle's scratch space
    # (/kaggle/temp, /tmp) is several times larger and is not saved, so the
    # job runs there and only curated results are copied back out.
    best_path, best_free = None, -1
    for cand in ("/kaggle/temp", "/kaggle/tmp", "/tmp", "/var/tmp"):
        try:
            os.makedirs(cand, exist_ok=True)
        except OSError:
            continue
        free = _free_bytes(cand)
        if free is not None and free > best_free:
            best_path, best_free = cand, free
    if best_path is None:
        return OUTPUT_DIR, (_free_bytes(OUTPUT_DIR) or 0)
    return best_path, best_free


SCRATCH_ROOT, SCRATCH_FREE = _pick_scratch_root()
WORKDIR = os.path.join(SCRATCH_ROOT, "orca_job")
os.makedirs(WORKDIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
log("[disk] Running ORCA in %s (%s free). Results are copied to %s at the end."
    % (WORKDIR, _gb(SCRATCH_FREE), OUTPUT_DIR))

# Never let the floor sit above a quarter of what the machine actually has:
# on a smaller-than-expected image that would trip the watchdog on its very
# first check and spin the job through restarts without ever running anything.
if SCRATCH_FREE:
    _floor_cap = max(128 * 1024 * 1024, SCRATCH_FREE // 4)
    if MIN_FREE_BYTES > _floor_cap:
        MIN_FREE_BYTES = _floor_cap
        log("[disk] Scratch is smaller than expected; lowering the free-space floor to %s."
            % _gb(MIN_FREE_BYTES))
log("[disk] The run is stopped and continued in a fresh session if free space drops below %s."
    % _gb(MIN_FREE_BYTES))


# ── 1. Materialize the files shipped from the website ──────────────────────
def _decode_payload(blob):
    raw = base64.b64decode(blob)
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass            # not gzipped (older format) — use as-is
    return json.loads(raw.decode("utf-8"))


files = _decode_payload(ENCODED_FILES_JSON)
for fname, b64content in files.items():
    safe_name = os.path.basename(fname)
    with open(os.path.join(WORKDIR, safe_name), "wb") as fh:
        fh.write(base64.b64decode(b64content))

inp_path = os.path.join(WORKDIR, os.path.basename(INPUT_FILE))
BASENAME = os.path.splitext(os.path.basename(INPUT_FILE))[0]
out_path = os.path.join(WORKDIR, BASENAME + ".out")


def _wp(name):
    return os.path.join(WORKDIR, name)


# ── 2. Locate the ORCA executable ─────────────────────────────────────────
ARCHIVE_EXTS = (".tar.xz", ".txz", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar", ".zip")


def _find_orca_under(root_dir):
    if not os.path.isdir(root_dir):
        return None
    for root, _dirs, fnames in os.walk(root_dir):
        for fn in fnames:
            if fn == "orca" or re.match(r"^orca(_\d+)*(\.(bat|cmd|exe))?$", fn, re.IGNORECASE):
                candidate = os.path.join(root, fn)
                if os.access(candidate, os.X_OK) or fn == "orca" or fn.lower().endswith((".bat", ".cmd", ".exe")):
                    return candidate
    return None


def _find_candidate_archives(root_dir):
    # Finds compressed archives that likely hold the ORCA package — e.g. a
    # Kaggle Dataset that stores the .tar.xz as-is instead of a pre-extracted
    # binary. Archives with "orca" in the name are tried first.
    if not os.path.isdir(root_dir):
        return []
    named, other = [], []
    for root, _dirs, fnames in os.walk(root_dir):
        for fn in fnames:
            lower = fn.lower()
            if lower.endswith(ARCHIVE_EXTS):
                (named if "orca" in lower else other).append(os.path.join(root, fn))
    return named + other


def _extract_archive(archive_path, dest_dir):
    # Kaggle Dataset mounts under /kaggle/input are read-only, so extraction
    # always targets a separate writable scratch directory.
    os.makedirs(dest_dir, exist_ok=True)
    try:
        if zipfile.is_zipfile(archive_path):
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(dest_dir)
            return True
        if tarfile.is_tarfile(archive_path):
            with tarfile.open(archive_path, "r:*") as tf:
                # Kaggle kernels run as root and ~/.kaggle holds a live API key,
                # so an archive fetched from a user-supplied link is untrusted
                # input. Without a filter, a member named ../../x or a symlink
                # pointing outside dest_dir writes anywhere on the VM --
                # including copying the credential into /kaggle/working, where
                # it persists in the notebook's saved output.
                try:
                    tf.extractall(dest_dir, filter="data")      # Python 3.12+
                except TypeError:
                    root = os.path.realpath(dest_dir)
                    safe = []
                    for member in tf.getmembers():
                        target = os.path.realpath(os.path.join(dest_dir, member.name))
                        if not (target == root or target.startswith(root + os.sep)):
                            log("[orca-import] Refused archive member outside the "
                                "destination: %s" % member.name)
                            continue
                        if member.issym() or member.islnk():
                            link = os.path.realpath(
                                os.path.join(os.path.dirname(target), member.linkname))
                            if not (link == root or link.startswith(root + os.sep)):
                                log("[orca-import] Refused link escaping the "
                                    "destination: %s" % member.name)
                                continue
                        safe.append(member)
                    tf.extractall(dest_dir, members=safe)
            return True
        log("[orca-import] %s is neither a zip nor a tar archive." % archive_path)
    except OSError as exc:
        log("[orca-import] Failed to extract %s: %s" % (archive_path, exc))
        if "space" in str(exc).lower() or getattr(exc, "errno", None) == 28:
            log("[orca-import] That was a DISK FULL error while unpacking ORCA. "
                "A pre-extracted Kaggle Dataset (no archive) avoids needing room for both "
                "the archive and its contents.")
    except Exception as exc:
        log("[orca-import] Failed to extract %s: %s" % (archive_path, exc))
    return False


def _locate_orca(search_root, scratch_dir):
    exe = _find_orca_under(search_root)
    if exe:
        return exe
    for i, archive_path in enumerate(_find_candidate_archives(search_root)):
        extract_to = os.path.join(scratch_dir, "extracted_%d" % i)
        log("[orca-import] Found archive %s; extracting to %s ..." % (archive_path, extract_to))
        if _extract_archive(archive_path, extract_to):
            exe = _find_orca_under(extract_to)
            if exe:
                return exe
        shutil.rmtree(extract_to, ignore_errors=True)   # wrong archive: reclaim the space now
    return None


def _download_orca_from_link(link, dest_dir):
    # Downloads ORCA from a Google Drive share link or a direct URL, then
    # extracts it. zip vs tar(.gz/.xz/.bz2) is detected by sniffing the bytes,
    # not the filename, since most direct-download hosts save to a generic
    # name with no extension at all.
    os.makedirs(dest_dir, exist_ok=True)
    downloaded_path = os.path.join(dest_dir, "_orca_download.bin")

    if "drive.google.com" in link or "docs.google.com" in link:
        try:
            import gdown
        except ImportError:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "gdown", "--quiet"])
            import gdown
        try:
            result_path = gdown.download(url=link, output=downloaded_path, quiet=False, fuzzy=True)
            downloaded_path = result_path or downloaded_path
        except Exception as exc:
            log("[orca-import] gdown failed: %s" % exc)
            return False
    else:
        try:
            with requests.get(link, stream=True, timeout=1800) as resp:
                resp.raise_for_status()
                with open(downloaded_path, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        if chunk:
                            fh.write(chunk)
        except Exception as exc:
            log("[orca-import] direct download failed: %s" % exc)
            return False

    if not os.path.exists(downloaded_path) or os.path.getsize(downloaded_path) == 0:
        log("[orca-import] Download produced no file; check the link.")
        return False

    log("[orca-import] Downloaded %d bytes. Extracting (auto-detecting zip/tar by content)..."
        % os.path.getsize(downloaded_path))
    ok = _extract_archive(downloaded_path, dest_dir)
    if not ok:
        log("[orca-import] Could not extract the downloaded file as a zip or tar archive. "
            "The link may not point directly at the archive (e.g. it lands on an HTML "
            "'confirm download' / 'sign in' page instead of the file itself) — for Google "
            "Drive, make sure sharing is set to 'Anyone with the link'.")
    try:
        os.remove(downloaded_path)      # reclaim the archive's space immediately
    except OSError:
        pass
    return ok


ORCA_SCRATCH = os.path.join(SCRATCH_ROOT, "orca_pkg")
orca_exe = _locate_orca("/kaggle/input", ORCA_SCRATCH)

if not orca_exe and ORCA_LINK:
    log("[orca-import] ORCA not found in attached datasets. Downloading from the link ...")
    if _download_orca_from_link(ORCA_LINK, ORCA_SCRATCH):
        orca_exe = _locate_orca(ORCA_SCRATCH, os.path.join(ORCA_SCRATCH, "_nested"))

if not orca_exe:
    log("FATAL: could not locate an 'orca' executable, even after checking for and extracting "
        ".tar.xz/.tar.gz/.zip archives. Make sure your dataset or link actually contains the "
        "ORCA package (a folder or archive with an 'orca' binary inside), that it is the LINUX "
        "build, and that the archive isn't corrupted/partial.")
    with open(os.path.join(OUTPUT_DIR, "JOB_NOTE.txt"), "a") as fh:
        fh.write("Could not find an 'orca' executable in the attached dataset or download link. "
                 "Check that it is the Linux build of ORCA and that the archive is complete.\n")
    sys.exit(1)

os.chmod(orca_exe, 0o755)
orca_dir = os.path.dirname(orca_exe)
log("[orca-import] Using ORCA at %s" % orca_exe)

# Any *other* archive we speculatively extracted is dead weight now — on a
# 20-30 GB ORCA package that alone is the difference between finishing and
# running out of disk.
for stale in glob.glob(os.path.join(ORCA_SCRATCH, "extracted_*")):
    if not os.path.abspath(orca_exe).startswith(os.path.abspath(stale) + os.sep):
        shutil.rmtree(stale, ignore_errors=True)

os.environ["PATH"] = orca_dir + os.pathsep + os.environ.get("PATH", "")
os.environ["LD_LIBRARY_PATH"] = orca_dir + os.pathsep + os.environ.get("LD_LIBRARY_PATH", "")
os.environ["OMPI_ALLOW_RUN_AS_ROOT"] = "1"
os.environ["OMPI_ALLOW_RUN_AS_ROOT_CONFIRM"] = "1"
os.environ["OMPI_MCA_btl_vader_single_copy_mechanism"] = "none"
os.environ["OMPI_MCA_rmaps_base_oversubscribe"] = "1"
os.environ["PRTE_MCA_rmaps_default_mapping_policy"] = ":oversubscribe"
# ORCA parallelises with MPI, not threads; leaving OpenMP unbounded on top of
# that oversubscribes the 4 vCPUs a Kaggle CPU session provides and can slow a
# run down several-fold (or trip BLAS thread limits).
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"


# ── 3. Right-size the job for the machine it actually landed on ────────────
with open(inp_path, "r", encoding="utf-8", errors="replace") as fh:
    inp_text = fh.read()

# Kept aside before the machine-specific clamps below, because that is what a
# continuation kernel should inherit: each window re-clamps %pal/%maxcore for
# the session it actually lands on, so one window forced to run serially (no
# mpirun that time) must not permanently pin the rest of the chain to 1 core.
ORIGINAL_INP_TEXT = inp_text


# ── 3a. Job classification, input preparation, refusal gates ───────────────
# Everything the rest of the script believes about the calculation is decided
# here, once, so that every window of a chain inherits the same prepared input
# and the same understanding of what kind of job it is.
#
# Three principles, each of which was learned from a way this used to be wrong:
#
#   * Classify from the *keyword line and the block headers*, never from a
#     substring of the whole file. "neb" appears in nebivolol; "scan" appears
#     in "# scan of the Fe complex". A misclassified job takes a restart path
#     built for a different calculation and makes no progress for twenty
#     windows while reporting that it resumed.
#
#   * Refuse loudly rather than continue approximately. This runner rewrites
#     the user's input between sessions. Every rewrite it cannot perform
#     faithfully -- a multi-job `$new_job` input, a coordinate block carrying
#     per-atom basis sets, an active-space method whose orbitals are the
#     restart state -- must end the chain with an explanation, because the
#     alternative is a converged-looking number that is wrong.
#
#   * Disclose every modification. A calculation whose input was silently
#     altered is not reproducible. Each note here is carried into JOB_NOTE.txt
#     and the unmodified input is shipped alongside the rewritten one.
DEFAULT_GEOM_MAXITER = 500
DEFAULT_SCAN_MAXITER = 120          # per scan POINT, so it must stay modest
MAX_GEOM_MAXITER = 4000
DEFAULT_SCF_MAXITER = 500
MAX_SCF_MAXITER = 2000

PREP_NOTES = []                     # every rewrite applied to the user's input
REFUSE_CONTINUATION = ""            # non-empty -> this job must never be continued


def _strip_comments(text):
    """ORCA treats `#` as end-of-line comment. Classification and keyword
    injection must both work on the comment-free text: `! Opt  # quick test`
    otherwise gets `NoAutoStart` appended INSIDE the comment, where ORCA never
    sees it, and the whole corrupt-wavefunction guard is silently off."""
    return re.sub(r"(?m)#.*$", "", text)


def _keyword_lines(text):
    """The `!` simple-input lines, comment-free. ORCA concatenates them all."""
    return [ln for ln in _strip_comments(text).splitlines() if ln.lstrip().startswith("!")]


def _kw(text):
    return " ".join(_keyword_lines(text)).lower()


def _has_block(text, name):
    return bool(re.search(r"(?im)^\s*%\s*" + name + r"\b", _strip_comments(text)))


def _find_block(text, name):
    """Returns (start, body_start, body_end, end_index) for `%name ... end`,
    honouring nesting, or None.

    A regex that stops at the first `end` gets this wrong for every block that
    contains a sub-block -- `%geom Constraints ... end MaxIter 30 end` being
    the common one. Reading MaxIter then misses the user's value and injecting
    one produces a duplicate key, so the escalated cycle budget silently does
    nothing on exactly the constrained optimisations that need it most."""
    clean = _strip_comments(text)
    m = re.search(r"(?im)^\s*%\s*" + name + r"\b", clean)
    if not m:
        return None
    body_start = m.end()
    depth = 1
    # Sub-blocks inside an ORCA block are opened by a bare keyword and closed by
    # `end`; only `end` tokens are counted, and the openers we must balance are
    # the documented nesting keywords.
    token = re.compile(r"(?i)\b(end|constraints|scan|potentials|connect|"
                       r"modifyinternal|invertconstraints|frozenatoms)\b")
    for tm in token.finditer(clean, body_start):
        word = tm.group(1).lower()
        if word == "end":
            depth -= 1
            if depth == 0:
                return m.start(), body_start, tm.start(), tm.end()
        else:
            depth += 1
    return None


def _block_value(text, block, key):
    """Reads `key <int>` out of `%block ... end`; None if absent."""
    span = _find_block(text, block)
    if not span:
        return None
    body = _strip_comments(text)[span[1]:span[2]]
    # Only at this block's own depth: a nested sub-block's key is not ours.
    body = re.sub(r"(?is)\b(constraints|scan|potentials|connect|modifyinternal|"
                  r"invertconstraints|frozenatoms)\b.*?\bend\b", " ", body)
    km = re.search(r"(?i)\b" + key + r"\s+(\d+)", body)
    return int(km.group(1)) if km else None


def _force_block_value(text, block, key, value):
    """Sets `key value` in `%block ... end`, creating the key or the block.

    Any existing occurrence at this block's own depth is replaced rather than
    shadowed -- ORCA would otherwise see the key twice, and either honour the
    user's old value or abort with UNRECOGNIZED OR DUPLICATED KEYWORD."""
    span = _find_block(text, block)
    if not span:
        return text.rstrip() + "\n%" + block + "\n  " + key + " " + str(value) + "\nend\n"
    start, body_start, body_end, _end = span
    body = text[body_start:body_end]
    pattern = re.compile(r"(?i)\b" + key + r"\s+\d+")
    # Mask nested sub-blocks so a key inside one is left alone.
    masked, guard = [], re.compile(
        r"(?is)\b(constraints|scan|potentials|connect|modifyinternal|"
        r"invertconstraints|frozenatoms)\b.*?\bend\b")
    last = 0
    for gm in guard.finditer(body):
        masked.append((last, gm.start()))
        last = gm.end()
    masked.append((last, len(body)))
    for lo, hi in masked:
        km = pattern.search(body, lo, hi)
        if km:
            body = body[:km.start()] + key + " " + str(value) + body[km.end():]
            return text[:body_start] + body + text[body_end:]
    body = "\n  " + key + " " + str(value) + "\n" + body.lstrip("\n")
    return text[:body_start] + body + text[body_end:]


def _ensure_simple_keyword(text, keyword):
    """Adds `keyword` to the simple-input line, never inside a comment."""
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if not ln.lstrip().startswith("!"):
            continue
        code, sep, comment = ln.partition("#")
        if keyword.lower() in code.lower():
            return "\n".join(lines)
        lines[i] = code.rstrip() + " " + keyword + (" " + sep + comment if sep else "")
        return "\n".join(lines)
    return "! " + keyword + "\n" + text


def _strip_opt_keyword(text):
    """Removes Opt/OptTS from the simple-input line only.

    `re.sub(r"\\bopt(ts)?\\b", "", text, count=1)` removed the FIRST match in the
    whole file, which for `# Opt of the Fe complex` above the keyword line was
    the comment -- leaving the real `Opt` in place, so the successor redid the
    entire optimisation while the job note said "continuing with frequencies
    only"."""
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if not ln.lstrip().startswith("!"):
            continue
        code, sep, comment = ln.partition("#")
        code = re.sub(r"(?i)\bopt(ts)?\b", "", code)
        code = re.sub(r"[ \t]{2,}", " ", code).rstrip()
        lines[i] = code + ((" " + sep + comment) if sep else "")
        break
    return "\n".join(lines)


def _strip_moread(text):
    """Removes every form of "read the previous wavefunction".

    Both the simple-input keyword and the two block forms have to go together:
    removing `MOREAD` while leaving `%scf Guess MORead` would leave ORCA with a
    dangling `Guess` and an input error, and leaving `MOInp` behind points at a
    .gbw that is deliberately not carried."""
    text = re.sub(r"(?i)\bMOREAD\b(?!\s*\")", "", text)
    text = re.sub(r"(?im)^[ \t]*%\s*moinp\b.*$", "", text)
    text = re.sub(r"(?im)^[ \t]*MOInp\b.*$", "", text)
    text = re.sub(r"(?i)\bGuess\s+MORead\b", "", text)
    return text


# ── Job classification (from the keyword line and block headers only) ──────
KW = _kw(inp_text)
CLEAN_INP = _strip_comments(inp_text)

is_neb_job = bool(re.search(r"(?i)\bneb\b", KW)) or _has_block(inp_text, "neb")
has_scan = bool(re.search(r"(?im)^\s*scan\b", CLEAN_INP)) or \
    bool(re.search(r"(?i)%\s*geom[^\n]*\bscan\b", CLEAN_INP))
is_md_job = _has_block(inp_text, "md") or bool(re.search(r"(?i)\bmd\b", KW))
# ORCA's own manual is explicit that ANALYTIC frequency jobs are not
# restartable; only NumFreq is. Treating any %freq block as numerical injected
# `Restart true` into analytic jobs and demanded .res.* files that never exist.
is_numfreq = bool(re.search(r"(?i)\bnumfreq\b", KW)) or \
    (_block_value(inp_text, "freq", "numfreq") is not None) or \
    bool(re.search(r"(?i)%\s*freq[^\n]*\bnumfreq\s+true\b", CLEAN_INP))
is_opt_job = bool(re.search(r"(?i)\bopt(ts)?\b", KW))
is_irc_job = bool(re.search(r"(?i)\birc\b", KW)) or _has_block(inp_text, "irc")
is_tddft_opt = (_has_block(inp_text, "tddft") or bool(re.search(r"(?i)\b(td-?dft|cis)\b", KW))) \
    and (is_opt_job or is_irc_job)
freq_requested = bool(re.search(r"(?i)\b(?:num|an)?freq\b", KW))
# "Iterative" jobs write intermediate, text-based progress we can resume from.
# A plain single point / TD-DFT SP / one-shot analytic Freq has only the binary
# wavefunction to continue from -- which is deliberately never trusted after a
# hard kill -- so relaunching one just repeats the same wall.
is_iterative = is_opt_job or is_neb_job or has_scan or is_md_job or is_numfreq or is_irc_job

# ORCA writes every artefact under %base when it is given, so the restart
# lookups (.xyz, .hess, .NNN.xyz, .mdrestart, .res.*) must follow it.
_base_m = re.search(r'(?im)^\s*%\s*base\s+"?([^"\s]+)"?', CLEAN_INP)
if _base_m:
    BASENAME = _base_m.group(1)


# ── Refusal gates: inputs this runner must not rewrite ────────────────────
def _coordinate_block_is_plain(text):
    """True only when every line of an inline coordinate block is `Sym x y z`.

    ORCA allows a great deal more in that block -- ghost atoms (`O:`) for
    counterpoise, per-atom `NewGTO`/`NewECP` for mixed basis sets, fragment
    labels `C(1)`, dummy atoms, inline point charges. The restart replaces the
    whole block with `* xyzfile ...`, which discards all of it: a mixed-basis
    calculation would continue in a uniform basis, and a counterpoise monomer
    would continue as a full dimer, in both cases converging quietly to a
    number that belongs to a different calculation."""
    m = re.search(r"\*\s*(?:xyz|gzmt|int|internal)\s+-?\d+\s+-?\d+(.*?)\*",
                  _strip_comments(text), re.IGNORECASE | re.DOTALL)
    if not m:
        return True                       # * xyzfile / no inline block
    for ln in m.group(1).splitlines():
        s = ln.strip()
        if not s:
            continue
        if not re.match(r"^[A-Za-z]{1,3}\s+-?\d", s):
            return False
    return True


_REFUSALS = (
    (bool(re.search(r"(?im)^\s*\$new_job\b", CLEAN_INP)),
     "this input runs several jobs in sequence ($new_job). Each has its own "
     "charge, multiplicity and geometry, and a restart cannot tell which one was "
     "in progress -- continuing would run the later jobs at the first job's "
     "charge and multiplicity. Submit each job separately"),
    (bool(re.search(r"(?i)\b(casscf|nevpt2|caspt2|mrci|dmrg|ice-?ci)\b", KW))
     or _has_block(inp_text, "casscf"),
     "multireference methods restart from the converged orbitals, and this "
     "runner deliberately never carries the binary wavefunction across a "
     "session. Each window would rebuild a different active space, so the "
     "windows would not describe the same electronic state"),
    (bool(re.search(r"(?i)\b(brokensym|flipspin)\b", KW))
     or (_block_value(inp_text, "scf", "flipspin") is not None),
     "a broken-symmetry / spin-flip calculation is defined by the orbitals it "
     "starts from. Without the previous wavefunction the SCF would fall back to "
     "the high-spin solution and the coupling constant would be wrong"),
    (not _coordinate_block_is_plain(inp_text),
     "the coordinate block carries per-atom information (ghost atoms, NewGTO/"
     "NewECP, fragment labels or point charges) that a restart geometry file "
     "cannot preserve. Continuing would change the basis set or the electron "
     "count without saying so"),
    (is_irc_job,
     "an IRC follows a specific path downhill from a transition state. Resuming "
     "it from a displaced geometry would start a DIFFERENT path, and the reaction "
     "it appears to connect would not be the one this transition state connects. "
     "Run the IRC as its own submission, or shorten it with %irc MaxIter"),
    (bool(re.search(r"(?i)\*\s*pdbfile\b", CLEAN_INP)) or _has_block(inp_text, "coords"),
     "this coordinate form (* pdbfile / %coords) cannot be repointed at a "
     "restart geometry by this runner"),
    (has_scan and len(re.findall(r"(?im)^\s*scan\s+[BADC]\b", CLEAN_INP)) > 1,
     "multi-dimensional scans cannot be resumed correctly: the completed-point "
     "count does not map onto a single coordinate range, so the outer grid "
     "points would be silently dropped"),
)
for _bad, _why in _REFUSALS:
    if _bad and not REFUSE_CONTINUATION:
        REFUSE_CONTINUATION = _why


def _prepare_input(text, first_window):
    """Returns the input ORCA will actually run for this window."""
    if not first_window:
        # The wavefunction is never carried across a session, so an explicit
        # read would dangle. On the FIRST window the user's own MOREAD is left
        # alone: they may have shipped a .gbw deliberately, and stripping it
        # would silently change the calculation before it has even run once.
        stripped = _strip_moread(text)
        if stripped != text:
            PREP_NOTES.append(
                "removed MOREAD/%moinp: the previous window's wavefunction is not "
                "carried across sessions, so this window builds its own SCF guess")
        text = _ensure_simple_keyword(stripped, "NoAutoStart")

    if is_opt_job or has_scan or is_irc_job:
        block = "irc" if (is_irc_job and not is_opt_job) else "geom"
        # A relaxed scan's MaxIter is PER SCAN POINT, so the generous
        # optimisation budget would let one pathological point eat a whole chain.
        default = DEFAULT_SCAN_MAXITER if has_scan else DEFAULT_GEOM_MAXITER
        want = int(GEOM_MAXITER)
        current = _block_value(text, block, "maxiter")
        if want > 0:
            if current != want:
                text = _force_block_value(text, block, "MaxIter", want)
                PREP_NOTES.append("cycle budget set to %%%s MaxIter %d for this window"
                                  % (block, want))
        elif current is None:
            text = _force_block_value(text, block, "MaxIter", default)
            PREP_NOTES.append(
                "no %%%s MaxIter in the input; set to %d so ORCA's cycle counter "
                "cannot end the run before the session clock does" % (block, default))

    if is_neb_job and int(GEOM_MAXITER) > 0:
        # NEB counts its own iterations; %geom MaxIter does not bound it.
        text = _force_block_value(text, "neb", "MaxIter", int(GEOM_MAXITER))
        PREP_NOTES.append("NEB iteration budget set to %%neb MaxIter %d" % int(GEOM_MAXITER))

    if int(SCF_MAXITER) > 0:
        text = _force_block_value(text, "scf", "MaxIter", int(SCF_MAXITER))
        text = _ensure_simple_keyword(text, "SlowConv")
        PREP_NOTES.append(
            "SCF budget raised to MaxIter %d with SlowConv after an SCF convergence "
            "failure. SlowConv changes the convergence PATH, so for a system with "
            "more than one SCF solution this window may land on a different one "
            "than the previous window did" % int(SCF_MAXITER))

    if is_tddft_opt and int(RESTART_COUNT) > 0:
        PREP_NOTES.append(
            "WARNING: this is an excited-state optimisation. ORCA follows the target "
            "root by comparing against the previous step's excited-state wavefunction, "
            "which does not survive a session boundary. If two states crossed during "
            "the previous window, this window may be following a different state. "
            "Check that the root character is the same at both ends of the chain")

    for n in PREP_NOTES:
        log("[prep] " + n)
    return text


ORIGINAL_USER_INP_TEXT = inp_text        # shipped verbatim for reproducibility

inp_text = _prepare_input(inp_text, first_window=(int(RESTART_COUNT) == 0))
ORIGINAL_INP_TEXT = inp_text
with open(inp_path, "w", encoding="utf-8") as fh:
    fh.write(inp_text)
# The input the person wrote, kept beside the one that ran. Without both, a
# reader of the archive cannot tell which parts of the calculation were theirs.
if ORIGINAL_USER_INP_TEXT != inp_text:
    with open(_wp("ORIGINAL_" + os.path.basename(INPUT_FILE)), "w", encoding="utf-8") as fh:
        fh.write(ORIGINAL_USER_INP_TEXT)


def _requested_nprocs(text):
    m = re.search(r"%\s*pal\b(?:(?!\bend\b).)*?nprocs\s+(\d+)", text, re.IGNORECASE | re.DOTALL)
    if m:
        return int(m.group(1))
    m = re.search(r"(?i)\bPAL([1-9]\d?)\b", text)
    if m:
        return int(m.group(1))
    return 1


def _set_nprocs(text, nprocs):
    new = re.sub(r"(%\s*pal\b(?:(?!\bend\b).)*?nprocs\s+)(\d+)",
                 lambda m: m.group(1) + str(nprocs), text, flags=re.IGNORECASE | re.DOTALL)
    if nprocs <= 1:
        # Strip the "! ... PALn" shorthand entirely for a serial run.
        new = re.sub(r"(?i)\s*\bPAL[1-9]\d?\b", "", new)
    else:
        # Anchored to the simple-input line and matched longest-first, so
        # `! ... PAL16` is rewritten whole instead of becoming "PAL1" + "6".
        lines = new.split("\n")
        for i, ln in enumerate(lines):
            if ln.lstrip().startswith("!"):
                code, sep, comment = ln.partition("#")
                code = re.sub(r"(?i)\bPAL(?:[1-9]\d?)\b", "PAL" + str(nprocs), code)
                lines[i] = code + ((sep + comment) if sep else "")
        new = "\n".join(lines)
    return new


def _cgroup_limit_mb():
    """The memory ceiling this *container* is actually allowed, in MB.

    /proc/meminfo reports the HOST's memory even inside a container, so a
    kernel that trusts it can hand ORCA a %maxcore the cgroup will never
    honour -- and the run dies to the OOM killer mid-calculation, which looks
    like a mysterious "stopped without finishing" rather than a memory error.
    Both cgroup layouts are read; an absent or 'max' value means unlimited."""
    for path in ("/sys/fs/cgroup/memory.max",                       # cgroup v2
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):    # cgroup v1
        try:
            with open(path) as fh:
                raw = fh.read().strip()
        except OSError:
            continue
        if not raw.isdigit():
            continue                     # "max" -> no limit set
        mb = int(raw) // (1 << 20)
        # cgroup v1 reports a sentinel close to 2**63 when unlimited.
        if 0 < mb < (1 << 30):
            return mb
    return None


def _total_ram_mb():
    meminfo = None
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    meminfo = int(line.split()[1]) // 1024
                    break
    except OSError:
        pass
    cgroup = _cgroup_limit_mb()
    candidates = [v for v in (meminfo, cgroup) if v]
    return min(candidates) if candidates else 12000


# ORCA's manual recommends leaving roughly a quarter of the machine's memory
# outside %maxcore: the value is a soft target for ORCA's own buffers, and real
# usage routinely runs above it, so a run sized to 100% of RAM is a run that
# gets OOM-killed. 0.70 keeps a margin for that overshoot plus the notebook
# process, the MPI runtime and the page cache.
MAXCORE_SAFETY_FRACTION = 0.70

#: Filled in by _clamp_maxcore so the reduction can be reported to the person
#: in JOB_NOTE.txt. Silently rewriting a number someone typed and mentioning it
#: only in a kernel log they may never open is not acceptable in a tool whose
#: output is meant to be reproducible -- the input that ran must be visible.
MAXCORE_NOTE = ""


def _clamp_maxcore(text, nprocs):
    # %maxcore is PER MPI PROCESS. nprocs x maxcore above what the machine has
    # makes the Linux OOM killer take the run down mid-calculation, which reads
    # as a mysterious "stopped without finishing" rather than an ORCA error.
    global MAXCORE_NOTE
    MAXCORE_NOTE = ""            # a later re-clamp (serial retry) supersedes an earlier one
    total_mb = _total_ram_mb()
    budget = int(total_mb * MAXCORE_SAFETY_FRACTION)
    # The floor is only useful while it still fits: on a small container
    # max(700, ...) could hand out more than the safety budget allows and
    # reintroduce the OOM this clamp exists to prevent.
    cap = min(max(700, budget // max(1, nprocs)), max(256, budget))
    changed = []

    def repl(m):
        want = int(m.group(2))
        if want <= cap:
            return m.group(0)
        changed.append((want, cap))
        return m.group(1) + str(cap)

    new = re.sub(r"(?i)(%\s*maxcore\s+)(\d+)", repl, text)
    if changed:
        want, got = changed[0]
        MAXCORE_NOTE = (
            "Memory: the input asked for %%maxcore %d MB per process x %d process(es) "
            "= %d MB, but this session has %d MB in total. ORCA treats %%maxcore as a "
            "soft target and regularly exceeds it, so the run was given %d MB per "
            "process (%d MB total, %d%% of the machine) instead. Without that "
            "reduction the Linux OOM killer would very likely have ended the run "
            "part-way through. To keep your requested value, lower the number of "
            "processes: %d MB x %d process(es) fits."
            % (want, nprocs, want * nprocs, total_mb, got, got * nprocs,
               int(MAXCORE_SAFETY_FRACTION * 100),
               want, max(1, budget // max(1, want))))
        log("[fit] " + MAXCORE_NOTE)
    return new


def _find_mpirun():
    found = shutil.which("mpirun") or shutil.which("orterun") or shutil.which("prterun")
    if found:
        return found
    # Some ORCA distributions ship an OpenMPI runtime next to the binaries.
    for pattern in ("mpirun", os.path.join("*", "mpirun"), os.path.join("*", "bin", "mpirun")):
        for cand in glob.glob(os.path.join(orca_dir, pattern)) + \
                glob.glob(os.path.join(os.path.dirname(orca_dir), pattern)):
            if os.path.isfile(cand):
                os.environ["PATH"] = os.path.dirname(cand) + os.pathsep + os.environ["PATH"]
                try:
                    os.chmod(cand, 0o755)
                except OSError:
                    pass
                return cand
    return None


def _try_install_openmpi():
    # ORCA 6 needs an external OpenMPI 4.1.x for any parallel run; without it a
    # PAL job dies in seconds with an MPI error. Kaggle sessions run as root
    # with internet enabled, so a bounded, quiet install is worth one attempt.
    for cmd in (["apt-get", "-y", "-qq", "update"],
                ["apt-get", "-y", "-qq", "install", "openmpi-bin", "libopenmpi3"]):
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except Exception as exc:
            log("[mpi] %s failed: %s" % (cmd[0], exc))
            return None
    return _find_mpirun()


real_cores = os.cpu_count() or 1
want_nprocs = _requested_nprocs(inp_text)
eff_nprocs = max(1, min(want_nprocs, real_cores))
if eff_nprocs != want_nprocs:
    log("[fit] Requested %d MPI processes but this session has %d cores; using %d."
        % (want_nprocs, real_cores, eff_nprocs))

mpi_available = True
if eff_nprocs > 1:
    mpirun = _find_mpirun()
    if not mpirun:
        log("[mpi] No mpirun found — ORCA cannot run in parallel without OpenMPI. Trying a quick install ...")
        mpirun = _try_install_openmpi()
    if mpirun:
        log("[mpi] Using %s" % mpirun)
    else:
        mpi_available = False
        log("[mpi] Still no mpirun available. Falling back to a SERIAL run so the calculation "
            "still completes (slower, but it finishes). To get parallel speed, include an "
            "OpenMPI 4.1.x build in your ORCA dataset.")
        eff_nprocs = 1

inp_text = _set_nprocs(inp_text, eff_nprocs)
inp_text = _clamp_maxcore(inp_text, eff_nprocs)
with open(inp_path, "w", encoding="utf-8") as fh:
    fh.write(inp_text)


# ── 4. Run ORCA under a time + disk watchdog ───────────────────────────────
stop_reason = {"why": None, "detail": ""}
proc_holder = {"proc": None}
# Set the moment ORCA exits. The watchdog waits on it instead of sleeping, so a
# run that finishes on its own is not held up by whatever is left of the poll
# interval -- which every window used to pay at the end.
run_finished = threading.Event()


def _kill_tree(proc, grace=25):
    # ORCA's parallel driver spawns mpirun/orted children in the same process
    # group. Terminating only the parent leaves them alive, still writing to
    # the scratch disk, and the kernel then hangs instead of restarting.
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        pgid = None
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 10)):
        if proc.poll() is not None:
            return
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
        except Exception:
            pass
        deadline = time.time() + wait
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.5)


def _watchdog():
    proc = proc_holder["proc"]
    warned_low = False
    while proc.poll() is None:
        elapsed = time.time() - START_TIME
        if elapsed > TIME_LIMIT:
            stop_reason["why"] = "time"
            stop_reason["detail"] = "reached the %ds session budget" % TIME_LIMIT
            log("[watchdog] Reached the time limit (%ds). Stopping cleanly for a restart ..." % TIME_LIMIT)
            _kill_tree(proc)
            return
        free = _free_bytes(WORKDIR)
        if free is not None and free < MIN_FREE_BYTES:
            stop_reason["why"] = "disk"
            stop_reason["detail"] = "only %s of scratch space left" % _gb(free)
            log("[watchdog] Scratch space down to %s (floor %s). Stopping cleanly so the run can "
                "continue in a fresh session instead of crashing on a full disk ..."
                % (_gb(free), _gb(MIN_FREE_BYTES)))
            _kill_tree(proc)
            return
        if free is not None and not warned_low and free < MIN_FREE_BYTES * 3:
            warned_low = True
            log("[watchdog] Heads up: scratch space is down to %s." % _gb(free))
        # Ten seconds against a 12-hour budget; proportionately shorter when the
        # budget is short, so a small TIME_LIMIT is still honoured promptly.
        if run_finished.wait(max(1.0, min(10.0, TIME_LIMIT / 8.0))):
            return


def _run_orca_once():
    stop_reason["why"], stop_reason["detail"] = None, ""
    out_fh = open(out_path, "w")
    try:
        proc = subprocess.Popen(
            [orca_exe, inp_path],
            cwd=WORKDIR, stdout=out_fh, stderr=subprocess.STDOUT,
            start_new_session=True,          # own process group -> killable as a tree
        )
    except OSError as exc:
        out_fh.close()
        log("FATAL: could not start ORCA: %s" % exc)
        return None
    proc_holder["proc"] = proc
    run_finished.clear()
    thread = threading.Thread(target=_watchdog, daemon=True)
    thread.start()
    proc.wait()
    run_finished.set()
    thread.join(timeout=15)
    out_fh.close()
    return proc.returncode


# Everything above -- downloading and unpacking a 20-30 GB ORCA archive,
# installing OpenMPI -- happens with no watchdog running, because the watchdog
# lives inside _run_orca_once. A Google Drive quota page or a stalled mirror
# could therefore consume the entire session and end the chain at window 1 with
# no note and no successor. The setup budget is checked here instead.
SETUP_BUDGET = 0.25 * TIME_LIMIT
_setup_seconds = time.time() - START_TIME
if _setup_seconds > SETUP_BUDGET:
    log("[setup] WARNING: setting up took %s, which is more than a quarter of this "
        "window's budget. ORCA gets what is left." % time.strftime(
            "%Hh%Mm", time.gmtime(_setup_seconds)))

log("[run] Starting ORCA (%d process(es), %d cores available) ..." % (eff_nprocs, real_cores))
returncode = _run_orca_once()


OUT_SCAN_TAIL_BYTES = 8 * 1024 * 1024      # every decision marker lives near the end
OUT_EARLY_MARKERS = (
    "ORCA TERMINATED NORMALLY", "FINAL SINGLE POINT ENERGY", "VIBRATIONAL FREQUENCIES",
    "THE OPTIMIZATION HAS CONVERGED", "THE NEB OPTIMIZATION HAS CONVERGED",
    "THE IRC HAS CONVERGED", "SCF NOT CONVERGED AFTER", "ORCA finished by error termination",
    "ORCA TERMINATED ABNORMALLY", "UNRECOGNIZED OR DUPLICATED KEYWORD", "INPUT ERROR",
    "not enough memory", "aborting the run",
)


def _read_out():
    """The tail of the ORCA output, plus any decision marker seen earlier.

    Reading the whole file was a real way to lose a chain. A multi-day NEB or
    Opt writes a .out of several GB; `fh.read()` plus the `.lower()` copy inside
    _has_marker needs twice that in transient RSS, and it happens at exactly the
    moment the successor still has to be built and pushed. An OOM kill there
    ends the chain and shows the user nothing but a notebook that stopped.

    Markers are near the end for every decision made here, so the tail is read
    directly and the rest of the file is streamed once, line by line, only to
    record which markers appeared. A marker line is kept verbatim so the
    classification below is unchanged."""
    if not os.path.exists(out_path):
        return ""
    try:
        size = os.path.getsize(out_path)
        with open(out_path, "r", errors="replace") as fh:
            if size <= OUT_SCAN_TAIL_BYTES:
                return fh.read()
            early = []
            budget = size - OUT_SCAN_TAIL_BYTES
            read = 0
            for line in fh:
                read += len(line)
                if any(m in line for m in OUT_EARLY_MARKERS):
                    early.append(line.rstrip("\n"))
                if read >= budget:
                    break
            tail = fh.read()
        header = ("[Chemistry Lab] This .out is %s; the classification below was made "
                  "from its last %s plus these marker lines from earlier in the file:\n%s\n"
                  "----- tail of the ORCA output -----\n"
                  % (_gb(size), _gb(OUT_SCAN_TAIL_BYTES), "\n".join(early[-200:])))
        return header + tail
    except OSError:
        return ""


out_text = _read_out()

MPI_ERROR_MARKERS = (
    "mpirun", "orterun", "prterun", "orted", "mpi_abort", "mpi_init",
    "there are not enough slots", "open mpi", "ompi_", "opal_",
    "error in mpi", "aborting the run because of an mpi",
)
DISK_ERROR_MARKERS = (
    "no space left on device", "errno 28", "disk full", "disk quota exceeded",
    "not enough disk space", "i/o operation failed", "error writing",
    "write error", "failed to write", "cannot write", "input/output error",
)


def _has_marker(text, markers):
    low = text.lower()
    return any(m in low for m in markers)


# A parallel start-up failure normally happens within a couple of minutes and
# leaves an MPI fingerprint in the output. Retrying serially inside the SAME
# session turns a guaranteed failure into a completed (if slower) calculation.
if (eff_nprocs > 1 and stop_reason["why"] is None
        and "ORCA TERMINATED NORMALLY" not in out_text
        and _has_marker(out_text, MPI_ERROR_MARKERS)
        and time.time() - START_TIME < TIME_LIMIT * 0.5):
    log("[mpi] The parallel run failed with an MPI error. Retrying SERIALLY in this same session ...")
    try:
        shutil.copyfile(out_path, _wp(BASENAME + ".parallel_attempt.out"))
    except OSError:
        pass
    eff_nprocs = 1
    mpi_available = False
    inp_text = _clamp_maxcore(_set_nprocs(inp_text, 1), 1)
    with open(inp_path, "w", encoding="utf-8") as fh:
        fh.write(inp_text)
    returncode = _run_orca_once()
    out_text = _read_out()


# ── 5. Classify the outcome ────────────────────────────────────────────────
# "ORCA TERMINATED NORMALLY" is the only reliable all-done signal: for an
# Opt Freq it appears only after BOTH the optimization and the frequencies.
orca_normal_end = "ORCA TERMINATED NORMALLY" in out_text
# ORCA prints "*** OPTIMIZATION RUN DONE ***" whenever the optimizer LOOP
# exits -- including after every relaxed-scan point and after a run that
# exhausted %geom MaxIter. It is not a convergence marker, and treating it as
# one is how an Opt Freq came to strip its own `Opt` keyword and compute
# thermochemistry at a structure with a non-zero gradient: the harmonic
# approximation does not hold there, so ZPE, entropy and Gibbs energy are all
# wrong, and an OptTS would report a "validated" transition state that is not
# a saddle point. Only ORCA's explicit convergence banners count.
opt_converged = ("THE OPTIMIZATION HAS CONVERGED" in out_text
                 or "THE NEB OPTIMIZATION HAS CONVERGED" in out_text)
orca_error = any(marker in out_text for marker in (
    "ORCA finished by error termination",
    "aborting the run",
    "ORCA TERMINATED ABNORMALLY",
    "UNRECOGNIZED OR DUPLICATED KEYWORD",
    "INPUT ERROR",
    "not enough memory",
))
disk_failure = _has_marker(out_text, DISK_ERROR_MARKERS)
free_after = _free_bytes(WORKDIR)
if free_after is not None and free_after < MIN_FREE_BYTES and not orca_normal_end:
    disk_failure = True

log("done=%s opt_converged=%s stopped_by=%s orca_error=%s disk_failure=%s "
    "restart=%d disk_restart=%d free=%s"
    % (orca_normal_end, opt_converged, stop_reason["why"], orca_error, disk_failure,
       RESTART_COUNT, DISK_RESTART_COUNT, _gb(free_after)))

# The job type was classified once, strictly, in section 3a -- from the keyword
# line and the block headers rather than from a substring of the whole file.
# Re-deriving it here from `inp_text.lower()` is what used to send a molecule
# named "nebivolol" down the NEB restart path and a comment reading "scan of the
# Fe complex" down the scan path, in both cases making no progress for twenty
# windows while reporting that the job had resumed.
low = KW


# ── Corruption-safe restart helpers (gbw-free continuation) ────────────────
# We never reuse the binary .gbw wavefunction: a force-killed job can leave it
# half-written, and ORCA AutoStart would then MORead it and abort with
# "GBWFile is corrupt / I/O OPERATION FAILED". Instead we continue from plain
# ASCII coordinate files (.xyz / _trj.xyz / .allxyz / .NNN.xyz), which survive
# a hard kill, and we force "! NoAutoStart" so any stray .gbw is ignored.
def _extract_charge_mult(text):
    m = re.search(r"\*\s*xyz(?:file)?\s+(-?\d+)\s+(-?\d+)", text, re.IGNORECASE)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"\*\s*(?:gzmt|internal|int)\s+(-?\d+)\s+(-?\d+)", text, re.IGNORECASE)
    if m:
        return m.group(1), m.group(2)
    return "0", "1"


# _ensure_simple_keyword and _strip_moread are defined once, in section 3a.
# They used to be re-declared here, and because this copy is executed LATER it
# silently replaced the comment-aware version -- so `! Opt  # test` got its
# NoAutoStart appended inside the comment.
def _set_geometry(text, xyzname, charge, mult):
    """Points the input at `xyzname` as its geometry. Returns (text, placed).

    `placed` is what matters. Every ORCA coordinate form has to be handled,
    because a substitution that quietly does nothing is worse than an error:
    the continuation kernel then re-runs the ORIGINAL geometry, reports "resumed
    from trajectory step N" in the job note, and repeats that for every window
    until the restart cap -- burning days of compute while claiming progress.
    Internal-coordinate blocks (gzmt / int / internal) used to do exactly that,
    since only the `xyz` forms were matched. They are converted to `xyzfile`
    here: the molecule is unchanged, only the coordinate system it is fed in."""
    repl = "* xyzfile " + str(charge) + " " + str(mult) + " " + xyzname
    # Block forms: * xyz|gzmt|int|internal <charge> <mult> ... *
    new, n = re.subn(r"\*\s*(?:xyz|gzmt|int|internal)\s+-?\d+\s+-?\d+.*?\*",
                     lambda m: repl, text, flags=re.IGNORECASE | re.DOTALL)
    if n:
        return new, True
    # File form: * xyzfile|gzmtfile <charge> <mult> <name>
    new, n = re.subn(r"\*\s*(?:xyzfile|gzmtfile)\s+-?\d+\s+-?\d+\s+\S+",
                     lambda m: repl, text, flags=re.IGNORECASE)
    if n:
        return new, True
    return text, False


def _xyz_is_complete(path):
    """True only if the file is a whole XYZ frame. A .xyz written by a process
    that was force-killed mid-write can end anywhere, and ORCA fed a truncated
    one either aborts or -- worse -- silently optimises a fragment."""
    try:
        with open(path, "r", errors="replace") as fh:
            lines = [ln.rstrip("\n") for ln in fh]
    except OSError:
        return False
    while lines and not lines[0].strip():
        lines.pop(0)
    if len(lines) < 3:
        return False
    try:
        natoms = int(lines[0].split()[0])
    except (ValueError, IndexError):
        return False
    body = [ln for ln in lines[2:2 + natoms] if ln.strip()]
    if natoms <= 0 or len(body) < natoms:
        return False
    for ln in body:
        parts = ln.split()
        if len(parts) < 4:
            return False
        try:
            [float(v) for v in parts[1:4]]
        except ValueError:
            return False
    return True


def _input_natoms(text, work_dir_lookup):
    """Atom count the input describes, or None when it cannot be told.

    Used to reject a restart geometry whose atom count does not match the
    molecule that was submitted -- a mismatch means the trajectory belongs to
    something else, and continuing from it would silently produce results for
    a different system."""
    m = re.search(r"\*\s*(?:xyz|gzmt|int|internal)\s+-?\d+\s+-?\d+(.*?)\*",
                  text, re.IGNORECASE | re.DOTALL)
    if m:
        rows = [ln for ln in m.group(1).splitlines() if ln.strip()]
        return len(rows) or None
    m = re.search(r"\*\s*xyzfile\s+-?\d+\s+-?\d+\s+(\S+)", text, re.IGNORECASE)
    if m:
        path = work_dir_lookup(m.group(1))
        try:
            with open(path, "r", errors="replace") as fh:
                return int(fh.readline().split()[0])
        except (OSError, ValueError, IndexError):
            return None
    return None


def _read_trj_frames(trj_path):
    # ORCA *_trj.xyz is concatenated multi-XYZ with NO blank lines between
    # frames: [natoms] / [comment] / natoms coordinate lines, repeated. Returns
    # every COMPLETE frame; a truncated tail (killed mid-write) is discarded.
    try:
        with open(trj_path, "r", errors="replace") as fh:
            lines = [ln.rstrip("\n") for ln in fh]
    except OSError:
        return []
    frames, i, ntot = [], 0, len(lines)
    while i < ntot:
        head = lines[i].strip()
        if not head:
            i += 1
            continue
        try:
            natoms = int(head.split()[0])
        except (ValueError, IndexError):
            break
        if natoms <= 0 or i + 2 + natoms > ntot:
            break
        frame = lines[i:i + 2 + natoms]
        if all(len(frame[2 + k].split()) >= 4 for k in range(natoms)):
            frames.append("\n".join(frame))
        else:
            break
        i += 2 + natoms
    return frames


def _last_complete_frame(trj_path):
    frames = _read_trj_frames(trj_path)
    return frames[-1] if frames else None


def _frame_natoms(frame_text):
    try:
        return int(frame_text.split("\n", 1)[0].split()[0])
    except (ValueError, IndexError):
        return 0


def _is_complete_hess(path):
    # An ORCA .hess is ASCII, but a run killed mid-write leaves the $hessian
    # matrix truncated, and pointing "InHess Read" at that would abort the new
    # run. ORCA prints the NxN matrix in column blocks (a header line of column
    # indices, then N data rows). Require every row 0..N-1 to accumulate all N
    # column values across the blocks, so a mid-block truncation is rejected.
    def _is_int(tok):
        try:
            int(tok)
            return True
        except ValueError:
            return False

    try:
        with open(path, "r", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return False
    h = -1
    for idx, ln in enumerate(lines):
        if ln.strip().lower() == "$hessian":
            h = idx
            break
    if h < 0:
        return False
    try:
        n = int(lines[h + 1].strip())
    except (IndexError, ValueError):
        return False
    if n <= 0:
        return False
    counts = {}
    for ln in lines[h + 2:]:
        s = ln.split()
        if not s:
            continue
        if s[0].startswith("$"):
            break
        if all(_is_int(t) for t in s):
            continue                     # column-index header line -> skip
        try:
            r = int(s[0])
        except ValueError:
            continue
        vals = s[1:]
        try:
            for v in vals:
                float(v)
        except ValueError:
            continue
        if 0 <= r < n:
            counts[r] = counts.get(r, 0) + len(vals)
    return len(counts) == n and all(counts.get(r, 0) == n for r in range(n))


def _allxyz_is_complete(path):
    # ORCA .allxyz = XYZ frames separated by lines that are exactly ">". Used to
    # confirm a NEB *_MEP.allxyz snapshot was not truncated mid-rewrite before
    # we ever feed it back through Restart_ALLXYZFile.
    try:
        with open(path, "r", errors="replace") as fh:
            lines = [ln.rstrip("\n") for ln in fh]
    except OSError:
        return False
    groups, cur = [], []
    for ln in lines:
        if ln.strip() == ">":
            groups.append(cur)
            cur = []
        else:
            cur.append(ln)
    groups.append(cur)
    frames, natoms0 = 0, None
    for g in groups:
        g = [x for x in g if x.strip() != ""]
        if not g:
            return False
        try:
            na = int(g[0].split()[0])
        except (ValueError, IndexError):
            return False
        coords = g[2:2 + na]
        if len(coords) < na or any(len(c.split()) < 4 for c in coords):
            return False
        if natoms0 is None:
            natoms0 = na
        elif na != natoms0:
            return False
        frames += 1
    return frames >= 2


trj_now = sorted(glob.glob(_wp(BASENAME + "_trj.xyz"))) or \
          [t for t in glob.glob(_wp("*.xyz")) if "_trj" in os.path.basename(t)]
frames_now = len(_read_trj_frames(trj_now[0])) if trj_now else 0
hess_ready = (os.path.exists(_wp(BASENAME + ".hess"))
              and _is_complete_hess(_wp(BASENAME + ".hess")))
scan_points_done = len(glob.glob(_wp(BASENAME + ".[0-9][0-9][0-9].xyz")))


# ── 5b. Did it really finish? ─────────────────────────────────────────────
# "ORCA TERMINATED NORMALLY" means "ORCA exited on its own terms", NOT "the
# calculation reached its goal". An optimization that runs out of %geom
# MaxIter, a scan that stops halfway, a NEB that never converges and a Freq
# that was never reached all end this way. Believing that line is the "false
# completion" that silently ends a job which was still making progress -- so
# every milestone the input actually asked for is checked here, and a missing
# one is a reason to CONTINUE rather than to declare success.
NOT_CONVERGED_MARKERS = (
    "the optimization did not converge",
    "optimization has not converged",
    "did not converge but reached the maximum",
    "maximum number of optimization cycles",
    "maximum number of optimization iterations",
    "the neb optimization has not converged",
    "serious problem in the geometry optimization",
)
scf_stalled = bool(re.search(r"(?i)SCF\s+NOT\s+CONVERGED\s+AFTER", out_text))
# A long optimization can have one bad SCF at an early geometry and still
# finish perfectly. "FINAL SINGLE POINT ENERGY" is only printed once an SCF
# has actually converged, so it is what separates "an SCF stumbled somewhere"
# from "the calculation ended without a converged wavefunction".
scf_unresolved = scf_stalled and "FINAL SINGLE POINT ENERGY" not in out_text
freq_done = "VIBRATIONAL FREQUENCIES" in out_text
neb_converged = ("THE NEB OPTIMIZATION HAS CONVERGED" in out_text
                 or "NEB OPTIMIZATION CONVERGED" in out_text.upper())
irc_converged = "THE IRC HAS CONVERGED" in out_text.upper()


def _requested_scan_points(text):
    """Number of points the scan asks for, for the `= a, b, N` form.

    The bracketed list form (`Scan B 1 2 [2.0 1.9 1.8] end`) is counted too,
    because a scan whose size cannot be read is a scan whose progress cannot be
    audited -- and an unauditable scan used to be reported as complete."""
    m = re.search(r"(?i)Scan\s+[BADC][\d\s]*?=\s*-?\d+(?:\.\d+)?\s*,\s*"
                  r"-?\d+(?:\.\d+)?\s*,\s*(\d+)", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(?i)Scan\s+[BADC][\d\s]*?\[([^\]]*)\]", text)
    if m:
        return len(m.group(1).split())
    return 0


def _completion_gap():
    """Empty string means the calculation genuinely reached every milestone its
    input asked for. Anything else names the first one it missed.

    Every requested milestone is checked; the audit does not stop at the first
    job type it recognises. `! NEB-TS Freq` used to return as soon as the band
    converged, so the transition-state optimisation that followed could run out
    of cycles and the frequencies could never run, and the job was still
    reported complete -- handing the user a structure that is not a saddle
    point and calling it a transition state."""
    if not orca_normal_end:
        return "ORCA never reported a normal termination"

    if is_neb_job:
        if not neb_converged:
            return "the NEB path never converged"
        # For NEB-TS/NEB-CI the band is only the first half of the job.
        tail = out_text[out_text.rfind("THE NEB OPTIMIZATION HAS CONVERGED"):]
        if _has_marker(tail, NOT_CONVERGED_MARKERS):
            return "the NEB band converged but the transition-state optimisation did not"

    if is_irc_job and not irc_converged:
        return "the IRC path never converged"

    if has_scan:
        want = SCAN_TOTAL_POINTS or _requested_scan_points(ORIGINAL_INP_TEXT)
        done_total = SCAN_POINTS_BEFORE + scan_points_done
        if want and done_total < want:
            return "the scan stopped at point %d of %d" % (done_total, want)
        if not want:
            return "the scan's extent could not be read, so its completion cannot be verified"
        if _has_marker(out_text, NOT_CONVERGED_MARKERS):
            return "a scan point ran out of optimization cycles"
    elif is_opt_job and not opt_converged:
        return "the geometry optimization never converged"

    if freq_requested and not freq_done:
        return "the frequency step never produced VIBRATIONAL FREQUENCIES"
    if scf_unresolved:
        return "the SCF did not converge"
    if not (has_scan or is_neb_job) and _has_marker(out_text, NOT_CONVERGED_MARKERS):
        return "ORCA reported that it stopped without converging"
    return ""


completion_gap = _completion_gap() if (orca_normal_end and not orca_error) else ""
if completion_gap:
    log("[audit] ORCA exited normally but %s -- this is NOT a finished calculation."
        % completion_gap)


# ── 6. Decide: continue in a fresh kernel, or finish here? ────────────────
_no_restart_reason = ""
restart_kind = None            # "time" | "disk" | "budget" | "scf" | None

DISK_ADVICE = (
    "Ways to cut the disk footprint: add RIJCOSX (and an /J auxiliary basis) for hybrid DFT, "
    "use RI-MP2/DLPNO-CCSD(T) instead of the conventional variants, shrink the basis set, "
    "reduce the number of TD-DFT roots, or switch to a composite method such as r2SCAN-3c."
)

if orca_normal_end and not orca_error and stop_reason["why"] is None:
    if completion_gap and is_iterative:
        # The window ended on ORCA's own iteration budget rather than on the
        # session clock. Continue from the latest ASCII checkpoint with a
        # LARGER budget -- restarting with the same one would reproduce this
        # window exactly.
        needs_continue = True
        restart_kind = "budget"
    else:
        needs_continue = False                              # finished cleanly
elif stop_reason["why"] == "time":
    needs_continue = True
    restart_kind = "time"
elif stop_reason["why"] == "disk" or (disk_failure and not orca_normal_end):
    # THIS is the case that used to end a run for good: the scratch disk filled
    # up, ORCA (or the copy step afterwards) died, and nothing was continued.
    needs_continue = True
    restart_kind = "disk"
elif orca_error and scf_stalled and int(SCF_MAXITER) < MAX_SCF_MAXITER:
    # An SCF that ran out of cycles is a *budget* failure, not a broken input:
    # one retry with a bigger SCF budget and SlowConv is worth far more than
    # ending a calculation that may be a few dozen iterations from converging.
    needs_continue = True
    restart_kind = "scf"
elif orca_error:
    needs_continue = False                                  # real error, not a resource limit
    _no_restart_reason = ("ORCA stopped with an error (this was not a time-out or a disk-space "
                          "problem, so relaunching the same input would fail the same way). Last "
                          "lines of the ORCA output:\n"
                          + "\n".join(out_text.strip().splitlines()[-15:]))
else:
    needs_continue = False
    _no_restart_reason = ("The run ended without finishing, without hitting the session time "
                          "limit, and without a disk-space problem (an unexpected stop — most "
                          "often the machine's memory ran out). Not auto-restarting so this "
                          "cannot loop silently. Try lowering %maxcore or the number of "
                          "processes, then resubmit.")

if needs_continue and REFUSE_CONTINUATION:
    # Section 3a decided this input cannot be rewritten faithfully. Continuing
    # it would produce a converged-looking number for a calculation that is not
    # the one the person submitted, which is worse than stopping.
    needs_continue = False
    _no_restart_reason = ("This calculation was not continued automatically because "
                          + REFUSE_CONTINUATION + ". The results here are the progress "
                          "from this session; resubmit from the latest geometry to carry on.")

made_progress = bool(frames_now or scan_points_done or hess_ready)

if needs_continue and restart_kind == "budget" and not made_progress:
    # Nothing completed in this window, so a fresh one would repeat it exactly.
    needs_continue = False
    _no_restart_reason = ("ORCA ended normally but %s, and this session produced no completed "
                          "step to continue from, so restarting would repeat it exactly. Check "
                          "the input and the last lines of the ORCA output." % completion_gap)

if (needs_continue and restart_kind == "budget"
        and int(GEOM_MAXITER) >= MAX_GEOM_MAXITER):
    needs_continue = False
    _no_restart_reason = ("The optimization has already been given the maximum cycle budget "
                          "(%d) across restarts and still has not converged (%s). It is very "
                          "likely stuck on a flat or oscillating surface -- try tightening the "
                          "starting geometry, loosening the convergence thresholds, or a "
                          "different optimizer/coordinate system." % (MAX_GEOM_MAXITER,
                                                                      completion_gap))

if needs_continue and restart_kind == "scf" and not is_iterative and RESTART_COUNT >= 1:
    # A single point gets exactly one bigger-SCF retry, never a loop.
    needs_continue = False
    _no_restart_reason = ("The SCF still did not converge with an enlarged iteration budget. "
                          "Try a different initial guess (e.g. ! PModel), SlowConv/VerySlowConv, "
                          "a smaller basis for a pre-converged guess, or check the geometry.")

if needs_continue and restart_kind not in ("budget", "scf") and not is_iterative:
    # A single point has no text checkpoint. One clean retry is still worth it
    # after a disk problem (the fresh session starts with an empty scratch and
    # ORCA is unpacked without leftovers); a second one never is.
    if restart_kind == "disk" and DISK_RESTART_COUNT == 0:
        pass
    else:
        needs_continue = False
        _no_restart_reason = (
            "This job (single point / TD-DFT with no optimization, scan, MD, or numerical-"
            "frequency loop) leaves no text checkpoint to continue from, so a restart would "
            "begin again from zero and hit the same wall. " + DISK_ADVICE)

if needs_continue and restart_kind == "disk" and DISK_RESTART_COUNT >= MAX_DISK_RESTARTS:
    needs_continue = False
    _no_restart_reason = ("The scratch disk filled up %d times, so this calculation needs more "
                          "temporary space than a Kaggle session provides. " % DISK_RESTART_COUNT
                          + DISK_ADVICE)

if (needs_continue and restart_kind == "disk" and DISK_RESTART_COUNT >= 1
        and frames_now == 0 and not hess_ready):
    needs_continue = False
    _no_restart_reason = ("The scratch disk filled up again before a single step completed, so "
                          "restarting cannot make progress. " + DISK_ADVICE)

# An optimization that made no step AND produced no Hessian in a whole session
# will never get anywhere by relaunching — stop after one grace window.
if (needs_continue and restart_kind == "time" and is_opt_job and not is_neb_job
        and not has_scan and frames_now == 0 and not hess_ready and RESTART_COUNT >= 1):
    needs_continue = False
    _no_restart_reason = ("A full session was not enough to finish even one optimization step, so "
                          "auto-restart cannot make progress. Reduce the per-step cost (smaller "
                          "basis set, add RIJCOSX/RI, or use a composite method like r2SCAN-3c) "
                          "and resubmit.")

if needs_continue and RESTART_COUNT >= MAX_RESTARTS:
    needs_continue = False
    _no_restart_reason = ("Reached the auto-restart cap (%d) without the calculation converging. "
                          "This is the latest partial progress." % MAX_RESTARTS)

# The ORCA iteration budgets the successor kernel will be built with. They are
# only ever raised, never lowered, and both are hard-capped so a pathological
# job cannot escalate forever.
NEXT_GEOM_MAXITER = int(GEOM_MAXITER)
NEXT_SCF_MAXITER = int(SCF_MAXITER)
if needs_continue and restart_kind == "budget":
    # Escalate the counter the job type actually uses. NEB counts its own
    # iterations in %neb and IRC in %irc; doubling %geom MaxIter for those left
    # the successor with the identical budget, so it reproduced the window it
    # was meant to advance past -- for every one of the twenty restarts.
    _budget_block = "neb" if is_neb_job else ("irc" if (is_irc_job and not is_opt_job)
                                              else "geom")
    _default = DEFAULT_SCAN_MAXITER if has_scan else DEFAULT_GEOM_MAXITER
    _used = max(int(GEOM_MAXITER), _block_value(inp_text, _budget_block, "maxiter") or 0) \
        or _default
    NEXT_GEOM_MAXITER = min(MAX_GEOM_MAXITER, max(_default, _used * 2))
if needs_continue and restart_kind == "scf":
    _used = max(int(SCF_MAXITER), _block_value(inp_text, "scf", "maxiter") or 0) \
        or DEFAULT_SCF_MAXITER
    NEXT_SCF_MAXITER = min(MAX_SCF_MAXITER, max(DEFAULT_SCF_MAXITER, _used * 2))


# ── 7. Result packaging (curated + budgeted, never fatal) ─────────────────
# Scratch files ORCA can regenerate but which are routinely GB-sized. They are
# removed only AFTER the restart checkpoints have been captured, both to free
# room for the results archive and to keep the notebook's saved output small.
PURGE_SUFFIXES = (
    ".tmp", ".ges", ".densities", ".densitiesinfo", ".cis", ".bas", ".basinfo",
    ".int", ".ijkl", ".rijk", ".fint", ".pmp2int", ".mp2int", ".lastint",
    ".hostnames", ".pcgrad", ".uco", ".opttmp", ".ltmp", ".stmp",
)
# Ordered whitelist: what a chemist actually wants back, most important first.
KEEP_PATTERNS = (
    BASENAME + ".out", "*.inp", "JOB_NOTE.txt", "MANIFEST.txt", "HISTORY.txt",
    BASENAME + ".property.txt", BASENAME + ".xyz", BASENAME + "_trj.xyz",
    "*.allxyz", "*.hess", "*.engrad", BASENAME + ".[0-9][0-9][0-9].xyz",
    BASENAME + ".res.*", BASENAME + ".mdrestart", BASENAME + ".opt",
    "*.gbw", "*.nbo", "*.molden*", "*.cube", "*.pdb", "*.mdcrd",
    "*.txt", "*.out", "*.xyz", "*.log", "*.dat", "*.csv",
)
SMALL_FILE_BYTES = 8 * 1024 * 1024      # anything else this small comes along too
OUT_TAIL_BYTES = 8 * 1024 * 1024


def _purge_scratch_junk():
    freed = 0
    for root, _dirs, fnames in os.walk(WORKDIR):
        for fn in fnames:
            low = fn.lower()
            if low.endswith(PURGE_SUFFIXES) or re.search(r"\.tmp[.\d]*$", low):
                path = os.path.join(root, fn)
                try:
                    size = os.path.getsize(path)
                    os.remove(path)
                    freed += size
                except OSError:
                    pass
    if freed:
        log("[disk] Removed %s of regenerable ORCA scratch files." % _gb(freed))
    return freed


def _ordered_result_files():
    seen, ordered = set(), []
    for pattern in KEEP_PATTERNS:
        for path in sorted(glob.glob(_wp(pattern))):
            key = os.path.basename(path)
            if key in seen or not os.path.isfile(path):
                continue
            seen.add(key)
            ordered.append(path)
    for path in sorted(glob.glob(_wp("*"))):
        key = os.path.basename(path)
        if key in seen or not os.path.isfile(path):
            continue
        try:
            if os.path.getsize(path) <= SMALL_FILE_BYTES:
                seen.add(key)
                ordered.append(path)
        except OSError:
            pass
    return ordered


def _write_text(path, text):
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text.rstrip() + "\n")
    except OSError as exc:
        log("[results] Could not write %s: %s" % (path, exc))


def _package_results(note=""):
    # Zips straight from the scratch directory into /kaggle/working, so the
    # results never have to exist twice inside the 20 GB output quota. Every
    # step is best-effort: if the disk is full we still want the .out and the
    # note to reach the user rather than losing the whole window's work.
    manifest, included, skipped, total = [], 0, [], 0
    zip_path = os.path.join(OUTPUT_DIR, "results.zip")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for path in _ordered_result_files():
                name = os.path.basename(path)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if total + size > RESULT_BUDGET_BYTES:
                    skipped.append("%s (%s, over the %s archive budget)"
                                   % (name, _gb(size), _gb(RESULT_BUDGET_BYTES)))
                    continue
                try:
                    zf.write(path, name)
                except OSError as exc:
                    skipped.append("%s (%s)" % (name, exc))
                    continue
                total += size
                included += 1
                manifest.append("%-40s %s" % (name, _gb(size)))
    except OSError as exc:
        log("[results] Packaging hit a disk error (%s); falling back to text-only output." % exc)
        try:
            os.remove(zip_path)
        except OSError:
            pass
        zip_path = None

    # Loose copies of the small, human-readable files so they can be previewed
    # on Kaggle without downloading the whole archive.
    for pattern in (BASENAME + ".out", "*.inp", BASENAME + ".property.txt",
                    BASENAME + ".xyz", "*.parallel_attempt.out"):
        for path in sorted(glob.glob(_wp(pattern))):
            try:
                size = os.path.getsize(path)
                dest = os.path.join(OUTPUT_DIR, os.path.basename(path))
                if size <= 64 * 1024 * 1024:
                    shutil.copyfile(path, dest)
                else:
                    with open(path, "rb") as src, open(dest + ".tail.txt", "wb") as out:
                        src.seek(max(0, size - OUT_TAIL_BYTES))
                        shutil.copyfileobj(src, out)
            except OSError as exc:
                log("[results] Could not copy %s: %s" % (os.path.basename(path), exc))

    _write_text(os.path.join(OUTPUT_DIR, "MANIFEST.txt"),
                "Files in results.zip (%d, %s total):\n%s%s"
                % (included, _gb(total), "\n".join(manifest),
                   ("\n\nLeft out:\n" + "\n".join(skipped)) if skipped else ""))
    if note:
        _write_text(os.path.join(OUTPUT_DIR, "JOB_NOTE.txt"), note)
    if zip_path and os.path.exists(zip_path):
        log("[results] Packaged %d file(s) into %s (%s on disk)."
            % (included, zip_path, _gb(os.path.getsize(zip_path))))
    else:
        log("[results] No archive could be written; loose files above are the output.")


# ── 8. Continuation kernel ────────────────────────────────────────────────
def _write_kaggle_credentials():
    cfg_dir = os.path.expanduser("~/.kaggle")
    os.makedirs(cfg_dir, exist_ok=True)
    os.environ["KAGGLE_USERNAME"] = KAGGLE_USERNAME
    os.environ["KAGGLE_CONFIG_DIR"] = cfg_dir
    if KAGGLE_API_TOKEN:
        os.environ["KAGGLE_API_TOKEN"] = KAGGLE_API_TOKEN
        os.environ["KAGGLE_KEY"] = KAGGLE_API_TOKEN
        with open(os.path.join(cfg_dir, "access_token"), "w") as fh:
            fh.write(KAGGLE_API_TOKEN)
        try:
            os.chmod(os.path.join(cfg_dir, "access_token"), 0o600)
        except OSError:
            pass
        with open(os.path.join(cfg_dir, "kaggle.json"), "w") as fh:
            json.dump({"username": KAGGLE_USERNAME, "key": KAGGLE_API_TOKEN}, fh)
        try:
            os.chmod(os.path.join(cfg_dir, "kaggle.json"), 0o600)
        except OSError:
            pass
    else:
        os.environ["KAGGLE_KEY"] = KAGGLE_KEY
        with open(os.path.join(cfg_dir, "kaggle.json"), "w") as fh:
            json.dump({"username": KAGGLE_USERNAME, "key": KAGGLE_KEY}, fh)
        try:
            os.chmod(os.path.join(cfg_dir, "kaggle.json"), 0o600)
        except OSError:
            pass


_PUSH_URL_RE = re.compile(r"https?://(?:www\.)?kaggle\.com/(?:code/)?([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)")


def _run_cli_cmd(args, **kwargs):
    cmd = list(args)
    if sys.platform == "win32":
        env = kwargs.get("env")
        path = env.get("PATH") if isinstance(env, dict) else os.environ.get("PATH")
        resolved = shutil.which(cmd[0], path=path)
        if resolved:
            cmd[0] = resolved
            if resolved.lower().endswith((".bat", ".cmd")):
                kwargs["shell"] = True
    return subprocess.run(cmd, **kwargs)


def _successor_already_exists(slug):
    """True if Kaggle already has this successor.

    Two ways the push is not idempotent without this check. A push that times
    out client-side after Kaggle accepted it makes the retry create a SECOND
    version of the same slug, so the successor runs twice and both copies go on
    to push the next window, one clobbering the other's checkpoint. And a user
    who sees a stopped notebook on kaggle.com and clicks Run re-executes it with
    its original RESTART_COUNT, re-pushing the same successor from an older
    checkpoint -- silently rewinding days of progress."""
    try:
        probe = _run_cli_cmd(
            ["kaggle", "kernels", "status", KAGGLE_USERNAME + "/" + slug],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
        )
    except Exception:  # noqa: BLE001
        return False
    text = (probe.stdout or "") + (probe.stderr or "")
    if probe.returncode != 0:
        return False
    return bool(re.search(r'status\s+"[^"]*(?:RUNNING|QUEUED|COMPLETE|NEW_SCRIPT)',
                          text, re.IGNORECASE))


def _push_continuation_with_retries(job_dir, max_attempts=5, base_delay=15.0):
    # Retries the kind of transient SSL/network error api.kaggle.com can throw
    # right as a 12-hour session wraps up. Nobody is watching this happen, so a
    # single blip must not silently end the chain and strand the progress.
    # Bounded by max_attempts AND by a wall-clock deadline derived from Kaggle's
    # real session limit, so packaging still gets its reserved time.
    # Measured from TIME_LIMIT (a value we control) rather than from Kaggle's
    # platform cap: the 12-hour clock starts at SESSION start, which includes
    # pulling the image and mounting a 20-30 GB ORCA dataset, so a deadline
    # anchored to the cap can already be spent before this script's first line.
    deadline = min(START_TIME + HARD_SESSION_LIMIT_SECONDS,
                   time.time() + max(600, TIME_LIMIT * 0.05)) - RESTART_PUSH_SAFETY_MARGIN
    slug_wanted = os.path.basename(job_dir)
    last = None
    for attempt in range(1, max_attempts + 1):
        remaining = deadline - time.time()
        if remaining <= 10:
            log("[auto-continue] Out of safe time budget before attempt %d; giving up." % attempt)
            break
        try:
            last = _run_cli_cmd(
                ["kaggle", "kernels", "push", "-p", job_dir],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=max(30, min(120, remaining - 5)),
            )
        except subprocess.TimeoutExpired:
            log("[auto-continue] push attempt %d/%d timed out" % (attempt, max_attempts))
            last = None
            if _successor_already_exists(slug_wanted):
                log("[auto-continue] The push actually landed before the timeout; "
                    "not pushing again.")
                return slug_wanted, ("https://www.kaggle.com/code/" + KAGGLE_USERNAME
                                     + "/" + slug_wanted), "recovered after timeout"
        else:
            combined = (last.stdout or "") + (last.stderr or "")
            if last.returncode == 0 and "error" not in (last.stdout or "").lower():
                m = _PUSH_URL_RE.search(combined)
                return (m.group(2) if m else None), (m.group(0) if m else None), combined
            log("[auto-continue] push attempt %d/%d failed (exit %s): %s"
                % (attempt, max_attempts, last.returncode, combined.strip()[-400:]))
        if attempt == max_attempts:
            break
        delay = min(base_delay * (2 ** (attempt - 1)), 90) + random.uniform(0, 5)
        if time.time() + delay > deadline:
            log("[auto-continue] Not enough time left for another retry; giving up.")
            break
        time.sleep(delay)
    detail = "no successful attempt (see the attempt log above)"
    if last is not None:
        detail = ((last.stderr or last.stdout or "unknown error").strip()[-400:])
    raise RuntimeError("kaggle kernels push failed after retries: " + detail)


def _build_next_input_and_carry():
    # Returns (next_input_text, carry_items, notes). carry_items are
    # (priority, filename, bytes) — lower priority number = more important.
    charge, mult = _extract_charge_mult(ORIGINAL_INP_TEXT)
    text = _strip_moread(ORIGINAL_INP_TEXT)
    items, notes = [], []

    def carry(path, priority=50):
        try:
            if os.path.getsize(path) > CARRY_FILE_RAW_LIMIT:
                notes.append("skipped carrying %s (too large)" % os.path.basename(path))
                return False
            with open(path, "rb") as fh:
                items.append((priority, os.path.basename(path), fh.read()))
            return True
        except OSError:
            return False

    if is_neb_job:
        mep = sorted(glob.glob(_wp("*_MEP.allxyz")))
        snap = os.path.basename(mep[0]) if (mep and _allxyz_is_complete(mep[0])) else None
        if snap:
            carry(_wp(snap), 10)
            if re.search(r"(?i)restart_allxyzfile", text):
                pass
            elif re.search(r"(?i)%\s*neb", text):
                text = re.sub(r"(?i)%\s*neb",
                              lambda m: m.group(0) + '\n  Restart_ALLXYZFile "' + snap + '"',
                              text, count=1)
            else:
                text = text.rstrip() + '\n%neb\n  Restart_ALLXYZFile "' + snap + '"\nend\n'
            notes.append("NEB resumed from " + snap)
        else:
            notes.append("NEB restarted from scratch (no complete _MEP.allxyz)")
        for f in glob.glob(_wp("*.xyz")):               # endpoints / product geometries
            if not os.path.basename(f).endswith("_trj.xyz"):
                carry(f, 20)

    elif has_scan:
        # Only frames that were written completely count as finished points; a
        # step file truncated by the kill would otherwise both inflate the
        # progress count and be handed back to ORCA as a geometry.
        step_files = [f for f in sorted(glob.glob(_wp(BASENAME + ".[0-9][0-9][0-9].xyz")))
                      if _xyz_is_complete(f)]
        sm = re.search(
            r"(?i)(Scan\s+[BAD][\d\s]*?=\s*)(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*(\d+)",
            text)
        done = len(step_files)
        # `npts - done == 1` would emit `Scan B 0 1 = b, b, 1`, whose grid step is
        # (end-start)/(N-1) = 0/0. The final point is left to a fresh submission.
        if step_files and sm and int(sm.group(4)) > 1 and 0 < done < int(sm.group(4)) - 1:
            a, b, npts = float(sm.group(2)), float(sm.group(3)), int(sm.group(4))
            step = (b - a) / (npts - 1)
            # "%g" keeps 6 significant digits. On a scan coordinate that is a
            # real displacement of the grid -- of order 1e-6 A -- and it is
            # re-truncated at every window, so the geometries a multi-session
            # scan reports drift away from the ones that were asked for. 17
            # significant digits round-trip a double exactly.
            resumed = (sm.group(1) + ("%.17g" % (a + done * step)) + ", "
                       + ("%.17g" % b) + ", " + str(npts - done))
            text = text[:sm.start()] + resumed + text[sm.end():]
            # Carried under a NEUTRAL name. Shipping it as `job.005.xyz` put a
            # stale step file in the successor's working directory, where ORCA
            # renumbers from .001 -- so the next window counted 3 new points as
            # 4, rewrote the range from the wrong index, and dropped a grid
            # point from the PES with nothing recording the omission.
            resume_xyz = "scan_resume.xyz"
            shutil.copyfile(step_files[-1], _wp(resume_xyz))
            text, placed = _set_geometry(text, resume_xyz, charge, mult)
            if not placed:
                raise RuntimeError(
                    "the scan's coordinate block is in a form this restart cannot "
                    "rewrite, so the next window would silently redo this one")
            carry(_wp(resume_xyz), 10)
            notes.append("scan resumed from point %d/%d"
                         % (SCAN_POINTS_BEFORE + done,
                            SCAN_TOTAL_POINTS or _requested_scan_points(ORIGINAL_INP_TEXT)))
        else:
            notes.append("scan restarted from scratch")

    elif is_md_job:
        # ORCA's %md block is an ordered command list, executed top to bottom.
        # `Restart IfExists` therefore has to sit immediately BEFORE `Run`, and
        # any `Initvel` needs `No_Overwrite`. Inserting Restart at the top --
        # which is what this did -- loaded the saved positions and velocities
        # and then let the user's `Initvel 350_K` on the next line resample the
        # momenta from a fresh Maxwell-Boltzmann distribution. Every session
        # boundary silently restarted the dynamics: energy conservation broken,
        # the velocity autocorrelation function destroyed, and a 20 ps
        # trajectory that is really a sequence of disjoint short runs.
        md_restart = _wp(BASENAME + ".mdrestart")
        if os.path.exists(md_restart):
            carry(md_restart, 10)
        span = _find_block(text, "md")
        if span and "restart" not in text[span[1]:span[2]].lower():
            body = text[span[1]:span[2]]
            body = re.sub(r"(?im)^([ \t]*Initvel\s+\S+)(?![^\n]*No_Overwrite)",
                          r"\1 No_Overwrite", body)
            run_m = None
            for run_m in re.finditer(r"(?im)^[ \t]*Run\b.*$", body):
                pass                                  # keep the LAST Run command
            if run_m:
                body = body[:run_m.start()] + "  Restart IfExists\n" + body[run_m.start():]
            else:
                body = body.rstrip() + "\n  Restart IfExists\n"
            text = text[:span[1]] + body + text[span[2]:]
            notes.append("MD resumed: 'Restart IfExists' inserted immediately before Run, "
                         "and Initvel marked No_Overwrite so the saved momenta are kept")
        else:
            notes.append("MD resumed via the restart command already in the input")
        notes.append("NOTE: the %md Run step count is NOT reduced between windows, so this "
                     "chain runs the requested number of steps per window rather than in "
                     "total. Check the trajectory length before analysing it")
        for f in glob.glob(_wp("*.xyz")):
            if not os.path.basename(f).endswith("_trj.xyz"):
                carry(f, 20)

    elif is_numfreq and (opt_converged or not is_opt_job):
        # A numerical Hessian is assembled from displaced-gradient columns, and
        # ORCA's manual is explicit that every column must come from the SAME
        # geometry and the SAME level of theory -- "any change will produce an
        # inconsistent, essentially meaningless Hessian". So the successor is
        # only allowed to reuse .res.* columns if its geometry is pinned: the
        # optimizer keyword is removed and the converged structure is installed.
        # Otherwise the columns are discarded and the Hessian is recomputed --
        # one wasted window is far cheaper than frequencies, ZPE and a Gibbs
        # energy that are quietly wrong by a few kcal/mol.
        pinned = False
        if opt_converged and _xyz_is_complete(_wp(BASENAME + ".xyz")):
            text = _strip_opt_keyword(text)
            text, placed = _set_geometry(text, BASENAME + ".xyz", charge, mult)
            if not placed:
                raise RuntimeError("the coordinate block could not be repointed at the "
                                   "converged geometry, so the Hessian columns from this "
                                   "window could not be reused safely")
            carry(_wp(BASENAME + ".xyz"), 10)
            pinned = True
        elif not is_opt_job:
            pinned = True

        if pinned:
            span = _find_block(text, "freq")
            if span and "restart" not in text[span[1]:span[2]].lower():
                text = text[:span[1]] + "\n  Restart true\n" + text[span[1]:]
            elif not span:
                text = text.rstrip() + "\n%freq Restart true end\n"
            for f in sorted(glob.glob(_wp(BASENAME + ".res.*"))):
                carry(f, 15)
            notes.append("NumFreq resumed at a pinned geometry ('Restart true' + carried "
                         ".res.* columns)")
        else:
            notes.append("NumFreq restarted from scratch: the geometry was not yet fixed, and "
                         "Hessian columns from different geometries cannot be combined")

    else:
        # Plain Opt / Opt Freq / OptTS: continue from the last COMPLETE geometry
        # in the append-only (corruption-proof) trajectory.
        last_frame = _last_complete_frame(trj_now[0]) if trj_now else None
        # A frame whose atom count does not match the submitted molecule is not
        # this molecule. Feeding it back would run a chemically different system
        # under the same job name -- the one failure a chemist cannot spot from
        # the job list -- so it is refused rather than used.
        want_atoms = _input_natoms(ORIGINAL_INP_TEXT, _wp)
        if last_frame and want_atoms and _frame_natoms(last_frame) != want_atoms:
            log("[restart] Ignoring the trajectory: its last frame has %d atoms but the "
                "input describes %d." % (_frame_natoms(last_frame), want_atoms))
            last_frame = None
        if (opt_converged and re.search(r"(?i)\bfreq\b", text)
                and _xyz_is_complete(_wp(BASENAME + ".xyz"))):
            text = _strip_opt_keyword(text)
            text, placed = _set_geometry(text, BASENAME + ".xyz", charge, mult)
            if not placed:
                raise RuntimeError("the coordinate block could not be repointed at the "
                                   "converged geometry")
            carry(_wp(BASENAME + ".xyz"), 10)
            notes.append("optimization converged; continuing with frequencies only")
        elif last_frame:
            coords = "\n".join(last_frame.split("\n")[2:])
            with open(_wp("last_geometry.xyz"), "w") as fh:
                fh.write(str(_frame_natoms(last_frame)) + "\n")
                fh.write("restart geometry after " + str(frames_now) + " step(s)\n")
                fh.write(coords + "\n")
            text, placed = _set_geometry(text, "last_geometry.xyz", charge, mult)
            if not placed:
                raise RuntimeError(
                    "the input's coordinate block is in a form this restart cannot "
                    "rewrite, so the next window would start from the ORIGINAL geometry "
                    "and repeat this window exactly. Resubmit with a Cartesian "
                    "(* xyz / * xyzfile) coordinate block to make the job continuable")
            carry(_wp("last_geometry.xyz"), 10)
            notes.append("optimization resumed from trajectory step " + str(frames_now))
        else:
            notes.append("no completed step yet; restarted from the original geometry")

        # Reuse a fully-written ASCII Hessian (chiefly for OptTS) so it is not
        # recomputed every window. Skipped silently if absent/partial/too big.
        if (re.search(r"(?i)\bopt(ts)?\b", text) and hess_ready
                and not re.search(r"(?i)inhess", text)):
            if carry(_wp(BASENAME + ".hess"), 30):
                if re.search(r"(?is)%\s*geom\b.*?\bend\b", text):
                    text = re.sub(r"(?i)(%\s*geom\b)",
                                  lambda m: m.group(0) + '\n  InHess Read\n  InHessName "'
                                  + BASENAME + '.hess"', text, count=1)
                else:
                    text = text.rstrip() + '\n%geom\n  InHess Read\n  InHessName "' \
                        + BASENAME + '.hess"\nend\n'
                notes.append("reused prior Hessian " + BASENAME + ".hess")

    # Belt-and-suspenders: never AutoStart-read a leftover (corrupt) .gbw, and
    # never leave a MOREAD pointing at a file that is deliberately not carried.
    text = _ensure_simple_keyword(_strip_moread(text), "NoAutoStart")
    if NEXT_GEOM_MAXITER and NEXT_GEOM_MAXITER != int(GEOM_MAXITER):
        notes.append("geometry-cycle budget raised to MaxIter %d" % NEXT_GEOM_MAXITER)
    if NEXT_SCF_MAXITER and NEXT_SCF_MAXITER != int(SCF_MAXITER):
        notes.append("SCF budget raised to MaxIter %d with SlowConv" % NEXT_SCF_MAXITER)
    return text, items, notes


def _drop_references(text, dropped):
    """Removes the directives that point at files which did not fit in the
    restart payload.

    Without this the two halves disagree: `_build_next_input_and_carry` writes
    `InHess Read` / `Restart_ALLXYZFile` on the assumption that the file rides
    along, and `_encode_carry` then drops that file to stay under Kaggle's
    source-size limit. The successor starts, cannot find the file, and aborts
    within seconds -- ending a chain for a reason that has nothing to do with
    the chemistry. Losing a Hessian only costs one recomputation; losing the
    chain costs the whole calculation."""
    removed = []
    for name in dropped:
        low = name.lower()
        if low.endswith(".hess"):
            text = re.sub(r"(?im)^[ \t]*InHess\s+Read[ \t]*$", "", text)
            text = re.sub(r"(?im)^[ \t]*InHessName\b.*$", "", text)
            text = re.sub(r"(?i)\bInHess\s+Read\b", "", text)
            removed.append("Hessian reuse (%s did not fit; it will be recomputed)" % name)
        elif low.endswith(".allxyz"):
            text = re.sub(r"(?im)^[ \t]*Restart_ALLXYZFile\b.*$", "", text)
            removed.append("NEB path restart (%s did not fit; the path restarts fresh)" % name)
        elif low.endswith(".res") or ".res." in low:
            text = re.sub(r"(?im)^[ \t]*Restart\s+true[ \t]*$", "", text)
            removed.append("NumFreq column reuse (%s did not fit)" % name)
    return text, removed


def _referenced_files(text):
    """Every filename the input quotes.

    `_geometry_file_of` only knew about `* xyzfile`. Nothing protected
    `%neb neb_end_xyzfile "product.xyz"`, `%pointcharges "charges.pc"`,
    `%qmmm ORCAFFFilename "..."` or `%basis GTOName "custom.bas"` -- so the
    payload trimmer could drop the file that defines the reaction product or
    the electrostatic embedding, and the successor either aborted at once or,
    worse, ran in a different environment."""
    names = set()
    for m in re.finditer(r'"([^"\n]+\.[A-Za-z0-9_]+)"', text):
        names.add(os.path.basename(m.group(1)))
    m = re.search(r"\*\s*xyzfile\s+-?\d+\s+-?\d+\s+(\S+)", text, re.IGNORECASE)
    if m:
        names.add(os.path.basename(m.group(1)))
    return names


def _geometry_file_of(text):
    """The geometry file the input depends on, if it reads one. Dropping THAT
    is not recoverable by editing the input, so the caller must refuse to push
    rather than send a successor that cannot start."""
    m = re.search(r"\*\s*xyzfile\s+-?\d+\s+-?\d+\s+(\S+)", text, re.IGNORECASE)
    return m.group(1) if m else None


def _encode_carry(next_text, items):
    # The restart files ride inside the pushed script.py as one base64 blob.
    # Kaggle rejects an oversized kernel source, and an oversized push is
    # exactly how a chain used to die silently on a large system, so the
    # payload is gzipped and filled in priority order under a hard cap.
    payload = {os.path.basename(INPUT_FILE):
               base64.b64encode(next_text.encode("utf-8")).decode("ascii")}
    dropped = []

    def encoded_size(obj):
        return len(base64.b64encode(gzip.compress(json.dumps(obj).encode("utf-8"), 6)))

    for _prio, name, data in sorted(items, key=lambda t: (t[0], t[1])):
        candidate = dict(payload)
        candidate[name] = base64.b64encode(data).decode("ascii")
        if encoded_size(candidate) > CARRY_PAYLOAD_LIMIT:
            dropped.append(name)
            continue
        payload = candidate
    blob = base64.b64encode(gzip.compress(json.dumps(payload).encode("utf-8"), 6)).decode("ascii")
    return blob, dropped


HEADER_VARS = (
    "ENCODED_FILES_JSON", "INPUT_FILE", "KAGGLE_USERNAME", "KAGGLE_KEY",
    "KAGGLE_API_TOKEN", "JOB_BASE_ID", "JOB_TITLE", "DATASET_SOURCES", "ORCA_LINK",
    "RESTART_COUNT", "MAX_RESTARTS", "DISK_RESTART_COUNT", "MAX_DISK_RESTARTS",
    "TIME_LIMIT", "MIN_FREE_GB", "RESULT_BUDGET_GB",
    "GEOM_MAXITER", "SCF_MAXITER", "SCAN_TOTAL_POINTS", "SCAN_POINTS_BEFORE",
    "HISTORY_B64", "STATIC_BODY_B64",
)


def _continue_in_new_kernel():
    # Returns (next_job_id, next_url, notes) or raises.
    _write_kaggle_credentials()
    next_text, items, notes = _build_next_input_and_carry()
    blob, dropped = _encode_carry(next_text, items)
    if dropped:
        geometry = _geometry_file_of(next_text)
        if geometry and geometry in dropped:
            raise RuntimeError(
                "the restart geometry (%s) is too large to ship inside a Kaggle "
                "notebook, so the successor could not start. Attach the large files "
                "as a Kaggle Dataset instead of inline attachments." % geometry)
        # A dropped file that the input still names by hand cannot be papered
        # over by removing a directive -- it defines the calculation.
        essential = sorted((_referenced_files(next_text) & set(dropped))
                           - {n for n in dropped if n.lower().endswith((".hess", ".allxyz"))})
        if essential:
            raise RuntimeError(
                "the successor input references %s, which did not fit in the restart "
                "payload. Put the large inputs in a Kaggle Dataset and attach it to the "
                "job instead of uploading them with the .inp." % ", ".join(essential))
        next_text, removed = _drop_references(next_text, dropped)
        if removed:
            # The input changed, so the payload has to be rebuilt around it.
            blob, dropped = _encode_carry(
                next_text, [it for it in items if it[1] not in dropped])
            notes.extend(removed)
        notes.append("restart payload trimmed (" + ", ".join(dropped) + ")")

    next_job_id = JOB_BASE_ID + "-r" + str(RESTART_COUNT + 1)
    # The kernel TITLE is deliberately identical to the slug: when the two
    # disagree, Kaggle can create the notebook under a slug derived from the
    # title instead of the requested id, and then every later status poll,
    # download and link points at a kernel that does not exist.
    job_dir = os.path.join(SCRATCH_ROOT, "next_kernel", next_job_id)
    shutil.rmtree(job_dir, ignore_errors=True)
    os.makedirs(job_dir, exist_ok=True)

    # Kaggle rejects a push whose title is shorter than 6 or longer than 50
    # characters. A long job name plus a "-rN" suffix can cross that line, and
    # the rejection lands twelve hours in, with nobody watching -- which is one
    # of the ways a chain used to die silently.
    next_title = next_job_id[:50] if len(next_job_id) > 50 else next_job_id
    while len(next_title) < 6:
        next_title += "-job"
    with open(os.path.join(job_dir, "kernel-metadata.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "id": KAGGLE_USERNAME + "/" + next_job_id,
            "title": next_title,
            "code_file": "script.py",
            "language": "python",
            "kernel_type": "script",
            "is_private": True,
            "enable_gpu": False,
            "enable_internet": True,
            "dataset_sources": DATASET_SOURCES,
        }, fh)

    overrides = {
        "ENCODED_FILES_JSON": blob,
        "RESTART_COUNT": RESTART_COUNT + 1,
        "DISK_RESTART_COUNT": DISK_RESTART_COUNT + (1 if restart_kind == "disk" else 0),
        "GEOM_MAXITER": int(NEXT_GEOM_MAXITER),
        "SCF_MAXITER": int(NEXT_SCF_MAXITER),
        "SCAN_TOTAL_POINTS": int(SCAN_TOTAL_POINTS
                                 or _requested_scan_points(ORIGINAL_INP_TEXT)),
        "SCAN_POINTS_BEFORE": int(SCAN_POINTS_BEFORE + scan_points_done),
        "HISTORY_B64": base64.b64encode(
            gzip.compress(json.dumps(CHAIN_HISTORY).encode("utf-8"), 6)).decode("ascii"),
    }
    header = ""
    for name in HEADER_VARS:
        header += name + " = " + repr(overrides.get(name, globals().get(name))) + "\n"
    with open(os.path.join(job_dir, "script.py"), "w", encoding="utf-8") as fh:
        fh.write(header + base64.b64decode(STATIC_BODY_B64).decode("utf-8"))

    if _successor_already_exists(next_job_id):
        log("[auto-continue] %s already exists on Kaggle; not pushing it again."
            % next_job_id)
        slug = next_job_id
        url = "https://www.kaggle.com/code/" + KAGGLE_USERNAME + "/" + slug
        shutil.rmtree(job_dir, ignore_errors=True)
        with open(os.path.join(OUTPUT_DIR, "NEXT_JOB_ID.txt"), "w", encoding="utf-8") as fh:
            fh.write(slug)
        with open(os.path.join(OUTPUT_DIR, "NEXT_JOB_URL.txt"), "w", encoding="utf-8") as fh:
            fh.write(url)
        return slug, url, notes + ["successor already existed; not duplicated"]

    real_slug, real_url, _combined = _push_continuation_with_retries(job_dir)
    slug = real_slug or next_job_id
    url = real_url or ("https://www.kaggle.com/code/" + KAGGLE_USERNAME + "/" + slug)
    shutil.rmtree(job_dir, ignore_errors=True)

    # Written FIRST and kept tiny: this is the hand-off the website polls for.
    with open(os.path.join(OUTPUT_DIR, "NEXT_JOB_ID.txt"), "w", encoding="utf-8") as fh:
        fh.write(slug)
    with open(os.path.join(OUTPUT_DIR, "NEXT_JOB_URL.txt"), "w", encoding="utf-8") as fh:
        fh.write(url)
    return slug, url, notes


def _decode_history():
    if not HISTORY_B64:
        return []
    try:
        return json.loads(gzip.decompress(base64.b64decode(HISTORY_B64)).decode("utf-8"))
    except Exception:
        return []


def _history_line():
    return {
        "window": int(RESTART_COUNT) + 1,
        "job_id": JOB_BASE_ID + ("-r%d" % RESTART_COUNT if RESTART_COUNT else ""),
        "wall_seconds": int(time.time() - START_TIME),
        "opt_steps_this_window": frames_now,
        "scan_points_this_window": scan_points_done,
        "opt_converged": bool(opt_converged),
        "orca_normal_end": bool(orca_normal_end),
        "stopped_by": stop_reason["why"] or ("cycle-budget" if completion_gap else None),
        "gap": completion_gap or "",
    }


CHAIN_HISTORY = _decode_history() + [_history_line()]


def _history_text():
    out = ["Chain history -- one line per Kaggle session window.",
           "The archive you are reading contains only THIS window's ORCA output;",
           "each earlier window has its own notebook and its own results.zip.",
           ""]
    for h in CHAIN_HISTORY:
        out.append("window %-3d %-38s %6ds  opt_steps=%-5d scan_points=%-4d %s"
                   % (h.get("window", 0), h.get("job_id", "?"), h.get("wall_seconds", 0),
                      h.get("opt_steps_this_window", 0), h.get("scan_points_this_window", 0),
                      h.get("gap") or ("converged" if h.get("opt_converged") else "")))
    return "\n".join(out) + "\n"


_LIMIT_PHRASE = {
    "time": "Hit the session-time limit",
    "disk": "Hit the scratch-disk limit",
    "budget": "Ran out of ORCA optimization cycles (a normal ORCA exit that is NOT a finished "
              "calculation)",
    "scf": "The SCF ran out of iterations",
}

final_note = _no_restart_reason
if needs_continue:
    # Order matters: the continuation is pushed BEFORE the (potentially huge)
    # results are packaged. Packaging is what runs out of disk or time, and if
    # it dies first the whole chain used to die with it.
    try:
        if restart_kind == "disk":
            # Make room for the tiny hand-off files and the continuation push
            # before anything else touches the disk.
            _purge_scratch_junk()
        slug, url, notes = _continue_in_new_kernel()
        log("[auto-continue] " + "; ".join(notes))
        log("[auto-continue] Continuation kernel pushed: %s (%s)" % (slug, url))
        final_note = ("%s after %s; continued in %s. %s"
                      % (_LIMIT_PHRASE.get(restart_kind, "Stopped"),
                         time.strftime("%Hh%Mm", time.gmtime(time.time() - START_TIME)),
                         slug, "; ".join(notes)))
    except Exception as exc:
        log("WARNING: auto-continuation failed: %s" % exc)
        final_note = ("%s and the automatic continuation could not be pushed even after "
                      "retries (%s). The files here are the latest partial progress — "
                      "resubmit with the newest geometry to carry on."
                      % (_LIMIT_PHRASE.get(restart_kind, "The run stopped"), exc))
elif orca_normal_end and not orca_error and completion_gap:
    # ORCA exited cleanly but the calculation did not reach its goal and there
    # is nothing to continue from. Say so loudly: the site shows this note as a
    # warning, instead of a green "Complete" on an unconverged result.
    log("[done] ORCA terminated normally, but %s." % completion_gap)
    final_note = ("ORCA terminated normally, but the calculation did not reach its target: %s. "
                  "%s These are the results as far as they got.\n\nLast lines of the ORCA "
                  "output:\n%s"
                  % (completion_gap,
                     _no_restart_reason or "",
                     "\n".join(out_text.strip().splitlines()[-15:])))
elif orca_normal_end and not orca_error:
    log("[done] ORCA terminated normally.")

if PREP_NOTES:
    # A calculation whose input was silently altered is not reproducible. These
    # went only to the kernel log -- which fetch_job_results deliberately
    # withholds, because it embeds the API key -- so they reached nobody.
    final_note = ("Input changes applied by Chemistry Lab for this window:\n  - "
                  + "\n  - ".join(PREP_NOTES)
                  + "\n(The unmodified input is in the archive as ORIGINAL_"
                  + os.path.basename(INPUT_FILE) + ".)\n\n"
                  + final_note) if final_note else (
                      "Input changes applied by Chemistry Lab for this window:\n  - "
                      + "\n  - ".join(PREP_NOTES))

if MAXCORE_NOTE:
    # Prepended, not appended: it explains the conditions the numbers in this
    # archive were produced under, which the reader needs before the outcome.
    final_note = (MAXCORE_NOTE + "\n\n" + final_note) if final_note else MAXCORE_NOTE

_write_text(os.path.join(OUTPUT_DIR, "HISTORY.txt"), _history_text())
try:
    _write_text(_wp("HISTORY.txt"), _history_text())
except OSError:
    pass

try:
    _purge_scratch_junk()
    _package_results(note=final_note)
except Exception as exc:
    log("WARNING: results packaging failed: %s" % exc)
    _write_text(os.path.join(OUTPUT_DIR, "JOB_NOTE.txt"),
                "Results packaging failed: %s" % exc)

log("[end] Wall time %s. Output in %s."
    % (time.strftime("%Hh%Mm%Ss", time.gmtime(time.time() - START_TIME)), OUTPUT_DIR))
'''


@contextmanager
def _temp_kaggle_env(username: str, key: str):
    """Yields an environment dict pointing KAGGLE_CONFIG_DIR at a throwaway
    directory holding one-off Kaggle credentials — credentials are never
    written anywhere persistent on the server.

    HOME is deliberately NOT redirected, though it used to be. Python resolves
    its per-user site-packages directory from HOME, so pointing HOME at an empty
    temp directory hides every package installed with `pip install --user` from
    any subprocess — including the `kaggle` console script, whose first line is
    `from kaggle.cli import main`. The CLI then dies with ModuleNotFoundError on
    every single call, and because that output reached the user through the
    sign-in path, a server-side environment problem was reported as "could not
    sign in to Kaggle" with a Python traceback attached. Anyone reading that
    goes and regenerates their API token, which cannot help and which kills
    every job already running.
    
    Isolation does not need HOME: KAGGLE_CONFIG_DIR points the CLI at the temp
    directory for its credential files, and KAGGLE_USERNAME/KAGGLE_KEY (or
    KAGGLE_API_TOKEN) are read before any file is consulted. Nothing is written
    to the real home directory either way.

    Accepts either credential shape:
      - the new single API token from Kaggle's Settings -> API page
        (e.g. 'KGAT_...'), written to an `access_token` file and exported
        as KAGGLE_API_TOKEN; or
      - the legacy username+key pair, written to `kaggle.json` and
        exported as KAGGLE_USERNAME/KAGGLE_KEY.
    Both are also exported as env vars directly, since the kaggle CLI
    reads env vars before falling back to files, which works reliably
    across kaggle-api versions regardless of how KAGGLE_CONFIG_DIR is
    resolved internally.
    """
    auth = resolve_kaggle_auth(username, key)
    tmp_home = tempfile.mkdtemp(prefix="kaggle-home-")
    try:
        cfg_dir = os.path.join(tmp_home, ".kaggle")
        os.makedirs(cfg_dir, exist_ok=True)

        env = os.environ.copy()
        env["KAGGLE_CONFIG_DIR"] = cfg_dir
        env["KAGGLE_USERNAME"] = auth["username"]

        if auth["api_token"]:
            env["KAGGLE_API_TOKEN"] = auth["api_token"]
            env["KAGGLE_KEY"] = auth["api_token"]
            with open(os.path.join(cfg_dir, "access_token"), "w") as fh:
                fh.write(auth["api_token"])
            try:
                os.chmod(os.path.join(cfg_dir, "access_token"), 0o600)
            except OSError:
                pass
            with open(os.path.join(cfg_dir, "kaggle.json"), "w") as fh:
                json.dump({"username": auth["username"], "key": auth["api_token"]}, fh)
            try:
                os.chmod(os.path.join(cfg_dir, "kaggle.json"), 0o600)
            except OSError:
                pass
        else:
            env["KAGGLE_KEY"] = auth["key"]
            env.pop("KAGGLE_API_TOKEN", None)
            with open(os.path.join(cfg_dir, "kaggle.json"), "w") as fh:
                json.dump({"username": auth["username"], "key": auth["key"]}, fh)
            try:
                os.chmod(os.path.join(cfg_dir, "kaggle.json"), 0o600)
            except OSError:
                pass

        yield env
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)


_DATASET_URL_RE = re.compile(r"(?:https?://)?(?:www\.)?kaggle\.com/(?:datasets/)?([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)")


def clean_dataset_source(raw: str) -> str:
    """Strips whitespace, quotes, and extracts owner/dataset-slug if a full URL
    like https://www.kaggle.com/datasets/username/orca-6-1-0 is passed."""
    raw = (raw or "").strip().strip('"').strip("'")
    m = _DATASET_URL_RE.search(raw)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return raw


def clean_dataset_sources(sources: list[str] | str) -> list[str]:
    """Cleans a list or comma-separated string of dataset sources into a list of
    valid Kaggle Dataset identifiers ('username/dataset-slug')."""
    if isinstance(sources, str):
        sources = [s.strip() for s in sources.split(",") if s.strip()]
    cleaned: list[str] = []
    for s in sources:
        item = clean_dataset_source(s)
        if item and item not in cleaned:
            cleaned.append(item)
    return cleaned


def build_job_dir(
    *,
    kaggle_username: str,
    kaggle_key: str,
    job_base_id: str,
    input_filename: str,
    files_payload: dict[str, str],
    dataset_sources: list[str] | None = None,
    orca_link: str | None = None,
    job_title: str | None = None,
    time_limit: int = TIME_LIMIT_DEFAULT,
    max_restarts: int = MAX_RESTARTS_DEFAULT,
    max_disk_restarts: int = MAX_DISK_RESTARTS_DEFAULT,
    min_free_gb: float = MIN_FREE_GB_DEFAULT,
    result_budget_gb: float = RESULT_BUDGET_GB_DEFAULT,
    geom_maxiter: int = 0,
    scf_maxiter: int = 0,
) -> str:
    """Writes kernel-metadata.json + script.py into a fresh temp dir, ready
    for `kaggle kernels push -p <dir>`. Returns the directory path.

    Exactly one ORCA source should normally be given: `dataset_sources`
    (a private Kaggle Dataset containing the ORCA package) and/or
    `orca_link` (a Google Drive / direct download link the kernel fetches
    and extracts into /tmp itself, same as the original Telegram bot).
    Both may be supplied — the dataset is checked first, the link is used
    as a fallback if no `orca` executable is found in it.

    `job_base_id` is used *both* as the kernel slug and as its title (see
    make_job_base_id for why they must be the same string), so it should
    always come from make_job_base_id(). `job_title` is the pretty name the
    person typed; it is carried into the kernel only as metadata for the
    job note, since Kaggle itself has to show the slug-safe title.
    """
    auth = resolve_kaggle_auth(kaggle_username, kaggle_key)
    dataset_sources = clean_dataset_sources(dataset_sources or [])
    job_dir = tempfile.mkdtemp(prefix="chemlab-push-")

    metadata = {
        "id": f"{auth['username']}/{job_base_id}",
        # Deliberately identical to the slug — see make_job_base_id().
        "title": job_base_id,
        "code_file": "script.py",
        "language": "python",
        "kernel_type": "script",
        "is_private": True,
        "enable_gpu": False,
        "enable_internet": True,
        "dataset_sources": dataset_sources,
    }
    with open(os.path.join(job_dir, "kernel-metadata.json"), "w", encoding="utf-8") as fh:
        json.dump(metadata, fh)

    encoded_files = encode_files_payload(files_payload)
    if len(encoded_files) > MAX_PUSH_PAYLOAD_BYTES:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise RuntimeError(
            "The .inp file plus its attached files are too big to ship inside a Kaggle "
            f"notebook ({len(encoded_files) // 1024} KB compressed, limit "
            f"{MAX_PUSH_PAYLOAD_BYTES // 1024} KB). Put the large files in a Kaggle Dataset "
            "and attach it to the job instead, or drop the extra attachments — a plain .inp "
            "with its coordinates inline is always small enough."
        )

    header = _build_header({
        "ENCODED_FILES_JSON": encoded_files,
        "INPUT_FILE": input_filename,
        "KAGGLE_USERNAME": auth["username"],
        "KAGGLE_KEY": auth["key"],
        "KAGGLE_API_TOKEN": auth["api_token"],
        "JOB_BASE_ID": job_base_id,
        "JOB_TITLE": (job_title or "").strip() or pretty_job_title(job_base_id),
        "DATASET_SOURCES": dataset_sources,
        "ORCA_LINK": (orca_link or "").strip() or None,
        "RESTART_COUNT": 0,
        "MAX_RESTARTS": int(max_restarts),
        "DISK_RESTART_COUNT": 0,
        "MAX_DISK_RESTARTS": int(max_disk_restarts),
        "TIME_LIMIT": int(time_limit),
        "MIN_FREE_GB": float(min_free_gb),
        "RESULT_BUDGET_GB": float(result_budget_gb),
        # 0 = "not forced yet": the first window keeps an explicit %geom MaxIter
        # written by the user and only fills one in when the input has none. A
        # continuation raises these itself.
        "GEOM_MAXITER": int(geom_maxiter),
        "SCF_MAXITER": int(scf_maxiter),
        "SCAN_TOTAL_POINTS": 0,
        "SCAN_POINTS_BEFORE": 0,
        "HISTORY_B64": "",
        "STATIC_BODY_B64": base64.b64encode(KAGGLE_RUNNER_BODY.encode("utf-8")).decode("ascii"),
    })

    with open(os.path.join(job_dir, "script.py"), "w", encoding="utf-8") as fh:
        fh.write(header + KAGGLE_RUNNER_BODY)

    return job_dir


def push_job(job_dir: str, kaggle_username: str, kaggle_key: str) -> dict:
    """Authenticates with the *user's own* Kaggle credentials (never stored
    on the server) and pushes the kernel.

    Returns {"job_id", "owner", "url"} taken from the URL Kaggle itself
    prints on a successful push. That readback matters: the notebook's real
    address is decided by Kaggle, and reassembling a URL locally is how the
    site ended up showing links that 404 and polling kernels that don't
    exist. The requested slug is used only as a fallback when the CLI prints
    nothing recognisable."""
    auth = resolve_kaggle_auth(kaggle_username, kaggle_key)
    with open(os.path.join(job_dir, "kernel-metadata.json"), encoding="utf-8") as fh:
        requested_slug = json.load(fh)["id"].split("/", 1)[1]

    with _temp_kaggle_env(kaggle_username, kaggle_key) as env:
        result = _run_kaggle_cli(
            ["kaggle", "kernels", "push", "-p", job_dir],
            env=env, timeout=180,
        )
        combined = (result.stdout or "") + "\n" + (result.stderr or "")
        if result.returncode != 0:
            _raise_if_cli_broken(combined)
            _raise_if_unreachable(combined)
            message = (result.stderr or result.stdout or "Unknown Kaggle CLI error").strip()
            if "401" in message or "Unauthorized" in message or "Authentication required" in message:
                message += (
                    "\n\nThis usually means: the API key/token was pasted with a typo/extra "
                    "character or stray quote, an old key/token was regenerated on Kaggle "
                    "(invalidating this one), the browser autofilled a stale saved value, or "
                    "the 'Kaggle username' field doesn't match the account that owns this "
                    "key/token. Go to kaggle.com → Settings → API and click 'Create New Token' "
                    "(or, for the older format, 'Create Legacy API Key' under Legacy API "
                    "Credentials), then paste the fresh value exactly as shown — either format "
                    "is accepted here."
                )
            raise RuntimeError(message)

    owner, slug = parse_pushed_kernel(combined)
    owner = (owner or auth["username"]).lower()
    slug = slug or requested_slug
    if slug != requested_slug:
        log_line = (f"Kaggle created the kernel as '{slug}' instead of the requested "
                    f"'{requested_slug}'; following the one Kaggle reported.")
        print(log_line)
    return {
        "job_id": slug,
        "owner": owner,
        "url": f"https://www.kaggle.com/code/{owner}/{slug}",
    }



def verify_kaggle_credentials(kaggle_username: str, kaggle_key: str) -> dict:
    """Fast authentication check without enumerating account history."""
    auth = resolve_kaggle_auth(kaggle_username, kaggle_key)
    with _temp_kaggle_env(kaggle_username, kaggle_key) as env:
        result = _run_kaggle_cli(["kaggle","kernels","list","--mine","--csv","--page-size","1","--page","1"], env=env, timeout=30, attempts=2, base_delay=1.0)
        combined=(result.stdout or "")+"\n"+(result.stderr or "")
        if result.returncode != 0:
            _raise_if_cli_broken(combined); _raise_if_unreachable(combined)
            low=combined.lower()
            if "401" in combined or "unauthorized" in low or "authentication required" in low or "forbidden" in low:
                raise RuntimeError("Kaggle rejected these credentials. Check the username and API key/token.")
            raise RuntimeError((result.stderr or result.stdout or "Kaggle credential verification failed.").strip())
    return {"username": auth["username"]}

def list_jobs(kaggle_username: str, kaggle_key: str) -> list[dict]:
    """Logs in with the person's own Kaggle username + key/token and asks
    Kaggle itself for every kernel this account owns whose slug matches the
    jobs this site creates (job_base_id always starts with 'chem-tools-').
    This is what lets someone recover their job list after clearing browser
    data / switching browsers — the website itself never stores a job
    history, Kaggle's kernel list is used as the source of truth on login.

    The prefix intentionally does NOT include the username (an earlier
    version built it as 'chem-tools-<username>-', but the id was built
    elsewhere from a differently-sanitized copy of the same username —
    e.g. a hyphen in a real Kaggle username like 'chem-lab-99' silently
    became an underscore — so the two strings could drift apart and this
    lookup would match nothing for any account whose username needed any
    sanitizing at all. `kaggle kernels list --mine` already scopes results
    to the authenticated account, so the username added nothing here
    except a way for the two sides to disagree; the explicit `owner`
    check below still confirms ownership just as strictly.)

    Returns a list of {job_id, kaggle_url, title, last_run} ordered most
    recent first. Raises RuntimeError (e.g. on bad credentials) exactly
    like the other kaggle_runner functions, so callers can treat this as
    both a credential check and a job-list fetch in one call.
    """
    auth = resolve_kaggle_auth(kaggle_username, kaggle_key)
    prefix = JOB_ID_PREFIX
    import csv
    import io as _io

    MAX_PAGES = 20                       # 2000 kernels; far past any real account
    rows: list[dict] = []

    def _list_page(env, page, sort_by):
        # Every window of a chain is its own notebook, so one 20-restart job
        # leaves 21 kernels and a handful of multi-day jobs exceeds a single
        # page. Without paging, sign-in recovery silently dropped whole jobs --
        # and because the CLI's default order is HOTNESS rather than recency,
        # which ones it dropped was arbitrary, so `newest = windows[-1]` could
        # name a stale window as a job's current one and the browser would then
        # poll and download the wrong kernel.
        args = ["kaggle", "kernels", "list", "--mine", "--csv",
                "--page-size", "100", "--page", str(page)]
        if sort_by:
            args += ["--sort-by", sort_by]
        return _run_kaggle_cli(args, env=env, timeout=60)

    with _temp_kaggle_env(kaggle_username, kaggle_key) as env:
        # Sorting by run date is what makes "the newest window" meaningful, but
        # the accepted values differ between kaggle CLI versions and this call
        # is also the sign-in check -- so an unrecognised option must degrade to
        # an unsorted listing rather than fail the login.
        sort_by = "dateRun"
        for page in range(1, MAX_PAGES + 1):
            result = _list_page(env, page, sort_by)
            if result.returncode != 0 and sort_by:
                combined = ((result.stderr or "") + (result.stdout or "")).lower()
                if ("sort-by" in combined or "invalid choice" in combined
                        or "unrecognized" in combined or "usage:" in combined):
                    log_line = ("This kaggle CLI does not accept --sort-by dateRun; "
                                "listing without it.")
                    print(log_line)
                    sort_by = ""
                    result = _list_page(env, page, sort_by)

            if result.returncode != 0:
                _raise_if_cli_broken((result.stderr or "") + (result.stdout or ""))
                _raise_if_unreachable((result.stderr or "") + (result.stdout or ""))
                if page > 1 and rows:
                    # Page 1 already succeeded, so this is a transient failure
                    # part-way through. Returning what was recovered beats
                    # failing a sign-in that has most of the answer in hand.
                    break
                message = (result.stderr or result.stdout or "Unknown Kaggle CLI error").strip()
                if ("401" in message or "Unauthorized" in message
                        or "Authentication required" in message):
                    message += (
                        "\n\nCheck your Kaggle username and API key/token — go to "
                        "kaggle.com → Settings → API to view or regenerate it."
                    )
                raise RuntimeError(message)

            page_rows = list(csv.DictReader(_io.StringIO(result.stdout)))
            rows.extend(page_rows)
            if len(page_rows) < 100:
                break                    # a short page is the last page

    if True:
        # An auto-restarted job owns several kernels — <base>, <base>-r1,
        # <base>-r2, ... — but they are all *one* job to the person who
        # submitted it. They are grouped back into a single entry whose job_id
        # is the newest window (the one worth polling and downloading) and
        # whose chain_ids cover every window, so deleting the job also deletes
        # every continuation kernel it spawned.
        chains: dict[str, list[dict]] = {}
        for row in rows:
            ref = (row.get("ref") or "").strip()
            if "/" not in ref:
                continue
            owner, slug = ref.split("/", 1)
            if owner.strip().lower() != auth["username"] or not slug.startswith(prefix):
                continue
            match = re.match(r"^(.*?)-r(\d+)$", slug)
            base, window = (match.group(1), int(match.group(2))) if match else (slug, 0)
            chains.setdefault(base, []).append({
                "slug": slug,
                "window": window,
                "kaggle_url": f"https://www.kaggle.com/code/{ref}",
                # The Kaggle-side title is the slug itself (they have to match,
                # see make_job_base_id), so turn it back into something
                # readable rather than showing an internal id in "My Jobs".
                "title": pretty_job_title((row.get("title") or "").strip() or slug, fallback=slug),
                "last_run": (row.get("lastRunTime") or "").strip(),
            })

        jobs: list[dict] = []
        for base, windows in chains.items():
            windows.sort(key=lambda w: w["window"])
            newest = windows[-1]
            jobs.append({
                "job_id": newest["slug"],
                "kaggle_url": newest["kaggle_url"],
                "title": pretty_job_title(base, fallback=newest["title"]),
                "last_run": max(w["last_run"] for w in windows),
                "chain_ids": [w["slug"] for w in windows],
                "restarts": newest["window"],
            })

        jobs.sort(key=lambda j: j["last_run"], reverse=True)
        return jobs


def delete_job(kaggle_username: str, kaggle_key: str, job_id: str) -> None:
    """Permanently deletes a kernel from the person's own Kaggle account
    (`kaggle kernels delete <owner>/<slug> --yes`).

    This is what makes deleting a job in "My Jobs" actually stick: list_jobs()
    always rebuilds the site's job list by asking Kaggle for every kernel the
    account owns, so a job that was only removed from the browser's local
    list — and never actually deleted on Kaggle — would simply be found
    again and re-added the next time that account signs in. Once this call
    succeeds, the kernel is gone from Kaggle for good, so list_jobs() will
    no longer see it either.

    Treats "the kernel is already gone" as success rather than an error, so
    a duplicate delete call (a double click, or a kernel removed directly on
    kaggle.com in the meantime) doesn't surface a confusing failure for an
    outcome the person already wanted.
    """
    if not is_valid_job_id(job_id):
        raise RuntimeError(f"'{job_id}' is not a valid job id.")
    auth = resolve_kaggle_auth(kaggle_username, kaggle_key)
    kernel_ref = f"{auth['username']}/{job_id}"
    with _temp_kaggle_env(kaggle_username, kaggle_key) as env:
        result = _run_kaggle_cli(
            ["kaggle", "kernels", "delete", kernel_ref, "--yes"],
            env=env, timeout=60,
        )
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "Unknown Kaggle CLI error").strip()
            # Checked BEFORE the 404 branch: a broken CLI prints
            # "No module named 'kaggle'", which contains "not found"-shaped text
            # in some locales and would otherwise be read as "already deleted"
            # — reporting success for a delete that never happened.
            _raise_if_cli_broken(message)
            _raise_if_unreachable(message)
            lowered = message.lower()
            if "404" in message or "not found" in lowered or "doesn't exist" in lowered or "does not exist" in lowered:
                return
            if "401" in message or "Unauthorized" in message or "Authentication required" in message:
                message += (
                    "\n\nCheck your Kaggle username and API key/token — go to "
                    "kaggle.com → Settings → API to view or regenerate it."
                )
            raise RuntimeError(message)


_KERNEL_STATUS_WORDS = (
    ("complete", "complete"),
    ("error", "error"),
    ("running", "running"),
    ("queued", "queued"),
    ("queue", "queued"),
    ("cancel", "cancelled"),
    ("new_script", "queued"),
)


def _classify_kernel_status(text: str) -> str:
    """`kaggle kernels status` prints e.g.
    `someone/chem-tools-x has status "KernelWorkerStatus.COMPLETE"`.
    The status word is read out of the quoted value only — matching against
    the whole line would let the kernel slug or a failure message decide the
    status (a job named 'my-error-test' is not an errored job)."""
    quoted = re.findall(r'status\s+"([^"]+)"', text or "", flags=re.IGNORECASE)
    probe = (quoted[-1] if quoted else (text or "")).lower()
    for needle, status in _KERNEL_STATUS_WORDS:
        if needle in probe:
            return status
    return "unknown"


_RESTART_SUFFIX_RE = re.compile(r"^(?P<base>.+?)-r(?P<n>\d+)$")


def _probe_successor(env, owner: str, job_id: str):
    """Asks Kaggle directly whether this window's successor exists.

    The successor's slug is deterministic -- `<base>-r<N+1>` -- but the site
    used to learn it only by reading NEXT_JOB_ID.txt out of the dying window's
    saved output. That output does not always arrive: a session force-cancelled
    at the 12-hour wall while packaging a multi-GB archive has already pushed
    its successor but never saves /kaggle/working. The chain was then alive and
    completely invisible to the site. Returns (slug, url) or (None, None)."""
    m = _RESTART_SUFFIX_RE.match(job_id)
    base, n = (m.group("base"), int(m.group("n"))) if m else (job_id, 0)
    candidate = "%s-r%d" % (base, n + 1)
    if not is_valid_job_id(candidate):
        return None, None
    try:
        probe = _run_kaggle_cli(
            ["kaggle", "kernels", "status", f"{owner}/{candidate}"],
            env=env, timeout=45, attempts=2, base_delay=2.0,
        )
    except Exception:  # noqa: BLE001
        return None, None
    text = ((probe.stdout or "") + "\n" + (probe.stderr or "")).strip()
    if probe.returncode != 0 or _classify_kernel_status(text) == "unknown":
        return None, None
    return candidate, f"https://www.kaggle.com/code/{owner}/{candidate}"


def check_job_status(kaggle_username: str, kaggle_key: str, job_id: str) -> dict:
    """Polls a kernel's status and looks for the chain hand-off file
    (NEXT_JOB_ID.txt) among its output files to detect an auto-restart.
    Actual result files are fetched separately, on demand, via
    fetch_job_results() — not bundled into every status check.

    Returns: {status, next_job_id, next_kaggle_url, note, warning?}
    status is one of: queued | running | restarting | complete | error |
    cancelled | unknown
    """
    if not is_valid_job_id(job_id):
        raise RuntimeError(f"'{job_id}' is not a valid job id.")

    with _temp_kaggle_env(kaggle_username, kaggle_key) as env:
        auth = resolve_kaggle_auth(kaggle_username, kaggle_key)
        kernel_ref = f"{auth['username']}/{job_id}"
        status_result = _run_kaggle_cli(
            ["kaggle", "kernels", "status", kernel_ref],
            env=env, timeout=60,
        )
        status_text = ((status_result.stdout or "") + "\n" + (status_result.stderr or "")).strip()
        status = _classify_kernel_status(status_text)

        if status_result.returncode != 0:
            _raise_if_cli_broken(status_text)
            _raise_if_unreachable(status_text)
        if status_result.returncode != 0 and status == "unknown":
            # A failed CLI call used to fall through as "unknown", which the
            # browser polls forever — the job looks stuck on "Running" even
            # though the real problem is a bad credential or a kernel that
            # isn't there. Say so instead.
            lowered = status_text.lower()
            note = status_text or "The Kaggle CLI returned no output."
            if "404" in lowered or "not found" in lowered or "denied" in lowered:
                note += ("\n\nKaggle has no notebook at this address for your account. It may have "
                         "been deleted on kaggle.com, or it belongs to a different account than "
                         "the credentials entered here.")
            elif "401" in lowered or "unauthorized" in lowered or "authentication" in lowered:
                note += ("\n\nCheck your Kaggle username and API key/token — go to "
                         "kaggle.com → Settings → API to view or regenerate it.")
            return {"status": "error", "next_job_id": None, "next_kaggle_url": None, "note": note}

        result = {"status": status, "next_job_id": None, "next_kaggle_url": None,
                  "note": status_text}

        # The hand-off marker is checked whenever the session has *stopped* for
        # any reason — not only on a clean "complete". A window that filled the
        # disk can be marked errored or cancelled by Kaggle even though it
        # already pushed its continuation, and skipping the check there is what
        # made those chains look dead while the next kernel was quietly running.
        if status in ("complete", "error", "cancelled"):
            # Fetch ONLY the tiny hand-off/marker files — never the job's full
            # output. A finished job's results.zip is routinely hundreds of MB
            # to multiple GB; downloading that on *every* status poll would
            # exceed the web server's own request timeout, which from the
            # browser's side looks exactly like a job stuck on "Running" for
            # hours after it actually finished. fetch_job_results() (the
            # "Download results" click) allows a much longer timeout.
            out_dir = tempfile.mkdtemp(prefix="kaggle-output-")
            try:
                handoff = _run_kaggle_cli(
                    ["kaggle", "kernels", "output", kernel_ref, "-p", out_dir,
                     "--file-pattern", r"(NEXT_JOB_ID|NEXT_JOB_URL|JOB_NOTE)\.txt$"],
                    env=env, timeout=45, attempts=3, base_delay=2.0,
                )
                if handoff.returncode != 0:
                    # This call's result used to be discarded. One throttled
                    # request then produced status "complete" with no hand-off,
                    # the browser marked the job terminal and STOPPED POLLING --
                    # while the successor kernel was running on Kaggle. The job
                    # was alive and invisible, and the user was handed the
                    # previous window's partial results as if they were final.
                    probe_id, probe_url = _probe_successor(env, auth["username"], job_id)
                    if probe_id:
                        return {"status": "restarting", "next_job_id": probe_id,
                                "next_kaggle_url": probe_url,
                                "note": ("Kaggle would not list this window's output, but its "
                                         "continuation notebook exists and is being followed.")}
                    return {"status": "unknown", "next_job_id": None,
                            "next_kaggle_url": None,
                            "note": ("Kaggle reported this window as %s but would not list its "
                                     "output files, so whether it continued cannot be "
                                     "determined yet. Still checking.\n\n%s"
                                     % (status, (handoff.stderr or handoff.stdout or "").strip()))}

                def _read(name):
                    path = os.path.join(out_dir, name)
                    if not os.path.exists(path):
                        return ""
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        return fh.read().strip()

                next_id = _read("NEXT_JOB_ID.txt")
                note_text = _read("JOB_NOTE.txt")
                if next_id and is_valid_job_id(next_id):
                    # Confirm the successor really exists on Kaggle BEFORE the
                    # browser is told to follow it. The hand-off file is written
                    # by the previous window, and a push that Kaggle later
                    # rejected (or a notebook since deleted) would otherwise
                    # move the job -- and its "View on Kaggle" link -- to an
                    # address that 404s, with the working notebook lost from
                    # view. That phantom hand-off is exactly what made a chain
                    # look alive while nothing was running.
                    probe = _run_kaggle_cli(
                        ["kaggle", "kernels", "status", f"{auth['username']}/{next_id}"],
                        env=env, timeout=45, attempts=2, base_delay=2.0,
                    )
                    probe_text = ((probe.stdout or "") + "\n" + (probe.stderr or "")).strip()
                    next_status = _classify_kernel_status(probe_text)
                    if next_status == "unknown" and probe.returncode != 0:
                        result["warning"] = (
                            f"This window reported that it continued in '{next_id}', but Kaggle "
                            f"has no notebook at that address for your account (it may have been "
                            f"deleted, or the continuation push was rejected). Staying on the "
                            f"current notebook so its link and results are not lost."
                            + (f"\n\n{note_text}" if note_text else "")
                        )
                    else:
                        result["next_job_id"] = next_id
                        result["next_kaggle_url"] = _read("NEXT_JOB_URL.txt") or \
                            f"https://www.kaggle.com/code/{auth['username']}/{next_id}"
                        result["status"] = "restarting"
                        if note_text:
                            result["note"] = note_text
                else:
                    # No hand-off file. It may genuinely be finished -- or the
                    # session may have been force-killed at the 12h wall while
                    # packaging, AFTER pushing its successor, so /kaggle/working
                    # never reached Kaggle's storage. The successor's slug is
                    # deterministic, so ask for it directly rather than
                    # declaring a live chain finished.
                    probe_id, probe_url = _probe_successor(env, auth["username"], job_id)
                    if probe_id:
                        result["next_job_id"] = probe_id
                        result["next_kaggle_url"] = probe_url
                        result["status"] = "restarting"
                        result["note"] = (
                            "This window did not save a hand-off file, but its continuation "
                            "notebook exists on Kaggle and is being followed.")
                    elif note_text:
                        result["warning"] = note_text
                    elif status == "error":
                        # Kaggle's own failure text is the only explanation the
                        # user can get when the kernel died before writing a note.
                        result["warning"] = status_text
            finally:
                shutil.rmtree(out_dir, ignore_errors=True)

        return result


def fetch_job_results(kaggle_username: str, kaggle_key: str, job_id: str):
    """Downloads a completed kernel's output files fresh from Kaggle (the
    source of truth — no third-party upload host involved) and returns
    (zip_path, cleanup_dir). The caller must delete cleanup_dir once the
    file has been sent to the browser. zip_path is None if no output
    files were found at all.

    results.zip is what the kernel script normally produces; if that's
    somehow missing (e.g. the kernel errored before reaching that step),
    whatever loose output files DO exist are bundled into a fresh zip so
    the person still gets something useful instead of a dead end."""
    if not is_valid_job_id(job_id):
        raise RuntimeError(f"'{job_id}' is not a valid job id.")
    auth = resolve_kaggle_auth(kaggle_username, kaggle_key)
    kernel_ref = f"{auth['username']}/{job_id}"
    out_dir = tempfile.mkdtemp(prefix="kaggle-results-")
    with _temp_kaggle_env(kaggle_username, kaggle_key) as env:
        result = _run_kaggle_cli(
            ["kaggle", "kernels", "output", kernel_ref, "-p", out_dir,
             "--page-size", "100"],
            # Two attempts at 600 s plus the backoff could reach ~1205 s, past
            # the 900 s gunicorn timeout in the Dockerfile. The worker was then
            # killed mid-request, so neither the temp-credential cleanup nor the
            # results cleanup ran: a directory holding a live Kaggle API key and
            # another holding a multi-GB archive were left behind, one pair per
            # killed request, with nothing to sweep them.
            env=env, timeout=380, attempts=2, base_delay=5.0,
        )
    if result.returncode != 0:
        shutil.rmtree(out_dir, ignore_errors=True)
        message = (result.stderr or result.stdout or "Unknown Kaggle CLI error").strip()
        _raise_if_cli_broken(message)
        _raise_if_unreachable(message)
        raise RuntimeError(message)

    zip_path = os.path.join(out_dir, "results.zip")
    if os.path.exists(zip_path) and os.path.getsize(zip_path) > 0:
        return zip_path, out_dir

    # Kaggle also returns the rendered notebook (__results__.html) and the
    # executed source (__script__.ipynb / script.py). Those EMBED the kernel's
    # source, and this kernel's source carries the person's Kaggle API key in
    # plain text so it can push its own continuation. Bundling them would put
    # a live credential inside a file people routinely email to a colleague or
    # attach to a support thread. They add nothing a chemist needs, so they are
    # excluded -- the ORCA log and the .out are already in the archive.
    SECRET_BEARING = ("__results__.html", "__script__.ipynb", "__notebook__.ipynb",
                      "script.py", "kernel-metadata.json")
    fallback_zip = os.path.join(out_dir, "_fallback_results.zip")
    bundled_anything, withheld = False, []
    with zipfile.ZipFile(fallback_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in glob.glob(os.path.join(out_dir, "*")):
            name = os.path.basename(f)
            if not os.path.isfile(f) or f == fallback_zip:
                continue
            if name in SECRET_BEARING:
                withheld.append(name)
                continue
            zf.write(f, name)
            bundled_anything = True
        if withheld:
            zf.writestr("WITHHELD.txt",
                        "These files were left out of this archive because they embed "
                        "the notebook's source, which contains your Kaggle API "
                        "credentials in plain text:\n\n  "
                        + "\n  ".join(sorted(withheld))
                        + "\n\nOpen them on kaggle.com if you need them.\n")

    if bundled_anything:
        return fallback_zip, out_dir

    shutil.rmtree(out_dir, ignore_errors=True)
    return None, out_dir
