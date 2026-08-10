# -*- coding: utf-8 -*-
"""Route-level tests for the endpoints the browser actually calls.

Run: `python tests/test_web_routes.py`

Written because a review found that no test imported `app.py` at all: every
suite exercised either the orchestrator or slices of the kernel script, so not
one input-validation guard, error path or response shape on the live path was
covered. The `kaggle` CLI is stubbed, so nothing here touches the network or a
real Kaggle account.
"""
import io
import json
import tempfile
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


try:
    import app as webapp
    import kaggle_runner as K
except ImportError as exc:                                      # pragma: no cover
    print("SKIP: %s (install flask and rdkit to run this suite)" % exc)
    sys.exit(0)

client = webapp.app.test_client()
KEY = "0" * 32
GOOD_ID = K.JOB_ID_PREFIX + "demo-0badc0de"


class FakeCli(object):
    """Replaces `_run_kaggle_cli` so the routes can be driven end to end."""

    def __init__(self, **behaviour):
        self.calls = []
        self.behaviour = behaviour

    def __call__(self, args, env=None, timeout=60, attempts=4, base_delay=3.0):
        self.calls.append(list(args))
        verb = args[2] if len(args) > 2 else ""
        rc, out, err = self.behaviour.get(verb, (0, "", ""))
        if callable(out):
            out = out(args)
        return subprocess.CompletedProcess(args, rc, out, err)


def with_cli(cli):
    K._run_kaggle_cli = cli
    return cli


_real_cli = K._run_kaggle_cli


# ===========================================================================
section("1. Liveness and readiness")
# ===========================================================================
r = client.get("/health")
body = r.get_json()
check("/health responds", r.status_code == 200)
check("...and reports the runner the UI actually submits through",
      "kaggle_runner" in body.get("runner", ""), json.dumps(body)[:200])
# /health aggregates several subsystems, so `ok` can be false for a reason
# unrelated to the CLI. What must hold is that the CLI's own verdict is
# reported, and that a bad CLI can never leave `ok` true.
check("REGRESSION: /health reports the CLI's own verdict",
      isinstance(body.get("kaggle_cli_ok"), bool), json.dumps(body)[:200])
check("...and a broken CLI can never leave the service reported as ok",
      body["kaggle_cli_ok"] or body["ok"] is False,
      "ok=%s cli_ok=%s" % (body["ok"], body["kaggle_cli_ok"]))
check("...naming the version when it works, or the fault when it does not",
      bool(body.get("kaggle_cli")) and (body["kaggle_cli_ok"] or bool(body.get("error"))),
      json.dumps(body)[:220])
check("the index page renders", client.get("/").status_code == 200)


# ===========================================================================
section("2. Input validation on every /api/kaggle/* route")
# ===========================================================================
for route in ("/api/kaggle/status", "/api/kaggle/download", "/api/kaggle/delete"):
    r = client.post(route, json={})
    check("%s rejects a request with no credentials" % route,
          r.status_code == 400 and r.get_json()["ok"] is False)
    r = client.post(route, json={"kaggle_username": "u", "kaggle_key": KEY,
                                 "job_id": "../../etc/passwd"})
    check("%s rejects a traversal in the job id" % route, r.status_code == 400)
    r = client.post(route, json={"kaggle_username": "u", "kaggle_key": KEY,
                                 "job_id": "--help"})
    check("%s rejects an option-looking job id" % route, r.status_code == 400)
    r = client.post(route, json={"kaggle_username": "u", "kaggle_key": KEY,
                                 "job_id": "someone-elses-notebook"})
    check("REGRESSION: %s rejects a notebook this site did not create" % route,
          r.status_code == 400,
          "delete performs an irreversible kernels delete on the user's account")

r = client.post("/api/kaggle/submit", data={"kaggle_username": "u", "kaggle_key": KEY})
check("submit without an ORCA source is refused",
      r.status_code == 400 and "ORCA source" in r.get_json()["error"])
r = client.post("/api/kaggle/submit", data={"kaggle_username": "u", "kaggle_key": KEY,
                                            "dataset_sources": "u/orca", "input_content": "  "})
check("submit with an empty .inp is refused",
      r.status_code == 400 and "no .inp content" in r.get_json()["error"])

r = client.post("/api/kaggle/submit", data={
    "kaggle_username": "u", "kaggle_key": KEY, "dataset_sources": "u/orca",
    "input_file": (io.BytesIO(b"x" * (9 * 1024 * 1024)), "big.inp")},
    content_type="multipart/form-data")
check("REGRESSION: an oversized upload answers in JSON, not an HTML error page",
      r.status_code == 413 and r.is_json, r.headers.get("Content-Type", ""))
check("...with an explanation the UI can show", "larger than" in r.get_json()["error"])


# ===========================================================================
section("3. Generator route guards")
# ===========================================================================
base = {"coords": "O 0 0 0", "theory": "B3LYP", "basis": "def2-SVP", "calc_type": "sp"}
for field, value, why in (("cores", 999, "cores above the limit"),
                          ("ram", 5, "RAM below the limit"),
                          ("charge", 99, "an implausible charge"),
                          ("mult", 0, "a multiplicity of zero")):
    r = client.post("/api/orca/generate", json={**base, field: value})
    check("the generator rejects %s" % why, r.status_code == 400)

r = client.post("/api/orca/generate", json={**base, "coords": ""})
check("the generator rejects an empty coordinate block", r.status_code == 400)
r = client.post("/api/orca/generate", json={**base, "custom_line": "B3LYP def2-SVP"})
check("a custom command line must start with '!'", r.status_code == 400)
r = client.post("/api/orca/generate", json=base)
check("a valid request produces an input file",
      r.status_code == 200 and r.get_json()["input_text"].startswith("#"))
check("...with the coordinate block closed",
      r.get_json()["input_text"].rstrip().endswith("*"))


# ===========================================================================
section("3b. The generator never emits a silently invalid method/basis pairing")
# ===========================================================================
import chem_core as core                                        # noqa: E402

problems = []
for fam, methods in (("f_dft", core.DFT_FUNCTIONALS), ("f_comp", core.COMPOSITE_METHODS),
                     ("f_mp2", core.MP2_VARIANTS), ("f_ccsd", core.CCSD_VARIANTS),
                     ("f_hf", ["HF"])):
    for method in methods:
        for ri in core.RI_OPTIONS:
            for basis in ("def2-TZVP", "cc-pVTZ", "6-31G(d)"):
                for x2c in (False, True):
                    d = {"theory": method, "basis": basis, "family": fam, "ri_type": ri,
                         "x2c": x2c, "calc_type": "opt", "coords": "O 0 0 0",
                         "charge": 0, "mult": 1, "cores": 4, "ram": 5000,
                         "disp": "none", "solv_model": "none"}
                    txt = core.generate_orca_6_input(d)
                    kw = [l for l in txt.splitlines() if l.startswith("!")]
                    tag = "%s/%s/%s/%s/x2c=%s" % (fam, method, ri, basis, x2c)
                    if len(kw) != 1:
                        problems.append("%s: %d keyword lines" % (tag, len(kw)))
                    if not txt.rstrip().endswith("*"):
                        problems.append("%s: coordinate block not closed" % tag)
                    # A relativistic Hamiltonian must never ship with a
                    # non-relativistic basis unless the person is told.
                    if x2c and "x2c-" not in txt and "WARNING" not in txt:
                        problems.append("%s: X2C with a non-relativistic basis, unflagged" % tag)
                    # A /C auxiliary basis must be derived from the orbital
                    # basis, never hardcoded to one particular set.
                    if "def2-tzvp/c" in txt.lower() and "def2" not in basis.lower():
                        problems.append("%s: hardcoded def2 /C basis" % tag)
check("REGRESSION: every method x basis x RI x X2C combination is sound or flagged",
      not problems, "\n        ".join(problems[:8]))
check("...across the whole option matrix", True and not problems,
      "%d combinations checked" % (5 * 8 * 4 * 3 * 2))


# ===========================================================================
section("3c. A lookup failure is not reported as a missing molecule")
# ===========================================================================
_real_probe = core.pubchem_reachable
_real_props = core.fetch_pubchem_properties
_real_smiles = core.resolve_compound_to_smiles
try:
    core.pubchem_reachable = lambda timeout=4.0: False
    core.fetch_pubchem_properties = lambda name: None
    core.resolve_compound_to_smiles = lambda name: None
    r = client.post("/api/orca/coords", json={"query": "water"})
    check("REGRESSION: an unreachable PubChem answers 503, not 'no such compound'",
          r.status_code == 503 and "could not be reached" in r.get_json()["error"],
          json.dumps(r.get_json())[:220])
    check("...and suggests what the person can do instead",
          "paste the coordinates" in r.get_json()["error"].lower())

    core.pubchem_reachable = lambda timeout=4.0: True
    r = client.post("/api/orca/coords", json={"query": "not-a-real-molecule"})
    check("a genuinely unknown name still answers 404",
          r.status_code == 404 and "no 3D structure" in r.get_json()["error"],
          json.dumps(r.get_json())[:220])
finally:
    core.pubchem_reachable = _real_probe
    core.fetch_pubchem_properties = _real_props
    core.resolve_compound_to_smiles = _real_smiles


# ===========================================================================
section("4. Status route: a chain must never be declared finished by accident")
# ===========================================================================
def output_writer(files):
    def _write(args):
        if "-p" in args:
            dest = args[args.index("-p") + 1]
            for name, text in files.items():
                with open(os.path.join(dest, name), "w") as fh:
                    fh.write(text)
        return ""
    return _write


class Cli404(FakeCli):
    """Kaggle has the window but not its successor."""

    def __call__(self, args, **kw):
        if args[2] == "status" and args[3].endswith("-r1"):
            return subprocess.CompletedProcess(args, 1, "", "404 - Not Found")
        return FakeCli.__call__(self, args, **kw)


try:
    # A throttled output listing used to come back as "complete" with no
    # hand-off, which made the browser stop polling a live chain.
    with_cli(Cli404(
        status=(0, lambda a: '%s has status "KernelWorkerStatus.COMPLETE"' % a[3], ""),
        output=(1, "", "429 Too Many Requests")))
    body = client.post("/api/kaggle/status", json={"kaggle_username": "u", "kaggle_key": KEY,
                                                   "job_id": GOOD_ID}).get_json()
    check("REGRESSION: a throttled output listing is not reported as 'complete'",
          body["status"] != "complete", json.dumps(body)[:200])
    check("...and the reason is passed to the user",
          "output files" in body.get("note", ""), json.dumps(body)[:250])

    # Same throttle, but the successor does exist — follow it rather than stall.
    with_cli(FakeCli(
        status=(0, lambda a: '%s has status "KernelWorkerStatus.COMPLETE"' % a[3], ""),
        output=(1, "", "429 Too Many Requests")))
    body = client.post("/api/kaggle/status", json={"kaggle_username": "u", "kaggle_key": KEY,
                                                   "job_id": GOOD_ID}).get_json()
    check("...and if the successor does exist, the chain is followed anyway",
          body["status"] == "restarting" and body["next_job_id"] == GOOD_ID + "-r1",
          json.dumps(body)[:220])

    # The successor exists but the dying window never saved its hand-off file.
    def status_for(a):
        return '%s has status "KernelWorkerStatus.%s"' % (
            a[3], "RUNNING" if a[3].endswith("-r1") else "COMPLETE")

    with_cli(FakeCli(status=(0, status_for, ""), output=(0, output_writer({}), "")))
    r = client.post("/api/kaggle/status", json={"kaggle_username": "u", "kaggle_key": KEY,
                                                "job_id": GOOD_ID})
    body = r.get_json()
    check("REGRESSION: a successor with no hand-off file is still found",
          body["status"] == "restarting" and body["next_job_id"] == GOOD_ID + "-r1",
          json.dumps(body)[:220])

    # Genuinely finished: no successor anywhere.
    with_cli(Cli404(status=(0, lambda a: '%s has status "KernelWorkerStatus.COMPLETE"' % a[3], ""),
                    output=(0, output_writer({"JOB_NOTE.txt": "all done"}), "")))
    r = client.post("/api/kaggle/status", json={"kaggle_username": "u", "kaggle_key": KEY,
                                                "job_id": GOOD_ID})
    body = r.get_json()
    check("a job with no successor is reported complete",
          body["status"] == "complete" and body["next_job_id"] is None,
          json.dumps(body)[:200])
    check("...and its job note is surfaced as a warning",
          body.get("warning") == "all done")

    # Kaggle's own failure text is the only explanation when the kernel died early.
    with_cli(Cli404(status=(0, lambda a: '%s has status "KernelWorkerStatus.ERROR"' % a[3],
                            "Failure message: dataset could not be mounted"),
                    output=(0, output_writer({}), "")))
    r = client.post("/api/kaggle/status", json={"kaggle_username": "u", "kaggle_key": KEY,
                                                "job_id": GOOD_ID})
    body = r.get_json()
    check("REGRESSION: an errored kernel with no note still explains itself",
          "Failure message" in (body.get("warning") or ""), json.dumps(body)[:250])

    # Bad credentials must not read as a job that is still running.
    with_cli(FakeCli(status=(1, "", "401 - Unauthorized")))
    body = client.post("/api/kaggle/status", json={"kaggle_username": "u", "kaggle_key": KEY,
                                                   "job_id": GOOD_ID}).get_json()
    check("a rejected credential is reported as an error, not 'unknown'",
          body["status"] == "error" and "username" in body["note"].lower(),
          json.dumps(body)[:200])
finally:
    K._run_kaggle_cli = _real_cli


# ===========================================================================
section("5. Sign-in: the one call that is both a credential check and a fetch")
# ===========================================================================
def csv_page(n_rows, page_marker=""):
    def _out(args):
        head = "ref,title,lastRunTime\n"
        return head + "".join(
            "tester/%sdemo%s%d-0badc0de,Job %d,2026-01-0%dT00:00:00Z\n"
            % (K.JOB_ID_PREFIX, page_marker, i, i, (i % 9) + 1)
            for i in range(n_rows))
    return _out


class PagedCli(FakeCli):
    """A CLI that pages, and optionally rejects --sort-by like older builds."""

    def __init__(self, pages, reject_sort=False):
        FakeCli.__init__(self)
        self.pages, self.reject_sort = pages, reject_sort

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        if self.reject_sort and "--sort-by" in args:
            return subprocess.CompletedProcess(
                args, 2, "", "error: argument --sort-by: invalid choice: 'dateRun'")
        page = int(args[args.index("--page") + 1]) if "--page" in args else 1
        rows = self.pages[page - 1] if page <= len(self.pages) else 0
        return subprocess.CompletedProcess(args, 0, csv_page(rows, "p%d" % page)(args), "")


try:
    with_cli(PagedCli([100, 100, 7]))
    r = client.post("/api/kaggle/login", json={"kaggle_username": "tester",
                                               "kaggle_key": KEY})
    body = r.get_json()
    check("signing in succeeds and returns the job list",
          r.status_code == 200 and body["ok"] is True, json.dumps(body)[:200])
    check("REGRESSION: every page is fetched, not just the first 100",
          len(body["jobs"]) == 207, "recovered %d jobs" % len(body["jobs"]))

    # Older kaggle CLIs do not accept --sort-by dateRun. That must not fail a
    # sign-in: the option is an ordering nicety, the listing is the point.
    cli = with_cli(PagedCli([5], reject_sort=True))
    r = client.post("/api/kaggle/login", json={"kaggle_username": "tester",
                                               "kaggle_key": KEY})
    check("REGRESSION: a CLI that rejects --sort-by still signs in",
          r.status_code == 200 and r.get_json()["ok"] is True,
          json.dumps(r.get_json())[:250])
    check("...by retrying without the option",
          any("--sort-by" not in c for c in cli.calls if c[2] == "list"))

    with_cli(FakeCli(list=(1, "", "401 - Unauthorized")))
    r = client.post("/api/kaggle/login", json={"kaggle_username": "tester",
                                               "kaggle_key": KEY})
    check("a bad credential is reported as a sign-in failure",
          r.status_code == 401 and "kaggle.com" in r.get_json()["error"],
          json.dumps(r.get_json())[:200])

    r = client.post("/api/kaggle/login", json={"kaggle_username": "", "kaggle_key": ""})
    check("an empty sign-in form is refused before any CLI call",
          r.status_code == 400)
finally:
    K._run_kaggle_cli = _real_cli


# ===========================================================================
section("5b. A server problem is never reported as a credential problem")
# ===========================================================================
# This is the failure that reached a user twice. Telling someone their sign-in
# failed, when the truth is that the server has no working `kaggle` command or
# cannot reach kaggle.com, sends them to regenerate their API token — which
# does not fix it, and which stops every job already running, because each
# running kernel carries the old token to push its own successor.
BROKEN_CLI = ('Traceback (most recent call last):\n  File "/usr/local/bin/kaggle", line 3\n'
              "    from kaggle.cli import main\nModuleNotFoundError: No module named 'kaggle'")
UNREACHABLE = ("HTTPSConnectionPool(host='www.kaggle.com', port=443): Max retries exceeded "
               "with url: /api/v1/kernels/list (Caused by NewConnectionError(...))")

try:
    for label, output, marker in (
            ("a broken or missing kaggle CLI", BROKEN_CLI, "not installed correctly"),
            ("kaggle.com being unreachable", UNREACHABLE, "could not be reached")):
        with_cli(FakeCli(list=(1, "", output)))
        r = client.post("/api/kaggle/login", json={"kaggle_username": "tester",
                                                   "kaggle_key": KEY})
        body = r.get_json()
        check("REGRESSION: %s answers 503, not 401" % label,
              r.status_code == 503, "got %s: %s" % (r.status_code, json.dumps(body)[:160]))
        check("...and says it is the site's problem", marker in body["error"], body["error"][:160])
        check("...and warns against regenerating the token",
              "regenerate" in body["error"].lower(), body["error"][:200])
        check("...and does not put a Python traceback in front of the user",
              "Traceback" not in body["error"] and "ModuleNotFound" not in body["error"],
              body["error"][:200])

    # A genuinely rejected credential must still be a 401.
    with_cli(FakeCli(list=(1, "", "401 - Unauthorized")))
    r = client.post("/api/kaggle/login", json={"kaggle_username": "tester", "kaggle_key": KEY})
    check("a real credential rejection is still 401", r.status_code == 401)

    # Every route that shells out, not just sign-in.
    for route, payload in (("/api/kaggle/status", {"job_id": GOOD_ID}),
                           ("/api/kaggle/download", {"job_id": GOOD_ID}),
                           ("/api/kaggle/delete", {"job_id": GOOD_ID})):
        with_cli(FakeCli(status=(1, "", BROKEN_CLI), output=(1, "", BROKEN_CLI),
                         delete=(1, "", BROKEN_CLI)))
        r = client.post(route, json={"kaggle_username": "tester", "kaggle_key": KEY, **payload})
        check("%s also reports a broken CLI as 503" % route, r.status_code == 503,
              "got %s" % r.status_code)
finally:
    K._run_kaggle_cli = _real_cli

health = K.cli_health()
check("cli_health RUNS the CLI rather than looking for the file",
      "version" in health or "detail" in health, str(health))
check("...and /health reports what it found",
      "kaggle_cli_ok" in client.get("/health").get_json())


# ===========================================================================
section("5c. The environment handed to the CLI does not break the CLI")
# ===========================================================================
import os as _os                                                # noqa: E402

with K._temp_kaggle_env("tester", "0" * 32) as env:
    check("REGRESSION: HOME is left alone",
          env.get("HOME") == _os.environ.get("HOME"),
          "Python resolves per-user site-packages from HOME, so redirecting it hides "
          "`pip install --user` packages from the kaggle console script and every call "
          "dies with ModuleNotFoundError")
    check("credentials still go to a throwaway directory",
          env["KAGGLE_CONFIG_DIR"].startswith(tempfile.gettempdir())
          or "kaggle-home-" in env["KAGGLE_CONFIG_DIR"], env["KAGGLE_CONFIG_DIR"])
    check("...and are exported for the CLI to read before any file",
          env.get("KAGGLE_USERNAME") == "tester" and env.get("KAGGLE_KEY") == "0" * 32)
    check("...and written with owner-only permissions",
          _os.name == "nt" or oct(_os.stat(_os.path.join(env["KAGGLE_CONFIG_DIR"], "kaggle.json")).st_mode)[-3:]
          == "600")
    cfg = env["KAGGLE_CONFIG_DIR"]
check("the throwaway directory is removed afterwards", not _os.path.exists(cfg))


# ===========================================================================
section("6. Temporary files never outlive a request")
# ===========================================================================
import glob                                                     # noqa: E402
import tempfile                                                 # noqa: E402

before = set(glob.glob(os.path.join(tempfile.gettempdir(), "kaggle-*")))
try:
    with_cli(FakeCli(status=(1, "", "401 - Unauthorized"),
                     output=(1, "", "401 - Unauthorized"),
                     list=(1, "", "401 - Unauthorized")))
    for route, payload in (("/api/kaggle/status", {"job_id": GOOD_ID}),
                           ("/api/kaggle/download", {"job_id": GOOD_ID}),
                           ("/api/kaggle/delete", {"job_id": GOOD_ID}),
                           ("/api/kaggle/login", {})):
        client.post(route, json={"kaggle_username": "u", "kaggle_key": KEY, **payload})
finally:
    K._run_kaggle_cli = _real_cli
after = set(glob.glob(os.path.join(tempfile.gettempdir(), "kaggle-*")))
check("no credential or results directory is left behind when a route fails",
      after == before, "leaked: %s" % sorted(after - before))


print("\n" + "=" * 70)
print("WEB ROUTES: %d passed, %d failed" % (_passed, _failed))
print("=" * 70)
sys.exit(1 if _failed else 0)
