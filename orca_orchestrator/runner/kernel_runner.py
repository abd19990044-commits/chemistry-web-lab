# -*- coding: utf-8 -*-
"""
The script that runs INSIDE the Kaggle kernel.

This is a real module, not a string literal. The previous design kept the
entire ~1300-line in-kernel program inside a triple-quoted string in
`kaggle_runner.py`, which meant it could not be syntax-checked, linted,
imported by a test, or read with syntax highlighting. Every bug in it was
found in production, twelve hours at a time. Here `builder.py` reads this
file's source at push time, so it is an ordinary Python module locally and an
embedded payload remotely.

Responsibilities, in order:

  1. **Preflight** -- establish honest disk accounting, verify the network is
     actually reachable, and take out a run token so two concurrent runs of the
     same kernel cannot both proceed.
  2. **Restore** -- fetch and verify the predecessor's checkpoint before
     touching anything (DOWNLOADING -> VERIFYING -> RESTORING).
  3. **Execute** -- run ORCA under a time+disk watchdog, in an *in-session
     loop*, with a heartbeat.
  4. **Checkpoint** -- stage, verify, and only then commit by pushing the
     successor.
  5. **Report** -- write the ledger, then package results.

Two production bugs are fixed here specifically.

`opt_converged=False` was reported as success
---------------------------------------------
The old classifier treated "ORCA TERMINATED NORMALLY" as completion. ORCA
prints that line even when a geometry optimisation exhausts `%geom MaxIter`,
and its own manual warns against exactly this inference. A 6 h 24 m run
therefore ended with the optimisation unconverged and the job marked finished.
Now `art.classify_outcome()` requires the job-type convergence marker, and a
MaxIter exhaustion is a *continuable* outcome.

`free=1006.8 GB` inside a container with a ~60 GiB quota
--------------------------------------------------------
`shutil.disk_usage()` reports the host overlay, not Kaggle's enforced quota, so
the free-space floor could never be crossed and the disk watchdog was
permanently blind. `art.DiskAccountant` measures consumption against the quota
and probes with a real write.

Additionally: because ORCA can exit at MaxIter after six hours of a twelve-hour
session, the runner now loops *within* the session. The old design burned a
whole restart and five idle hours on exactly that case.
"""
# NOTE: this module deliberately contains NO `from __future__ import ...`.
# Its source is concatenated after a generated header when it is embedded into
# the Kaggle kernel, and a __future__ import is only legal as the first
# statement of a file -- so one here would turn every pushed script into an
# immediate SyntaxError. `builder.render_script` strips them defensively as
# well, but not having any is the real guarantee.
import base64
import glob
import gzip
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import traceback
import uuid
import zipfile

try:                                    # inside the Kaggle kernel (embedded)
    import orca_artifacts as art
except ImportError:                     # running from the package, e.g. in tests
    from orca_orchestrator import orca_artifacts as art  # type: ignore

START_TIME = time.time()
RUN_TOKEN = uuid.uuid4().hex

OUTPUT_DIR = "/kaggle/working"
STATE_FILE = os.path.join(OUTPUT_DIR, "STATE.json")
CHECKPOINT_FILE = os.path.join(OUTPUT_DIR, "CHECKPOINT.json")
CHECKPOINT_BUNDLE = os.path.join(OUTPUT_DIR, "CHECKPOINT_BUNDLE.zip")
HEARTBEAT_FILE = os.path.join(OUTPUT_DIR, "HEARTBEAT.json")
NOTE_FILE = os.path.join(OUTPUT_DIR, "JOB_NOTE.txt")
RUNLOG_FILE = os.path.join(OUTPUT_DIR, "RUN_LOG.jsonl")
# Written for backwards compatibility so a deploy of the *old* server can still
# follow a chain produced by this runner. Removing it would strand any browser
# still polling with the previous protocol.
LEGACY_NEXT_ID = os.path.join(OUTPUT_DIR, "NEXT_JOB_ID.txt")
LEGACY_NEXT_URL = os.path.join(OUTPUT_DIR, "NEXT_JOB_URL.txt")


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
def _header() -> dict:
    """Values injected by `builder.build_window_directory` as a module-level
    `ORCA_JOB_HEADER` dict. Defaults exist for every key so that a continuation
    kernel pushed by an *older* deploy -- whose header lacks newer keys -- still
    runs, instead of dying on a NameError halfway through a long chain."""
    header = dict(globals().get("ORCA_JOB_HEADER") or {})
    header.setdefault("job_id", "unknown-job")
    header.setdefault("epoch", 0)
    header.setdefault("owner", "")
    header.setdefault("title", "")
    header.setdefault("input_filename", "molecule.inp")
    header.setdefault("inline_files_b64", "")
    header.setdefault("checkpoint_manifest", None)
    header.setdefault("predecessor_slug", "")
    header.setdefault("dataset_sources", [])
    header.setdefault("orca_link", None)
    header.setdefault("kaggle_username", "")
    header.setdefault("kaggle_key", None)
    header.setdefault("kaggle_api_token", None)
    header.setdefault("job_kind", "unknown")
    header.setdefault("cumulative_opt_cycles", 0)
    header.setdefault("disk_epochs_used", 0)
    header.setdefault("budgets", {})
    header.setdefault("runner_body_b64", "")
    header.setdefault("artifacts_source_b64", "")
    return header


H = _header()
B = dict(H.get("budgets") or {})
TIME_LIMIT = int(B.get("time_limit_seconds", 39600))
HANDOFF_RESERVE = int(B.get("handoff_reserve_seconds", 1500))
MIN_FREE_BYTES = int(B.get("min_free_bytes", 6 << 30))
RESULT_BUDGET = int(B.get("result_budget_bytes", 9 << 30))
MAX_EPOCHS = int(B.get("max_epochs", 24))
MAX_DISK_EPOCHS = int(B.get("max_disk_epochs", 6))
MAX_TOTAL_OPT_CYCLES = int(B.get("max_total_opt_cycles", 1500))
PER_WINDOW_MAXITER = int(B.get("per_window_opt_maxiter", 200))
HEARTBEAT_SECONDS = int(B.get("heartbeat_seconds", 45))
WATCHDOG_POLL = int(B.get("watchdog_poll_seconds", 10))
INLINE_CARRY_LIMIT = int(B.get("inline_carry_limit_bytes", 350 * 1024))
WORKING_QUOTA = int(B.get("working_quota_bytes", 20 << 30))
SCRATCH_QUOTA = int(B.get("scratch_quota_bytes", 60 << 30))
IMPLAUSIBLE_FREE = int(B.get("implausible_free_bytes", 200 << 30))
HARD_SESSION_LIMIT = int(B.get("hard_session_seconds", 43200))
MAX_CKPT_FILE = int(B.get("max_checkpoint_file_bytes", 256 << 20))
MAX_CKPT_BUNDLE = int(B.get("max_checkpoint_bundle_bytes", 512 << 20))

JOB_ID = H["job_id"]
EPOCH = int(H["epoch"])
BASENAME = os.path.splitext(os.path.basename(H["input_filename"]))[0]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_log_lock = threading.Lock()


def emit(event, message="", **fields):
    """Structured log line to stdout AND to a durable JSONL file.

    Kaggle's rendered notebook output is the only place a user can read what
    happened, and it truncates. `RUN_LOG.jsonl` lands in the saved output, so
    the orchestrator can retrieve a full, machine-readable trace of a window
    that has long since disappeared."""
    record = {"ts": round(time.time(), 3),
              "elapsed": round(time.time() - START_TIME, 1),
              "event": event, "job_id": JOB_ID, "epoch": EPOCH,
              "run_token": RUN_TOKEN[:8]}
    record.update(fields)
    if message:
        record["msg"] = message
    line = json.dumps(record, default=str)
    with _log_lock:
        print("[%s] %s" % (event, message or ""), flush=True)
        try:
            with open(RUNLOG_FILE, "a") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def fail_log(what, why, recovery, next_action, **fields):
    emit("failure", "%s failed: %s" % (what, why), failure_what=what, failure_why=why,
         recovery_attempted=recovery, next_action=next_action, **fields)


_SECRET_RE = re.compile(r"(KGAT_[A-Za-z0-9_\-]{6,}|\b[0-9a-f]{32}\b)")


def _scrub(text):
    return _SECRET_RE.sub("<redacted>", str(text))


def _excepthook(exc_type, exc, tb):
    """A traceback inside this kernel can print module globals, and those
    globals contain a Kaggle API token. The rendered notebook output is saved
    with the kernel, so an unscrubbed traceback would persist the credential in
    readable form. Every frame is scrubbed before it is printed."""
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    print(_scrub(text), flush=True)
    emit("unhandled_exception", "the runner raised an unhandled exception",
         error_class=exc_type.__name__, detail=_scrub(str(exc))[:500])


sys.excepthook = _excepthook


# ---------------------------------------------------------------------------
# Atomic writes
# ---------------------------------------------------------------------------
def atomic_write_bytes(path, data):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        pass
    tmp = os.path.join(directory, ".tmp-%s" % uuid.uuid4().hex[:8])
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def atomic_write_json(path, obj):
    atomic_write_bytes(path, json.dumps(obj, sort_keys=True, default=str).encode("utf-8"))


def stable_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def digest_fileset(entries):
    import hashlib

    h = hashlib.sha256()
    for name, digest, size in sorted(entries, key=lambda e: e[0]):
        h.update(name.encode("utf-8")); h.update(b"\0")
        h.update(str(digest).encode("ascii")); h.update(b"\0")
        h.update(str(int(size)).encode("ascii")); h.update(b"\n")
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Duplicate-run guard
# ---------------------------------------------------------------------------
def claim_run():
    """Refuses to proceed if another run of this same kernel is alive.

    `/kaggle/working` persists across runs of the same notebook, so a re-run --
    triggered by a user clicking "Run All", by Kaggle retrying, or by the
    orchestrator pushing a new version of a kernel that was already scheduled --
    starts a *second* process against the same output directory and the same
    scratch. Two ORCA runs interleaving their writes is the single most
    destructive race in this system, and it is invisible in the logs because
    both processes look healthy.

    A live heartbeat under a different run token is treated as authoritative:
    this run exits without touching anything.
    """
    try:
        with open(HEARTBEAT_FILE) as fh:
            beat = json.load(fh)
    except (OSError, ValueError):
        beat = None

    if isinstance(beat, dict) and beat.get("run_token") and beat.get("run_token") != RUN_TOKEN:
        age = time.time() - float(beat.get("at") or 0)
        if age < HEARTBEAT_SECONDS * 6:
            emit("duplicate_run_aborted",
                 "another run of this kernel is alive and heartbeating; exiting without "
                 "touching the working directory to avoid two ORCA processes racing",
                 other_run_token=str(beat.get("run_token"))[:8],
                 heartbeat_age_seconds=round(age, 1))
            return False
        emit("stale_heartbeat_reclaimed",
             "found a heartbeat from a previous run that is older than the liveness "
             "threshold; that run is dead, so this one takes over",
             other_run_token=str(beat.get("run_token"))[:8],
             heartbeat_age_seconds=round(age, 1))
    write_heartbeat("CLAIMED")
    return True


_heartbeat_state = {"state": "CLAIMED", "detail": {}}
_heartbeat_stop = threading.Event()


def write_heartbeat(state=None, **detail):
    if state:
        _heartbeat_state["state"] = state
    if detail:
        _heartbeat_state["detail"] = detail
    try:
        atomic_write_json(HEARTBEAT_FILE, {
            "schema_version": 2, "job_id": JOB_ID, "epoch": EPOCH,
            "run_token": RUN_TOKEN, "state": _heartbeat_state["state"],
            "at": time.time(), "elapsed": round(time.time() - START_TIME, 1),
            "detail": _heartbeat_state["detail"],
        })
    except OSError:
        pass


def _heartbeat_loop():
    while not _heartbeat_stop.wait(timeout=HEARTBEAT_SECONDS):
        write_heartbeat()


# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------
def pick_scratch_root():
    """ORCA scratch runs to tens of GB. `/kaggle/working` is the auto-saved
    output directory and is capped around 20 GB, so running the calculation
    there both fills the quota and drags every scratch byte into the saved
    output. Kaggle's scratch space is several times larger and is not saved."""
    candidates = []
    for path in ("/kaggle/temp", "/kaggle/tmp", "/tmp", "/var/tmp"):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            continue
        try:
            probe = os.path.join(path, ".w")
            with open(probe, "w") as fh:
                fh.write("x")
            os.remove(probe)
        except OSError:
            continue
        candidates.append(path)
    return candidates or [OUTPUT_DIR]


SCRATCH_ROOTS = pick_scratch_root()
SCRATCH_ROOT = SCRATCH_ROOTS[0]
WORKDIR = os.path.join(SCRATCH_ROOT, "orca_job")
ORCA_PKG_DIR = os.path.join(SCRATCH_ROOT, "orca_pkg")
os.makedirs(WORKDIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

DISK = art.DiskAccountant(
    OUTPUT_DIR, [WORKDIR, ORCA_PKG_DIR],
    working_quota=WORKING_QUOTA, scratch_quota=SCRATCH_QUOTA,
    implausible_free=IMPLAUSIBLE_FREE,
)


def disk_snapshot(probe=False):
    report = DISK.snapshot(probe=probe)
    report["min_free_bytes"] = MIN_FREE_BYTES
    report["under_pressure"] = report["effective_headroom_bytes"] < MIN_FREE_BYTES
    return report


def wp(name):
    return os.path.join(WORKDIR, name)


# ---------------------------------------------------------------------------
# Kaggle credentials + CLI
# ---------------------------------------------------------------------------
def install_credentials():
    cfg_dir = os.path.expanduser("~/.kaggle")
    os.makedirs(cfg_dir, exist_ok=True)
    os.environ["KAGGLE_USERNAME"] = H["kaggle_username"]
    os.environ["KAGGLE_CONFIG_DIR"] = cfg_dir
    if H.get("kaggle_api_token"):
        os.environ["KAGGLE_API_TOKEN"] = H["kaggle_api_token"]
        path = os.path.join(cfg_dir, "access_token")
        with open(path, "w") as fh:
            fh.write(H["kaggle_api_token"])
        os.chmod(path, 0o600)
    elif H.get("kaggle_key"):
        os.environ["KAGGLE_KEY"] = H["kaggle_key"]
        path = os.path.join(cfg_dir, "kaggle.json")
        with open(path, "w") as fh:
            json.dump({"username": H["kaggle_username"], "key": H["kaggle_key"]}, fh)
        os.chmod(path, 0o600)


def remove_credentials():
    """Deletes the credential files as soon as the successor push is done.

    The token is still present in this kernel's *source*, which cannot be
    avoided -- a self-continuing kernel has to authenticate. Removing the
    on-disk copies shortens the window in which an ORCA subprocess, a stray
    `!ls ~`, or a packaging step could pick them up and carry them into the
    saved output."""
    shutil.rmtree(os.path.expanduser("~/.kaggle"), ignore_errors=True)
    for key in ("KAGGLE_KEY", "KAGGLE_API_TOKEN"):
        os.environ.pop(key, None)


def run_cli(args, timeout, retries=4, base_delay=6.0, deadline=None):
    """CLI invocation with typed-ish retry.

    Bounded by a wall-clock deadline as well as an attempt count. Inside a
    Kaggle session the difference is what protects the reserved time for
    packaging: five retries at ninety seconds will happily consume the window's
    remaining runway and lose everything the run produced."""
    transient = ("sslerror", "eof occurred", "max retries", "connection reset",
                 "connection aborted", "remote end closed", "read timed out",
                 "timed out", "connection refused", "temporary failure",
                 "bad handshake", "connection broken", "502", "503", "504",
                 "500 server error", "429", "too many requests")
    last = None
    for attempt in range(1, retries + 1):
        if deadline is not None and time.time() > deadline - 10:
            emit("cli_deadline_reached",
                 "no safe time left for another attempt", command=args[1:3])
            break
        budget = timeout
        if deadline is not None:
            budget = max(20, min(timeout, deadline - time.time() - 5))
        try:
            last = subprocess.run(args, capture_output=True, text=True, timeout=budget)
        except subprocess.TimeoutExpired:
            combined = "timed out after %.0fs" % budget
            last = None
        else:
            combined = (last.stdout or "") + (last.stderr or "")
            if last.returncode == 0:
                return True, combined
        is_transient = any(m in combined.lower() for m in transient) or last is None
        if not is_transient or attempt == retries:
            return False, combined
        delay = min(base_delay * (2 ** (attempt - 1)), 90) + random.uniform(0, 4)
        fail_log("kaggle CLI call " + " ".join(args[1:3]),
                 "transient failure: %s" % _scrub(combined.strip()[-200:]),
                 "attempt %d/%d; the operation is idempotent so replay is safe"
                 % (attempt, retries),
                 "retrying in %.1fs" % delay)
        time.sleep(delay)
    return False, (_scrub((last.stdout or "") + (last.stderr or "")) if last else "no output")


def preflight_network():
    """Confirms outbound network before anything depends on it.

    `enable_internet` requires a phone-verified Kaggle account. Without it the
    push succeeds and the *run* has no network, so the successor push at the
    end of a twelve-hour window fails and the chain dies silently. Finding out
    at minute one instead of hour twelve is the difference between a clear
    error and a lost day."""
    ok, _out = run_cli(["kaggle", "--version"], timeout=60, retries=2)
    if not ok:
        emit("preflight_network_warning",
             "the kaggle CLI is not responding; if this session has no internet the "
             "automatic continuation at the end of this window cannot be pushed. "
             "Enable internet on the notebook (requires a phone-verified Kaggle account).")
    return ok


# ---------------------------------------------------------------------------
# 1. Materialise inputs and restore the checkpoint
# ---------------------------------------------------------------------------
def decode_inline_files(blob):
    if not blob:
        return {}
    raw = base64.b64decode(blob)
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    payload = json.loads(raw.decode("utf-8"))
    return {name: base64.b64decode(data) for name, data in payload.items()}


def fetch_predecessor_bundle(slug, deadline):
    """DOWNLOADING. Pulls the checkpoint bundle from the previous window.

    Files too large for the inline payload live here. The old design silently
    dropped them ("restart payload trimmed"), which meant a large Hessian or a
    long trajectory was quietly discarded and the successor recomputed it or
    resumed from further back -- work that had already been paid for, thrown
    away without a word.

    Always downloads into a fresh directory. A retry must never be able to
    stitch a torn transfer onto a previous one.
    """
    target = os.path.join(SCRATCH_ROOT, "ckpt_download_%s" % uuid.uuid4().hex[:8])
    shutil.rmtree(target, ignore_errors=True)
    os.makedirs(target, exist_ok=True)
    ref = "%s/%s" % (H["kaggle_username"], slug)
    ok, out = run_cli(
        ["kaggle", "kernels", "output", ref, "-p", target,
         "--file-pattern", r"CHECKPOINT_BUNDLE\.zip$"],
        timeout=600, retries=4, deadline=deadline,
    )
    bundle = os.path.join(target, "CHECKPOINT_BUNDLE.zip")
    if not ok or not os.path.exists(bundle):
        fail_log("fetching the predecessor's checkpoint bundle",
                 "the bundle could not be downloaded: %s" % _scrub(out.strip()[-300:]),
                 "retried the download with exponential backoff into a clean directory",
                 "continuing with the inline payload only; any file that was carried "
                 "only in the bundle is unavailable and its work will be redone",
                 predecessor=slug)
        shutil.rmtree(target, ignore_errors=True)
        return {}
    try:
        files = {}
        with zipfile.ZipFile(bundle) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise zipfile.BadZipFile("CRC failure on %s" % bad)
            for info in zf.infolist():
                if info.is_dir() or info.file_size > MAX_CKPT_FILE:
                    continue
                files[os.path.basename(info.filename)] = zf.read(info)
        emit("bundle_fetched", "downloaded and CRC-checked the predecessor's bundle",
             predecessor=slug, files=len(files),
             bytes=os.path.getsize(bundle))
        return files
    except (zipfile.BadZipFile, OSError) as exc:
        fail_log("opening the predecessor's checkpoint bundle",
                 "the archive is corrupt (%s), which means the transfer was truncated "
                 "or the producer was killed mid-write" % exc,
                 "the archive's own CRC was checked and rejected",
                 "falling back to the inline payload; the orchestrator will roll back "
                 "to an earlier verified checkpoint if the required files are missing",
                 predecessor=slug)
        return {}
    finally:
        shutil.rmtree(target, ignore_errors=True)


def restore_checkpoint(deadline):
    """DOWNLOADING -> VERIFYING -> RESTORING.

    Files land in a staging directory, the whole set is verified there, and
    only a fully verified set is moved into the working directory. That is what
    prevents a half-restored state: the working directory goes from empty to
    complete, never through 'three of five files present', which is the shape
    of failure that produces an unexplainable ORCA abort an hour later.
    """
    manifest = H.get("checkpoint_manifest")
    inline = decode_inline_files(H.get("inline_files_b64"))

    if not manifest:
        for name, data in inline.items():
            atomic_write_bytes(wp(os.path.basename(name)), data)
        emit("fresh_start", "no checkpoint to restore; this is the job's first window",
             files=len(inline))
        return True, None

    records = manifest.get("files") or []
    need_download = any(r.get("transport") == "kaggle_output" for r in records)
    fetched = {}
    if need_download and H.get("predecessor_slug"):
        write_heartbeat("DOWNLOADING")
        fetched = fetch_predecessor_bundle(H["predecessor_slug"], deadline)

    available = {}
    available.update(fetched)
    available.update(inline)          # inline wins: it travelled with the header

    staging = os.path.join(SCRATCH_ROOT, "ckpt_staging")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)
    try:
        for name, data in available.items():
            atomic_write_bytes(os.path.join(staging, os.path.basename(name)), data)

        write_heartbeat("VERIFYING")
        outcome = art.verify_bundle(
            records, staging,
            next_input_text=manifest.get("next_input_text"),
            next_input_sha256=manifest.get("next_input_sha256"),
        )

        if not outcome["ok"]:
            fail_log("verifying the inherited checkpoint",
                     "; ".join("%s: %s" % (p["file"], p["problem"])
                               for p in outcome["blocking"]),
                     "every file was re-hashed and structurally re-validated in a staging "
                     "directory, so nothing corrupt was written into the working directory",
                     "reporting VERIFICATION_FAILED so the orchestrator rolls back to the "
                     "previous verified checkpoint",
                     checkpoint_id=manifest.get("checkpoint_id"),
                     problems=outcome["problems"])
            return False, outcome

        write_heartbeat("RESTORING")
        for record in records:
            src = os.path.join(staging, record["name"])
            if os.path.exists(src):
                os.replace(src, wp(record["name"]))

        next_input = manifest.get("next_input_text") or ""
        if next_input:
            atomic_write_bytes(wp(os.path.basename(H["input_filename"])),
                               next_input.encode("utf-8"))
        for name, data in inline.items():
            path = wp(os.path.basename(name))
            if not os.path.exists(path):
                atomic_write_bytes(path, data)

        emit("checkpoint_restored",
             "inherited checkpoint verified and restored",
             checkpoint_id=manifest.get("checkpoint_id"),
             files=len(outcome["verified"]),
             degraded=len(outcome["problems"]),
             phase=manifest.get("orca_phase"),
             cumulative_opt_cycles=manifest.get("cumulative_opt_cycles"))
        return True, outcome
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2. Locate ORCA
# ---------------------------------------------------------------------------
ARCHIVE_EXTS = (".tar.xz", ".txz", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar", ".zip")


def find_orca_under(root):
    if not os.path.isdir(root):
        return None
    for base, _dirs, names in os.walk(root):
        for name in names:
            if name == "orca" or re.match(r"^orca(_\d+)*$", name):
                candidate = os.path.join(base, name)
                if os.access(candidate, os.X_OK) or name == "orca":
                    return candidate
    return None


def extract_archive(path, dest):
    os.makedirs(dest, exist_ok=True)
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                zf.extractall(dest)
            return True
        if tarfile.is_tarfile(path):
            with tarfile.open(path, "r:*") as tf:
                # Guard against path traversal in a user-supplied archive.
                safe = []
                for member in tf.getmembers():
                    target = os.path.realpath(os.path.join(dest, member.name))
                    if target.startswith(os.path.realpath(dest) + os.sep):
                        safe.append(member)
                tf.extractall(dest, members=safe)
            return True
    except OSError as exc:
        if getattr(exc, "errno", None) == 28 or "space" in str(exc).lower():
            fail_log("extracting the ORCA package",
                     "the disk filled up while unpacking (%s)" % exc,
                     "the partial extraction is removed to reclaim the space",
                     "a pre-extracted Kaggle Dataset avoids needing room for both the "
                     "archive and its contents")
        else:
            emit("orca_extract_failed", str(exc))
    except Exception as exc:  # noqa: BLE001
        emit("orca_extract_failed", str(exc))
    return False


def locate_orca():
    exe = find_orca_under("/kaggle/input")
    if exe:
        return exe
    for index, archive in enumerate(sorted(
            p for root in ("/kaggle/input",) if os.path.isdir(root)
            for base, _d, names in os.walk(root) for p in
            [os.path.join(base, n) for n in names]
            if p.lower().endswith(ARCHIVE_EXTS))):
        dest = os.path.join(ORCA_PKG_DIR, "extracted_%d" % index)
        emit("orca_extracting", "unpacking a candidate ORCA archive", archive=archive)
        if extract_archive(archive, dest):
            exe = find_orca_under(dest)
            if exe:
                return exe
        shutil.rmtree(dest, ignore_errors=True)

    link = H.get("orca_link")
    if link:
        if download_orca(link, ORCA_PKG_DIR):
            return find_orca_under(ORCA_PKG_DIR)
    return None


def download_orca(link, dest):
    os.makedirs(dest, exist_ok=True)
    target = os.path.join(dest, "_orca_download.bin")
    try:
        if "drive.google.com" in link or "docs.google.com" in link:
            try:
                import gdown
            except ImportError:
                subprocess.check_call([sys.executable, "-m", "pip", "install", "gdown", "--quiet"])
                import gdown
            result = gdown.download(url=link, output=target, quiet=False, fuzzy=True)
            target = result or target
        else:
            import requests
            with requests.get(link, stream=True, timeout=1800) as resp:
                resp.raise_for_status()
                with open(target, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        if chunk:
                            fh.write(chunk)
    except Exception as exc:  # noqa: BLE001
        fail_log("downloading ORCA from the supplied link",
                 str(exc), "no retry: a bad link fails identically on replay",
                 "the job will fail with an explanation, since without ORCA nothing "
                 "can run")
        return False
    if not os.path.exists(target) or os.path.getsize(target) == 0:
        return False
    ok = extract_archive(target, dest)
    try:
        os.remove(target)
    except OSError:
        pass
    return ok


def find_mpirun(orca_dir):
    found = shutil.which("mpirun") or shutil.which("orterun") or shutil.which("prterun")
    if found:
        return found
    for pattern in ("mpirun", os.path.join("*", "mpirun"), os.path.join("*", "bin", "mpirun")):
        for candidate in (glob.glob(os.path.join(orca_dir, pattern))
                          + glob.glob(os.path.join(os.path.dirname(orca_dir), pattern))):
            if os.path.isfile(candidate):
                os.environ["PATH"] = os.path.dirname(candidate) + os.pathsep + os.environ["PATH"]
                try:
                    os.chmod(candidate, 0o755)
                except OSError:
                    pass
                return candidate
    for cmd in (["apt-get", "-y", "-qq", "update"],
                ["apt-get", "-y", "-qq", "install", "openmpi-bin", "libopenmpi3"]):
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except Exception:  # noqa: BLE001
            return None
    return shutil.which("mpirun")


def total_ram_mb():
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 12000


# ---------------------------------------------------------------------------
# 3. Execution
# ---------------------------------------------------------------------------
class Execution:
    """Runs ORCA under a time+disk watchdog and reports why it stopped."""

    def __init__(self, orca_exe, inp_path, out_path):
        self.orca_exe = orca_exe
        self.inp_path = inp_path
        self.out_path = out_path
        self.stop_reason = None
        self.stop_detail = ""
        self.proc = None
        self.disk_report = {}

    def _kill_tree(self, grace=25):
        """ORCA's parallel driver spawns mpirun/orted children in the same
        process group. Terminating only the parent leaves them alive, still
        writing to the scratch disk, and the kernel then hangs instead of
        checkpointing."""
        proc = self.proc
        if proc is None:
            return
        try:
            pgid = os.getpgid(proc.pid)
        except Exception:  # noqa: BLE001
            pgid = None
        for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 15)):
            if proc.poll() is not None:
                return
            try:
                if pgid is not None:
                    os.killpg(pgid, sig)
                else:
                    proc.send_signal(sig)
            except Exception:  # noqa: BLE001
                pass
            deadline = time.time() + wait
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.5)

    def _watchdog(self):
        warned = False
        probe_due = time.time() + 300
        while self.proc.poll() is None:
            elapsed = time.time() - START_TIME
            if elapsed > TIME_LIMIT - HANDOFF_RESERVE:
                self.stop_reason = "time"
                self.stop_detail = ("reached the %ds working budget, leaving %ds reserved "
                                    "for checkpoint, verification and successor push"
                                    % (TIME_LIMIT, HANDOFF_RESERVE))
                emit("watchdog_time", self.stop_detail, elapsed=round(elapsed))
                self._kill_tree()
                return

            # A periodic real write probe, because on Kaggle a quota is enforced
            # by the write path returning ENOSPC while statvfs keeps reporting a
            # terabyte free. Consumption accounting alone can be fooled by
            # anything writing outside the directories we measure.
            probe = time.time() >= probe_due
            if probe:
                probe_due = time.time() + 300
            report = disk_snapshot(probe=probe)
            self.disk_report = report

            if report["under_pressure"]:
                self.stop_reason = "disk"
                self.stop_detail = (
                    "effective headroom is %s against a floor of %s (accounting mode: %s)"
                    % (art.DiskAccountant.human(report["effective_headroom_bytes"]),
                       art.DiskAccountant.human(MIN_FREE_BYTES),
                       report["accounting_mode"]))
                emit("watchdog_disk", self.stop_detail, **{
                    k: report[k] for k in
                    ("effective_headroom_bytes", "scratch_used_bytes",
                     "working_used_bytes", "statvfs_free_bytes", "statvfs_trusted",
                     "accounting_mode")})
                self._kill_tree()
                return

            if not warned and report["effective_headroom_bytes"] < MIN_FREE_BYTES * 3:
                warned = True
                emit("disk_warning", "scratch headroom is getting low",
                     effective_headroom=art.DiskAccountant.human(
                         report["effective_headroom_bytes"]))

            write_heartbeat("RUNNING", elapsed=round(time.time() - START_TIME),
                            headroom_bytes=report["effective_headroom_bytes"],
                            accounting_mode=report["accounting_mode"])
            time.sleep(WATCHDOG_POLL)

    def run(self):
        self.stop_reason, self.stop_detail = None, ""
        out_fh = open(self.out_path, "a")
        try:
            self.proc = subprocess.Popen(
                [self.orca_exe, self.inp_path], cwd=WORKDIR,
                stdout=out_fh, stderr=subprocess.STDOUT,
                start_new_session=True,       # own process group -> killable as a tree
            )
        except OSError as exc:
            out_fh.close()
            fail_log("starting ORCA", str(exc),
                     "none: the executable could not be launched at all",
                     "failing the job; without a runnable ORCA binary nothing can proceed")
            return None
        thread = threading.Thread(target=self._watchdog, daemon=True)
        thread.start()
        self.proc.wait()
        thread.join(timeout=20)
        out_fh.close()
        return self.proc.returncode


def read_output(path):
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def time_remaining():
    return (START_TIME + TIME_LIMIT - HANDOFF_RESERVE) - time.time()


# ---------------------------------------------------------------------------
# 4. Continuation input
# ---------------------------------------------------------------------------
def build_continuation(original_text, out_text, job_kind, outcome):
    """Produces the input the successor will run, plus the files it needs.

    Restart is driven from ASCII artefacts, never from the binary `.gbw`. A
    force-killed ORCA can leave the wavefunction half-written; AutoStart then
    MORead's it and the successor aborts with 'GBWFile is corrupt'. Trajectories
    and `.allxyz` files are append-only or fully rewritten, so a torn tail can
    be *detected and discarded*, which turns a hard kill into a clean resume.
    `! NoAutoStart` is forced for the same reason.
    """
    charge, mult = art.extract_charge_mult(original_text)
    text = art.strip_moread(original_text)
    carried, notes = [], []

    def carry(path, priority=50):
        if not os.path.exists(path):
            return False
        try:
            if os.path.getsize(path) > MAX_CKPT_FILE:
                notes.append("%s is too large to carry" % os.path.basename(path))
                return False
        except OSError:
            return False
        result = art.validate_file(path)
        if not result.ok:
            notes.append("%s excluded (%s)" % (os.path.basename(path), result.reason))
            return False
        carried.append((priority, path))
        return True

    trj_candidates = sorted(glob.glob(wp(BASENAME + "_trj.xyz")))
    frames = art.read_trajectory_frames(trj_candidates[0]) if trj_candidates else []
    hess_path = wp(BASENAME + ".hess")
    hess_ok = os.path.exists(hess_path) and art.validate_hessian(hess_path).ok

    if job_kind == "neb":
        mep = sorted(glob.glob(wp("*_MEP.allxyz")))
        snapshot = mep[0] if mep and art.validate_allxyz(mep[0]).ok else None
        if snapshot:
            name = os.path.basename(snapshot)
            carry(snapshot, 10)
            if not re.search(r"(?i)restart_allxyzfile", text):
                if re.search(r"(?i)%\s*neb", text):
                    text = re.sub(r"(?i)%\s*neb",
                                  lambda m: m.group(0) + '\n  Restart_ALLXYZFile "%s"' % name,
                                  text, count=1)
                else:
                    text = text.rstrip() + '\n%%neb\n  Restart_ALLXYZFile "%s"\nend\n' % name
            notes.append("NEB resumed from " + name)
        else:
            notes.append("no complete _MEP.allxyz; NEB restarts from its endpoints")
        for path in glob.glob(wp("*.xyz")):
            if not path.endswith("_trj.xyz"):
                carry(path, 20)

    elif job_kind == "scan":
        steps = sorted(glob.glob(wp(BASENAME + ".[0-9][0-9][0-9].xyz")))
        match = re.search(
            r"(?i)(Scan\s+[BAD][\d\s]*?=\s*)(-?\d+(?:\.\d+)?)\s*,\s*"
            r"(-?\d+(?:\.\d+)?)\s*,\s*(\d+)", text)
        done = len(steps)
        if steps and match and int(match.group(4)) > 1 and 0 < done < int(match.group(4)):
            start, end, npts = float(match.group(2)), float(match.group(3)), int(match.group(4))
            step = (end - start) / (npts - 1)
            resumed = "%s%g, %g, %d" % (match.group(1), start + done * step, end, npts - done)
            text = text[:match.start()] + resumed + text[match.end():]
            text = art.set_geometry(text, os.path.basename(steps[-1]), charge, mult)
            carry(steps[-1], 10)
            notes.append("scan resumed at point %d of %d" % (done, npts))
        else:
            notes.append("scan restarts from its first point")

    elif job_kind == "md":
        restart = wp(BASENAME + ".mdrestart")
        if carry(restart, 10):
            block = re.search(r"(?is)%\s*md\b.*?\bend\b", text)
            if block and "restart" not in block.group(0).lower():
                text = re.sub(r"(?i)(%\s*md\b)",
                              lambda m: m.group(0) + "\n  Restart IfExists", text, count=1)
            notes.append("MD resumed via 'Restart IfExists'")
        else:
            notes.append("no usable .mdrestart; MD restarts from its initial conditions")
        for path in glob.glob(wp("*.xyz")):
            if not path.endswith("_trj.xyz"):
                carry(path, 20)

    elif job_kind == "freq" or (job_kind == "opt_freq" and outcome.opt_converged):
        block = re.search(r"(?is)%\s*freq\b.*?\bend\b", text)
        if block and "restart" not in block.group(0).lower():
            text = re.sub(r"(?i)(%\s*freq\b)", lambda m: m.group(0) + "\n  Restart true",
                          text, count=1)
        elif not block and re.search(r"(?i)\bnumfreq\b", text):
            text = re.sub(r"(?i)(![^\n]*\bnumfreq\b[^\n]*)",
                          lambda m: m.group(0) + "\n%freq Restart true end", text, count=1)
        for path in sorted(glob.glob(wp(BASENAME + ".res.*"))):
            carry(path, 15)
        if outcome.opt_converged and os.path.exists(wp(BASENAME + ".xyz")):
            # The optimisation is done; drop it and continue with frequencies
            # only, so the successor does not redo a converged geometry.
            text = re.sub(r"(?i)\bopt(ts)?\b", "", text, count=1)
            text = art.set_geometry(text, BASENAME + ".xyz", charge, mult)
            carry(wp(BASENAME + ".xyz"), 10)
            notes.append("optimisation converged; continuing with frequencies only")
        else:
            notes.append("frequency calculation resumed from its partial Hessian columns")

    else:
        if frames:
            last = frames[-1]
            natoms = last.split("\n", 1)[0].split()[0]
            coords = "\n".join(last.split("\n")[2:])
            geometry = wp("last_geometry.xyz")
            atomic_write_bytes(geometry, (
                "%s\nrestart geometry after %d optimisation step(s)\n%s\n"
                % (natoms, len(frames), coords)).encode("utf-8"))
            text = art.set_geometry(text, "last_geometry.xyz", charge, mult)
            carry(geometry, 10)
            notes.append("resumed from optimisation step %d" % len(frames))
        elif os.path.exists(wp(BASENAME + ".xyz")):
            text = art.set_geometry(text, BASENAME + ".xyz", charge, mult)
            carry(wp(BASENAME + ".xyz"), 10)
            notes.append("resumed from the last written geometry")
        else:
            notes.append("no completed step yet; restarting from the original geometry")

        if hess_ok and not re.search(r"(?i)inhess", text):
            if carry(hess_path, 30):
                if re.search(r"(?is)%\s*geom\b.*?\bend\b", text):
                    text = re.sub(
                        r"(?i)(%\s*geom\b)",
                        lambda m: m.group(0) + '\n  InHess Read\n  InHessName "%s.hess"'
                        % BASENAME, text, count=1)
                else:
                    text = (text.rstrip() +
                            '\n%%geom\n  InHess Read\n  InHessName "%s.hess"\nend\n' % BASENAME)
                notes.append("reused the completed Hessian instead of recomputing it")

    if trj_candidates:
        carry(trj_candidates[0], 25)

    # A fresh optimisation budget for the next window. Without this, a job that
    # ended at MaxIter inherits the same limit and stalls in exactly the same
    # place, forever. The cumulative-cycle budget is what eventually stops a
    # genuinely non-converging system.
    if job_kind in ("opt", "opt_freq", "scan", "irc"):
        text = art.set_geom_maxiter(text, PER_WINDOW_MAXITER)

    text = art.ensure_simple_keyword(text, "NoAutoStart")
    carried.sort(key=lambda item: (item[0], item[1]))
    return text, [path for _p, path in carried], notes


# ---------------------------------------------------------------------------
# 5. Checkpoint transaction
# ---------------------------------------------------------------------------
REQUIRED_ROLES = {
    "opt": ("geometry",), "opt_freq": ("geometry",), "freq": ("geometry",),
    "scan": ("geometry",), "neb": ("neb_path",), "md": ("md_restart",),
    "irc": ("geometry",), "sp": (), "unknown": (),
}
ROLE_BY_VALIDATOR = {
    "xyz": "geometry", "trajectory": "trajectory", "hessian": "hessian",
    "allxyz": "neb_path", "engrad": "gradient",
    "mdrestart": "md_restart", "input": "input", "auxiliary": "auxiliary",
    "text": "auxiliary",
}


def stage_and_verify_checkpoint(next_text, carried_paths, job_kind, outcome,
                                cumulative_cycles):
    """CHECKPOINTING -> VERIFYING.

    Writes CHECKPOINT.json and CHECKPOINT_BUNDLE.zip into the saved output, then
    re-reads and re-verifies both. A checkpoint that does not survive its own
    verification is marked rejected and never offered to a successor -- which is
    the whole point of the transaction: the successor is only ever handed
    something that has been proven readable *after* it was written.
    """
    write_heartbeat("CHECKPOINTING")
    required = set(REQUIRED_ROLES.get(job_kind, ()))
    records, total = [], 0

    for path in carried_paths:
        name = os.path.basename(path)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if total + size > MAX_CKPT_BUNDLE:
            emit("checkpoint_file_skipped",
                 "the bundle budget is spent; this file is left out",
                 file=name, size=size)
            continue
        role = ROLE_BY_VALIDATOR.get(art.validator_for(name), "auxiliary")
        records.append({
            "name": name,
            "sha256": art.sha256_file(path),
            "size": size,
            "role": role,
            "required": role in required,
            "transport": "inline" if size <= INLINE_CARRY_LIMIT // 2 else "kaggle_output",
            "structural_check": art.validator_for(name),
        })
        total += size

    next_sha = art.sha256_text(next_text)
    manifest = {
        "schema_version": 2,
        "checkpoint_id": "ckpt_%s" % uuid.uuid4().hex,
        "job_id": JOB_ID,
        "epoch": EPOCH,
        "created_at": time.time(),
        "status": "staged",
        "files": records,
        "next_input_text": next_text,
        "next_input_sha256": next_sha,
        "orca_phase": job_kind,
        "completed_opt_cycles": outcome.opt_cycles,
        "cumulative_opt_cycles": cumulative_cycles,
        "scan_points_done": outcome.scan_steps,
        "opt_converged": outcome.opt_converged,
        "last_energy": outcome.energy,
        "source_kernel_slug": os.environ.get("KAGGLE_KERNEL_SLUG", "") or JOB_ID,
        "bundle_digest": digest_fileset(
            [(r["name"], r["sha256"], r["size"]) for r in records]
            + [("__next_input__", next_sha, len(next_text.encode("utf-8")))]),
    }

    # The bundle goes into the saved output so the successor can fetch anything
    # too large to ride inline. Written before the manifest is marked verified.
    try:
        tmp_zip = CHECKPOINT_BUNDLE + ".tmp"
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for path in carried_paths:
                name = os.path.basename(path)
                if any(r["name"] == name for r in records):
                    zf.write(path, name)
        os.replace(tmp_zip, CHECKPOINT_BUNDLE)
    except OSError as exc:
        fail_log("writing the checkpoint bundle", str(exc),
                 "the partial archive was discarded",
                 "the successor will receive only the inline payload; any file that "
                 "would have travelled in the bundle is unavailable to it")
        try:
            os.remove(CHECKPOINT_BUNDLE + ".tmp")
        except OSError:
            pass

    write_heartbeat("VERIFYING")
    # Re-verify from the ORIGINAL files on disk. Verifying the in-memory copy
    # would prove nothing: the question is whether what was written survives
    # being read back.
    outcome_v = art.verify_bundle(records, WORKDIR,
                                  next_input_text=next_text, next_input_sha256=next_sha)
    if not outcome_v["ok"]:
        manifest["status"] = "rejected"
        manifest["rejection_reason"] = "; ".join(
            "%s: %s" % (p["file"], p["problem"]) for p in outcome_v["blocking"])
        atomic_write_json(CHECKPOINT_FILE, manifest)
        fail_log("verifying the checkpoint this window produced",
                 manifest["rejection_reason"],
                 "every file was re-read from disk, re-hashed and structurally "
                 "re-validated after being written",
                 "the checkpoint is marked rejected; the orchestrator will roll the job "
                 "back to the previous verified checkpoint rather than hand a successor "
                 "something that cannot be read",
                 problems=outcome_v["problems"])
        return manifest, False

    if outcome_v["problems"]:
        # Optional artefacts were lost. Drop them from the manifest so the
        # successor is not promised something it will not receive.
        lost = {p["file"] for p in outcome_v["problems"]}
        manifest["files"] = [r for r in records if r["name"] not in lost]
        manifest["degraded"] = outcome_v["problems"]
        manifest["bundle_digest"] = digest_fileset(
            [(r["name"], r["sha256"], r["size"]) for r in manifest["files"]]
            + [("__next_input__", next_sha, len(next_text.encode("utf-8")))])

    manifest["status"] = "verified"
    manifest["verified_at"] = time.time()
    atomic_write_json(CHECKPOINT_FILE, manifest)
    emit("checkpoint_verified",
         "checkpoint written, read back, re-hashed and structurally validated",
         checkpoint_id=manifest["checkpoint_id"], files=len(manifest["files"]),
         degraded=len(outcome_v["problems"]), bytes=total,
         bundle_digest=manifest["bundle_digest"][:16])
    return manifest, True


def encode_inline_payload(manifest):
    """Packs the small, highest-value files into the successor's header.

    Filled in priority order under a hard cap, because Kaggle rejects an
    oversized kernel source and an oversized push is how a chain used to die
    silently on a large system. Anything that does not fit is *not* dropped --
    it is marked `kaggle_output` and the successor downloads it. That is the
    difference between this design and the old one, where the trim was silent
    and permanent."""
    payload, inline_names = {}, []
    for record in sorted(manifest["files"], key=lambda r: r["size"]):
        path = wp(record["name"])
        if not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        candidate = dict(payload)
        candidate[record["name"]] = base64.b64encode(data).decode("ascii")
        encoded = base64.b64encode(gzip.compress(
            json.dumps(candidate).encode("utf-8"), 6))
        if len(encoded) > INLINE_CARRY_LIMIT:
            record["transport"] = "kaggle_output"
            continue
        payload = candidate
        record["transport"] = "inline"
        inline_names.append(record["name"])

    blob = base64.b64encode(gzip.compress(
        json.dumps(payload).encode("utf-8"), 6)).decode("ascii")
    remote = [r["name"] for r in manifest["files"] if r["transport"] == "kaggle_output"]
    emit("payload_encoded", "split the checkpoint between inline and downloadable transport",
         inline=len(inline_names), via_kaggle_output=len(remote),
         inline_bytes=len(blob))
    return blob


def push_successor(manifest, next_epoch, job_kind, cumulative_cycles, disk_epochs):
    """RESTARTING. Builds and pushes the next window.

    Deliberately runs BEFORE result packaging. Packaging is the step that runs
    out of disk or time, and in the old design it ran first -- so when it died,
    it took the whole restart chain with it. Here the continuation is secured
    first and packaging becomes best-effort.
    """
    write_heartbeat("RESTARTING")
    slug = "%s-r%d" % (JOB_ID, next_epoch)
    deadline = START_TIME + HARD_SESSION_LIMIT - 240

    # Duplicate-launch guard. Pushing over a kernel that is already running
    # makes Kaggle schedule a second run against the same output directory.
    ok, out = run_cli(["kaggle", "kernels", "status",
                       "%s/%s" % (H["kaggle_username"], slug)],
                      timeout=45, retries=2, deadline=deadline)
    if ok and re.search(r'status\s+"[^"]*(RUNNING|QUEUED)', out, re.IGNORECASE):
        emit("successor_already_active",
             "the successor window already exists and is active; not pushing a duplicate",
             slug=slug)
        return slug, "https://www.kaggle.com/code/%s/%s" % (H["kaggle_username"], slug)

    blob = encode_inline_payload(manifest)
    header = dict(H)
    header.update({
        "epoch": next_epoch,
        "inline_files_b64": blob,
        "checkpoint_manifest": manifest,
        "predecessor_slug": os.environ.get("KAGGLE_KERNEL_SLUG", "")
                            or (JOB_ID if EPOCH == 0 else "%s-r%d" % (JOB_ID, EPOCH)),
        "job_kind": job_kind,
        "cumulative_opt_cycles": cumulative_cycles,
        "disk_epochs_used": disk_epochs,
    })

    job_dir = os.path.join(SCRATCH_ROOT, "next_window", slug)
    shutil.rmtree(job_dir, ignore_errors=True)
    os.makedirs(job_dir, exist_ok=True)

    atomic_write_json(os.path.join(job_dir, "kernel-metadata.json"), {
        "id": "%s/%s" % (H["kaggle_username"], slug),
        # Identical to the slug on purpose: when title and id disagree, Kaggle
        # derives the slug from the title and the notebook is created at an
        # address nobody is polling.
        "title": slug,
        "code_file": "script.py",
        "language": "python",
        "kernel_type": "script",
        "is_private": True,
        "enable_gpu": False,
        "enable_internet": True,
        "dataset_sources": list(H.get("dataset_sources") or []),
        "competition_sources": [],
        "kernel_sources": [],
    })

    script = (
        "ORCA_JOB_HEADER = " + repr(header) + "\n"
        + "_ARTIFACTS_SRC = " + repr(base64.b64decode(
            H["artifacts_source_b64"]).decode("utf-8")) + "\n"
        + "import sys as _sys, types as _types\n"
        + "_m = _types.ModuleType('orca_artifacts')\n"
        + "exec(compile(_ARTIFACTS_SRC, 'orca_artifacts.py', 'exec'), _m.__dict__)\n"
        + "_sys.modules['orca_artifacts'] = _m\n"
        + base64.b64decode(H["runner_body_b64"]).decode("utf-8")
    )
    with open(os.path.join(job_dir, "script.py"), "w", encoding="utf-8") as fh:
        fh.write(script)

    ok, out = run_cli(["kaggle", "kernels", "push", "-p", job_dir],
                      timeout=180, retries=5, base_delay=12.0, deadline=deadline)
    shutil.rmtree(job_dir, ignore_errors=True)
    if not ok:
        raise RuntimeError("kaggle kernels push failed: " + _scrub(out.strip()[-400:]))

    match = re.search(r"https?://(?:www\.)?kaggle\.com/(?:code/)?([\w.-]+)/([\w.-]+)", out)
    real_slug = match.group(2) if match else slug
    url = (match.group(0) if match
           else "https://www.kaggle.com/code/%s/%s" % (H["kaggle_username"], real_slug))
    emit("successor_pushed", "the next window was accepted by Kaggle",
         slug=real_slug, url=url, diverged=real_slug != slug)
    return real_slug, url


# ---------------------------------------------------------------------------
# 6. Results
# ---------------------------------------------------------------------------
PURGE_SUFFIXES = (
    ".tmp", ".ges", ".densities", ".densitiesinfo", ".cis", ".bas", ".basinfo",
    ".int", ".ijkl", ".rijk", ".fint", ".pmp2int", ".mp2int", ".lastint",
    ".hostnames", ".pcgrad", ".uco", ".opttmp", ".ltmp", ".stmp",
)


def purge_scratch():
    freed = 0
    for base, _dirs, names in os.walk(WORKDIR):
        for name in names:
            low = name.lower()
            if low.endswith(PURGE_SUFFIXES) or re.search(r"\.tmp[.\d]*$", low):
                path = os.path.join(base, name)
                try:
                    size = os.path.getsize(path)
                    os.remove(path)
                    freed += size
                except OSError:
                    pass
    if freed:
        emit("scratch_purged", "removed regenerable ORCA scratch files",
             freed=art.DiskAccountant.human(freed))
    return freed


def package_results(note=""):
    keep = (BASENAME + ".out", "*.inp", BASENAME + ".property.txt", BASENAME + ".xyz",
            BASENAME + "_trj.xyz", "*.allxyz", "*.hess", "*.engrad",
            BASENAME + ".[0-9][0-9][0-9].xyz", BASENAME + ".res.*",
            BASENAME + ".mdrestart", BASENAME + ".opt", "*.gbw", "*.nbo",
            "*.molden*", "*.cube", "*.pdb", "*.txt", "*.out", "*.xyz", "*.log", "*.dat")
    seen, ordered = set(), []
    for pattern in keep:
        for path in sorted(glob.glob(wp(pattern))):
            name = os.path.basename(path)
            if name not in seen and os.path.isfile(path):
                seen.add(name)
                ordered.append(path)

    manifest, included, skipped, total = [], 0, [], 0
    zip_path = os.path.join(OUTPUT_DIR, "results.zip")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for path in ordered:
                name = os.path.basename(path)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if total + size > RESULT_BUDGET:
                    skipped.append("%s (%s, over the archive budget)"
                                   % (name, art.DiskAccountant.human(size)))
                    continue
                try:
                    zf.write(path, name)
                except OSError as exc:
                    skipped.append("%s (%s)" % (name, exc))
                    continue
                total += size
                included += 1
                manifest.append("%-44s %s" % (name, art.DiskAccountant.human(size)))
    except OSError as exc:
        emit("packaging_degraded", "the archive could not be written; loose files only",
             error=str(exc))
        zip_path = None

    for pattern in (BASENAME + ".out", "*.inp", BASENAME + ".property.txt",
                    BASENAME + ".xyz"):
        for path in sorted(glob.glob(wp(pattern))):
            try:
                size = os.path.getsize(path)
                dest = os.path.join(OUTPUT_DIR, os.path.basename(path))
                if size <= 64 << 20:
                    shutil.copyfile(path, dest)
                else:
                    with open(path, "rb") as src, open(dest + ".tail.txt", "wb") as out:
                        src.seek(max(0, size - (8 << 20)))
                        shutil.copyfileobj(src, out)
            except OSError:
                pass

    try:
        with open(os.path.join(OUTPUT_DIR, "MANIFEST.txt"), "w") as fh:
            fh.write("Files in results.zip (%d, %s total):\n%s%s\n"
                     % (included, art.DiskAccountant.human(total), "\n".join(manifest),
                        ("\n\nLeft out:\n" + "\n".join(skipped)) if skipped else ""))
    except OSError:
        pass
    if note:
        try:
            with open(NOTE_FILE, "w") as fh:
                fh.write(note.strip() + "\n")
        except OSError:
            pass
    emit("results_packaged", "packaged the window's output",
         files=included, bytes=total, archive=bool(zip_path))


def write_state(state, *, checkpoint=None, note="", error=None, extra=None,
                next_slug=None, next_url=None, job_kind="unknown",
                cumulative_cycles=0, disk_epochs=0, outcome=None):
    """Writes STATE.json -- the durable, authoritative record of this window.

    This is the file the orchestrator reads back to learn what happened. It is
    written at every significant transition, not only at the end, so a window
    that is killed without warning still leaves behind an accurate account of
    how far it got."""
    job = {
        "job_id": JOB_ID, "owner": H["kaggle_username"], "title": H.get("title") or JOB_ID,
        "created_at": START_TIME, "updated_at": time.time(),
        "state": state, "epoch": EPOCH,
        "current_slug": os.environ.get("KAGGLE_KERNEL_SLUG", "") or JOB_ID,
        "chain_slugs": [], "run_token": RUN_TOKEN,
        "last_heartbeat_at": time.time(),
        "input_filename": H["input_filename"],
        "job_kind": job_kind,
        "verified_checkpoint_id": (checkpoint or {}).get("checkpoint_id")
                                  if (checkpoint or {}).get("status") in
                                  ("verified", "committed") else None,
        "pending_checkpoint_id": (checkpoint or {}).get("checkpoint_id")
                                 if (checkpoint or {}).get("status") == "staged" else None,
        "cumulative_opt_cycles": cumulative_cycles,
        "disk_epochs_used": disk_epochs,
        "max_epochs": MAX_EPOCHS, "max_disk_epochs": MAX_DISK_EPOCHS,
        "max_total_opt_cycles": MAX_TOTAL_OPT_CYCLES,
        "total_runtime_seconds": time.time() - START_TIME,
        "last_note": note, "last_error": error,
        "disk_report": disk_snapshot(),
    }
    document = {
        "schema_version": 2, "written_at": time.time(), "run_token": RUN_TOKEN,
        "job": job, "checkpoint": checkpoint, "disk_report": job["disk_report"],
        "extra": dict(extra or {}, next_slug=next_slug, next_url=next_url,
                      outcome=outcome.to_dict() if outcome is not None else None),
    }
    import hashlib
    document["_digest"] = hashlib.sha256(
        stable_json({k: v for k, v in document.items() if k != "_digest"}).encode("utf-8")
    ).hexdigest()
    atomic_write_json(STATE_FILE, document)

    # Legacy markers, so a browser or server still speaking the old protocol
    # keeps following the chain instead of declaring the job stuck.
    if next_slug:
        try:
            atomic_write_bytes(LEGACY_NEXT_ID, next_slug.encode("utf-8"))
            atomic_write_bytes(LEGACY_NEXT_URL, (next_url or "").encode("utf-8"))
        except OSError:
            pass


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    emit("window_start", "window starting",
         job_id=JOB_ID, epoch=EPOCH, time_limit=TIME_LIMIT,
         handoff_reserve=HANDOFF_RESERVE, workdir=WORKDIR)

    if not claim_run():
        return 0

    threading.Thread(target=_heartbeat_loop, daemon=True).start()

    report = disk_snapshot(probe=True)
    emit("disk_baseline", "disk accounting established", **report)
    if report.get("statvfs_note"):
        emit("disk_accounting_note", report["statvfs_note"])

    install_credentials()
    preflight_network()

    write_state("QUEUED", note="window booted")

    deadline = START_TIME + TIME_LIMIT - HANDOFF_RESERVE
    ok, verification = restore_checkpoint(deadline)
    if not ok:
        write_state("FAILED",
                    note="The checkpoint inherited from the previous window failed "
                         "verification, so this window refused to start from it. The "
                         "orchestrator will roll back to the previous verified checkpoint.",
                    error={"code": "checkpoint_verification_failed",
                           "problems": (verification or {}).get("blocking")})
        package_results(note="Inherited checkpoint failed verification; nothing was run.")
        return 1

    inp_path = wp(os.path.basename(H["input_filename"]))
    out_path = wp(BASENAME + ".out")
    if not os.path.exists(inp_path):
        write_state("FAILED", note="No ORCA input file was present after restore.",
                    error={"code": "missing_input"})
        return 1

    with open(inp_path, "r", encoding="utf-8", errors="replace") as fh:
        original_text = fh.read()
    job_kind = H.get("job_kind") or art.detect_job_kind(original_text)

    write_heartbeat("READY")
    orca_exe = locate_orca()
    if not orca_exe:
        note = ("Could not find an 'orca' executable in the attached dataset or behind the "
                "download link. Check that it is the LINUX build of ORCA and that the "
                "archive is complete.")
        write_state("FAILED", note=note, error={"code": "orca_unavailable"},
                    job_kind=job_kind)
        package_results(note=note)
        return 1

    os.chmod(orca_exe, 0o755)
    orca_dir = os.path.dirname(orca_exe)
    os.environ["PATH"] = orca_dir + os.pathsep + os.environ.get("PATH", "")
    os.environ["LD_LIBRARY_PATH"] = orca_dir + os.pathsep + os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["OMPI_ALLOW_RUN_AS_ROOT"] = "1"
    os.environ["OMPI_ALLOW_RUN_AS_ROOT_CONFIRM"] = "1"
    os.environ["OMPI_MCA_btl_vader_single_copy_mechanism"] = "none"
    os.environ["OMPI_MCA_rmaps_base_oversubscribe"] = "1"
    os.environ["PRTE_MCA_rmaps_default_mapping_policy"] = ":oversubscribe"
    # ORCA parallelises with MPI, not threads. Leaving OpenMP unbounded on top
    # oversubscribes the session's vCPUs and can slow a run several-fold.
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"

    cores = os.cpu_count() or 1
    want = art.requested_nprocs(original_text)
    nprocs = max(1, min(want, cores))
    if nprocs > 1 and not find_mpirun(orca_dir):
        emit("mpi_unavailable",
             "no mpirun is available, so the run falls back to serial; it will be slower "
             "but it will finish. Include an OpenMPI 4.1.x build in the ORCA dataset for "
             "parallel speed.")
        nprocs = 1

    text = art.set_nprocs(original_text, nprocs)
    text, clamp = art.clamp_maxcore(text, nprocs, total_ram_mb())
    if clamp:
        emit("maxcore_clamped",
             "%%maxcore was reduced to fit this machine, avoiding an out-of-memory kill",
             **clamp)
    if job_kind in ("opt", "opt_freq", "scan", "irc"):
        text = art.set_geom_maxiter(text, PER_WINDOW_MAXITER)
    atomic_write_bytes(inp_path, text.encode("utf-8"))

    cumulative = int(H.get("cumulative_opt_cycles") or 0)
    disk_epochs = int(H.get("disk_epochs_used") or 0)
    outcome = None
    passes = 0

    # ------------------------------------------------------------------
    # The in-session loop.
    #
    # ORCA can exit at MaxIter after six hours of a twelve-hour session. The old
    # design ran ORCA exactly once, so that case burned an entire restart AND
    # left five hours of paid-for compute idle. Continuing inside the same
    # session is strictly better: no push, no queue wait, no re-extraction of a
    # multi-gigabyte ORCA package.
    # ------------------------------------------------------------------
    while True:
        passes += 1
        write_heartbeat("RUNNING", pass_number=passes)
        emit("orca_start", "starting ORCA",
             pass_number=passes, nprocs=nprocs, cores=cores, job_kind=job_kind,
             time_remaining=round(time_remaining()))
        write_state("RUNNING", job_kind=job_kind, cumulative_cycles=cumulative,
                    disk_epochs=disk_epochs, note="ORCA pass %d" % passes)

        execution = Execution(orca_exe, inp_path, out_path)
        execution.run()
        out_text = read_output(out_path)
        outcome = art.classify_outcome(out_text, job_kind=job_kind,
                                       killed_by=execution.stop_reason)
        cumulative = int(H.get("cumulative_opt_cycles") or 0) + outcome.opt_cycles

        emit("orca_outcome", outcome.reason,
             pass_number=passes, **outcome.to_dict(),
             stopped_by=execution.stop_reason,
             cumulative_opt_cycles=cumulative)

        # A parallel start-up failure normally happens within minutes and leaves
        # an MPI fingerprint. Retrying serially in the same session turns a
        # guaranteed failure into a completed, if slower, calculation.
        if (outcome.kind == art.OUTCOME_MPI and nprocs > 1
                and time_remaining() > TIME_LIMIT * 0.4):
            emit("mpi_serial_retry",
                 "the parallel run failed during MPI start-up; retrying serially in this "
                 "same session")
            try:
                shutil.copyfile(out_path, wp(BASENAME + ".parallel_attempt.out"))
                os.remove(out_path)
            except OSError:
                pass
            nprocs = 1
            text, _c = art.clamp_maxcore(art.set_nprocs(text, 1), 1, total_ram_mb())
            atomic_write_bytes(inp_path, text.encode("utf-8"))
            continue

        if outcome.is_complete or outcome.is_fatal:
            break
        if execution.stop_reason in ("time", "disk"):
            break
        if cumulative >= MAX_TOTAL_OPT_CYCLES:
            emit("cycle_budget_spent",
                 "the cumulative optimisation-cycle budget across all windows is spent",
                 cumulative_opt_cycles=cumulative, budget=MAX_TOTAL_OPT_CYCLES)
            break

        # MaxIter exhausted (or an unexplained stop) with real time left: build
        # the continuation and run it right here rather than paying for a whole
        # session handover.
        if time_remaining() > 1800:
            next_text, carried, notes = build_continuation(
                original_text, out_text, job_kind, outcome)
            emit("in_session_continue",
                 "continuing in this same session instead of spending a restart",
                 pass_number=passes, reason=outcome.kind,
                 time_remaining=round(time_remaining()), notes=notes)
            try:
                shutil.copyfile(out_path, wp("%s.pass%d.out" % (BASENAME, passes)))
                os.remove(out_path)
            except OSError:
                pass
            atomic_write_bytes(inp_path, next_text.encode("utf-8"))
            original_text = next_text
            continue
        break

    # ------------------------------------------------------------------
    # Window wrap-up
    # ------------------------------------------------------------------
    out_text = read_output(out_path)
    if outcome is None:
        outcome = art.classify_outcome(out_text, job_kind=job_kind)

    if outcome.is_complete:
        note = ("The calculation finished: %s. Optimisation cycles in this window: %d; "
                "cumulative across all windows: %d."
                % (outcome.reason, outcome.opt_cycles, cumulative))
        write_state("FINISHED", note=note, job_kind=job_kind,
                    cumulative_cycles=cumulative, disk_epochs=disk_epochs, outcome=outcome)
        purge_scratch()
        package_results(note=note)
        emit("window_end", "the calculation is complete",
             wall_seconds=round(time.time() - START_TIME))
        return 0

    if outcome.is_fatal:
        note = ("ORCA stopped with an error in the input, which is not a time or disk "
                "limit, so relaunching the same input would fail the same way.\n\n"
                "Last lines of the ORCA output:\n" + art.tail(out_text, 20))
        write_state("FAILED", note=note, job_kind=job_kind,
                    error={"code": "orca_fatal", "reason": outcome.reason},
                    cumulative_cycles=cumulative, disk_epochs=disk_epochs, outcome=outcome)
        purge_scratch()
        package_results(note=note)
        return 1

    # -- continuation path ---------------------------------------------
    if EPOCH + 1 > MAX_EPOCHS:
        note = ("Reached the limit of %d session windows without converging. The files "
                "here are the latest partial progress." % MAX_EPOCHS)
        write_state("FAILED", note=note, job_kind=job_kind,
                    error={"code": "epoch_budget_exhausted"},
                    cumulative_cycles=cumulative, disk_epochs=disk_epochs, outcome=outcome)
        purge_scratch()
        package_results(note=note)
        return 1

    if outcome.kind == art.OUTCOME_DISK:
        disk_epochs += 1
        if disk_epochs > MAX_DISK_EPOCHS:
            note = ("The scratch disk filled up %d times, so this calculation needs more "
                    "temporary space than a Kaggle session provides. Ways to cut the disk "
                    "footprint: add RIJCOSX with a /J auxiliary basis for hybrid DFT, use "
                    "RI-MP2 or DLPNO-CCSD(T) instead of the conventional variants, shrink "
                    "the basis set, reduce the number of TD-DFT roots, or switch to a "
                    "composite method such as r2SCAN-3c." % disk_epochs)
            write_state("FAILED", note=note, job_kind=job_kind,
                        error={"code": "disk_budget_exhausted"},
                        cumulative_cycles=cumulative, disk_epochs=disk_epochs,
                        outcome=outcome)
            purge_scratch()
            package_results(note=note)
            return 1
        # Reclaim room before anything else touches the disk, so the tiny
        # handoff files and the push have somewhere to go.
        purge_scratch()

    if not art.is_iterative(job_kind) and outcome.kind != art.OUTCOME_DISK:
        note = ("This job (a single point or TD-DFT with no optimisation, scan, MD or "
                "numerical-frequency loop) leaves no text checkpoint to continue from, so "
                "a restart would begin again from zero and hit the same wall.")
        write_state("FAILED", note=note, job_kind=job_kind,
                    error={"code": "not_resumable"},
                    cumulative_cycles=cumulative, disk_epochs=disk_epochs, outcome=outcome)
        purge_scratch()
        package_results(note=note)
        return 1

    next_text, carried, notes = build_continuation(original_text, out_text, job_kind, outcome)
    write_state("CHECKPOINTING", job_kind=job_kind, cumulative_cycles=cumulative,
                disk_epochs=disk_epochs, note="staging a checkpoint",
                extra={"continuation_notes": notes}, outcome=outcome)

    manifest, verified = stage_and_verify_checkpoint(
        next_text, carried, job_kind, outcome, cumulative)

    if not verified:
        note = ("This window produced a checkpoint that failed its own verification, so "
                "it was not handed to a successor. The orchestrator will roll back to the "
                "previous verified checkpoint.")
        write_state("ROLLING_BACK", checkpoint=manifest, note=note, job_kind=job_kind,
                    cumulative_cycles=cumulative, disk_epochs=disk_epochs, outcome=outcome)
        purge_scratch()
        package_results(note=note)
        return 1

    # Intent is persisted BEFORE the push. If this process dies during the push,
    # the orchestrator sees RESTARTING with a verified checkpoint and replays
    # it -- safely, because the successor slug is deterministic.
    write_state("RESTARTING", checkpoint=manifest, job_kind=job_kind,
                cumulative_cycles=cumulative, disk_epochs=disk_epochs,
                note="checkpoint verified; pushing the successor window",
                extra={"continuation_notes": notes}, outcome=outcome)

    try:
        next_slug, next_url = push_successor(manifest, EPOCH + 1, job_kind,
                                             cumulative, disk_epochs)
        manifest["status"] = "committed"
        manifest["committed_at"] = time.time()
        atomic_write_json(CHECKPOINT_FILE, manifest)
        note = ("Stopped by the %s limit after %s and continued in %s. %s"
                % ("scratch-disk" if outcome.kind == art.OUTCOME_DISK else
                   "optimisation-cycle" if outcome.kind == art.OUTCOME_MAXITER else
                   "session-time",
                   time.strftime("%Hh%Mm", time.gmtime(time.time() - START_TIME)),
                   next_slug, "; ".join(notes)))
        write_state("QUEUED", checkpoint=manifest, note=note, job_kind=job_kind,
                    cumulative_cycles=cumulative, disk_epochs=disk_epochs,
                    next_slug=next_slug, next_url=next_url, outcome=outcome)
        emit("handoff_complete", note, next_slug=next_slug)
    except Exception as exc:  # noqa: BLE001
        note = ("The window ended and its checkpoint verified, but the automatic "
                "continuation could not be pushed even after retries (%s). The files here "
                "are the latest verified progress; the orchestrator will retry the push, "
                "and nothing has been lost." % _scrub(str(exc)))
        fail_log("pushing the successor window", _scrub(str(exc)),
                 "the push was retried with exponential backoff inside the session's "
                 "reserved handoff budget",
                 "the checkpoint remains verified and committed in this window's output, "
                 "so the orchestrator can replay the push from outside")
        write_state("RESTARTING", checkpoint=manifest, note=note, job_kind=job_kind,
                    cumulative_cycles=cumulative, disk_epochs=disk_epochs,
                    error={"code": "successor_push_failed", "detail": _scrub(str(exc))[:400]},
                    outcome=outcome)
    finally:
        remove_credentials()

    purge_scratch()
    package_results(note=note)
    emit("window_end", "window finished",
         wall_seconds=round(time.time() - START_TIME), passes=passes)
    return 0


if __name__ == "__main__" or globals().get("ORCA_JOB_HEADER") is not None:
    _heartbeat_stop.clear()
    try:
        _exit_code = main()
    finally:
        _heartbeat_stop.set()
        write_heartbeat("EXITED")
