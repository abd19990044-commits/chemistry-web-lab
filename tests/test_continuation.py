# -*- coding: utf-8 -*-
"""
Regression suite for the legacy Kaggle runner's continuation logic.

Runs with plain `python tests/test_continuation.py` -- no pytest, so it works
inside the Docker image on Hugging Face without adding a dependency.

The bug this file exists for, reproduced from a real production log:

    22742.1s  done=True opt_converged=False stopped_by=None orca_error=False
              disk_failure=False restart=0 disk_restart=0 free=1006.8 GB
    22742.1s  [done] ORCA terminated normally.
    22746.2s  [results] Packaged 8 file(s) into results.zip
    22746.2s  [end] Wall time 06h19m05s.

A TD-DFT geometry optimisation exhausted ORCA's own `%geom MaxIter` budget
after six hours. ORCA prints "ORCA TERMINATED NORMALLY" when that happens --
it is a clean ORCA exit, not an error -- and the runner took that at face
value, marked the job complete and threw away five and a half hours of
remaining session time. Nothing had converged.

`opt_converged=False` was right there in the log line and was not acted on.
These tests exec the decision logic straight out of KAGGLE_RUNNER_BODY, so
they fail if that stops being true.
"""
import io
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kaggle_runner                                            # noqa: E402

BODY = kaggle_runner.KAGGLE_RUNNER_BODY

_passed, _failed = 0, 0


def check(label, condition):
    global _passed, _failed
    if condition:
        _passed += 1
        print("  PASS  %s" % label)
    else:
        _failed += 1
        print("  FAIL  %s" % label)


def section(title):
    print("\n%s\n%s" % (title, "-" * len(title)))


def slice_body(start, end):
    """Lifts a region out of the in-kernel script by its own comment markers,
    so these tests exercise the code that actually ships rather than a copy
    that can drift away from it."""
    i = BODY.index(start)
    j = BODY.index(end, i)
    return BODY[i:j]


# Section 3a is lifted whole -- helpers AND the module-level classification and
# refusal gates -- so these tests exercise the real classifier on a real input
# rather than a paraphrase of it.
HELPERS = slice_body("DEFAULT_GEOM_MAXITER = 500",
                     "ORIGINAL_USER_INP_TEXT = inp_text")
DECISION = slice_body("NOT_CONVERGED_MARKERS = (",
                      "# ── 7. Result packaging")

DEFAULT_INP = "! B3LYP def2-SVP Opt\n* xyz 0 1\nO 0.0 0.0 0.0\nH 0.0 0.0 0.96\n*\n"


def base_env(**over):
    """A namespace holding everything the two lifted regions read, with the
    machine-specific pieces (files on disk, the ORCA process) stubbed.

    `inp_text` drives the real section-3a classifier; anything passed in `over`
    that section 3a also computes is applied AFTER it, so a test can still pin
    a flag deliberately."""
    logged = []

    def _has_marker(text, markers):
        low = text.lower()
        return any(m in low for m in markers)

    inp = over.get("inp_text", DEFAULT_INP)
    env = {
        "re": re, "os": os, "glob": _FakeGlob(), "log": logged.append,
        "_has_marker": _has_marker, "_wp": lambda name: name,
        "_logged": logged,

        "GEOM_MAXITER": 0, "SCF_MAXITER": 0, "RESTART_COUNT": 0,
        "SCAN_TOTAL_POINTS": 0, "SCAN_POINTS_BEFORE": 0,
        "BASENAME": "job", "INPUT_FILE": "job.inp",
        "inp_text": inp, "ORIGINAL_INP_TEXT": over.get("ORIGINAL_INP_TEXT", inp),
    }
    env.update({k: v for k, v in over.items()
                if k in ("GEOM_MAXITER", "SCF_MAXITER", "RESTART_COUNT",
                         "SCAN_TOTAL_POINTS", "SCAN_POINTS_BEFORE", "BASENAME")})
    exec(compile(HELPERS, "<section-3a>", "exec"), env)

    env.update({
        "orca_normal_end": True, "orca_error": False, "opt_converged": False,
        "disk_failure": False, "stop_reason": {"why": None, "detail": ""},
        "out_text": "",
        "frames_now": 40, "hess_ready": False, "scan_points_done": 0,
        "MAX_RESTARTS": 20, "DISK_RESTART_COUNT": 0, "MAX_DISK_RESTARTS": 6,
    })
    env.update(over)
    env["ORIGINAL_INP_TEXT"] = over.get("ORIGINAL_INP_TEXT", env["inp_text"])
    # exec() with a single mapping runs the region at module scope, so names the
    # region reads must be present in it; section 3a supplies these, but only
    # when it is the same mapping. Copy them across explicitly.
    exec(compile(DECISION, "<section-5b-6>", "exec"), env)
    return env


class _FakeGlob(object):
    """`glob.glob` is only used inside the decision region to count scan-step
    files; the tests set scan_points_done directly, so it returns nothing."""

    @staticmethod
    def glob(_pattern):
        return []


NORMAL_END = "\n" + " " * 20 + "****ORCA TERMINATED NORMALLY****\n"
MAXITER_HIT = ("\nThe optimization did not converge but reached the maximum "
               "number of\noptimization cycles.\n")
CONVERGED = ("\n***********************HURRAY********************\n"
             "***        THE OPTIMIZATION HAS CONVERGED     ***\n")
FREQS = "\n-----------------------\nVIBRATIONAL FREQUENCIES\n-----------------------\n"


# ===========================================================================
section("1. Regression: 'ORCA TERMINATED NORMALLY' is not proof of completion")
# ===========================================================================
env = base_env(out_text=MAXITER_HIT + NORMAL_END)
check("an optimisation that exhausted MaxIter is NOT reported as finished",
      env["completion_gap"] != "")
check("the gap names the geometry optimisation",
      "optimization never converged" in env["completion_gap"])
check("REGRESSION: the six-hour TD-DFT window continues instead of stopping",
      env["needs_continue"] is True)
check("it is classified as a budget restart, not a time or disk restart",
      env["restart_kind"] == "budget")
check("the successor is given a LARGER cycle budget than this window had",
      env["NEXT_GEOM_MAXITER"] > 0 and env["NEXT_GEOM_MAXITER"] > env["GEOM_MAXITER"])
check("no 'do not restart' reason is invented for a continuable outcome",
      env["_no_restart_reason"] == "")

env = base_env(out_text=CONVERGED + NORMAL_END, opt_converged=True,
               inp_text="! B3LYP def2-SVP Opt\n", ORIGINAL_INP_TEXT="! B3LYP def2-SVP Opt\n")
check("a genuinely converged optimisation still finishes", env["completion_gap"] == "")
check("...and is not restarted", env["needs_continue"] is False)

env = base_env(out_text=CONVERGED + NORMAL_END, opt_converged=True,
               inp_text="! B3LYP def2-SVP Opt Freq\n",
               ORIGINAL_INP_TEXT="! B3LYP def2-SVP Opt Freq\n")
check("an Opt Freq whose frequencies never ran is not 'complete' either",
      "VIBRATIONAL FREQUENCIES" in env["completion_gap"])
env = base_env(out_text=CONVERGED + FREQS + NORMAL_END, opt_converged=True,
               inp_text="! B3LYP def2-SVP Opt Freq\n",
               ORIGINAL_INP_TEXT="! B3LYP def2-SVP Opt Freq\n")
check("an Opt Freq that produced frequencies IS complete", env["completion_gap"] == "")


# ===========================================================================
section("2. A restart must be able to make progress, or it must not happen")
# ===========================================================================
env = base_env(out_text=MAXITER_HIT + NORMAL_END, frames_now=0, hess_ready=False)
check("no completed step in the whole window -> no restart loop",
      env["needs_continue"] is False)
check("...and the reason says so plainly",
      "no completed step" in env["_no_restart_reason"])

env = base_env(out_text=MAXITER_HIT + NORMAL_END, GEOM_MAXITER=4000)
check("an optimisation already at the maximum cycle budget stops",
      env["needs_continue"] is False)
check("...and the reason names the budget and suggests what to change",
      "maximum cycle budget" in env["_no_restart_reason"])

env = base_env(out_text=MAXITER_HIT + NORMAL_END, GEOM_MAXITER=500)
check("the budget escalates geometrically between windows",
      env["NEXT_GEOM_MAXITER"] == 1000)
env = base_env(out_text=MAXITER_HIT + NORMAL_END, GEOM_MAXITER=3000)
check("...but is clamped at the cap rather than growing without bound",
      env["NEXT_GEOM_MAXITER"] == 4000)


# ===========================================================================
section("3. Job kinds with no text checkpoint are still judged honestly")
# ===========================================================================
sp = dict(inp_text="! B3LYP def2-SVP TDDFT\n* xyz 0 1\nO 0 0 0\n*\n")
env = base_env(out_text=NORMAL_END, **sp)
check("a TD-DFT single point that ended normally is genuinely complete",
      env["completion_gap"] == "" and env["needs_continue"] is False)

env = base_env(out_text="\nSCF NOT CONVERGED AFTER  125 CYCLES\n" + NORMAL_END, **sp)
check("a single point whose SCF stalled is NOT silently reported complete",
      env["completion_gap"] == "the SCF did not converge")

env = base_env(out_text="\nSCF NOT CONVERGED AFTER 125 CYCLES\nORCA finished by error "
                        "termination in SCF\n",
               orca_normal_end=False, orca_error=True, **sp)
check("an SCF that ran out of iterations earns one bigger-budget retry",
      env["needs_continue"] is True and env["restart_kind"] == "scf")
check("...with a real SCF budget increase behind it",
      env["NEXT_SCF_MAXITER"] >= 500)
env = base_env(out_text="\nSCF NOT CONVERGED AFTER 125 CYCLES\nORCA finished by error "
                        "termination in SCF\n",
               orca_normal_end=False, orca_error=True, SCF_MAXITER=2000, **sp)
check("...but never more than once at the cap", env["needs_continue"] is False)

env = base_env(out_text=CONVERGED + "\nFINAL SINGLE POINT ENERGY   -76.4321\n"
                        + "\nSCF NOT CONVERGED AFTER 125 CYCLES\n" + NORMAL_END,
               opt_converged=True)
check("one bad SCF early in a converged optimisation is not a reason to restart",
      env["completion_gap"] == "" and env["needs_continue"] is False)

env = base_env(out_text=CONVERGED + NORMAL_END, opt_converged=True,
               inp_text="! B3LYP Opt  # neb comes later\n* xyz 0 1\nO 0 0 0\n*\n")
check("REGRESSION: a molecule or comment containing 'neb' is not a NEB job",
      env["is_neb_job"] is False)
check("...so it is not held to a NEB convergence banner",
      env["completion_gap"] == "" and env["needs_continue"] is False)

env = base_env(out_text=NORMAL_END,
               inp_text="! B3LYP NEB-TS\n%neb NImages 8 end\n* xyz 0 1\nO 0 0 0\n*\n")
check("a real NEB run is recognised", env["is_neb_job"] is True)
check("...and still has to print its convergence banner",
      env["completion_gap"] == "the NEB path never converged")

env = base_env(out_text=CONVERGED + NORMAL_END, opt_converged=True,
               inp_text="! B3LYP Opt  # add freq next time\n* xyz 0 1\nO 0 0 0\n*\n")
check("a commented-out 'freq' does not make a finished Opt look incomplete",
      env["completion_gap"] == "")

scan = dict(inp_text="! B3LYP Opt\n%geom Scan B 0 1 = 1.0, 2.0, 10 end end\n"
                     "* xyz 0 1\nO 0 0 0\n*\n")
env = base_env(out_text=CONVERGED + NORMAL_END, opt_converged=True,
               scan_points_done=5, **scan)
check("a scan that stopped at point 5 of 10 is not complete",
      env["completion_gap"] == "the scan stopped at point 5 of 10")
check("...and continues", env["needs_continue"] is True and env["restart_kind"] == "budget")
env = base_env(out_text=CONVERGED + NORMAL_END, opt_converged=True,
               scan_points_done=10, **scan)
check("a scan that ran every point is complete",
      env["completion_gap"] == "" and env["needs_continue"] is False)


# ===========================================================================
section("4. Resource limits keep behaving exactly as before")
# ===========================================================================
env = base_env(out_text="partial output", orca_normal_end=False,
               stop_reason={"why": "time", "detail": "budget"})
check("the session-time watchdog still triggers a time restart",
      env["needs_continue"] is True and env["restart_kind"] == "time")
env = base_env(out_text="partial output", orca_normal_end=False,
               stop_reason={"why": "disk", "detail": "full"})
check("the scratch-disk watchdog still triggers a disk restart",
      env["needs_continue"] is True and env["restart_kind"] == "disk")
env = base_env(out_text="UNRECOGNIZED OR DUPLICATED KEYWORD", orca_normal_end=False,
               orca_error=True)
check("a genuine input error is still never restarted",
      env["needs_continue"] is False and "error" in env["_no_restart_reason"].lower())


# ===========================================================================
section("5. Input preparation: budgets in, wavefunction out")
# ===========================================================================
def prepare_with(inp, first_window=False, **kw):
    e = base_env(inp_text=inp, **kw)
    return e["_prepare_input"](inp, first_window=first_window)


out = prepare_with("! B3LYP def2-SVP Opt\n* xyz 0 1\nH 0 0 0\n*\n")
check("a MaxIter budget is injected when the input has none", "MaxIter 500" in out)
check("NoAutoStart is forced so a stray .gbw can never be auto-read",
      "NoAutoStart" in out)

out = prepare_with("! B3LYP Opt\n%geom MaxIter 80 end\n")
check("an explicit MaxIter written by the person is respected on window 1",
      "MaxIter 80" in out and "MaxIter 500" not in out)

out = prepare_with("! B3LYP Opt\n%geom MaxIter 80 end\n", GEOM_MAXITER=1000)
check("...but a continuation overrides it with the raised budget",
      "MaxIter 1000" in out and "MaxIter 80" not in out)

out = prepare_with('! B3LYP Opt MOREAD\n%moinp "previous.gbw"\n')
check("MOREAD is stripped", "MOREAD" not in out.upper())
check("%moinp is stripped", "moinp" not in out.lower())
check("no .gbw reference survives into the prepared input", ".gbw" not in out.lower())

nested = ("! B3LYP Opt\n%geom\n  Constraints\n    { B 0 1 C }\n  end\n"
          "  maxstep 0.1\nend\n* xyz 0 1\nH 0 0 0\n*\n")
out = prepare_with(nested)
check("MaxIter lands inside %geom even when it holds a nested Constraints...end",
      re.search(r"(?is)%geom\s+MaxIter\s+500", out) is not None)
check("...without disturbing the constraints themselves",
      "{ B 0 1 C }" in out and out.count("end") == nested.count("end"))

one_line = "! B3LYP Opt\n%geom Scan B 0 1 = 1.0, 2.0, 10 end end\n"
out = prepare_with(one_line)
check("a one-line %geom Scan block keeps every keyword on its own line",
      all(len([w for w in ("MaxIter", "Scan") if w in ln]) <= 1
          for ln in out.splitlines()))
check("...and the scan definition itself is untouched",
      "Scan B 0 1 = 1.0, 2.0, 10 end" in out)

out = prepare_with("! B3LYP def2-SVP Opt\n", SCF_MAXITER=1000)
check("an SCF retry ships a bigger SCF budget", "MaxIter 1000" in out)
check("...and SlowConv to go with it", "SlowConv" in out)


# ===========================================================================
section("6. The generated kernel script still compiles")
# ===========================================================================

job_dir = kaggle_runner.build_job_dir(
    kaggle_username="tester", kaggle_key="0" * 32,
    job_base_id="chem-tools-regression-0badc0de",
    input_filename="molecule.inp",
    files_payload={"molecule.inp": "IyB0ZXN0"},
    dataset_sources=["tester/orca-6-1-0"],
)
try:
    with open(os.path.join(job_dir, "script.py"), encoding="utf-8") as fh:
        script = fh.read()
    compile(script, "script.py", "exec")
    check("build_job_dir produces a script.py that compiles", True)
    check("the new budget variables are in the shipped header",
          "GEOM_MAXITER = 0" in script and "SCF_MAXITER = 0" in script)
    check("every header variable is populated",
          all(("\n%s = " % name) in ("\n" + script) or script.startswith(name + " = ")
              for name in kaggle_runner.HEADER_VARS))
finally:
    shutil.rmtree(job_dir, ignore_errors=True)

check("a continuation kernel title stays inside Kaggle's 6-50 character rule",
      all(6 <= len(kaggle_runner.kaggle_safe_title("x" * n)) <= 50
          for n in range(1, 120)))


# ===========================================================================
section("7. A continuation the browser is told to follow must actually exist")
# ===========================================================================
# The other half of the reported problem: the site moved a job -- and its
# "View on Kaggle" link -- onto whatever notebook name the previous window
# wrote into NEXT_JOB_ID.txt, without ever asking Kaggle whether that notebook
# was there. A push Kaggle rejected, or a notebook since deleted, therefore
# swapped a working link for a dead one and the running work vanished from
# the list.
import subprocess                                               # noqa: E402


def fake_cli(hand_off, successor_exists):
    def _cli(args, env=None, timeout=60, attempts=4, base_delay=3.0):
        argv = list(args)
        if argv[2] == "status":
            ref = argv[3]
            if ref.endswith("-r1") and not successor_exists:
                return subprocess.CompletedProcess(
                    argv, 1, "", "404 - Not Found: kernel not found")
            word = "COMPLETE" if not ref.endswith("-r1") else "RUNNING"
            return subprocess.CompletedProcess(
                argv, 0, '%s has status "KernelWorkerStatus.%s"' % (ref, word), "")
        if argv[2] == "output":
            dest = argv[argv.index("-p") + 1]
            for name, text in hand_off.items():
                with open(os.path.join(dest, name), "w", encoding="utf-8") as fh:
                    fh.write(text)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    return _cli


JOB = "chem-tools-tddft-0badc0de"
real_cli = kaggle_runner._run_kaggle_cli
try:
    kaggle_runner._run_kaggle_cli = fake_cli(
        {"NEXT_JOB_ID.txt": JOB + "-r1",
         "NEXT_JOB_URL.txt": "https://www.kaggle.com/code/tester/%s-r1" % JOB,
         "JOB_NOTE.txt": "Ran out of ORCA optimization cycles; continued."},
        successor_exists=True)
    res = kaggle_runner.check_job_status("tester", "0" * 32, JOB)
    check("a real continuation is followed", res["status"] == "restarting")
    check("...to the id the previous window handed off",
          res["next_job_id"] == JOB + "-r1")
    check("...at the URL that window reported",
          res["next_kaggle_url"].endswith("%s-r1" % JOB))

    kaggle_runner._run_kaggle_cli = fake_cli(
        {"NEXT_JOB_ID.txt": JOB + "-r1",
         "NEXT_JOB_URL.txt": "https://www.kaggle.com/code/tester/%s-r1" % JOB},
        successor_exists=False)
    res = kaggle_runner.check_job_status("tester", "0" * 32, JOB)
    check("REGRESSION: a hand-off to a notebook Kaggle does not have is refused",
          res["next_job_id"] is None)
    check("...so the job keeps the link it already had",
          res["next_kaggle_url"] is None and res["status"] == "complete")
    check("...and the person is told why", "no notebook at that address" in res["warning"])

    kaggle_runner._run_kaggle_cli = fake_cli(
        {"JOB_NOTE.txt": "ORCA terminated normally, but the SCF did not converge."},
        successor_exists=False)
    res = kaggle_runner.check_job_status("tester", "0" * 32, JOB)
    check("a finished-but-unconverged job surfaces its note as a warning",
          "SCF did not converge" in (res.get("warning") or ""))
finally:
    kaggle_runner._run_kaggle_cli = real_cli


# ===========================================================================
section("8. Review findings: restart correctness under awkward inputs")
# ===========================================================================
GEOM = {}
for _fn in ("_extract_charge_mult", "_set_geometry", "_xyz_is_complete",
            "_input_natoms", "_drop_references", "_geometry_file_of"):
    _i = BODY.index("def %s(" % _fn)
    _j = BODY.index("\n\n\n", _i)
    exec(compile(BODY[_i:_j], "<geom>", "exec"), {"re": re, "os": os}, GEOM)
GEOM = dict(GEOM)
for _k, _v in list(GEOM.items()):        # let the helpers see each other
    if callable(_v):
        _v.__globals__.update({"re": re, "os": os, **GEOM})

set_geometry = GEOM["_set_geometry"]
extract = GEOM["_extract_charge_mult"]

# Every ORCA coordinate form must be repointable, or the continuation silently
# reruns the original geometry and reports progress that did not happen.
for label, inp in (
        ("* xyz block", "! B3LYP Opt\n* xyz -1 3\nO 0 0 0\nH 0 0 1\n*\n"),
        ("* xyzfile",   "! B3LYP Opt\n* xyzfile -1 3 start.xyz\n"),
        ("* gzmt (Z-matrix)", "! B3LYP Opt\n* gzmt -1 3\nO\nH 1 0.96\n*\n"),
        ("* int",       "! B3LYP Opt\n* int -1 3\nO 0 0 0 0.0 0.0 0.0\n*\n")):
    charge, mult = extract(inp)
    out, placed = set_geometry(inp, "last_geometry.xyz", charge, mult)
    check("REGRESSION: %s can be repointed at the restart geometry" % label, placed)
    check("...and keeps charge/multiplicity (-1, 3)" if placed else "...(skipped)",
          placed and "xyzfile -1 3 last_geometry.xyz" in out)

out, placed = set_geometry("! B3LYP SP\n%coords\n CTyp internal\nend\n",
                           "last_geometry.xyz", 0, 1)
check("an unrecognised coordinate form reports failure instead of doing nothing",
      placed is False)

# Truncated geometry files must never be fed back to ORCA.
import tempfile                                                 # noqa: E402
tmp = tempfile.mkdtemp()
whole = os.path.join(tmp, "whole.xyz")
open(whole, "w").write("2\ncomment\nO 0.0 0.0 0.0\nH 0.0 0.0 0.96\n")
cut = os.path.join(tmp, "cut.xyz")
open(cut, "w").write("2\ncomment\nO 0.0 0.0 0.0\nH 0.0 0.0\n")
short = os.path.join(tmp, "short.xyz")
open(short, "w").write("3\ncomment\nO 0.0 0.0 0.0\n")
check("a complete .xyz is accepted", GEOM["_xyz_is_complete"](whole) is True)
check("a .xyz cut mid-line is rejected", GEOM["_xyz_is_complete"](cut) is False)
check("a .xyz missing atoms is rejected", GEOM["_xyz_is_complete"](short) is False)

check("the atom count is read from an inline coordinate block",
      GEOM["_input_natoms"]("! Opt\n* xyz 0 1\nO 0 0 0\nH 0 0 1\n*\n",
                            lambda n: n) == 2)
check("...and from a referenced .xyz file",
      GEOM["_input_natoms"]("! Opt\n* xyzfile 0 1 whole.xyz\n",
                            lambda n: os.path.join(tmp, n)) == 2)
shutil.rmtree(tmp, ignore_errors=True)

# A trimmed payload must not leave the input pointing at a missing file.
inp = ('! B3LYP OptTS NoAutoStart\n%geom\n  InHess Read\n  InHessName "job.hess"\n'
       'end\n* xyzfile 0 1 last_geometry.xyz\n')
out, removed = GEOM["_drop_references"](inp, ["job.hess"])
check("REGRESSION: a dropped .hess also removes InHess Read",
      "InHess" not in out and "InHessName" not in out)
check("...and the person is told the Hessian will be recomputed",
      any("recomputed" in r for r in removed))
check("...while the geometry the restart depends on is untouched",
      "xyzfile 0 1 last_geometry.xyz" in out)

neb = '! NEB-TS\n%neb\n  Restart_ALLXYZFile "path_MEP.allxyz"\nend\n'
out, removed = GEOM["_drop_references"](neb, ["path_MEP.allxyz"])
check("a dropped NEB path also removes Restart_ALLXYZFile",
      "Restart_ALLXYZFile" not in out and removed)

check("the geometry file a restart depends on is identifiable",
      GEOM["_geometry_file_of"]("* xyzfile 0 1 last_geometry.xyz\n") == "last_geometry.xyz")
check("...and is None when the geometry is inline",
      GEOM["_geometry_file_of"]("* xyz 0 1\nO 0 0 0\n*\n") is None)

# Scan restart arithmetic must not quietly move the grid.
a, b, npts, done = 1.4285714, 2.9285714, 15, 6
step = (b - a) / (npts - 1)
exact = a + done * step
check("REGRESSION: the scan restart point keeps full double precision",
      float("%.17g" % exact) == exact)
check("...where the previous '%g' form did not",
      float("%g" % exact) != exact)
check("the resumed scan reproduces the original grid spacing",
      abs(((b - exact) / (npts - done - 1)) - step) < 1e-12)

# The memory ceiling must come from the container, not the host.
ram = {}
for _fn in ("_cgroup_limit_mb", "_total_ram_mb"):
    _i = BODY.index("def %s(" % _fn)
    _j = BODY.index("\n\n\n", _i)
    exec(compile(BODY[_i:_j], "<ram>", "exec"), {"os": os}, ram)
for _v in ram.values():
    _v.__globals__.update({"os": os, **ram})
check("a memory ceiling is always produced", isinstance(ram["_total_ram_mb"](), int))
check("REGRESSION: the cgroup limit is consulted, not just /proc/meminfo",
      "cgroup" in BODY and "memory.max" in BODY)
check("the %maxcore reduction is recorded for the person, not only logged",
      "MAXCORE_NOTE" in BODY and "final_note = (MAXCORE_NOTE" in BODY)


# ===========================================================================
section("9. Review findings: credentials must not ride out in results")
# ===========================================================================
src = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "kaggle_runner.py"), encoding="utf-8").read()
check("REGRESSION: the fallback archive excludes the rendered notebook",
      "__results__.html" in src and "SECRET_BEARING" in src)
check("...and the executed source", "__script__.ipynb" in src)
check("...and says why they were withheld", "WITHHELD.txt" in src)


print("\n" + "=" * 70)
print("RESULT: %d passed, %d failed" % (_passed, _failed))
print("=" * 70)
sys.exit(1 if _failed else 0)
