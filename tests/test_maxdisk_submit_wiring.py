# -*- coding: utf-8 -*-
"""P2-2 regression: MaxDisk budgets are wired end-to-end.

Covers BOTH documented surfaces:
- JobSubmitRequest.maxdisk_mb  (submission path, FastAPI + shared service)
- OrcaGenerateRequest.maxdisk  (generator path, schema-documented contract)

Proves:
- omitted   -> input stays backend-neutral; the runner's own gate applies its
               configured default (20000 MB)
- explicit  -> the caller's budget reaches the canonical input text and
               survives BOTH execution-runner gates: the legacy notebook gate
               (extracted verbatim from KAGGLE_RUNNER_BODY) and the
               orchestrator gate (art.set_maxdisk, kernel_runner per window)
- successor -> the budget survives continuation input derivation
- invalid   -> rejected (422) at the Pydantic/FastAPI boundary
- identity  -> the budget participates in the idempotency identity: different
               budgets never replay each other's kernels; identical requests
               still replay exactly one push
- %maxcore  -> never touched by the override
"""
import ast
import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

INP = "! B3LYP Opt\n* xyz 0 1\nO 0 0 0\nH 0 0 0.96\nH 0 0 -0.96\n*\n"
PAYLOAD = dict(kaggle_username="tester", kaggle_key="0" * 32,
               input_filename="mol.inp", input_content=INP,
               dataset_sources_raw="user/orca-dataset", orca_link="",
               job_name="w")

API_BODY = {"kaggle_username": "tester", "kaggle_key": "0" * 32,
            "input_content": "! B3LYP Opt\n* xyz 0 1\nO 0 0 0\n*\n",
            "dataset_sources": "user/orca-dataset"}

GENERATE_BODY = {"coords": "O 0.0 0.0 0.0\nH 0.0 0.0 0.96\nH 0.0 0.0 -0.96",
                 "name": "water", "calc_type": "sp"}


def _legacy_gate():
    """Extracts the legacy runner's window gate (_normalize_maxdisk plus its
    helpers) VERBATIM from the embedded KAGGLE_RUNNER_BODY template - the exact
    code that executes inside the Kaggle notebook on every window."""
    import kaggle_runner as kr
    tree = ast.parse(kr.KAGGLE_RUNNER_BODY)
    wanted = {"_strip_comments", "_find_block", "_block_value",
              "_force_block_value", "_normalize_maxdisk"}
    ns = {"re": __import__("re")}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         "<legacy-runner-gate>", "exec"), ns)
    missing = wanted - set(ns)
    assert not missing, "legacy gate functions not found in template: %s" % missing
    return ns["_normalize_maxdisk"]


@pytest.fixture(scope="module")
def api_client():
    from fastapi.testclient import TestClient
    import api.main as api_main
    with TestClient(api_main.app) as client:
        yield client


@pytest.fixture()
def submit_harness(tmp_path, monkeypatch):
    """(submit, captures, pushes): runs the SHARED submission service against a
    stubbed Kaggle transport and a REAL orchestrator store (hermetic tmp dir).
    captures records every build_job_dir(**kw) call."""
    import services.kaggle_service as ks
    from orca_orchestrator.config import StoreConfig
    from orca_orchestrator.store import JobStore

    shared = str(tmp_path / "state")
    os.makedirs(shared, exist_ok=True)
    store = JobStore(StoreConfig(state_dir=shared))

    captures, pushes = [], []

    def fake_build_job_dir(**kw):
        d = os.path.join(str(tmp_path), "job_%d" % len(pushes))
        os.makedirs(d, exist_ok=True)
        captures.append(kw)
        return d

    def fake_push(job_dir, username, key):
        pushes.append(job_dir)
        slug = "chem-tools-x-%08x" % len(pushes)
        return {"job_id": slug, "url": "https://x/" + slug,
                "owner": username, "slug": slug}

    monkeypatch.setattr(ks.kaggle_runner, "build_job_dir", fake_build_job_dir)
    monkeypatch.setattr(ks.kaggle_runner, "push_job", fake_push)

    def submit(**kw):
        payload, status = ks.submit_job(store=store, **kw)
        assert status == 200, payload
        return payload

    return submit, captures, pushes


def _submitted_input(captures, idx=0):
    files = captures[idx]["files_payload"]
    name = [n for n in files if n.endswith(".inp")][0]
    return base64.b64decode(files[name]).decode("utf-8")


# ---------------------------------------------------------------------------
# 1-3: omitted / 20000 / 50000 reach the runner
# ---------------------------------------------------------------------------
def test_submit_omitted_maxdisk_leaves_input_backend_neutral(submit_harness):
    submit, captures, _ = submit_harness
    body = submit(**PAYLOAD)
    assert body["ok"] is True
    text = _submitted_input(captures)
    assert "MaxDisk" not in text, "omitted budget must not inject anything"
    gate = _legacy_gate()
    _out, effective, action = gate(text, 20000)
    assert (effective, action) == (20000, "inserted"), \
        "the runner gate must apply its configured default when omitted"


def test_submit_custom_maxdisk_50000_reaches_runner(submit_harness):
    submit, captures, _ = submit_harness
    body = submit(**dict(PAYLOAD, maxdisk_mb=50000))
    assert body["ok"] is True
    text = _submitted_input(captures)
    assert "MaxDisk 50000" in text
    gate = _legacy_gate()
    _out, eff_legacy, act_legacy = gate(text, 20000)
    assert (eff_legacy, act_legacy) == (50000, "preserved")
    from orca_orchestrator import orca_artifacts as art
    _out2, eff_orch, act_orch = art.set_maxdisk(text, 20000)
    assert (eff_orch, act_orch) == (50000, "preserved")


def test_submit_explicit_20000_is_preserved(submit_harness):
    submit, captures, _ = submit_harness
    submit(**dict(PAYLOAD, maxdisk_mb=20000))
    text = _submitted_input(captures)
    assert "MaxDisk 20000" in text
    gate = _legacy_gate()
    _out, effective, action = gate(text, 20000)
    assert (effective, action) == (20000, "preserved")


# ---------------------------------------------------------------------------
# 4-5: invalid values are rejected at the API boundary (422)
# ---------------------------------------------------------------------------
def test_zero_negative_and_malformed_maxdisk_rejected_422(api_client):
    for bad in (0, -1, "abc"):
        r = api_client.post("/api/v1/kaggle/jobs", json=dict(API_BODY, maxdisk_mb=bad))
        assert r.status_code == 422, "maxdisk_mb=%r must be rejected" % (bad,)
        body = r.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 6-7: the custom budget survives the runner gates and successor derivation
# ---------------------------------------------------------------------------
def test_custom_maxdisk_survives_successor_derivation(submit_harness):
    submit, captures, _ = submit_harness
    submit(**dict(PAYLOAD, maxdisk_mb=50000))
    original = _submitted_input(captures)
    from orca_orchestrator import orca_artifacts as art
    # The successor input is derived from the original (moread stripped,
    # geometry carried over); the %maxdisk block is not part of that surgery.
    successor = art.strip_moread(original)
    gate = _legacy_gate()
    _o1, eff_legacy, act_legacy = gate(successor, 20000)
    _o2, eff_orch, act_orch = art.set_maxdisk(successor, 20000)
    assert (eff_legacy, act_legacy) == (50000, "preserved")
    assert (eff_orch, act_orch) == (50000, "preserved")


def test_runner_budget_stays_configurable_not_hardcoded():
    """The default lives in backend configuration (env-tunable), never in the
    API transport layer. AST-level check: no set_maxdisk() CALL in the
    transport/service layers may pass a literal budget Constant - comments and
    docstrings are ignored by construction."""
    from orca_orchestrator.config import CONFIG
    assert CONFIG.runner.maxdisk_mb == 20000
    import kaggle_runner as kr
    src = open(kr.__file__, "r", encoding="utf-8").read()
    assert 'os.environ.get("ORCA_MAXDISK_MB", "20000")' in src, \
        "the legacy runner budget must stay env-configurable"
    base = os.path.dirname(kr.__file__)
    for rel in ("api/routes/kaggle.py", "services/kaggle_service.py",
                "services/orca_service.py"):
        with open(os.path.join(base, rel), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (
                fn.attr if isinstance(fn, ast.Attribute) else "")
            if name != "set_maxdisk":
                continue
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                assert not (isinstance(arg, ast.Constant) and arg.value == 20000), \
                    "%s passes a hard-coded 20000 budget to set_maxdisk" % rel


def test_service_level_guard_rejects_invalid_explicit_maxdisk(tmp_path, monkeypatch):
    """Non-Pydantic callers (the legacy Flask route omits the field; future
    internal callers) are defended at the service boundary: an invalid EXPLICIT
    budget can never become an invalid ORCA directive. Omitted stays untouched."""
    import services.kaggle_service as ks
    from orca_orchestrator.config import StoreConfig
    from orca_orchestrator.store import JobStore

    shared = str(tmp_path / "state")
    os.makedirs(shared, exist_ok=True)
    store = JobStore(StoreConfig(state_dir=shared))
    pushes = []

    monkeypatch.setattr(ks.kaggle_runner, "build_job_dir",
                        lambda **kw: str(tmp_path / "jobdir"))

    def fake_push(job_dir, username, key):
        pushes.append(job_dir)
        return {"job_id": "chem-tools-x-1", "url": "https://x",
                "owner": username, "slug": "chem-tools-x-1"}

    monkeypatch.setattr(ks.kaggle_runner, "push_job", fake_push)

    for bad in (0, -5, "abc", 1.5):
        payload, status = ks.submit_job(store=store, **dict(PAYLOAD, maxdisk_mb=bad))
        assert status == 400, "maxdisk_mb=%r must be rejected at the service" % (bad,)
        assert payload["ok"] is False
    assert pushes == [], "a rejected budget must never reach the transport"
    # omitted stays untouched and still succeeds
    payload, status = ks.submit_job(store=store, **PAYLOAD)
    assert status == 200 and payload["ok"] is True
    assert len(pushes) == 1


# ---------------------------------------------------------------------------
# 8: MaxDisk participates in the idempotency identity
# ---------------------------------------------------------------------------
def test_different_maxdisk_budgets_never_collide_idempotency(submit_harness):
    submit, _captures, pushes = submit_harness
    r1 = submit(**dict(PAYLOAD, maxdisk_mb=20000))   # push #1
    r2 = submit(**dict(PAYLOAD, maxdisk_mb=50000))   # push #2 - NOT a replay
    r3 = submit(**dict(PAYLOAD, maxdisk_mb=50000))   # replay of #2
    assert len(pushes) == 2, "different budgets must never replay each other"
    assert r2["job_id"] != r1["job_id"]
    assert r3["job_id"] == r2["job_id"], "identical request must replay from the store"


def test_same_budget_without_key_replays_single_push(submit_harness):
    submit, _captures, pushes = submit_harness
    r1 = submit(**dict(PAYLOAD, maxdisk_mb=50000))
    r2 = submit(**dict(PAYLOAD, maxdisk_mb=50000))
    assert len(pushes) == 1
    assert r2["job_id"] == r1["job_id"]


# ---------------------------------------------------------------------------
# 9-10: generator path + %maxcore independence
# ---------------------------------------------------------------------------
def test_generator_custom_maxdisk_is_written_into_input(api_client):
    r = api_client.post("/api/v1/orca/generate", json=dict(GENERATE_BODY, maxdisk=12345))
    assert r.status_code == 200
    text = base64.b64decode(r.json()["file_base64"]).decode()
    assert "MaxDisk 12345" in text
    from orca_orchestrator import orca_artifacts as art
    _o, eff, act = art.set_maxdisk(text, 20000)
    assert (eff, act) == (12345, "preserved")


def test_generator_omitted_maxdisk_stays_backend_neutral(api_client):
    r = api_client.post("/api/v1/orca/generate", json=dict(GENERATE_BODY))
    assert r.status_code == 200
    text = base64.b64decode(r.json()["file_base64"]).decode()
    assert "MaxDisk" not in text, "omitted budget must stay backend-neutral"


def test_force_override_semantics_and_maxcore_independence():
    from orca_orchestrator import orca_artifacts as art
    inp = ("%maxcore 4000\n%maxdisk\n  MaxDisk 30000\nend\n"
           "! B3LYP Opt\n* xyz 0 1\nO 0 0 0\n*\n")
    # default contract unchanged: existing valid directive preserved
    _o, eff, act = art.set_maxdisk(inp, 50000)
    assert (eff, act) == (30000, "preserved")
    # force: explicit caller budget overrides, %maxcore untouched
    out, eff, act = art.set_maxdisk(inp, 50000, force=True)
    assert (eff, act) == (50000, "updated")
    assert "MaxDisk 50000" in out and "MaxDisk 30000" not in out
    assert "%maxcore 4000" in out
    # force with no existing directive inserts
    out2, eff2, act2 = art.set_maxdisk(INP, 50000, force=True)
    assert (eff2, act2) == (50000, "inserted") and "MaxDisk 50000" in out2


def test_explicit_api_budget_overrides_preexisting_directive(submit_harness):
    submit, captures, _ = submit_harness
    pre = "%maxdisk\n  MaxDisk 30000\nend\n" + INP
    submit(**dict(PAYLOAD, input_content=pre, maxdisk_mb=50000))
    text = _submitted_input(captures)
    assert "MaxDisk 50000" in text and "MaxDisk 30000" not in text
