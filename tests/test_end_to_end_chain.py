# -*- coding: utf-8 -*-
"""End-to-end simulation of a multi-session ORCA job, for every calculation type.

Run: `python tests/test_end_to_end_chain.py`   (no pytest, no network, no ORCA)

Why this exists. `test_continuation.py` checks the decision logic in isolation.
It cannot catch the failure that actually matters here: a chain that runs, looks
healthy in every log line, and makes no progress — or worse, makes progress and
then loses it. Only running the whole runner, window after window, with state
that persists in the *molecule* rather than in the files, exposes that.

What is real. The entire `KAGGLE_RUNNER_BODY` executes, unmodified except for
two absolute paths that do not exist off Kaggle (`/kaggle/working` and the
scratch-root candidates) and the two pip warm-ups. Input preparation, job
classification, the ORCA invocation, the completion audit, the restart decision,
checkpoint selection, payload encoding, the successor's header and its script
all run for real. `tests/fake_orca.py` stands in for the binary and
`bin/kaggle` for the CLI; the harness then feeds the successor's script back in
as the next window, which is exactly what Kaggle would do.

The invariant every scenario is measured against: **a chain must either finish,
or advance, or stop with a reason.** Running twenty windows that each redo the
same work is the failure mode this file is built to detect, because it is
invisible in a log.
"""
import base64
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kaggle_runner as K                                       # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
_passed, _failed = 0, 0


def check(label, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print("  PASS  %s" % label)
    else:
        _failed += 1
        print("  FAIL  %s%s" % (label, ("\n        " + detail) if detail else ""))


def section(title):
    print("\n%s\n%s" % (title, "-" * len(title)))


# ---------------------------------------------------------------------------
# The Kaggle-shaped sandbox
# ---------------------------------------------------------------------------
def portable_body():
    """The shipped kernel body with only the un-runnable absolutes replaced."""
    body = K.KAGGLE_RUNNER_BODY
    subs = [
        ('OUTPUT_DIR = "/kaggle/working"',
         'OUTPUT_DIR = os.environ["FAKE_WORKING"]'),
        ('for cand in ("/kaggle/temp", "/kaggle/tmp", "/tmp", "/var/tmp"):',
         'for cand in (os.environ["FAKE_SCRATCH"],):'),
        ('orca_exe = _locate_orca("/kaggle/input", ORCA_SCRATCH)',
         'orca_exe = _locate_orca(os.environ["FAKE_INPUT"], ORCA_SCRATCH)'),
        ('cfg_dir = os.path.expanduser("~/.kaggle")',
         'cfg_dir = os.path.join(os.environ["FAKE_HOME"], ".kaggle")'),
    ]
    for old, new in subs:
        assert body.count(old) == 1, "portability anchor moved: %r" % old[:50]
        body = body.replace(old, new)
    # The `pip install --upgrade kaggle` warm-up needs a network and costs ~9 s
    # per window. It is a deployment convenience, not part of the logic these
    # tests exercise, so it is stubbed rather than exercised.
    marker = ('[sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "kaggle"],')
    assert body.count(marker) == 1, "the pip warm-up moved; check the harness"
    body = body.replace(marker, '["true"],')
    return body


BODY = portable_body()


class Sandbox(object):
    """One Kaggle account with one job chain running in it."""

    def __init__(self, scenario):
        self.root = tempfile.mkdtemp(prefix="chain-")
        self.scratch = os.path.join(self.root, "scratch")
        self.fake_input = os.path.join(self.root, "input")
        self.home = os.path.join(self.root, "home")
        self.bin = os.path.join(self.root, "bin")
        self.pushes = os.path.join(self.root, "pushes")
        for d in (self.scratch, self.fake_input, self.home, self.bin, self.pushes):
            os.makedirs(d, exist_ok=True)

        # A "licensed ORCA in a Kaggle Dataset".
        orca_dir = os.path.join(self.fake_input, "orca-6-1-0")
        os.makedirs(orca_dir, exist_ok=True)
        fake_orca_py = os.path.join(HERE, "fake_orca.py")
        if sys.platform == "win32":
            self.orca = os.path.join(orca_dir, "orca.bat")
            with open(self.orca, "w", encoding="utf-8") as fh:
                fh.write(f'@echo off\n"{sys.executable}" "{fake_orca_py}" %*\n')
            with open(os.path.join(orca_dir, "orca.cmd"), "w", encoding="utf-8") as fh:
                fh.write(f'@echo off\n"{sys.executable}" "{fake_orca_py}" %*\n')
        else:
            self.orca = os.path.join(orca_dir, "orca")
            with open(self.orca, "w", encoding="utf-8") as fh:
                fh.write('#!/bin/sh\nexec "%s" "%s" "$@"\n'
                         % (sys.executable, fake_orca_py))
            os.chmod(self.orca, 0o755)

        self.scenario_path = os.path.join(self.root, "scenario.json")
        with open(self.scenario_path, "w") as fh:
            json.dump(scenario, fh)
        self.state_path = os.path.join(self.root, "orca_state.json")

        # A `kaggle` CLI that records pushes instead of making them.
        kaggle_script_body = (
            "import json, os, shutil, sys\n"
            "argv = sys.argv[1:]\n"
            "store = os.environ['FAKE_PUSHES']\n"
            "if argv[:2] == ['kernels', 'push']:\n"
            "    src = argv[argv.index('-p') + 1]\n"
            "    meta = json.load(open(os.path.join(src, 'kernel-metadata.json')))\n"
            "    slug = meta['id'].split('/', 1)[1]\n"
            "    dst = os.path.join(store, slug)\n"
            "    shutil.rmtree(dst, ignore_errors=True)\n"
            "    shutil.copytree(src, dst)\n"
            "    print('Kernel version 1 successfully pushed.  Please check progress at '\n"
            "          'https://www.kaggle.com/code/tester/' + slug)\n"
            "    sys.exit(0)\n"
            "if argv[:2] == ['kernels', 'status']:\n"
            "    slug = argv[2].split('/', 1)[1]\n"
            "    if os.path.isdir(os.path.join(store, slug)):\n"
            "        print(argv[2] + ' has status \"KernelWorkerStatus.QUEUED\"')\n"
            "        sys.exit(0)\n"
            "    sys.stderr.write('404 - Not Found\\n')\n"
            "    sys.exit(1)\n"
            "sys.exit(0)\n"
        )
        if sys.platform == "win32":
            fake_kaggle_py = os.path.join(self.bin, "fake_kaggle.py")
            with open(fake_kaggle_py, "w", encoding="utf-8") as fh:
                fh.write(kaggle_script_body)
            cli = os.path.join(self.bin, "kaggle.bat")
            with open(cli, "w", encoding="utf-8") as fh:
                fh.write(f'@echo off\n"{sys.executable}" "{fake_kaggle_py}" %*\n')
        else:
            cli = os.path.join(self.bin, "kaggle")
            with open(cli, "w", encoding="utf-8") as fh:
                fh.write("#!/usr/bin/env python3\n" + kaggle_script_body)
            os.chmod(cli, os.stat(cli).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def env(self):
        e = dict(os.environ)
        e.update({
            "FAKE_WORKING": self.working, "FAKE_SCRATCH": self.scratch,
            "FAKE_INPUT": self.fake_input, "FAKE_HOME": self.home,
            "FAKE_PUSHES": self.pushes,
            "FAKE_ORCA_SCENARIO": self.scenario_path,
            "FAKE_ORCA_STATE": self.state_path,
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
            "HOME": self.home,
        })
        return e

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def run_chain(scenario, inp_text, *, max_windows=8, time_limit=60,
              input_name="molecule.inp", job_name="chem-tools-sim-0badc0de"):
    """Runs a job through as many session windows as it asks for.

    Returns a list of per-window dicts: the header it ran with, the input ORCA
    actually saw, the job note, and whether it handed off to a successor."""
    box = Sandbox(scenario)
    windows = []
    try:
        header = {
            "ENCODED_FILES_JSON": K.encode_files_payload(
                {input_name: base64.b64encode(inp_text.encode()).decode()}),
            "INPUT_FILE": input_name, "KAGGLE_USERNAME": "tester",
            "KAGGLE_KEY": "0" * 32, "KAGGLE_API_TOKEN": None,
            "JOB_BASE_ID": job_name, "JOB_TITLE": "sim",
            "DATASET_SOURCES": ["tester/orca-6-1-0"], "ORCA_LINK": None,
            "RESTART_COUNT": 0, "MAX_RESTARTS": max_windows,
            "DISK_RESTART_COUNT": 0, "MAX_DISK_RESTARTS": 6,
            "TIME_LIMIT": time_limit, "MIN_FREE_GB": 0.001,
            "RESULT_BUDGET_GB": 1.0, "GEOM_MAXITER": 0, "SCF_MAXITER": 0,
            "SCAN_TOTAL_POINTS": 0, "SCAN_POINTS_BEFORE": 0, "HISTORY_B64": "",
            "STATIC_BODY_B64": base64.b64encode(BODY.encode()).decode(),
        }
        for w in range(max_windows):
            box.working = os.path.join(box.root, "working-%d" % w)
            os.makedirs(box.working, exist_ok=True)
            shutil.rmtree(box.scratch, ignore_errors=True)
            os.makedirs(box.scratch, exist_ok=True)

            script = os.path.join(box.root, "script-%d.py" % w)
            with open(script, "w", encoding="utf-8") as fh:
                fh.write("".join("%s = %r\n" % (k, header[k]) for k in K.HEADER_VARS))
                fh.write(BODY)
            proc = subprocess.run([sys.executable, script], env=box.env(),
                                  capture_output=True, text=True, timeout=300)

            def read(name, where=None):
                p = os.path.join(where or box.working, name)
                return open(p, encoding="utf-8", errors="replace").read() if os.path.exists(p) else ""

            ran_inp = ""
            for cand in (os.path.join(box.scratch, "orca_job", input_name),):
                if os.path.exists(cand):
                    ran_inp = open(cand, errors="replace").read()
            rec = {
                "window": w, "rc": proc.returncode, "stdout": proc.stdout,
                "stderr": proc.stderr, "header": dict(header),
                "input_that_ran": ran_inp,
                "orca_out": read(os.path.basename(input_name).replace(".inp", ".out")),
                "note": read("JOB_NOTE.txt"), "history": read("HISTORY.txt"),
                "next_id": read("NEXT_JOB_ID.txt").strip(),
                "working": box.working,
            }
            windows.append(rec)
            if not rec["next_id"]:
                break
            nxt = os.path.join(box.pushes, rec["next_id"], "script.py")
            assert os.path.exists(nxt), "hand-off named %s but nothing was pushed" % rec["next_id"]
            src = open(nxt, encoding="utf-8").read()
            header = {}
            for name in K.HEADER_VARS:
                m = re.search(r"(?m)^%s = (.*)$" % re.escape(name), src)
                header[name] = eval(m.group(1)) if m else None      # noqa: S307 (our own repr)
        return windows, box
    except Exception:
        box.cleanup()
        raise


def orca_state(box):
    try:
        return json.load(open(box.state_path))
    except (OSError, ValueError):
        return {}


XYZ = "* xyz 0 1\nO 0.0 0.0 0.0\nH 0.0 0.0 0.96\nH 0.9 0.0 -0.2\n*\n"


# ===========================================================================
section("User 1 — geometry optimisation that outlives its cycle budget")
# ===========================================================================
# The reported production failure, end to end: ORCA gives up on %geom MaxIter,
# says so, and terminates NORMALLY. Twelve steps are needed; each window does
# five, so the job cannot finish in one.
# The user set their own MaxIter, which window 1 respects on purpose, so ORCA
# stops on its own cycle budget after 5 of the 12 steps this molecule needs.
wins, box = run_chain({"kind": "opt", "converge_after": 12},
                      "! B3LYP def2-SVP Opt\n%geom MaxIter 5 end\n" + XYZ)
st = orca_state(box)
check("the chain runs more than one window", len(wins) > 1,
      "windows=%d, first note=%r" % (len(wins), wins[0]["note"][:200]))
check("REGRESSION: window 1 does not report a false completion",
      "did not reach its target" in wins[0]["note"] or wins[0]["next_id"] != "")
check("every window after the first inherits a LARGER cycle budget",
      all(w["header"]["GEOM_MAXITER"] > 0 for w in wins[1:]),
      str([w["header"]["GEOM_MAXITER"] for w in wins]))
check("the optimisation really advances — the molecule keeps moving",
      st.get("steps", 0) >= 12, "steps=%s" % st.get("steps"))
check("...and converges rather than exhausting the restart cap",
      any("HURRAY" in w["orca_out"] for w in wins))
check("the last window reports no outstanding gap",
      wins[-1]["next_id"] == "")
check("the escalation is what made the difference — MaxIter grows each window",
      [w["header"]["GEOM_MAXITER"] for w in wins] == sorted(
          w["header"]["GEOM_MAXITER"] for w in wins),
      str([w["header"]["GEOM_MAXITER"] for w in wins]))
check("the chain history records every window",
      wins[-1]["history"].count("window ") >= len(wins),
      wins[-1]["history"][:300])
check("the input ORCA ran carries an explicit MaxIter",
      "MaxIter" in wins[0]["input_that_ran"])
check("...and NoAutoStart from window 2 on",
      "NoAutoStart" in wins[1]["input_that_ran"])
check("the successor resumes from a geometry file, not the original block",
      "xyzfile" in wins[1]["input_that_ran"], wins[1]["input_that_ran"][:200])
box.cleanup()


wins, box = run_chain({"kind": "opt", "converge_after": 3, "sleep_seconds": 22},
                      "! B3LYP def2-SVP Opt\n" + XYZ, max_windows=3, time_limit=8)
check("REGRESSION: the session-time watchdog still ends a window and continues it",
      len(wins) > 1 or "session-time" in wins[0]["note"],
      "windows=%d note=%r" % (len(wins), wins[0]["note"][:200]))
check("...and says the time limit was the reason",
      any("session-time" in w["note"] for w in wins),
      " | ".join(w["note"][:100] for w in wins))
box.cleanup()


# ===========================================================================
section("User 2 — Opt Freq: thermochemistry must not be computed early")
# ===========================================================================
wins, box = run_chain({"kind": "opt", "converge_after": 9},
                      "! B3LYP def2-SVP Opt Freq\n%geom MaxIter 4 end\n" + XYZ)
first = wins[0]
check("REGRESSION: an unconverged Opt Freq does not strip its own Opt keyword",
      re.search(r"(?im)^\s*!.*\bopt\b", wins[1]["input_that_ran"]) is not None,
      wins[1]["input_that_ran"][:200])
check("...so no window computes frequencies before convergence",
      all(("VIBRATIONAL FREQUENCIES" not in w["orca_out"]) or ("HURRAY" in w["orca_out"])
          for w in wins))
check("the job only finishes once frequencies exist",
      "VIBRATIONAL FREQUENCIES" in wins[-1]["orca_out"] and wins[-1]["next_id"] == "")
box.cleanup()


# ===========================================================================
section("User 3 — relaxed surface scan across three windows")
# ===========================================================================
scan_inp = ("! B3LYP def2-SVP Opt\n%geom Scan B 0 1 = 1.0, 2.0, 12 end end\n" + XYZ)
wins, box = run_chain({"kind": "scan", "scan_points": 12, "points_per_window": 5}, scan_inp)
st = orca_state(box)
check("the scan spans several windows", len(wins) > 1)
check("REGRESSION: every grid point is computed exactly once",
      st.get("scan_points") == 12, "points=%s" % st.get("scan_points"))
check("progress is reported against the ORIGINAL grid, not the shrunken one",
      all(("/12" in w["note"]) or (w["next_id"] == "") for w in wins),
      " | ".join(w["note"].replace("\n", " ")[-140:] for w in wins))
check("the successor's scan bookkeeping travels in the header",
      wins[1]["header"]["SCAN_TOTAL_POINTS"] == 12
      and wins[1]["header"]["SCAN_POINTS_BEFORE"] > 0,
      str((wins[1]["header"]["SCAN_TOTAL_POINTS"], wins[1]["header"]["SCAN_POINTS_BEFORE"])))
check("no stale step file is shipped under a name ORCA would renumber",
      "scan_resume.xyz" in wins[1]["input_that_ran"] or wins[1]["next_id"] == "",
      wins[1]["input_that_ran"][:200])
box.cleanup()


# ===========================================================================
section("User 4 — NEB-TS: the band is only half the job")
# ===========================================================================
wins, box = run_chain(
    {"kind": "neb_ts", "converge_after": 6, "steps_per_window": 6, "ts_converges": False},
    "! B3LYP def2-SVP NEB-TS Freq\n%neb NImages 8 end\n" + XYZ, max_windows=4)
converged_band = [w for w in wins if "NEB OPTIMIZATION HAS CONVERGED" in w["orca_out"]]
check("the band converges at some point", bool(converged_band))
check("REGRESSION: a converged band with an unconverged TS is NOT reported complete",
      all(w["next_id"] != "" or "did not" in w["note"] or "transition-state" in w["note"]
          for w in converged_band),
      " | ".join(w["note"][:120] for w in converged_band))
box.cleanup()


# ===========================================================================
section("User 5 — molecular dynamics must not resample its velocities")
# ===========================================================================
md_inp = ("! B3LYP def2-SVP MD\n%md\n  Timestep 0.5_fs\n  Initvel 350_K\n"
          "  Thermostat NHC 350_K Timecon 10.0_fs\n  Dump Position Stride 1 Filename \"traj.xyz\"\n"
          "  Run 2000\nend\n" + XYZ)
wins, box = run_chain({"kind": "md", "steps_per_window": 500, "sleep_seconds": 30},
                      md_inp, max_windows=2, time_limit=3)
st = orca_state(box)
restarted = [w for w in wins[1:] if "Restart" in w["input_that_ran"]]
check("MD continues into a second window when the session clock ends the first",
      len(wins) > 1, "note=%r" % wins[0]["note"][:200])
if restarted:
    body = re.search(r"(?is)%\s*md\b(.*?)\bend\b\s*$", restarted[0]["input_that_ran"],
                     re.MULTILINE)
    txt = restarted[0]["input_that_ran"]
    ri = txt.lower().find("restart ifexists")
    run = txt.lower().rfind("run ")
    check("REGRESSION: Restart is placed BEFORE Run, not at the top of %md",
          ri != -1 and run != -1 and ri < run, txt[:400])
    check("REGRESSION: Initvel is marked No_Overwrite so the momenta survive",
          "No_Overwrite" in txt, txt[:400])
    check("...and the fake dynamics confirms no velocity reset happened",
          st.get("velocity_resets", 0) == 0, str(st))
check("the trajectory-length caveat is disclosed to the user",
      any("Run step count is NOT reduced" in w["note"] for w in wins),
      " | ".join(w["note"][:120] for w in wins))
box.cleanup()


# ===========================================================================
section("User 6 — single point and TD-DFT single point")
# ===========================================================================
wins, box = run_chain({"kind": "sp"}, "! B3LYP def2-SVP SP\n" + XYZ, max_windows=3)
check("a single point finishes in one window", len(wins) == 1 and wins[0]["next_id"] == "")
check("...and is not reported as needing continuation", "did not reach" not in wins[0]["note"])
box.cleanup()

wins, box = run_chain({"kind": "sp"}, "! wB97X-D4 def2-TZVP SP\n%tddft NRoots 10 end\n" + XYZ,
                      max_windows=3)
check("a TD-DFT single point finishes in one window", len(wins) == 1)
box.cleanup()


# ===========================================================================
section("User 7 — inputs this runner must refuse rather than corrupt")
# ===========================================================================
# Each of these is given a cycle budget it cannot finish in, so the run really
# does reach the point of wanting a continuation — which is where the gate has
# to stop it.
CAP = "%geom MaxIter 3 end\n"
REFUSALS = [
    ("a $new_job multi-job input",
     "! B3LYP Opt\n" + CAP + XYZ + "\n$new_job\n! B3LYP SP\n* xyz 1 2\nO 0 0 0\n*\n",
     "$new_job"),
    ("a CASSCF calculation",
     "! CASSCF def2-SVP Opt\n%casscf nel 6 norb 6 end\n" + CAP + XYZ, "multireference"),
    ("a broken-symmetry calculation",
     "! B3LYP def2-SVP Opt BrokenSym 2,2\n" + CAP + XYZ, "broken-symmetry"),
    ("a coordinate block with a per-atom basis",
     '! B3LYP def2-SVP Opt\n' + CAP
     + '* xyz 0 1\nFe 0 0 0\n  NewGTO "def2-TZVP" end\nO 0 0 1.6\n*\n',
     "per-atom"),
    ("a two-dimensional scan",
     "! B3LYP Opt\n%geom\n  MaxIter 3\n  Scan B 0 1 = 1.0, 2.0, 6 end\n"
     "  Scan B 0 2 = 1.0, 1.5, 5 end\nend\n" + XYZ, "multi-dimensional"),
]
for label, inp, marker in REFUSALS:
    wins, box = run_chain({"kind": "opt", "converge_after": 99}, inp, max_windows=3)
    check("REGRESSION: %s is refused, not continued approximately" % label,
          wins[-1]["next_id"] == "" and marker in wins[-1]["note"],
          "note=%r" % wins[-1]["note"][:220])
    box.cleanup()


# ===========================================================================
section("User 8 — failure paths end the chain with a usable explanation")
# ===========================================================================
wins, box = run_chain({"kind": "opt", "fail": "input"}, "! B3LYP Bogus Opt\n" + XYZ,
                      max_windows=3)
check("a genuine input error is not retried",
      len(wins) == 1 and wins[0]["next_id"] == "")
check("...and the note quotes the ORCA output",
      "UNRECOGNIZED" in wins[0]["note"], wins[0]["note"][:200])
box.cleanup()

wins, box = run_chain({"kind": "opt", "fail": "scf"}, "! B3LYP def2-SVP Opt\n" + XYZ,
                      max_windows=4)
check("an SCF that ran out of iterations earns one bigger-budget retry",
      len(wins) > 1, "note=%r" % wins[0]["note"][:200])
if len(wins) > 1:
    check("...and the retry really ships a larger SCF budget with SlowConv",
          wins[1]["header"]["SCF_MAXITER"] >= 500
          and "SlowConv" in wins[1]["input_that_ran"])
    check("...and the change of convergence path is disclosed",
          "SlowConv" in wins[1]["note"] or "convergence PATH" in wins[1]["note"],
          wins[1]["note"][:250])
box.cleanup()


# ===========================================================================
section("User 9 — reproducibility of the archive")
# ===========================================================================
wins, box = run_chain({"kind": "opt", "converge_after": 8},
                      "! B3LYP def2-SVP Opt   # my test run\n%geom MaxIter 4 end\n" + XYZ)
# Window 1 runs the person's input untouched apart from the budget, which is
# deliberate. Window 2 is where NoAutoStart and the raised budget go in, and
# that is what has to be disclosed.
check("every rewrite of the input is disclosed in the job note",
      len(wins) > 1 and "Input changes applied" in wins[1]["note"],
      wins[1]["note"][:250] if len(wins) > 1 else "only one window")
check("REGRESSION: a comment on the keyword line does not swallow NoAutoStart",
      all(("NoAutoStart" not in w["input_that_ran"].split("#")[-1])
          for w in wins[1:] if "#" in w["input_that_ran"]),
      wins[1]["input_that_ran"][:200] if len(wins) > 1 else "")
check("the unmodified input is shipped beside the one that ran",
      os.path.exists(os.path.join(wins[-1]["working"], "results.zip")))
import zipfile                                                  # noqa: E402
with zipfile.ZipFile(os.path.join(wins[-1]["working"], "results.zip")) as zf:
    names = zf.namelist()
check("...inside results.zip", any(n.startswith("ORIGINAL_") for n in names), str(names))
check("the chain history is in the archive", "HISTORY.txt" in names, str(names))
box.cleanup()


print("\n" + "=" * 70)
print("END-TO-END: %d passed, %d failed" % (_passed, _failed))
print("=" * 70)
sys.exit(1 if _failed else 0)
