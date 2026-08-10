# -*- coding: utf-8 -*-
"""Load-time checks for the browser code.

Run: `python tests/test_frontend.py`

This exists because of a bug that reached a user. `app.js` runs as one IIFE;
`autoSignIn()` executes during page load and assigned a `let` binding that was
declared two hundred lines further down. `let` is in a temporal dead zone until
its declaration is evaluated, so that assignment threw
`ReferenceError: Cannot access 'sessionKaggleKey' before initialization`, which
aborted the rest of the script. Every handler registered after that point —
including the Kaggle sign-in form's — was never attached, so signing in silently
did nothing. Nothing in the test suite could see it: `node --check` only parses,
and no test evaluated the file.

Two layers here:

  1. Static, always runs: every element id and every localStorage key the script
     reaches for must exist in `index.html`.
  2. Dynamic, runs when node is available: the whole file is evaluated against a
     stub DOM, which surfaces load-time errors like the one above.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def served(flat, nested):
    """The copy the app would actually serve.

    The project carries both layouts: `index.html` beside `app.py`, and the
    documented `templates/` + `static/` tree. `app.py` prefers the nested one
    when it exists, so that is the copy these checks have to read — testing the
    flat copy while the site serves the nested one would pass on code nobody
    runs."""
    nested_path = os.path.join(ROOT, nested)
    return nested_path if os.path.exists(nested_path) else os.path.join(ROOT, flat)


APP_JS = served("app.js", os.path.join("static", "js", "app.js"))
INDEX = served("index.html", os.path.join("templates", "index.html"))

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


js = open(APP_JS, encoding="utf-8").read()
html = open(INDEX, encoding="utf-8").read()


# ===========================================================================
section("1. Every element the script reaches for exists in the page")
# ===========================================================================
html_ids = set(re.findall(r'\bid="([^"]+)"', html))
wanted = set(re.findall(r'getElementById\(\s*"([^"]+)"\s*\)', js))
missing = sorted(wanted - html_ids)
check("no getElementById targets an id that is not in index.html",
      not missing, "missing: %s" % missing)

selectors = set(re.findall(r'querySelector(?:All)?\(\s*\'([^\']+)\'', js))
named = set(re.findall(r'\bname="([^"]+)"', html))
bad_names = sorted(n for s in selectors for n in re.findall(r'\[name="([^"]+)"\]', s)
                   if n not in named)
check("no querySelector targets a name that is not in index.html",
      not bad_names, "missing: %s" % bad_names)


# ===========================================================================
section("1b. The two copies of each front-end file have not drifted")
# ===========================================================================
# Both layouts are present, and only one of each pair is served. A fix applied
# to the copy that is not served looks correct in the diff and changes nothing
# in the browser, which is among the most expensive kinds of bug to chase.
import hashlib                                                  # noqa: E402

for flat, nested in (("app.js", os.path.join("static", "js", "app.js")),
                     ("style.css", os.path.join("static", "css", "style.css")),
                     ("index.html", os.path.join("templates", "index.html"))):
    a, b = os.path.join(ROOT, flat), os.path.join(ROOT, nested)
    if not (os.path.exists(a) and os.path.exists(b)):
        continue
    digest = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()   # noqa: E731
    check("%s and %s are identical" % (flat, nested), digest(a) == digest(b),
          "the app serves %s; the other copy is stale" % nested)


# ===========================================================================
section("2. Declarations come before the code that uses them")
# ===========================================================================
# Narrowly targeted at the pattern that caused the outage: an immediately
# invoked function — which runs during page load — assigning a `let`/`const`
# whose declaration appears later in the file. Assignments inside ordinary
# handlers are fine, because those run long after the whole file is evaluated,
# so they are deliberately not flagged here; the node evaluation in section 4
# is what covers the general case.
decls = {}
for m in re.finditer(r"(?m)^\s*(?:let|const)\s+([A-Za-z_$][\w$]*)", js):
    decls.setdefault(m.group(1), m.start())

problems = []
for iife in re.finditer(r"\(function\s+[A-Za-z_$][\w$]*\s*\([^)]*\)\s*\{", js):
    depth, i = 1, iife.end()
    while i < len(js) and depth:
        depth += (js[i] == "{") - (js[i] == "}")
        i += 1
    body = js[iife.end():i]
    for m in re.finditer(r"(?m)^\s*([A-Za-z_$][\w$]*)\s*=\s*[^=]", body):
        name = m.group(1)
        if name in decls and decls[name] > iife.start():
            problems.append(
                "%s is assigned by the immediately-invoked function at line %d "
                "but declared at line %d — a temporal dead zone at page load"
                % (name, js[:iife.start()].count("\n") + 1,
                   js[:decls[name]].count("\n") + 1))
check("REGRESSION: no page-load IIFE assigns a binding declared later",
      not problems, "\n        ".join(problems))


# ===========================================================================
section("3. Credentials are not written where the privacy text says they are not")
# ===========================================================================
check("no job entry carries the Kaggle key",
      not re.search(r"kaggleKey:\s*key", js),
      "the saved job list is written regardless of 'remember me'")
check("sign-out clears the key held for the session",
      re.search(r'signout-btn[\s\S]{0,600}sessionKaggleKey\s*=\s*""', js) is not None)
check("the ORCA source is remembered separately from any credential",
      "orcaDataset" in js and "orcaLink" in js)
check("...and there is a control to forget it",
      "kaggle-forget-source" in js and "kaggle-forget-source" in html)


# ===========================================================================
section("3b. The equation help is present and matches what the code does")
# ===========================================================================
check("the page explains how to write an equation", "reaction-help" in html)
for topic, marker in (("the coefficient forms", "1/2 O2"),
                      ("names, formulas, SMILES and InChI", "InChI="),
                      ("which species are written rather than drawn", "C<sub>2</sub>H<sub>6</sub>O"),
                      ("what the balance check does", "legitimately unbalanced"),
                      ("the repetition convention in the .rxn file", "once per unit"),
                      ("a worked example", "2 benzene + 15 O2")):
    check("...it covers %s" % topic, marker in html, "looked for %r" % marker)
check("the worked example the page shows is the one the button fills in",
      "2 benzene + 15 O2" in js and "12 CO2 + 6 H2O" in js)
check("the formula toggle exists and is sent to the server",
      "reaction-small-as-formula" in html and "small_as_formula" in js)


# ===========================================================================
section("4. The whole file evaluates without throwing (needs node)")
# ===========================================================================
node = shutil.which("node")
if not node:
    print("  SKIP  node is not installed; the static checks above still ran")
else:
    stub = r"""
const seen = new Set(JSON.parse(process.argv[2]));
const inline = JSON.parse(process.argv[4] || '{}');
const store = JSON.parse(process.argv[5] || '{}');
function el(id) {
  const target = function () { return el(); };
  return new Proxy(target, {
    get(_t, prop) {
      if (prop === 'classList') return { toggle() {}, add() {}, remove() {}, contains() { return false; } };
      if (prop === 'dataset' || prop === 'style') return {};
      if (prop === 'textContent' && id && id in inline) return inline[id];
      if (prop === 'value' || prop === 'textContent' || prop === 'innerHTML') return '';
      if (prop === 'checked' || prop === 'disabled') return false;
      if (prop === 'files') return [];
      if (prop === 'length') return 0;
      if (prop === Symbol.iterator) return function* () {};
      if (prop === 'then') return undefined;
      return el();
    },
    set() { return true; },
    apply() { return el(); },
  });
}
global.window = { addEventListener() {}, location: { href: '' }, matchMedia: () => ({ matches: false, addEventListener() {} }) };
global.localStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: k => { delete store[k]; },
};
global.document = {
  getElementById(id) { if (!seen.has(id)) { throw new Error('unknown element id: ' + id); } return el(id); },
  querySelector() { return el(); },
  querySelectorAll() { return []; },
  createElement() { return el(); },
  addEventListener() {},
  body: el(),
  documentElement: el(),
  readyState: 'complete',
};
global.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) });
global.setInterval = () => 0;
global.setTimeout = () => 0;
global.alert = () => {};
global.confirm = () => false;
global.navigator = { clipboard: { writeText: () => Promise.resolve() }, userAgent: 'node' };
global.URL = { createObjectURL: () => '', revokeObjectURL() {} };
global.Blob = class {};
global.FormData = class { append() {} };
require(process.argv[3]);
console.log('EVALUATED-OK');
"""
    # The page ships its configuration in a <script type="application/json">
    # block that app.js parses on its very first line. In the file that block is
    # still a Jinja template, so it is taken from the RENDERED page instead --
    # which also proves the template and the script agree about its shape.
    inline, rendered = {}, ""
    try:
        sys.path.insert(0, ROOT)
        import app as webapp
        rendered = webapp.app.test_client().get("/").get_data(as_text=True)
    except Exception as exc:                                    # noqa: BLE001
        print("  note  could not render the page (%s); using the raw template" % exc)
        rendered = html
    for cid, content in re.findall(
            r'<script[^>]*id="([^"]+)"[^>]*>(.*?)</script>', rendered, re.S):
        inline[cid] = content
    check("the rendered page carries a parseable orca-config block",
          bool(inline.get("orca-config", "").strip())
          and json.loads(inline["orca-config"]) is not None,
          "app.js reads it on its first line; an empty or invalid block stops "
          "the whole script before any handler is registered")
    with tempfile.TemporaryDirectory() as tmp:
        runner = os.path.join(tmp, "run.js")
        with open(runner, "w", encoding="utf-8") as fh:
            fh.write(stub)
        # Both states matter. A first visit leaves localStorage empty, so the
        # auto sign-in branch never runs; a returning visit takes that branch,
        # and THAT is the path the temporal-dead-zone bug was on. Testing only
        # the empty case would have missed it entirely.
        states = {
            "a first visit (empty storage)": {},
            "a returning visit (saved credentials)": {
                "chemlab_kaggle_username": "tester",
                "chemlab_kaggle_key": "0" * 32,
                "chemlab_orca_dataset": "tester/orca-6-1-0",
                "chemlab_orca_source_kind": "dataset",
                "chemlab_jobs": json.dumps([{
                    "name": "old job", "jobId": "chem-tools-old-0badc0de",
                    "kaggleUsername": "tester", "kaggleKey": "leftover-from-an-old-version",
                    "status": "running", "submittedAt": 0,
                }]),
            },
        }
        results = {}
        for label, seed in states.items():
            proc = subprocess.run(
                [node, runner, json.dumps(sorted(html_ids)), APP_JS,
                 json.dumps(inline), json.dumps(seed)],
                capture_output=True, text=True, timeout=60)
            results[label] = proc

    for label, proc in results.items():
        ok = "EVALUATED-OK" in proc.stdout
        check("REGRESSION: app.js runs to the end on %s" % label,
              ok, (proc.stderr or proc.stdout).strip()[-800:])
        check("...with no ReferenceError, so every later handler is registered (%s)"
              % label.split(" (")[0],
              ok and "ReferenceError" not in proc.stderr)


print("\n" + "=" * 70)
print("FRONTEND: %d passed, %d failed" % (_passed, _failed))
print("=" * 70)
sys.exit(1 if _failed else 0)
