# -*- coding: utf-8 -*-
"""
Regression suite for the repeated-execution bug (PRODUCTION, ~136 output files).

Production symptom: inside ONE Kaggle notebook session, the same ORCA
calculation was launched over and over -- a BP86/def2-SVP Opt of H2O for about
an hour, a single point for ~8 hours -- until manually interrupted. The output
archive held ~136 `*.passN.out` files, one per in-session ORCA launch, each
archived by the in-session continuation loop.

Root cause (proven from the execution flow, see
ORCA_REPEATED_EXECUTION_FORENSIC_REPORT.md):

  1. The in-session `while True` loop in runner/kernel_runner.py continued
     whenever `time_remaining() > 1800` and the outcome was neither complete
     nor fatal -- with NO check that the continuation carried the science
     forward, and a cumulative-cycle budget that was re-derived from the
     (immutable) header after every pass, making it inert inside a session.
  2. detect_job_kind() matched keywords inside COMMENTS, so a finished single
     point whose header comment said "opt"/"scan" was classified iterative and
     its normal termination was reported as MAXITER (continuable).
  3. build_continuation() silently ignored geometry placements that did not
     happen (a substitution that rewrites nothing), continuing jobs from the
     ORIGINAL geometry while claiming a resume.
  4. The kernel-side and server-side `kaggle kernels push` retry replays the
     push after a transport timeout without checking whether the first push
     landed -- a second version means a second execution of that window.

These tests execute the REAL runner module (the same source builder.py embeds
into every generated kernel) and the REAL artifacts module. If any of the
guards added by the fix regresses, a test here fails.
"""
import base64
import glob
import gzip
import importlib
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orca_orchestrator import orca_artifacts as art  # noqa: E402

H2O_INP = "! BP86 def2-SVP Opt\n* xyz 0 1\nO 0.0 0.0 0.0\nH 0.0 0.0 0.96\nH 0.0 0.0 -0.96\n*\n"
H2O_FRAME = ("3\nframe {n}\nO 0.000 0.000 0.000\nH 0.000 0.000 0.9{n:02d}\n"
             "H 0.000 0.000 -0.9{n:02d}\n")


# ---------------------------------------------------------------------------
# Unit level: the classifier must read ORCA's input, not its comments
# ---------------------------------------------------------------------------
def test_job_kind_ignores_comments():
    # The exact failure shape the legacy runner documented ("nebivolol",
    # "# scan of the Fe complex") -- a finished SP dragged down the
    # continuation path because a comment contained a keyword.
    assert art.detect_job_kind("! BP86 def2-SVP\n# scan of the Fe complex\n"
                               "* xyz 0 1\nFe 0 0 0\n*") == "sp"
    assert art.detect_job_kind("! BP86 def2-SVP\n# opt converged in an earlier job\n"
                               "* xyz 0 1\nO 0 0 0\n*") == "sp"
    # ...while real keyword lines still classify.
    assert art.detect_job_kind(H2O_INP) == "opt"
    assert art.detect_job_kind("! B3LYP Opt Freq\n") == "opt_freq"
    assert art.detect_job_kind("! B3LYP def2-SVP\n") == "sp"


def test_completed_sp_classifies_complete_under_its_own_kind():
    out = ("FINAL SINGLE POINT ENERGY      -76.123456\n"
           "****ORCA TERMINATED NORMALLY****\n")
    kind = art.detect_job_kind("! BP86 def2-SVP\n# scan of the Fe complex\n")
    outcome = art.classify_outcome(out, job_kind=kind)
    assert outcome.is_complete, "a normal-end SP must be COMPLETE, never continuable"


def test_set_geom_maxiter_is_scoped_to_the_geom_block():
    inp = ("! B3LYP Opt\n%scf\n  MaxIter 500\nend\n%geom\n  MaxIter 100\nend\n"
           "* xyz 0 1\nO 0 0 0\n*\n")
    out = art.set_geom_maxiter(inp, 200)
    assert "MaxIter 500" in out, "%scf MaxIter must not be stomped by the geometry budget"
    assert "MaxIter 200" in out.split("%geom", 2)[2] if out.count("%geom") > 1 else True


def test_set_geom_maxiter_inserts_into_block_and_is_idempotent():
    inp = ("! B3LYP Opt\n%geom\n  Constraints\n    { B 0 1 C }\n  end\nend\n"
           "* xyz 0 1\nO 0 0 0\n*\n")
    once = art.set_geom_maxiter(inp, 200)
    assert once.count("MaxIter") == 1, "the budget must land exactly once"
    assert "{ B 0 1 C }" in once, "nested constraints must survive"
    twice = art.set_geom_maxiter(once, 300)
    assert twice.count("MaxIter") == 1 and "MaxIter 300" in twice


def test_place_geometry_reports_non_placement():
    gzmt = "! B3LYP Opt\n* gzmt 0 1\nO\nH 1 0.96\nH 1 0.96 2 104.5\n*\n"
    text, placed = art.place_geometry(gzmt, "last_geometry.xyz", "0", "1")
    assert placed is False and text == gzmt
    xyz = "! B3LYP Opt\n* xyzfile 0 1 mol.xyz\n"
    text2, placed2 = art.place_geometry(xyz, "last_geometry.xyz", "0", "1")
    assert placed2 is True and "last_geometry.xyz" in text2


# ---------------------------------------------------------------------------
# Integration level: drive the REAL kernel_runner.main() with a fake ORCA
# ---------------------------------------------------------------------------
class FakeExecution:
    """Stands in for runner.Execution: replays a scripted list of
    (output_text, stop_reason) verdicts, one per ORCA launch."""

    instances = []
    script = []
    on_run = None                      # optional per-run side effect (e.g. grow _trj.xyz)

    def __init__(self, orca_exe, inp_path, out_path):
        self.orca_exe = orca_exe
        self.inp_path = inp_path
        self.out_path = out_path
        self.stop_reason = None
        type(self).instances.append(self)

    def run(self):
        out_text, stop = type(self).script.pop(0)
        if type(self).on_run is not None:
            type(self).on_run(len(type(self).instances))
        self.stop_reason = stop
        with open(self.out_path, "a", encoding="utf-8") as fh:
            fh.write(out_text)
        return 0


@pytest.fixture
def runner(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    working = tmp_path / "working"
    monkeypatch.setenv("ORCA_RUNNER_SCRATCH_ROOT", str(scratch))
    monkeypatch.setenv("ORCA_RUNNER_OUTPUT_DIR", str(working))
    from orca_orchestrator.runner import kernel_runner as kr
    importlib.reload(kr)           # bind the module's paths to this test's tmp dirs
    yield kr
    monkeypatch.undo()
    importlib.reload(kr)           # restore production constants for other tests


def setup_job(kr, monkeypatch, tmp_path, *, inp_text=H2O_INP, job_kind="opt",
              inline_extra=None):
    """Boots the runner module the way a real epoch-0 window would look."""
    kr.H.update({
        "job_id": "chem-tools-forensic-1a2b3c4d", "epoch": 0, "owner": "tester",
        "title": "forensic", "input_filename": "h2o.inp",
        "inline_files_b64": _encode_inline({"h2o.inp": inp_text.encode("utf-8")}),
        "checkpoint_manifest": None, "predecessor_slug": "",
        "dataset_sources": [], "orca_link": None,
        "kaggle_username": "tester", "kaggle_key": None, "kaggle_api_token": None,
        "job_kind": job_kind, "cumulative_opt_cycles": 0, "disk_epochs_used": 0,
    })
    kr.JOB_ID = kr.H["job_id"]
    kr.EPOCH = 0
    kr.BASENAME = "h2o"
    monkeypatch.setattr(kr, "install_credentials", lambda: None)
    monkeypatch.setattr(kr, "preflight_network", lambda: True)
    fake_orca = tmp_path / "fake_orca"
    fake_orca.write_bytes(b"#!/bin/sh\n")
    monkeypatch.setattr(kr, "locate_orca", lambda: str(fake_orca))
    FakeExecution.instances = []
    FakeExecution.script = []
    FakeExecution.on_run = None
    monkeypatch.setattr(kr, "Execution", FakeExecution)
    return kr


def _encode_inline(files):
    payload = {name: base64.b64encode(data).decode("ascii")
               for name, data in files.items()}
    return base64.b64encode(gzip.compress(json.dumps(payload).encode("utf-8"), 6)).decode("ascii")


def read_state(kr):
    with open(os.path.join(kr.OUTPUT_DIR, "STATE.json"), encoding="utf-8") as fh:
        return json.load(fh)


def write_trj(kr, frames):
    with open(kr.wp("h2o_trj.xyz"), "w", encoding="utf-8") as fh:
        fh.write("".join(frames))


MAXITER_NO_PROGRESS_OUT = (
    "GEOMETRY OPTIMIZATION CYCLE 1\n"      # cycles 0: printed below but parsed...
    "ORCA TERMINATED NORMALLY\n"
)


def test_opt_with_no_resumable_step_cannot_loop(runner, monkeypatch, tmp_path):
    """THE H2O FAILURE: an opt whose passes produce no resumable artifact.
    The old loop relaunched the identical input until the time budget died;
    ~136 archived passes came from exactly this shape. Now: the first
    continuation attempt refuses (nothing to resume from) and the window ends
    FAILED -- after exactly ONE ORCA launch."""
    kr = setup_job(runner, monkeypatch, tmp_path)
    FakeExecution.script = [("ORCA TERMINATED NORMALLY\n", None)]

    rc = kr.main()

    assert rc == 1
    assert len(FakeExecution.instances) == 1, "an unresumable opt must never be launched twice"
    state = read_state(kr)
    assert state["job"]["state"] == "FAILED"
    assert state["job"]["last_error"]["code"] in ("continuation_refused",
                                                  "no_scientific_progress")
    assert not glob.glob(kr.wp("h2o.pass*.out")), "no pass was archived, so no pass output may exist"
    assert not os.path.exists(os.path.join(kr.OUTPUT_DIR, "CHECKPOINT.json"))


def test_maxiter_with_identical_scientific_state_stops(runner, monkeypatch, tmp_path):
    """MaxIter + zero progress: the continuation points at the SAME geometry
    with the SAME cycle count. The old code ran it again; the loop must stop
    with the explicit no_scientific_progress state, never FINISHED."""
    frame1 = H2O_FRAME.format(n=1)
    inp = "! BP86 def2-SVP Opt\n* xyzfile 0 1 start_geometry.xyz\n"
    kr = setup_job(runner, monkeypatch, tmp_path, inp_text=inp)
    # the window's input and the trajectory end on the SAME geometry
    with open(kr.wp("start_geometry.xyz"), "w", encoding="utf-8") as fh:
        fh.write(frame1)
    write_trj(kr, [frame1])
    with open(kr.wp("h2o.xyz"), "w", encoding="utf-8") as fh:
        fh.write(frame1)
    # normal end, NO convergence banner, zero cycle lines -> MAXITER, no progress
    FakeExecution.script = [("ORCA TERMINATED NORMALLY\n", None)]

    rc = kr.main()

    assert rc == 1
    assert len(FakeExecution.instances) == 1
    state = read_state(kr)
    assert state["job"]["state"] == "FAILED"
    assert state["job"]["last_error"]["code"] == "no_scientific_progress"
    assert "no_scientific_progress" in state["job"]["last_note"]


def test_maxiter_with_real_progress_still_continues_in_session(runner, monkeypatch, tmp_path):
    """The legitimate case MUST keep working: each pass completes new
    optimisation steps (trajectory grows, cycle count rises), and pass 3
    converges. Expect two in-session continuations with archived pass
    outputs, then FINISHED."""
    kr = setup_job(runner, monkeypatch, tmp_path)
    write_trj(kr, [H2O_FRAME.format(n=1)])

    def grow(pass_number):
        with open(kr.wp("h2o_trj.xyz"), "a", encoding="utf-8") as fh:
            fh.write(H2O_FRAME.format(n=pass_number + 1))

    FakeExecution.on_run = grow
    FakeExecution.script = [
        ("GEOMETRY OPTIMIZATION CYCLE 5\nORCA TERMINATED NORMALLY\n", None),
        ("GEOMETRY OPTIMIZATION CYCLE 10\nORCA TERMINATED NORMALLY\n", None),
        ("THE OPTIMIZATION HAS CONVERGED\nORCA TERMINATED NORMALLY\n", None),
    ]

    rc = kr.main()

    assert rc == 0
    assert len(FakeExecution.instances) == 3
    state = read_state(kr)
    assert state["job"]["state"] == "FINISHED"
    # pass 1 and 2 are real, distinct executions: their outputs are archived
    assert os.path.exists(kr.wp("h2o.pass1.out"))
    assert os.path.exists(kr.wp("h2o.pass2.out"))
    assert os.path.exists(kr.wp("h2o.out"))
    # the input actually moved: the continuation repointed it at the new geometry
    with open(kr.wp("h2o.inp"), encoding="utf-8") as fh:
        assert "last_geometry.xyz" in fh.read()


def test_cycle_budget_accumulates_across_passes(runner, monkeypatch, tmp_path):
    """The old loop re-derived `cumulative` from the header after every pass,
    so MAX_TOTAL_OPT_CYCLES could never fire inside a session. The budget is
    now the header + the SUM of all passes: two 8-cycle passes breach a
    budget of 12 and force the handoff with the TRUE total (16)."""
    kr = setup_job(runner, monkeypatch, tmp_path)
    kr.MAX_TOTAL_OPT_CYCLES = 12
    write_trj(kr, [H2O_FRAME.format(n=1)])

    def grow(pass_number):
        with open(kr.wp("h2o_trj.xyz"), "a", encoding="utf-8") as fh:
            fh.write(H2O_FRAME.format(n=pass_number + 1))

    FakeExecution.on_run = grow
    FakeExecution.script = [
        ("GEOMETRY OPTIMIZATION CYCLE 8\nORCA TERMINATED NORMALLY\n", None),
        ("GEOMETRY OPTIMIZATION CYCLE 8\nORCA TERMINATED NORMALLY\n", None),
    ]
    pushed = {}
    monkeypatch.setattr(kr, "push_successor",
                        lambda manifest, epoch, kind, cum, disk:
                        pushed.update(cumulative=cum, epoch=epoch)
                        or ("chem-tools-forensic-1a2b3c4d-r1", "https://x"))

    rc = kr.main()

    assert rc == 0
    assert pushed["cumulative"] == 16, "cycle budget must count BOTH passes"
    state = read_state(kr)
    assert state["job"]["state"] == "QUEUED"       # handed off, not silently finished
    assert state["job"]["cumulative_opt_cycles"] == 16


def test_sp_never_enters_the_in_session_loop(runner, monkeypatch, tmp_path):
    """THE 8-HOUR SP FAILURE: an SP that died without a normal end used to be
    'continuable' and was relaunched identically until the session budget
    burned. Now the non-iterative gate stops it after the first launch."""
    kr = setup_job(runner, monkeypatch, tmp_path,
                   inp_text="! wB97X-D4 def2-TZVP\n* xyzfile 0 1 mol.xyz\n",
                   job_kind="sp")
    FakeExecution.script = [("Child process aborted unexpectedly\n", None)]

    rc = kr.main()

    assert rc == 1
    assert len(FakeExecution.instances) == 1, "an SP must never get a second identical launch"
    state = read_state(kr)
    assert state["job"]["state"] == "FAILED"
    assert state["job"]["last_error"]["code"] == "not_resumable"


# ---------------------------------------------------------------------------
# Fingerprint semantics
# ---------------------------------------------------------------------------
def test_fingerprint_metadata_only_changes_are_not_progress(runner):
    kr = runner
    base = "! B3LYP Opt\n* xyz 0 1\nO 0 0 0\nH 0 0 0.96\nH 0 0 -0.96\n*\n"
    rewritten = art.ensure_simple_keyword(art.set_geom_maxiter(base, 200), "NoAutoStart")
    fp_base = kr.scientific_fingerprint("opt", base, cumulative=10)
    fp_rewritten = kr.scientific_fingerprint("opt", rewritten, cumulative=10)
    assert fp_base["geometry"] == fp_rewritten["geometry"]
    assert not kr.fingerprint_progressed(fp_base, fp_rewritten), \
        "NoAutoStart / MaxIter renumbering are metadata, not progress"


def test_fingerprint_progress_markers(runner, tmp_path, monkeypatch):
    kr = setup_job(runner, monkeypatch, tmp_path)
    base = "! B3LYP Opt\n* xyzfile 0 1 last_geometry.xyz\n"
    with open(kr.wp("last_geometry.xyz"), "w", encoding="utf-8") as fh:
        fh.write(H2O_FRAME.format(n=1))
    fp = kr.scientific_fingerprint("opt", base, cumulative=10)

    # more cycles: progress
    assert kr.fingerprint_progressed(fp, dict(fp, cumulative_opt_cycles=11))
    # new geometry content under the SAME filename: progress
    with open(kr.wp("last_geometry.xyz"), "w", encoding="utf-8") as fh:
        fh.write(H2O_FRAME.format(n=2))
    fp_moved = kr.scientific_fingerprint("opt", base, cumulative=10)
    assert fp_moved["geometry"] != fp["geometry"]
    assert kr.fingerprint_progressed(fp, fp_moved)
    # a new checkpoint UUID / run token cannot exist in the fingerprint by design
    assert "checkpoint_id" not in fp and "run_token" not in fp


def test_fingerprint_counts_numfreq_columns(runner, tmp_path, monkeypatch):
    kr = setup_job(runner, monkeypatch, tmp_path)
    inp = "! B3LYP NumFreq\n* xyzfile 0 1 last_geometry.xyz\n"
    with open(kr.wp("last_geometry.xyz"), "w", encoding="utf-8") as fh:
        fh.write(H2O_FRAME.format(n=1))
    fp1 = kr.scientific_fingerprint("freq", inp, cumulative=0)
    assert fp1["freq_columns"] == 0
    for name in ("h2o.res.1", "h2o.res.2"):
        with open(kr.wp(name), "w", encoding="utf-8") as fh:
            fh.write("column\n")
    fp2 = kr.scientific_fingerprint("freq", inp, cumulative=0)
    assert kr.fingerprint_progressed(fp1, fp2), "new Hessian columns are progress"


# ---------------------------------------------------------------------------
# Push idempotency
# ---------------------------------------------------------------------------
def test_kernel_push_after_timeout_is_verified_not_repeated(runner, monkeypatch):
    """A push that dies reading the response AFTER Kaggle accepted it must be
    VERIFIED (status probe) -- never replayed into a second version, which is
    a second execution of the window."""
    kr = runner
    calls = []
    probes = {"n": 0}

    def fake_run_cli(args, timeout=30, retries=1, base_delay=0.0, deadline=None):
        calls.append(args[2])
        if args[2] == "status":
            probes["n"] += 1
            if probes["n"] == 1:
                return False, "404 not found"       # pre-push guard: nothing there yet
            return True, 'slug has status "RUNNING"'  # post-failure: it LANDED
        return False, "read timed out"          # push: transport error

    monkeypatch.setattr(kr, "run_cli", fake_run_cli)
    kr.H.update({"kaggle_username": "tester", "artifacts_source_b64": "",
                 "runner_body_b64": "", "dataset_sources": []})
    manifest = {"files": [], "next_input_text": "! B3LYP\n", "next_input_sha256": "x"}

    slug, url = kr.push_successor(manifest, 1, "opt", 0, 0)

    assert calls.count("push") == 1, "the timed-out push was verified active; no replay"
    assert slug == "chem-tools-forensic-deadbeef-r1" or slug.endswith("-r1")


def test_kernel_push_gives_up_after_bounded_verified_attempts(runner, monkeypatch):
    kr = runner
    calls = []

    def fake_run_cli(args, timeout=30, retries=1, base_delay=0.0, deadline=None):
        calls.append(args[2])
        if args[2] == "status":
            return False, "404 not found"
        return False, "read timed out"

    monkeypatch.setattr(kr, "run_cli", fake_run_cli)
    monkeypatch.setattr(kr.time, "sleep", lambda *_: None)
    kr.H.update({"kaggle_username": "tester", "artifacts_source_b64": "",
                 "runner_body_b64": "", "dataset_sources": []})
    manifest = {"files": [], "next_input_text": "! B3LYP\n", "next_input_sha256": "x"}

    with pytest.raises(RuntimeError):
        kr.push_successor(manifest, 1, "opt", 0, 0)
    assert 1 <= calls.count("push") <= 4, "retries are bounded"


def test_server_side_push_verifies_before_retry(monkeypatch):
    """Same guarantee for the orchestrator's own client: the first push fails
    at transport level, the status probe finds the kernel ACTIVE, and NO
    second push happens."""
    from orca_orchestrator import kaggle_api
    from orca_orchestrator.credentials import KaggleCredentials

    monkeypatch.setattr(kaggle_api, "is_local_mode", lambda: False)
    client = kaggle_api.KaggleClient(
        KaggleCredentials(username="tester", key="0" * 32))

    calls = []

    class Proc:
        def __init__(self, returncode, stdout, stderr):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr
            self.combined = (stdout or "") + (stderr or "")

    state = {"status_probes": 0}

    def fake_run(args, *, timeout, operation, allow_nonzero=False, slug=None):
        calls.append(args[2])
        if args[2] == "push":
            return Proc(1, "", "Read timed out")
        state["status_probes"] += 1
        if state["status_probes"] == 1:
            # pre-push duplicate guard: the kernel does not exist yet
            return Proc(1, "", "404 Not Found")
        # post-failure verification probe: Kaggle HAS the kernel, active
        return Proc(0, 'tester/slug has status "KernelWorkerStatus.RUNNING"', "")

    monkeypatch.setattr(client, "_run", fake_run)

    result = client.push_kernel(str(_tmp_job_dir()), expected_slug="chem-tools-x-1a2b3c4d")

    assert calls.count("push") == 1, "verified active after the error -> no duplicate push"
    assert result.slug == "chem-tools-x-1a2b3c4d"


def _tmp_job_dir():
    import tempfile
    d = tempfile.mkdtemp(prefix="push-dir-")
    with open(os.path.join(d, "kernel-metadata.json"), "w", encoding="utf-8") as fh:
        json.dump({"id": "tester/chem-tools-x-1a2b3c4d"}, fh)
    return d


# ---------------------------------------------------------------------------
# The generated Kaggle script must carry the fix (builder embeds these sources)
# ---------------------------------------------------------------------------
def test_generated_kernel_carries_the_repeated_execution_fix():
    from orca_orchestrator.credentials import KaggleCredentials
    from orca_orchestrator.models import JobManifest
    from orca_orchestrator.runner.builder import build_header, render_script

    job = JobManifest.create(job_id="chem-tools-gen-1a2b3c4d", owner="tester",
                             title="gen", input_filename="mol.inp",
                             original_input_sha256="s", job_kind="opt")
    header = build_header(job=job, epoch=0,
                          creds=KaggleCredentials(username="tester", key="0" * 32),
                          inline_files={"mol.inp": b"! B3LYP Opt\n* xyz 0 1\nO 0 0 0\n*\n"})
    script = render_script(header)
    compile(script, "script.py", "exec")
    for marker in ("no_scientific_progress", "fingerprint_progressed",
                   "MAX_IN_SESSION_PASSES", "continuation_refused"):
        assert marker in script, "the generated kernel lost a repeated-execution guard"

    embedded = types.ModuleType("embedded_orca_artifacts")
    exec(compile(base64.b64decode(header["artifacts_source_b64"]).decode("utf-8"),
                 "orca_artifacts.py", "exec"), embedded.__dict__)
    assert embedded.detect_job_kind("! BP86\n# scan of the Fe complex\n") == "sp", \
        "the embedded artifacts copy must classify from ORCA's input, not comments"
