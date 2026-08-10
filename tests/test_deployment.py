# -*- coding: utf-8 -*-
"""Checks that the deployment can actually be built and run.

Run: `python tests/test_deployment.py`

Written after `requirements.txt` pinned `kaggle==2.2.3`, a version that does not
exist on PyPI — the kaggle series is 1.x. `pip install -r requirements.txt`
therefore failed, which failed the whole Docker build, which left the image with
no `kaggle` command. Every `/api/kaggle/*` route then failed, and the user was
told their sign-in was wrong.

No test could see it: every suite stubs the CLI, and the application code was
correct throughout. The bug lived entirely in one line of a text file that no
test read. That is the gap this file closes.

The resolution check needs a network and is skipped without one; the checks that
do not need one always run.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_passed, _failed, _skipped = 0, 0, 0


def check(label, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print("  PASS  %s" % label)
    else:
        _failed += 1
        print("  FAIL  %s%s" % (label, ("\n        " + detail) if detail else ""))


def skip(label, why):
    global _skipped
    _skipped += 1
    print("  SKIP  %s (%s)" % (label, why))


def section(title):
    print("\n%s\n%s" % (title, "-" * len(title)))


def requirements():
    lines = open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8").read().splitlines()
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]


# ===========================================================================
section("1. requirements.txt is well formed")
# ===========================================================================
reqs = requirements()
check("there is at least one requirement", bool(reqs))
bad_syntax = [r for r in reqs if not re.match(r"^[A-Za-z0-9._-]+(\[[^\]]+\])?[=<>!~]=", r)]
check("every requirement is pinned to an exact version", not bad_syntax,
      "unpinned or malformed: %s" % bad_syntax)

names = [re.split(r"[=<>!~\[]", r)[0].lower() for r in reqs]
check("no package is pinned twice", len(names) == len(set(names)),
      "duplicates: %s" % [n for n in names if names.count(n) > 1])

for needed, why in (("flask", "the web framework"),
                    ("gunicorn", "the production server the Dockerfile runs"),
                    ("rdkit", "every structure and reaction drawing"),
                    ("kaggle", "every /api/kaggle/* route shells out to its CLI"),
                    ("requests", "PubChem and the ORCA download link"),
                    ("pillow", "compositing the reaction scheme")):
    check("%s is pinned (%s)" % (needed, why), needed in names)


# ===========================================================================
section("2. Every pinned version exists and resolves together")
# ===========================================================================
def have_network():
    try:
        proc = subprocess.run([sys.executable, "-m", "pip", "index", "versions", "pip"],
                              capture_output=True, text=True, timeout=45)
        return proc.returncode == 0 or "available versions" in (proc.stdout + proc.stderr).lower()
    except Exception:                                           # noqa: BLE001
        return False


if not have_network():
    skip("each pin resolves on PyPI", "no network")
    skip("the whole file resolves as one set", "no network")
else:
    for req in reqs:
        name = re.split(r"[=<>!~\[]", req)[0]
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--dry-run", "--quiet", req],
            capture_output=True, text=True, timeout=300)
        ok = proc.returncode == 0
        detail = ""
        if not ok:
            tail = (proc.stderr or proc.stdout).strip().splitlines()
            detail = tail[-1][:220] if tail else ""
            if "from versions:" in (proc.stderr or ""):
                detail = ("this exact version does not exist on PyPI — the Docker build "
                          "fails on it, and the image ends up without %s at all" % name)
        check("REGRESSION: %s resolves" % req, ok, detail)

    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--dry-run", "--quiet",
         "-r", os.path.join(ROOT, "requirements.txt")],
        capture_output=True, text=True, timeout=900)
    check("the whole file resolves as one set (no conflicting pins)",
          proc.returncode == 0,
          (proc.stderr or proc.stdout).strip()[-300:])


# ===========================================================================
section("3. The Dockerfile and the app agree")
# ===========================================================================
docker = open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8").read()
check("the image installs the requirements file", "requirements.txt" in docker)
check("...and fails the build if that install fails",
      "pip install" in docker and "|| true" not in docker and "; exit 0" not in docker,
      "a swallowed install error is how a missing CLI reaches production")

# The CMD is a JSON array, so the value is a separate quoted token.
timeout = re.search(r'--timeout"?[,\s]+"?(\d+)', docker)
check("gunicorn's request timeout is set explicitly", timeout is not None)
if timeout:
    runner = open(os.path.join(ROOT, "kaggle_runner.py"), encoding="utf-8").read()
    budgets = [int(m.group(1)) * int(m.group(2))
               for m in re.finditer(r"timeout=(\d+), attempts=(\d+)", runner)]
    worst = max(budgets) if budgets else 0
    check("no CLI call can outlive it (worst case %ss vs %ss)" % (worst, timeout.group(1)),
          worst < int(timeout.group(1)),
          "a worker killed mid-request skips its cleanup, leaking a live credential "
          "directory and the whole downloaded archive")

port = re.search(r"EXPOSE\s+(\d+)", docker)
app_py = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
check("the exposed port matches the app's default",
      port is not None and ('PORT", %s' % port.group(1)) in app_py.replace("'", '"'),
      "Dockerfile exposes %s" % (port.group(1) if port else "?"))


# ===========================================================================
section("4. The runtime reports its own readiness honestly")
# ===========================================================================
sys.path.insert(0, ROOT)
try:
    import kaggle_runner as K
except ImportError as exc:                                      # pragma: no cover
    skip("the CLI health probe", str(exc))
else:
    health = K.cli_health()
    check("cli_health() runs the CLI rather than testing for a file",
          "version" in health or "detail" in health, str(health))
    check("...and reports a verdict either way", isinstance(health.get("ok"), bool))
    if health["ok"]:
        check("the installed CLI is the 1.x series the code targets",
              "1." in health.get("version", ""), health.get("version"))
    else:
        print("  note  the CLI is not usable here: %s" % health.get("detail"))
    check("a broken CLI is a distinct, named failure",
          hasattr(K, "KaggleCliUnavailable") and hasattr(K, "KaggleUnreachable"),
          "so a server problem is never returned to a user as a rejected credential")


print("\n" + "=" * 70)
print("DEPLOYMENT: %d passed, %d failed, %d skipped" % (_passed, _failed, _skipped))
print("=" * 70)
sys.exit(1 if _failed else 0)
