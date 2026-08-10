# -*- coding: utf-8 -*-
"""
ORCA domain logic: outcome classification, artefact structure validation, and
honest disk accounting.

**This module imports nothing but the standard library, on purpose.** Its
source is read verbatim and embedded into the script that runs inside the
Kaggle kernel (see `runner/builder.py`), so the producer of a checkpoint and
the consumer that verifies it execute byte-identical validation code. Any
divergence between "how the kernel decided the run was finished" and "how the
server decided the run was finished" is a class of bug this arrangement
eliminates outright.

Two production bugs are fixed here.

Bug 1 -- "ORCA TERMINATED NORMALLY" was treated as success
----------------------------------------------------------
Observed: `done=True opt_converged=False stopped_by=None orca_error=False`,
after 6 h 24 m, and the job was declared finished. The ORCA manual is explicit
about why that is wrong:

    "Even if the optimization does not converge, the ORCA output may still end
    with '****ORCA TERMINATED NORMALLY****'. Therefore do not rely on the
    presence of this line as an indicator of whether the geometry optimization
    is converged!"
    -- ORCA 6 manual, Geometry Optimizations, "Some Notes and Tricks"

A geometry optimisation that exhausts `%geom MaxIter` (ORCA's default is 200)
prints a warning, then terminates normally with exit code 0. Under the old
classifier that is indistinguishable from success. `classify_outcome()` makes
the distinction structural: for an iterative job, completion requires the
job-type convergence marker, and normal termination without it is
`MAXITER_EXHAUSTED` -- a continuable outcome, not a finished one.

Bug 2 -- free space was measured against the wrong filesystem
-------------------------------------------------------------
Observed: `free=1006.8 GB` inside a Kaggle session. `shutil.disk_usage()`
reports the host overlay the container is layered on, not the per-notebook
quota Kaggle enforces on top of it. The watchdog compared 1006.8 GB against a
5 GB floor, concluded there was a terabyte of headroom, and could therefore
never fire. `DiskAccountant` measures *consumption against a quota* and only
uses statvfs as an upper bound, never as the primary signal.
"""
from __future__ import annotations

import os
import re
import shutil

# ---------------------------------------------------------------------------
# Outcome vocabulary
# ---------------------------------------------------------------------------
OUTCOME_COMPLETE = "COMPLETE"                  # converged / genuinely done
OUTCOME_MAXITER = "MAXITER_EXHAUSTED"          # normal exit, not converged
OUTCOME_SCF_FAILED = "SCF_NOT_CONVERGED"       # SCF gave up
OUTCOME_FATAL = "FATAL"                        # input error; resubmission fails identically
OUTCOME_INCOMPLETE = "INCOMPLETE"              # killed: watchdog, OOM, MPI, unknown
OUTCOME_DISK = "DISK_EXHAUSTED"
OUTCOME_MPI = "MPI_FAILURE"

#: Outcomes that justify continuing the calculation in another window.
CONTINUABLE_OUTCOMES = frozenset({
    OUTCOME_MAXITER, OUTCOME_INCOMPLETE, OUTCOME_DISK, OUTCOME_SCF_FAILED,
})

# ---------------------------------------------------------------------------
# Marker sets
# ---------------------------------------------------------------------------
NORMAL_END = "ORCA TERMINATED NORMALLY"

CONVERGENCE_MARKERS = (
    "THE OPTIMIZATION HAS CONVERGED",
    "THE NEB OPTIMIZATION HAS CONVERGED",
)

#: ORCA wraps this warning across a line break, so it can only be matched after
#: whitespace normalisation. Matching the raw text is why a naive substring
#: search for the full sentence silently never fires.
MAXITER_FRAGMENTS = (
    "did not converge but reached the maximum number of optimization cycles",
    "did not converge but reached the maximum number of optimisation cycles",
    "maximum number of optimization cycles reached",
    "the optimization did not converge",
)

SCF_FAILURE_FRAGMENTS = (
    "scf not converged after",
    "this wavefunction is not fully converged",
    "serious problem in soscf",
    "the scf did not converge",
)

FATAL_FRAGMENTS = (
    "orca finished by error termination",
    "orca terminated abnormally",
    "unrecognized or duplicated keyword",
    "input error",
    "aborting the run",
    "please correct your input",
    "unknown method",
    "basis set not found",
    "wrong number of electrons",
    "illegal multiplicity",
    "there is no basis set",
)

#: A fatal marker that is really a resource problem, not a broken input.
#: Ordering matters: these are checked BEFORE FATAL_FRAGMENTS, because
#: "aborting the run" appears in both cases and misclassifying an
#: out-of-memory abort as a bad input permanently kills a recoverable job.
RESOURCE_FRAGMENTS = (
    "not enough memory",
    "insufficient memory",
    "cannot allocate memory",
    "out of memory",
    "please increase maxcore",
)

DISK_FRAGMENTS = (
    "no space left on device", "errno 28", "disk full", "disk quota exceeded",
    "not enough disk space", "error writing", "write error", "failed to write",
    "cannot write", "input/output error", "i/o operation failed",
)

MPI_FRAGMENTS = (
    "there are not enough slots", "mpi_abort", "mpi_init", "orte_", "prte_",
    "aborting the run because of an mpi", "opal_", "ompi_",
)

FREQ_DONE_MARKERS = ("VIBRATIONAL FREQUENCIES", "NORMAL MODES")

_WS = re.compile(r"\s+")
_OPT_CYCLE_RE = re.compile(r"GEOMETRY\s+OPTIMIZATION\s+CYCLE\s+(\d+)", re.IGNORECASE)
_SCAN_STEP_RE = re.compile(r"RELAXED SURFACE SCAN STEP\s+(\d+)", re.IGNORECASE)
_ENERGY_RE = re.compile(r"FINAL SINGLE POINT ENERGY\s+(-?\d+\.\d+)", re.IGNORECASE)


def _normalize(text: str) -> str:
    """Collapse all whitespace so line-wrapped ORCA banners match reliably."""
    return _WS.sub(" ", (text or "")).lower()


def _has_any(haystack: str, needles) -> bool:
    return any(n in haystack for n in needles)


def tail(text: str, lines: int = 25) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:])


# ---------------------------------------------------------------------------
# Job kind
# ---------------------------------------------------------------------------
def detect_job_kind(input_text: str) -> str:
    """Classifies an ORCA input. Determines what "finished" means, and whether
    the job leaves a text checkpoint that can be resumed at all."""
    low = _normalize(input_text)
    has_opt = bool(re.search(r"(?<![a-z])opt(ts|h)?(?![a-z])", low)) or "%geom" in low
    has_freq = bool(re.search(r"(?<![a-z])(num)?freq(?![a-z])", low)) or "%freq" in low
    # Order matters: the more specific driver wins. A NEB input also contains
    # 'opt', and a scan input also contains '%geom'.
    if re.search(r"(?<![a-z])neb(-[a-z]+)?(?![a-z])", low) or "%neb" in low:
        return "neb"
    if re.search(r"(?<![a-z])scan(?![a-z])", low):
        return "scan"
    if re.search(r"%\s*md(?![a-z])", low):
        return "md"
    if re.search(r"(?<![a-z])irc(?![a-z])", low):
        return "irc"
    if has_opt and has_freq:
        return "opt_freq"
    if has_opt:
        return "opt"
    if has_freq:
        return "freq"
    return "sp"


#: Job kinds that write resumable ASCII progress. A plain single point has no
#: text checkpoint, so relaunching it repeats the same work and hits the same
#: wall -- which is why it is not auto-continued on a time-out.
ITERATIVE_KINDS = frozenset({"opt", "opt_freq", "scan", "neb", "md", "irc", "freq"})


def is_iterative(job_kind: str) -> bool:
    return job_kind in ITERATIVE_KINDS


# ---------------------------------------------------------------------------
# Progress extraction
# ---------------------------------------------------------------------------
def count_opt_cycles(out_text: str) -> int:
    """Highest geometry-optimisation cycle index reached in this window.

    Used for two things: reporting real progress instead of "still running",
    and enforcing the cumulative-cycle budget so a system that will never
    converge stops consuming windows forever."""
    matches = _OPT_CYCLE_RE.findall(out_text or "")
    return max((int(m) for m in matches), default=0)


def count_scan_steps(out_text: str) -> int:
    matches = _SCAN_STEP_RE.findall(out_text or "")
    return max((int(m) for m in matches), default=0)


def last_energy(out_text: str) -> float | None:
    matches = _ENERGY_RE.findall(out_text or "")
    try:
        return float(matches[-1]) if matches else None
    except (TypeError, ValueError):
        return None


class OrcaOutcome:
    """Structured verdict on one ORCA invocation."""

    __slots__ = ("kind", "job_kind", "opt_converged", "normal_end", "opt_cycles",
                 "scan_steps", "energy", "reason", "evidence", "freq_done")

    def __init__(self, kind, job_kind, *, opt_converged=False, normal_end=False,
                 opt_cycles=0, scan_steps=0, energy=None, reason="", evidence="",
                 freq_done=False):
        self.kind = kind
        self.job_kind = job_kind
        self.opt_converged = opt_converged
        self.normal_end = normal_end
        self.opt_cycles = opt_cycles
        self.scan_steps = scan_steps
        self.energy = energy
        self.reason = reason
        self.evidence = evidence
        self.freq_done = freq_done

    @property
    def is_complete(self) -> bool:
        return self.kind == OUTCOME_COMPLETE

    @property
    def is_continuable(self) -> bool:
        return self.kind in CONTINUABLE_OUTCOMES

    @property
    def is_fatal(self) -> bool:
        return self.kind == OUTCOME_FATAL

    def to_dict(self) -> dict:
        return {
            "outcome": self.kind, "job_kind": self.job_kind,
            "opt_converged": self.opt_converged, "normal_end": self.normal_end,
            "opt_cycles": self.opt_cycles, "scan_steps": self.scan_steps,
            "energy": self.energy, "freq_done": self.freq_done,
            "reason": self.reason, "evidence": self.evidence,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<OrcaOutcome {self.kind} job={self.job_kind} reason={self.reason!r}>"


def classify_outcome(out_text: str, *, job_kind: str, killed_by: str | None = None) -> OrcaOutcome:
    """The single decision point for "is this calculation finished?".

    `killed_by` is set when *we* stopped ORCA ("time" or "disk"), which takes
    precedence over anything in the output: an output that happens to contain
    a convergence banner from an earlier phase must not make a killed run look
    complete.

    Evaluation order is deliberate. Resource problems are checked before fatal
    input errors because ORCA prints "aborting the run" for both, and calling an
    out-of-memory abort a broken input would permanently fail a job that only
    needed a smaller %maxcore.
    """
    text = out_text or ""
    low = _normalize(text)
    normal_end = NORMAL_END in text
    converged = _has_any(low, [m.lower() for m in CONVERGENCE_MARKERS])
    freq_done = any(m in text for m in FREQ_DONE_MARKERS)
    cycles = count_opt_cycles(text)
    steps = count_scan_steps(text)
    energy = last_energy(text)

    def make(kind, reason, evidence=""):
        return OrcaOutcome(kind, job_kind, opt_converged=converged, normal_end=normal_end,
                           opt_cycles=cycles, scan_steps=steps, energy=energy,
                           reason=reason, evidence=evidence or tail(text, 20),
                           freq_done=freq_done)

    # 1. We stopped it. Nothing in the output can override that.
    if killed_by == "disk":
        return make(OUTCOME_DISK, "the run was stopped by the disk watchdog before ORCA finished")
    if killed_by == "time":
        return make(OUTCOME_INCOMPLETE, "the run was stopped by the session-time watchdog")

    # 2. Disk exhaustion reported by ORCA or the OS.
    if _has_any(low, DISK_FRAGMENTS):
        return make(OUTCOME_DISK, "ORCA reported a disk/IO failure while writing scratch files")

    # 3. Resource exhaustion. Continuable: a different %maxcore or a fresh
    #    session can succeed where this one could not.
    if _has_any(low, RESOURCE_FRAGMENTS):
        return make(OUTCOME_INCOMPLETE,
                    "ORCA ran out of memory; this is a resource limit, not a broken input")

    # 4. MPI start-up failure. Retryable serially inside the same window.
    if _has_any(low, MPI_FRAGMENTS) and not normal_end:
        return make(OUTCOME_MPI, "the parallel run failed during MPI start-up")

    # 5. Genuine input errors.
    if _has_any(low, FATAL_FRAGMENTS) and not normal_end:
        return make(OUTCOME_FATAL,
                    "ORCA stopped with an error in the input; an identical resubmission "
                    "would fail identically")

    # 6. SCF non-convergence.
    if _has_any(low, SCF_FAILURE_FRAGMENTS) and not converged:
        return make(OUTCOME_SCF_FAILED,
                    "the SCF did not converge; continuing with a different guess or "
                    "convergence strategy may still succeed")

    # 7. --- The MaxIter fix. --------------------------------------------
    # ORCA exited normally but the optimisation did not converge. The manual
    # warns explicitly that normal termination says nothing about convergence.
    if normal_end and job_kind in ("opt", "opt_freq", "scan", "neb", "irc"):
        if not converged:
            hit_maxiter = _has_any(low, MAXITER_FRAGMENTS)
            return make(
                OUTCOME_MAXITER,
                "ORCA terminated normally but the optimisation did NOT converge"
                + (" (the maximum number of optimisation cycles was reached)"
                   if hit_maxiter else
                   " (no convergence banner was printed)")
                + " -- this is unfinished work, not a finished job",
                evidence=tail(text, 30),
            )
        if job_kind == "opt_freq" and not freq_done:
            # The optimisation converged but the frequency stage never ran or
            # never completed. Continuing needs only the frequency job.
            return make(OUTCOME_MAXITER,
                        "the optimisation converged but the frequency calculation did not "
                        "complete; only the frequency stage needs to be continued")
        return make(OUTCOME_COMPLETE, "ORCA terminated normally and the job-type "
                                      "convergence criterion was met")

    # 8. Non-iterative jobs: normal termination genuinely is completion.
    if normal_end:
        if job_kind == "freq" and not freq_done:
            return make(OUTCOME_MAXITER,
                        "ORCA terminated normally but no vibrational frequencies were printed")
        return make(OUTCOME_COMPLETE, "ORCA terminated normally")

    # 9. Stopped without a normal end and without a recognised cause.
    return make(OUTCOME_INCOMPLETE,
                "the run ended without 'ORCA TERMINATED NORMALLY', without hitting a "
                "watchdog limit, and without a recognised error -- most often the machine's "
                "memory ran out or the process was killed by the platform")


# ---------------------------------------------------------------------------
# Structural validators
# ---------------------------------------------------------------------------
# A hash proves a file arrived intact. It does NOT prove the producer finished
# writing it before being killed: a truncated .hess has a perfectly valid
# sha256 of its truncated self. Structure is therefore checked in addition to,
# never instead of, the hash.
class ValidationResult:
    __slots__ = ("ok", "validator", "reason", "detail")

    def __init__(self, ok: bool, validator: str, reason: str = "", **detail):
        self.ok = ok
        self.validator = validator
        self.reason = reason
        self.detail = detail

    def __bool__(self) -> bool:
        return self.ok

    def to_dict(self) -> dict:
        return {"ok": self.ok, "validator": self.validator,
                "reason": self.reason, **self.detail}


def read_trajectory_frames(path_or_text: str, *, is_text: bool = False) -> list[str]:
    """Parses an ORCA `*_trj.xyz`: concatenated multi-XYZ with no separators.

    Returns only COMPLETE frames. A trajectory is append-only, so a process
    killed mid-write can only ever corrupt the final frame -- discarding a
    torn tail turns a hard kill into a clean, losslessly-resumable checkpoint.
    This property is why the design resumes from ASCII trajectories rather than
    from the binary `.gbw` wavefunction, which has no such guarantee.
    """
    if is_text:
        lines = (path_or_text or "").splitlines()
    else:
        try:
            with open(path_or_text, "r", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            return []

    frames, i, total = [], 0, len(lines)
    while i < total:
        head = lines[i].strip()
        if not head:
            i += 1
            continue
        try:
            natoms = int(head.split()[0])
        except (ValueError, IndexError):
            break
        if natoms <= 0 or i + 2 + natoms > total:
            break
        block = lines[i:i + 2 + natoms]
        coords_ok = True
        for k in range(natoms):
            parts = block[2 + k].split()
            if len(parts) < 4:
                coords_ok = False
                break
            try:
                float(parts[1]); float(parts[2]); float(parts[3])
            except ValueError:
                coords_ok = False
                break
        if not coords_ok:
            break
        frames.append("\n".join(block))
        i += 2 + natoms
    return frames


def validate_xyz(path: str) -> ValidationResult:
    frames = read_trajectory_frames(path)
    if not frames:
        return ValidationResult(False, "xyz", "no complete XYZ frame could be parsed",
                                path=os.path.basename(path))
    natoms = int(frames[0].split("\n", 1)[0].split()[0])
    if natoms <= 0:
        return ValidationResult(False, "xyz", "frame declares a non-positive atom count")
    return ValidationResult(True, "xyz", frames=len(frames), natoms=natoms)


def validate_trajectory(path: str) -> ValidationResult:
    frames = read_trajectory_frames(path)
    if not frames:
        return ValidationResult(False, "trajectory", "no complete frame in the trajectory")
    counts = {int(f.split("\n", 1)[0].split()[0]) for f in frames}
    if len(counts) != 1:
        return ValidationResult(False, "trajectory",
                                "frames disagree on the atom count, so the file is not a "
                                "single coherent trajectory", atom_counts=sorted(counts))
    return ValidationResult(True, "trajectory", frames=len(frames), natoms=counts.pop())


def validate_hessian(path: str) -> ValidationResult:
    """An ORCA `.hess` is ASCII, so it survives a kill -- but it survives it
    *truncated*. Pointing `InHess Read` at a partial matrix aborts the new run
    immediately, which reads to the user as "the restart is broken".

    ORCA prints the NxN Hessian in column blocks: a header row of column
    indices, then N data rows. Completeness therefore means every row index
    0..N-1 has accumulated exactly N values across all blocks. Anything less is
    a mid-block truncation.
    """
    def _all_ints(tokens):
        for t in tokens:
            try:
                int(t)
            except ValueError:
                return False
        return bool(tokens)

    try:
        with open(path, "r", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        return ValidationResult(False, "hessian", f"unreadable: {exc}")

    start = -1
    for idx, line in enumerate(lines):
        if line.strip().lower() == "$hessian":
            start = idx
            break
    if start < 0:
        return ValidationResult(False, "hessian", "no $hessian block present")

    try:
        n = int(lines[start + 1].strip())
    except (IndexError, ValueError):
        return ValidationResult(False, "hessian", "the $hessian block has no dimension line")
    if n <= 0:
        return ValidationResult(False, "hessian", "the Hessian dimension is not positive")

    counts: dict[int, int] = {}
    for line in lines[start + 2:]:
        parts = line.split()
        if not parts:
            continue
        if parts[0].startswith("$"):
            break
        if _all_ints(parts):
            continue                                  # column-index header row
        try:
            row = int(parts[0])
        except ValueError:
            continue
        values = parts[1:]
        try:
            for v in values:
                float(v)
        except ValueError:
            continue
        if 0 <= row < n:
            counts[row] = counts.get(row, 0) + len(values)

    complete = len(counts) == n and all(counts.get(r, 0) == n for r in range(n))
    if not complete:
        got = len(counts)
        short = [r for r in range(n) if counts.get(r, 0) != n][:5]
        return ValidationResult(
            False, "hessian",
            "the Hessian matrix is truncated -- the producer was killed mid-write",
            dimension=n, rows_present=got, first_short_rows=short,
        )
    return ValidationResult(True, "hessian", dimension=n)


def validate_allxyz(path: str) -> ValidationResult:
    """An ORCA `.allxyz` is XYZ frames separated by lines containing exactly
    `>`. NEB rewrites it in place each iteration, so a kill can tear it; a torn
    file fed back through `Restart_ALLXYZFile` aborts the successor."""
    try:
        with open(path, "r", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        return ValidationResult(False, "allxyz", f"unreadable: {exc}")

    groups, current = [], []
    for line in lines:
        if line.strip() == ">":
            groups.append(current)
            current = []
        else:
            current.append(line)
    groups.append(current)

    frames, natoms0 = 0, None
    for group in groups:
        group = [g for g in group if g.strip()]
        if not group:
            return ValidationResult(False, "allxyz", "an empty frame block was found")
        try:
            natoms = int(group[0].split()[0])
        except (ValueError, IndexError):
            return ValidationResult(False, "allxyz", "a frame has no valid atom-count line")
        coords = group[2:2 + natoms]
        if len(coords) < natoms or any(len(c.split()) < 4 for c in coords):
            return ValidationResult(False, "allxyz",
                                    "a frame is truncated mid-coordinate-block")
        if natoms0 is None:
            natoms0 = natoms
        elif natoms != natoms0:
            return ValidationResult(False, "allxyz", "frames disagree on the atom count")
        frames += 1
    if frames < 2:
        return ValidationResult(False, "allxyz",
                                "a NEB path needs at least two images to restart from")
    return ValidationResult(True, "allxyz", images=frames, natoms=natoms0)


def validate_engrad(path: str) -> ValidationResult:
    try:
        with open(path, "r", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        return ValidationResult(False, "engrad", f"unreadable: {exc}")
    if "number of atoms" not in text.lower():
        return ValidationResult(False, "engrad", "not a recognisable .engrad file")
    return ValidationResult(True, "engrad")


def validate_gbw(path: str) -> ValidationResult:
    """The binary wavefunction — always rejected as a restart artefact.

    A `.gbw` cannot be verified. It is binary, it carries no length or checksum
    that would reveal a truncated tail, and a force-killed ORCA leaves one that
    looks perfectly plausible by size alone. Carrying it "just as an SCF guess"
    was still enough to kill a chain: the successor AutoStarts or MOReads it and
    aborts with "GBWFile is corrupt / I/O OPERATION FAILED", throwing away the
    good ASCII checkpoints that were already in hand. The SCF guess it saves is
    worth minutes; the chain it costs is worth days.

    Restart is therefore driven entirely from artefacts that CAN be verified —
    .xyz, *_trj.xyz, .allxyz, .hess, .mdrestart, .res.* — and `! NoAutoStart`
    is forced so a stray file left in the working directory is ignored too."""
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return ValidationResult(False, "gbw", f"unreadable: {exc}")
    return ValidationResult(
        False, "gbw",
        "the binary wavefunction is never used for restart here — it cannot be checked for "
        "truncation, and a corrupt one aborts the successor run. Continuation uses the ASCII "
        "geometry/trajectory/Hessian artefacts instead.",
        size=size)


def validate_mdrestart(path: str) -> ValidationResult:
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return ValidationResult(False, "mdrestart", f"unreadable: {exc}")
    if size <= 0:
        return ValidationResult(False, "mdrestart", "empty MD restart file")
    return ValidationResult(True, "mdrestart", size=size)


def validate_text(path: str) -> ValidationResult:
    try:
        return (ValidationResult(True, "text", size=os.path.getsize(path))
                if os.path.getsize(path) > 0
                else ValidationResult(False, "text", "file is empty"))
    except OSError as exc:
        return ValidationResult(False, "text", f"unreadable: {exc}")


VALIDATORS = {
    "xyz": validate_xyz,
    "trajectory": validate_trajectory,
    "hessian": validate_hessian,
    "allxyz": validate_allxyz,
    "engrad": validate_engrad,
    "gbw": validate_gbw,
    "mdrestart": validate_mdrestart,
    "input": validate_text,
    "text": validate_text,
    "auxiliary": validate_text,
}


def validator_for(filename: str) -> str:
    low = filename.lower()
    if low.endswith("_trj.xyz"):
        return "trajectory"
    if low.endswith(".allxyz"):
        return "allxyz"
    if low.endswith(".hess"):
        return "hessian"
    if low.endswith(".engrad"):
        return "engrad"
    if low.endswith(".gbw"):
        return "gbw"
    if low.endswith(".mdrestart"):
        return "mdrestart"
    if low.endswith(".xyz"):
        return "xyz"
    if low.endswith(".inp"):
        return "input"
    return "auxiliary"


def validate_file(path: str) -> ValidationResult:
    return VALIDATORS[validator_for(os.path.basename(path))](path)


# ---------------------------------------------------------------------------
# Bundle verification (shared by the server and the in-kernel runner)
# ---------------------------------------------------------------------------
def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def verify_bundle(file_records, directory: str, *, next_input_text=None,
                  next_input_sha256=None) -> dict:
    """Re-reads and re-validates a checkpoint bundle from disk.

    `file_records` is a list of plain dicts (`name`, `sha256`, `size`, `role`,
    `required`) rather than typed objects, so this function is callable from
    the in-kernel runner, which has no access to the orchestrator package.
    Both sides therefore run the identical checks: the producer before it
    commits a checkpoint, and the consumer before it trusts one.

    Returns `{"ok": bool, "problems": [...], "verified": [...]}`. Problems are
    reported, never raised, so the caller decides whether a missing optional
    artefact is a degradation or a rejection.
    """
    problems, verified = [], []

    for record in file_records or []:
        name = record.get("name", "")
        path = os.path.join(directory, name)
        required = bool(record.get("required"))
        role = record.get("role", "auxiliary")

        if not os.path.exists(path):
            problems.append({"file": name, "role": role, "required": required,
                             "problem": "missing from the bundle"})
            continue
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            problems.append({"file": name, "role": role, "required": required,
                             "problem": "unreadable: %s" % exc})
            continue
        if record.get("size") is not None and size != int(record["size"]):
            problems.append({"file": name, "role": role, "required": required,
                             "problem": "size %d does not match the recorded %s"
                                        % (size, record["size"])})
            continue
        if record.get("sha256"):
            actual = sha256_file(path)
            if actual != record["sha256"]:
                problems.append({"file": name, "role": role, "required": required,
                                 "problem": "sha256 mismatch: the content changed in transit"})
                continue
        result = validate_file(path)
        if not result.ok:
            problems.append({"file": name, "role": role, "required": required,
                             "problem": "structural check failed: %s" % result.reason})
            continue
        verified.append(name)

    if next_input_sha256 is not None:
        actual = sha256_text(next_input_text or "")
        if actual != next_input_sha256:
            problems.append({"file": "<next_input>", "role": "input", "required": True,
                             "problem": "the continuation input does not match its digest"})

    blocking = [p for p in problems if p["required"]]
    return {"ok": not blocking, "problems": problems, "verified": verified,
            "blocking": blocking}


# ---------------------------------------------------------------------------
# Input rewriting for continuation
# ---------------------------------------------------------------------------
def extract_charge_mult(text: str) -> tuple[str, str]:
    m = re.search(r"\*\s*xyz(?:file)?\s+(-?\d+)\s+(-?\d+)", text or "", re.IGNORECASE)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"\*\s*(?:gzmt|internal|int)\s+(-?\d+)\s+(-?\d+)", text or "", re.IGNORECASE)
    if m:
        return m.group(1), m.group(2)
    return "0", "1"


def ensure_simple_keyword(text: str, keyword: str) -> str:
    lines = (text or "").split("\n")
    for i, line in enumerate(lines):
        if line.lstrip().startswith("!"):
            if keyword.lower() not in line.lower():
                lines[i] = line.rstrip() + " " + keyword
            return "\n".join(lines)
    return "! " + keyword + "\n" + (text or "")


def set_geometry(text: str, xyz_name: str, charge: str, mult: str) -> str:
    replacement = f"* xyzfile {charge} {mult} {xyz_name}"
    new, n = re.subn(r"\*\s*xyz\s+-?\d+\s+-?\d+.*?\*", lambda _m: replacement,
                     text or "", flags=re.IGNORECASE | re.DOTALL)
    if n:
        return new
    new, n = re.subn(r"\*\s*xyzfile\s+-?\d+\s+-?\d+\s+\S+", lambda _m: replacement,
                     text or "", flags=re.IGNORECASE)
    return new if n else text


def strip_moread(text: str) -> str:
    """Removes any explicit wavefunction read, since the `.gbw` is never a
    required restart input here and a dangling MORead aborts the run."""
    text = re.sub(r"(?i)\bMOREAD\b", "", text or "")
    text = re.sub(r"(?im)^[ \t]*%\s*moinp\b.*$", "", text)
    return text


def set_geom_maxiter(text: str, maxiter: int) -> str:
    """Guarantees each window gets a full optimisation budget.

    Without this, a job that reached MaxIter is restarted from its last
    geometry and immediately inherits the same limit, so long optimisations
    make one window's worth of progress and then stall in exactly the same
    place -- forever. The cumulative-cycle budget in config.py, not this value,
    is what eventually stops a genuinely non-converging system.
    """
    text = text or ""
    if re.search(r"(?is)%\s*geom\b.*?\bend\b", text):
        if re.search(r"(?i)\bmaxiter\b", text):
            return re.sub(r"(?i)(\bmaxiter\s+)(\d+)", lambda m: m.group(1) + str(maxiter), text)
        return re.sub(r"(?i)(%\s*geom\b)", lambda m: m.group(0) + f"\n  MaxIter {maxiter}",
                      text, count=1)
    return text.rstrip() + f"\n%geom\n  MaxIter {maxiter}\nend\n"


def requested_nprocs(text: str) -> int:
    m = re.search(r"%\s*pal\b(?:(?!\bend\b).)*?nprocs\s+(\d+)", text or "",
                  re.IGNORECASE | re.DOTALL)
    if m:
        return int(m.group(1))
    m = re.search(r"(?i)\bPAL([1-9]\d?)\b", text or "")
    return int(m.group(1)) if m else 1


def set_nprocs(text: str, nprocs: int) -> str:
    new = re.sub(r"(%\s*pal\b(?:(?!\bend\b).)*?nprocs\s+)(\d+)",
                 lambda m: m.group(1) + str(nprocs), text or "",
                 flags=re.IGNORECASE | re.DOTALL)
    if nprocs <= 1:
        new = re.sub(r"(?i)\s*\bPAL[1-9]\d?\b", "", new)
    else:
        new = re.sub(r"(?i)\bPAL[1-9]\d?\b", "PAL" + str(nprocs), new)
    return new


def clamp_maxcore(text: str, nprocs: int, total_ram_mb: int,
                  fraction: float = 0.70) -> tuple[str, dict | None]:
    """`%maxcore` is PER MPI PROCESS. nprocs x maxcore above physical RAM gets
    the run killed by the OOM killer, which presents as a mysterious silent
    stop rather than an ORCA error."""
    budget = int(total_ram_mb * fraction)
    cap = max(700, budget // max(1, nprocs))
    changed: list[tuple[int, int]] = []

    def repl(m):
        want = int(m.group(2))
        if want <= cap:
            return m.group(0)
        changed.append((want, cap))
        return m.group(1) + str(cap)

    new = re.sub(r"(?i)(%\s*maxcore\s+)(\d+)", repl, text or "")
    if not changed:
        return new, None
    return new, {"requested_mb": changed[0][0], "granted_mb": cap,
                 "nprocs": nprocs, "machine_total_mb": total_ram_mb}


# ---------------------------------------------------------------------------
# Disk accounting
# ---------------------------------------------------------------------------
class DiskAccountant:
    """Quota-aware disk accounting for a Kaggle session.

    The production log line `free=1006.8 GB` is what this class exists to
    prevent. Inside a Kaggle container, `shutil.disk_usage(path).free` reports
    the host's overlay filesystem, which has terabytes free, while Kaggle
    separately enforces roughly 20 GB on `/kaggle/working` and about 60 GiB of
    scratch. Trusting statvfs means the free-space watchdog can never fire and
    the run dies on ENOSPC instead of checkpointing cleanly.

    So headroom is the MINIMUM of three independent estimates:

      1. quota headroom  = quota - bytes we have actually written
      2. statvfs free    - a real upper bound; the host can still fill up
      3. probe result    - an actual write, because a quota can be enforced by
                           the filesystem returning ENOSPC/EDQUOT with no
                           warning at all

    Every number is reported, always. An operator must never again have to
    guess which filesystem a free-space figure referred to.
    """

    def __init__(self, working_dir: str, scratch_dirs, *, working_quota: int,
                 scratch_quota: int, implausible_free: int):
        self.working_dir = working_dir
        self.scratch_dirs = [d for d in scratch_dirs if d]
        self.working_quota = int(working_quota)
        self.scratch_quota = int(scratch_quota)
        self.implausible_free = int(implausible_free)
        self.statvfs_untrusted = False

    @staticmethod
    def dir_size(path: str) -> int:
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return total

    @staticmethod
    def statvfs_free(path: str) -> int | None:
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return None

    @staticmethod
    def probe_write(path: str, size_bytes: int = 8 << 20) -> tuple[bool, str]:
        """Actually writes and fsyncs a block.

        This is the only test that catches a quota enforced purely by the write
        path returning ENOSPC or EDQUOT while statvfs keeps reporting a
        terabyte free -- which is exactly the Kaggle situation."""
        probe = os.path.join(path, ".orca-space-probe")
        try:
            with open(probe, "wb") as fh:
                fh.write(b"\0" * int(size_bytes))
                fh.flush()
                os.fsync(fh.fileno())
            return True, ""
        except OSError as exc:
            return False, f"{type(exc).__name__}: {exc}"
        finally:
            try:
                os.remove(probe)
            except OSError:
                pass

    def snapshot(self, *, probe: bool = False) -> dict:
        working_used = self.dir_size(self.working_dir)
        scratch_used = sum(self.dir_size(d) for d in self.scratch_dirs)
        raw_free = self.statvfs_free(self.scratch_dirs[0] if self.scratch_dirs
                                     else self.working_dir)

        self.statvfs_untrusted = raw_free is not None and raw_free > self.implausible_free

        working_headroom = max(0, self.working_quota - working_used)
        scratch_headroom = max(0, self.scratch_quota - scratch_used)

        candidates = [scratch_headroom]
        if raw_free is not None and not self.statvfs_untrusted:
            candidates.append(raw_free)
        effective = min(candidates)

        report = {
            "working_dir": self.working_dir,
            "working_used_bytes": working_used,
            "working_quota_bytes": self.working_quota,
            "working_headroom_bytes": working_headroom,
            "scratch_dirs": list(self.scratch_dirs),
            "scratch_used_bytes": scratch_used,
            "scratch_quota_bytes": self.scratch_quota,
            "scratch_headroom_bytes": scratch_headroom,
            "statvfs_free_bytes": raw_free,
            "statvfs_trusted": not self.statvfs_untrusted,
            "effective_headroom_bytes": effective,
            "accounting_mode": "quota" if self.statvfs_untrusted else "min(quota, statvfs)",
        }
        if self.statvfs_untrusted:
            report["statvfs_note"] = (
                "statvfs reports %.1f GB free, which exceeds the plausibility "
                "threshold of %.0f GB. That number is the host overlay filesystem, "
                "not Kaggle's enforced per-notebook quota, so it is ignored for "
                "watchdog decisions and quota accounting is used instead."
                % (raw_free / float(1 << 30), self.implausible_free / float(1 << 30))
            )
        if probe:
            ok, detail = self.probe_write(self.scratch_dirs[0] if self.scratch_dirs
                                          else self.working_dir)
            report["probe_ok"] = ok
            if not ok:
                report["probe_error"] = detail
                report["effective_headroom_bytes"] = 0
                report["accounting_mode"] = "probe-failed"
        return report

    @staticmethod
    def human(nbytes) -> str:
        if nbytes is None:
            return "?"
        for unit, scale in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
            if nbytes >= scale:
                return "%.1f %s" % (nbytes / float(scale), unit)
        return "%d B" % nbytes
