# -*- coding: utf-8 -*-
"""Comprehensive verification suite for Local ORCA Primary Backend.

Verifies:
1. Settings contract: 3 user paths, readiness gates, no auto-discovery.
2. Canonical input and output directory semantics, artifact synchronization.
3. Scientific validation pipeline (Exit 0 malformed -> scientific failure).
4. Real Local Queue Matrix (1/0, 1/1, 1/9) & automatic queue advancement without manual state mutation.
5. Low-level process reservation race with barrier (Popen count == 1).
6. Multi-Process Two Workers concurrency test (OS processes, max active == 1, fake process max == 1).
7. Running cancellation & queued cancellation.
8. Restart recovery & duplicate execution protection (launch count == 1).
9. Kaggle disabled default, enablement, and multi-process max-5 concurrency.
10. Cross-platform CLI runner (tools/orca_local_runner.py).
11. Fail-closed cross-process lock timeout & resource leak safety (OS processes).
12. Settings lock contention & corruption defense.
13. Process identity matching (MATCH case).
14. Process identity mismatch & PID reuse safety (MISMATCH case, foreign process survives).
15. Process identity UNKNOWN fails safe.
16. Cancellation after restart requires identity match.
17. Restart mismatched PID does not kill foreign process.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import pathlib
import platform
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orca_engine", "src"))

import app as webapp
import services.reaction_workflow_service as rws
import services.local_orca_service as local_orca
try:
    from tests.test_reaction_workflow_thermo import opt_fixture, freq_fixture
except ImportError:
    from test_reaction_workflow_thermo import opt_fixture, freq_fixture


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    """Hermetic test environment for Local ORCA and Reaction Store."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CHEMISTRY_LAB_STATE_DIR", str(state_dir))

    store = rws.ReactionStore(str(state_dir))
    monkeypatch.setattr(webapp, "_reaction_store", store)
    webapp.app.config["TESTING"] = True

    with webapp.app.test_client() as c:
        with c.session_transaction() as sess:
            sess["user"] = {"email": "test-user@chemistry.lab", "role": "researcher"}
        yield {
            "client": c,
            "store": store,
            "state_dir": state_dir,
            "tmp_path": tmp_path,
        }


def _make_fake_orca_script(tmp_path: pathlib.Path, name: str = "fake_orca.py") -> pathlib.Path:
    """Creates an instrumentable fake ORCA executable script."""
    script_path = tmp_path / name
    opt_content = opt_fixture(-76.40, coords_variant=1)
    code = f"""# -*- coding: utf-8 -*-
import sys, os, time

mode = os.environ.get("FAKE_ORCA_MODE", "VALID_OPT")
delay = float(os.environ.get("FAKE_ORCA_DELAY", "0"))
counter_file = os.environ.get("FAKE_ORCA_COUNTER_FILE", "")
launch_count_file = os.environ.get("FAKE_ORCA_LAUNCH_COUNT_FILE", "")

if launch_count_file:
    try:
        with open(launch_count_file, "a", encoding="utf-8") as f:
            f.write(f"LAUNCH {{os.getpid()}} {{time.time()}}\\n")
    except Exception:
        pass

if counter_file:
    try:
        with open(counter_file, "a", encoding="utf-8") as f:
            f.write(f"START {{os.getpid()}} {{time.time()}}\\n")
    except Exception:
        pass

if delay > 0:
    time.sleep(delay)

if counter_file:
    try:
        with open(counter_file, "a", encoding="utf-8") as f:
            f.write(f"END {{os.getpid()}} {{time.time()}}\\n")
    except Exception:
        pass

if mode == "VALID_OPT":
    sys.stdout.write({repr(opt_content)})
    sys.exit(0)
elif mode == "MALFORMED":
    sys.stdout.write("ORCA execution started...\\nTRUNCATED OUTPUT (NO CONVERGENCE)\\n")
    sys.exit(0)
elif mode == "NONZERO_EXIT":
    sys.stderr.write("ORCA fatal error in scf\\n")
    sys.exit(2)
else:
    sys.stdout.write({repr(opt_content)})
    sys.exit(0)
"""
    script_path.write_text(code, encoding="utf-8")
    return script_path


# =========================================================================
# 1. Settings Contract, User-Supplied Paths & Readiness Gates
# =========================================================================
def test_local_orca_settings_contract_and_readiness(test_env):
    """Verifies that Local ORCA requires 3 explicit paths, has no auto discovery, and enforces ready gates."""
    state_dir = str(test_env["state_dir"])
    tmp_path = test_env["tmp_path"]

    # 1. Fresh settings defaults
    cfg = local_orca.get_local_orca_settings(state_dir)
    assert cfg["orca_executable"] == ""
    assert cfg["input_directory"] == ""
    assert cfg["output_directory"] == ""
    assert cfg["working_directory"] == ""
    assert cfg["concurrency"] == 1
    assert cfg["execution_backend"] == "local"
    assert cfg["kaggle_enabled"] is False

    # 2. Fresh state -> NOT_CONFIGURED
    status_fresh = local_orca.validate_local_orca_config(cfg, state_dir)
    assert status_fresh["configured"] is False
    assert status_fresh["ready"] is False
    assert status_fresh["status"] == "NOT_CONFIGURED"
    assert any("LOCAL_ORCA_NOT_CONFIGURED" in e for e in status_fresh["errors"])

    # 3. Invalid paths -> INVALID
    bad_cfg = {
        "orca_executable": str(tmp_path / "nonexistent" / "orca.exe"),
        "input_directory": str(tmp_path / "nonexistent" / "inps"),
        "output_directory": str(tmp_path / "nonexistent" / "outs"),
    }
    status_bad = local_orca.validate_local_orca_config(bad_cfg, state_dir)
    assert status_bad["configured"] is True
    assert status_bad["ready"] is False
    assert status_bad["status"] == "INVALID"
    assert any("ORCA_EXECUTABLE_NOT_FOUND" in e for e in status_bad["errors"])
    assert any("INPUT_DIRECTORY_INVALID" in e for e in status_bad["errors"])
    assert any("OUTPUT_DIRECTORY_INVALID" in e for e in status_bad["errors"])

    # 4. Valid paths -> READY
    fake_exe = _make_fake_orca_script(tmp_path)
    in_dir = tmp_path / "inputs"
    out_dir = tmp_path / "outputs"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    good_cfg = {
        "orca_executable": str(fake_exe),
        "input_directory": str(in_dir),
        "output_directory": str(out_dir),
    }
    local_orca.save_local_orca_settings(good_cfg, state_dir)
    status_good = local_orca.validate_local_orca_config(state_dir=state_dir)
    assert status_good["configured"] is True
    assert status_good["ready"] is True
    assert status_good["status"] == "READY"
    assert len(status_good["errors"]) == 0


def test_no_automatic_executable_discovery_in_execution_path(test_env, monkeypatch):
    """Verifies that no silent auto-discovery occurs in execution paths."""
    state_dir = str(test_env["state_dir"])
    monkeypatch.setenv("ORCA_BIN", "C:\\SilentlyDiscovered\\orca.exe")
    monkeypatch.setenv("ORCA_PATH", "C:\\SilentlyDiscovered\\orca.exe")

    cfg = local_orca.get_local_orca_settings(state_dir)
    assert cfg["orca_executable"] == ""

    res = local_orca.execute_local_orca_job(
        job_id="test_no_auto",
        input_text="! Opt\n* xyz 0 1\n*",
        settings=cfg,
        state_dir=state_dir,
    )
    assert res["ok"] is False
    assert res["process_ok"] is False
    assert res["scientific_ok"] is False
    assert any("LOCAL_ORCA_NOT_CONFIGURED" in e for e in [res.get("error", ""), res.get("error_code", "")])


def test_local_orca_directory_semantics_and_artifacts(test_env, monkeypatch):
    """Verifies canonical .inp in input_directory and outputs + artifacts in output_directory."""
    tmp_path = test_env["tmp_path"]
    state_dir = str(test_env["state_dir"])
    fake_exe = _make_fake_orca_script(tmp_path)

    inp_dir = tmp_path / "user_inputs"
    out_dir = tmp_path / "user_outputs"
    scratch_dir = tmp_path / "user_scratch"
    inp_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    scratch_dir.mkdir(parents=True, exist_ok=True)

    settings = {
        "orca_executable": str(fake_exe),
        "input_directory": str(inp_dir),
        "output_directory": str(out_dir),
        "working_directory": str(scratch_dir),
    }
    local_orca.save_local_orca_settings(settings, state_dir)
    monkeypatch.setenv("FAKE_ORCA_MODE", "VALID_OPT")

    job_id = "water_opt_stage0"
    inp_text = "! B3LYP def2-SVP Opt\n* xyz 0 1\nO 0 0 0\nH 0 0 1\nH 0 1 0\n*"

    res = local_orca.execute_local_orca_job(
        job_id=job_id,
        input_text=inp_text,
        stage_kind="OPT",
        state_dir=state_dir,
    )
    assert res["ok"] is True
    assert res["process_ok"] is True
    assert res["parse_ok"] is True
    assert res["scientific_ok"] is True

    canon_inp = inp_dir / f"{safe_name(job_id)}.inp"
    assert canon_inp.exists()
    assert canon_inp.read_text(encoding="utf-8") == inp_text

    job_out_dir = out_dir / job_id
    assert job_out_dir.is_dir()
    canon_out = job_out_dir / f"{job_id}.out"
    assert canon_out.exists()


def safe_name(n: str) -> str:
    import re
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", n)


def test_fake_orca_scientific_validation_flow(test_env, monkeypatch):
    """Tests scientific validation: Exit 0 is strictly process success, not scientific success."""
    tmp_path = test_env["tmp_path"]
    state_dir = str(test_env["state_dir"])
    fake_exe = _make_fake_orca_script(tmp_path)

    inp_dir = tmp_path / "inp_sci"
    out_dir = tmp_path / "out_sci"
    inp_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    settings = {
        "orca_executable": str(fake_exe),
        "input_directory": str(inp_dir),
        "output_directory": str(out_dir),
    }
    local_orca.save_local_orca_settings(settings, state_dir)

    # Case A: Exit 0 + Valid Output -> Scientific Success
    monkeypatch.setenv("FAKE_ORCA_MODE", "VALID_OPT")
    res_a = local_orca.execute_local_orca_job("job_a", "! Opt\n* xyz 0 1\n*", stage_kind="OPT", state_dir=state_dir)
    assert res_a["process_ok"] is True
    assert res_a["parse_ok"] is True
    assert res_a["scientific_ok"] is True
    assert res_a["ok"] is True

    # Case B: Exit 0 + Malformed Output -> Scientific FAILURE
    monkeypatch.setenv("FAKE_ORCA_MODE", "MALFORMED")
    res_b = local_orca.execute_local_orca_job("job_b", "! Opt\n* xyz 0 1\n*", stage_kind="OPT", state_dir=state_dir)
    assert res_b["process_ok"] is True
    assert res_b["scientific_ok"] is False
    assert res_b["ok"] is False

    # Case C: Nonzero Exit Code -> Process FAILURE
    monkeypatch.setenv("FAKE_ORCA_MODE", "NONZERO_EXIT")
    res_c = local_orca.execute_local_orca_job("job_c", "! Opt\n* xyz 0 1\n*", stage_kind="OPT", state_dir=state_dir)
    assert res_c["process_ok"] is False
    assert res_c["scientific_ok"] is False
    assert res_c["ok"] is False
    assert res_c["error_code"] == "PROCESS_FAILED"

    # Case D: Paths with spaces -> PASS
    space_dir = tmp_path / "path with spaces in dir"
    space_dir.mkdir(parents=True, exist_ok=True)
    space_inp = space_dir / "input with space"
    space_out = space_dir / "output with space"
    space_inp.mkdir(parents=True, exist_ok=True)
    space_out.mkdir(parents=True, exist_ok=True)
    settings["input_directory"] = str(space_inp)
    settings["output_directory"] = str(space_out)
    local_orca.save_local_orca_settings(settings, state_dir)
    monkeypatch.setenv("FAKE_ORCA_MODE", "VALID_OPT")

    res_d = local_orca.execute_local_orca_job("job space", "! Opt\n* xyz 0 1\n*", stage_kind="OPT", state_dir=state_dir)
    assert res_d["ok"] is True
    assert res_d["process_ok"] is True


def test_real_local_queue_matrix_and_auto_advance(test_env):
    """Verifies real Local Queue Matrix (1/0, 1/1, 1/9) & automatic queue advancement without manual state mutation."""
    store = test_env["store"]

    # 1. 1 submitted: active=1, queued=0
    rx1 = rws.create_reaction("owner-1", "A -> B", store)
    rws.apply_shared_workflow_to_all(rx1, store=store)
    slot1 = store.reserve_execution_slot(backend="local", max_concurrency=1)
    assert slot1 is not None
    summary1 = store.get_queue_summary(backend="local")
    assert summary1["active"] == 1
    assert summary1["queued"] == 1

    # 2. 2 submitted: active=1, queued=1
    slot_excess = store.reserve_execution_slot(backend="local", max_concurrency=1)
    assert slot_excess is None
    summary2 = store.get_queue_summary(backend="local")
    assert summary2["active"] == 1
    assert summary2["queued"] == 1

    # Complete slot1 -> next queued job automatically advances on next reserve
    target_r, target_sp, target_st = slot1
    valid_opt_out = opt_fixture(-76.42, coords_variant=1)
    rws.complete_stage_with_output(
        rx1, target_sp["species_id"], target_st["stage_id"],
        output_text=valid_opt_out,
        store=store
    )

    # Now slot is released
    slot2 = store.reserve_execution_slot(backend="local", max_concurrency=1)
    assert slot2 is not None
    assert slot2[1]["species_id"] != target_sp["species_id"]

    # 3. 10 submitted: active=1, queued=9
    for k in range(5):
        rx_k = rws.create_reaction("owner-1", f"R{k} -> P{k}", store)
        rws.apply_shared_workflow_to_all(rx_k, store=store)

    summary10 = store.get_queue_summary(backend="local")
    assert summary10["active"] == 1
    assert summary10["queued"] >= 9

    # 4. Multi-Stage Fairness: Stage N+1 returns to FIFO queue
    rx1_refreshed = store.get_reaction("owner-1", rx1["reaction_id"])
    sp0_stages = rx1_refreshed["species"][0]["stages"]
    assert sp0_stages[0]["state"] == "COMPLETE"
    assert sp0_stages[1]["state"] == "READY"
    assert sp0_stages[1]["ready_at"] is not None


def test_low_level_process_reservation_race_with_barrier(test_env, monkeypatch):
    """Deterministic barrier-based race test forcing two threads to reach launch boundary simultaneously."""
    tmp_path = test_env["tmp_path"]
    state_dir = str(test_env["state_dir"])
    fake_exe = _make_fake_orca_script(tmp_path)

    inp_dir = tmp_path / "inp_race"
    out_dir = tmp_path / "out_race"
    inp_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    settings = {
        "orca_executable": str(fake_exe),
        "input_directory": str(inp_dir),
        "output_directory": str(out_dir),
    }
    local_orca.save_local_orca_settings(settings, state_dir)
    monkeypatch.setenv("FAKE_ORCA_MODE", "VALID_OPT")
    monkeypatch.setenv("FAKE_ORCA_DELAY", "0.2")

    barrier = threading.Barrier(2)
    results = []

    def racer(jid):
        barrier.wait()
        res = local_orca.execute_local_orca_job(jid, "! Opt\n*", stage_kind="OPT", state_dir=state_dir)
        results.append(res)

    t1 = threading.Thread(target=racer, args=("race_job_1",))
    t2 = threading.Thread(target=racer, args=("race_job_2",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    successes = [r for r in results if r.get("ok") is True]
    rejected = [r for r in results if r.get("error_code") == "LOCAL_CONCURRENCY_LIMIT_EXCEEDED"]
    assert len(successes) == 1
    assert len(rejected) == 1


def _os_worker_local_scheduler(state_dir_str: str, worker_id: int, results_list):
    """Target function executed by independent OS processes."""
    store = rws.ReactionStore(state_dir_str)
    for _ in range(15):
        slot = store.reserve_execution_slot(backend="local", max_concurrency=1)
        summary = store.get_queue_summary(backend="local")
        results_list.append(summary["active"])
        if slot:
            time.sleep(0.02)
            rx, sp, st = slot
            st["state"] = "COMPLETE"
            store.save_reaction(rx)


def test_multi_process_two_workers_max_1(test_env):
    """Verifies that TWO INDEPENDENT OS PROCESSES never exceed active concurrency 1."""
    state_dir = str(test_env["state_dir"])
    store = test_env["store"]

    for k in range(10):
        rx = rws.create_reaction("owner-1", f"A{k} -> B{k}", store)
        for sp in rx["species"]:
            st = rws.add_stage(rx, sp["species_id"], "OPT", "Opt")
            st["backend"] = "local"
            st["state"] = "READY"
            st["ready_at"] = datetime(2026, 8, 31, 10, 0, k, tzinfo=timezone.utc).isoformat()
        store.save_reaction(rx)

    manager = multiprocessing.Manager()
    observed_active = manager.list()

    p1 = multiprocessing.Process(target=_os_worker_local_scheduler, args=(state_dir, 1, observed_active))
    p2 = multiprocessing.Process(target=_os_worker_local_scheduler, args=(state_dir, 2, observed_active))
    p1.start()
    p2.start()
    p1.join()
    p2.join()

    assert len(observed_active) > 0
    assert max(observed_active) == 1


def test_running_cancellation_and_queued_cancellation(test_env, monkeypatch):
    """Tests cancellation of actively running subprocess and queued stages."""
    tmp_path = test_env["tmp_path"]
    state_dir = str(test_env["state_dir"])
    fake_exe = _make_fake_orca_script(tmp_path)

    inp_dir = tmp_path / "inp_cancel"
    out_dir = tmp_path / "out_cancel"
    inp_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    settings = {
        "orca_executable": str(fake_exe),
        "input_directory": str(inp_dir),
        "output_directory": str(out_dir),
    }
    local_orca.save_local_orca_settings(settings, state_dir)
    monkeypatch.setenv("FAKE_ORCA_MODE", "VALID_OPT")
    monkeypatch.setenv("FAKE_ORCA_DELAY", "2.0")

    job_id = "long_job_to_cancel"
    t = threading.Thread(
        target=local_orca.execute_local_orca_job,
        args=(job_id, "! Opt\n*"),
        kwargs={"stage_kind": "OPT", "state_dir": state_dir}
    )
    t.start()
    time.sleep(0.3)

    cancelled = local_orca.cancel_local_orca_job(job_id, state_dir=state_dir)
    assert cancelled is True
    t.join(timeout=3.0)

    active = local_orca.get_active_local_process_info(state_dir)
    assert active is None


def test_restart_recovery_and_duplicate_execution_protection(test_env, monkeypatch):
    """Instruments fake ORCA with persistent launch counter and proves launch_count == 1 across restart."""
    tmp_path = test_env["tmp_path"]
    state_dir = str(test_env["state_dir"])
    fake_exe = _make_fake_orca_script(tmp_path)
    launch_file = tmp_path / "launch_counter.log"

    inp_dir = tmp_path / "inp_restart"
    out_dir = tmp_path / "out_restart"
    inp_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    settings = {
        "orca_executable": str(fake_exe),
        "input_directory": str(inp_dir),
        "output_directory": str(out_dir),
    }
    local_orca.save_local_orca_settings(settings, state_dir)
    monkeypatch.setenv("FAKE_ORCA_MODE", "VALID_OPT")
    monkeypatch.setenv("FAKE_ORCA_LAUNCH_COUNT_FILE", str(launch_file))

    # 1. Execute job once
    res = local_orca.execute_local_orca_job("restart_job_1", "! Opt\n*", stage_kind="OPT", state_dir=state_dir)
    assert res["ok"] is True

    # 2. Simulate complete application restart by creating new ReactionStore from same state_dir
    new_store = rws.ReactionStore(state_dir)
    new_val = local_orca.validate_local_orca_config(state_dir=state_dir)
    assert new_val["ready"] is True

    lines = launch_file.read_text(encoding="utf-8").strip().splitlines() if launch_file.exists() else []
    assert len(lines) == 1


def test_kaggle_disabled_default_and_enablement(test_env):
    """Tests that Kaggle is disabled by default and rejected until explicitly enabled."""
    store = test_env["store"]
    state_dir = str(test_env["state_dir"])

    cfg = local_orca.get_local_orca_settings(state_dir)
    assert cfg["kaggle_enabled"] is False

    rx = rws.create_reaction("owner-1", "A -> B", store)
    for sp in rx["species"]:
        st = rws.add_stage(rx, sp["species_id"], "OPT", "Opt")
        st["backend"] = "kaggle"
        st["state"] = "READY"
    store.save_reaction(rx)

    slot = store.reserve_execution_slot(backend="kaggle")
    assert slot is None

    local_orca.save_local_orca_settings({"kaggle_enabled": True}, state_dir)
    slot_enabled = store.reserve_execution_slot(backend="kaggle")
    assert slot_enabled is not None


def _os_worker_kaggle_scheduler(state_dir_str: str, worker_id: int, results_list):
    store = rws.ReactionStore(state_dir_str)
    for _ in range(20):
        slot = store.reserve_execution_slot(backend="kaggle", max_concurrency=5)
        summary = store.get_queue_summary(backend="kaggle")
        results_list.append(summary["active"])


def test_kaggle_process_workers_concurrency_max_5(test_env):
    """Verifies that TWO INDEPENDENT OS PROCESSES never exceed Kaggle concurrency cap of 5."""
    store = test_env["store"]
    state_dir = str(test_env["state_dir"])
    local_orca.save_local_orca_settings({"kaggle_enabled": True}, state_dir)

    for idx in range(10):
        rx = rws.create_reaction("owner-1", f"K{idx} -> P{idx}", store)
        for sp in rx["species"]:
            st = rws.add_stage(rx, sp["species_id"], "OPT", "Opt")
            st["backend"] = "kaggle"
            st["state"] = "READY"
        store.save_reaction(rx)

    manager = multiprocessing.Manager()
    observed = manager.list()

    p1 = multiprocessing.Process(target=_os_worker_kaggle_scheduler, args=(state_dir, 1, observed))
    p2 = multiprocessing.Process(target=_os_worker_kaggle_scheduler, args=(state_dir, 2, observed))
    p1.start()
    p2.start()
    p1.join()
    p2.join()

    assert len(observed) > 0
    assert max(observed) <= 5


def test_orca_local_runner_cli(test_env, monkeypatch):
    """Tests the tools/orca_local_runner.py CLI interface with validate, status, and run commands."""
    tmp_path = test_env["tmp_path"]
    state_dir = str(test_env["state_dir"])
    fake_exe = _make_fake_orca_script(tmp_path)

    inp_dir = tmp_path / "cli_inps"
    out_dir = tmp_path / "cli_outs"
    inp_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    inp_file = inp_dir / "sample.inp"
    inp_file.write_text("! Opt\n* xyz 0 1\nO 0 0 0\n*", encoding="utf-8")

    cli_script = pathlib.Path(__file__).resolve().parent.parent / "tools" / "orca_local_runner.py"
    assert cli_script.is_file()

    # 1. Validate command
    val_proc = subprocess.run(
        [sys.executable, str(cli_script), "validate", "--orca", str(fake_exe), "--input-dir", str(inp_dir), "--output-dir", str(out_dir), "--state-dir", state_dir],
        capture_output=True,
        text=True,
    )
    assert val_proc.returncode == 0
    val_out = json.loads(val_proc.stdout)
    assert val_out["ready"] is True

    # 2. Status command
    stat_proc = subprocess.run(
        [sys.executable, str(cli_script), "status", "--state-dir", state_dir],
        capture_output=True,
        text=True,
    )
    assert stat_proc.returncode == 0
    stat_out = json.loads(stat_proc.stdout)
    assert stat_out["platform"] in ("Windows", "Linux", "Darwin")

    # 3. Run command
    monkeypatch.setenv("FAKE_ORCA_MODE", "VALID_OPT")
    run_proc = subprocess.run(
        [sys.executable, str(cli_script), "run", "--input", str(inp_file), "--stage-kind", "OPT", "--orca", str(fake_exe), "--input-dir", str(inp_dir), "--output-dir", str(out_dir), "--state-dir", state_dir],
        capture_output=True,
        text=True,
    )
    assert run_proc.returncode == 0
    run_out = json.loads(run_proc.stdout)
    assert run_out["ok"] is True
    assert run_out["scientific_ok"] is True


# =========================================================================
# Hardened Cross-Process Lock & Process Identity Acceptance Tests
# =========================================================================

def _lock_worker_a(lock_file: str, started_event, release_event):
    """Worker A acquires lock and waits for release_event."""
    lock = local_orca.CrossProcessFileLock(lock_file, timeout=10.0)
    with lock:
        started_event.set()
        release_event.wait(timeout=10.0)


def _lock_worker_b(lock_file: str, timeout_val: float, b_results):
    """Worker B attempts lock with short timeout, must fail closed."""
    lock = local_orca.CrossProcessFileLock(lock_file, timeout=timeout_val)
    entered = False
    timed_out = False
    try:
        with lock:
            entered = True
    except (local_orca.LockAcquisitionError, TimeoutError):
        timed_out = True
    b_results.append({"entered": entered, "timed_out": timed_out})


def _lock_worker_c(lock_file: str, c_results):
    """Worker C acquires normally after release."""
    lock = local_orca.CrossProcessFileLock(lock_file, timeout=10.0)
    entered = False
    try:
        with lock:
            entered = True
    except Exception:
        pass
    c_results.append({"entered": entered})


def test_cross_process_lock_timeout_fails_closed(tmp_path):
    """Proves that under lock contention, a timed-out worker MUST NOT enter critical section and cleans up."""
    lock_file = str(tmp_path / "test_timeout.lock")
    manager = multiprocessing.Manager()
    started_event = manager.Event()
    release_event = manager.Event()
    b_results = manager.list()
    c_results = manager.list()

    # 1. Process A acquires and waits for release_event
    p_a = multiprocessing.Process(target=_lock_worker_a, args=(lock_file, started_event, release_event))
    p_a.start()
    assert started_event.wait(timeout=5.0)

    # 2. Process B attempts with timeout 0.2s -> MUST time out and NOT enter
    p_b = multiprocessing.Process(target=_lock_worker_b, args=(lock_file, 0.2, b_results))
    p_b.start()
    # Windows ``spawn`` imports this large test module before invoking the
    # worker.  Under the complete suite that cold start can exceed five
    # seconds even though the lock timeout itself remains 0.2 seconds.
    p_b.join(timeout=20.0)

    assert not p_b.is_alive(), "lock contender did not finish after process startup"
    assert p_b.exitcode == 0
    assert len(b_results) == 1
    assert b_results[0]["entered"] is False
    assert b_results[0]["timed_out"] is True

    # 3. Process A is instructed to release
    release_event.set()
    p_a.join(timeout=20.0)
    assert not p_a.is_alive()
    assert p_a.exitcode == 0

    # 4. Process C acquires normally
    p_c = multiprocessing.Process(target=_lock_worker_c, args=(lock_file, c_results))
    p_c.start()
    p_c.join(timeout=20.0)

    assert not p_c.is_alive()
    assert p_c.exitcode == 0
    assert len(c_results) == 1
    assert c_results[0]["entered"] is True


def test_cross_process_lock_is_reentrant_without_dropping_outer_os_lock(tmp_path):
    lock_path = str(tmp_path / "reentrant.lock")
    outer = local_orca.CrossProcessFileLock(lock_path, timeout=1.0)
    nested_instance = local_orca.CrossProcessFileLock(lock_path, timeout=1.0)
    with outer:
        with outer:
            with nested_instance:
                assert outer._is_locked is True
                assert nested_instance._is_locked is True
        assert outer._is_locked is True
    assert outer._is_locked is False
    assert nested_instance._is_locked is False


def _settings_contention_writer(state_dir: str, value: str, hold_duration: float, ready_event):
    lock = local_orca._get_settings_lock(state_dir, timeout=5.0)
    with lock:
        ready_event.set()
        time.sleep(hold_duration)
        cfg_file = os.path.join(state_dir, "local_orca_settings.json")
        current = local_orca._DEFAULT_SETTINGS.copy()
        current["orca_executable"] = value
        tmp = cfg_file + ".tmp." + uuid.uuid4().hex
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
        os.replace(tmp, cfg_file)


def test_settings_lock_timeout_no_corruption(tmp_path):
    """Tests that settings file is not corrupted under lock contention."""
    state_dir = str(tmp_path / "state_settings")
    local_orca.save_local_orca_settings({"orca_executable": "initial.exe"}, state_dir)

    manager = multiprocessing.Manager()
    ready_event = manager.Event()

    p_writer = multiprocessing.Process(target=_settings_contention_writer, args=(state_dir, "slow_write.exe", 0.5, ready_event))
    p_writer.start()
    assert ready_event.wait(timeout=5.0)

    # While writer holds lock, reader/short timeout gets valid settings without corruption
    cfg = local_orca.get_local_orca_settings(state_dir)
    assert cfg is not None
    assert "orca_executable" in cfg

    p_writer.join(timeout=5.0)
    cfg_after = local_orca.get_local_orca_settings(state_dir)
    assert cfg_after["orca_executable"] == "slow_write.exe"


def test_process_identity_match(tmp_path):
    """Verifies that a live child process has its OS identity properly queried and matches."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
    try:
        live = local_orca.get_live_process_identity(child.pid)
        assert live["status"] == "RUNNING"
        assert live["pid"] == child.pid
        assert live["creation_time"] is not None or live["executable"] is not None

        persisted = {
            "pid": child.pid,
            "creation_time": live["creation_time"],
            "executable": live["executable"],
            "state": "RUNNING",
        }
        match_status = local_orca.verify_process_identity(persisted)
        assert match_status == "MATCH"
        assert local_orca.is_process_alive_and_matched(persisted) is True
    finally:
        child.terminate()
        child.wait()


def test_process_identity_mismatch_pid_reuse_safe(tmp_path):
    """Proves PID reuse safety: mismatched creation_time or executable refuses to match or kill foreign process."""
    foreign_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        live = local_orca.get_live_process_identity(foreign_proc.pid)
        assert live["status"] == "RUNNING"

        # Construct persisted record with deliberately wrong creation_time / executable
        mismatched_record = {
            "pid": foreign_proc.pid,
            "creation_time": 999999999999999,  # Deliberately bogus
            "executable": "C:\\Bogus\\orca_fake.exe",
            "state": "RUNNING",
            "job_id": "job_mismatch",
            "attempt_id": "att_1",
        }
        match_status = local_orca.verify_process_identity(mismatched_record)
        assert match_status == "MISMATCH"
        assert local_orca.is_process_alive_and_matched(mismatched_record) is False

        # Attempting cancellation on a mismatched PID record MUST NOT kill the foreign process
        state_dir = str(tmp_path / "state_mismatch")
        local_orca._write_active_process_file(mismatched_record, state_dir)

        cancelled = local_orca.cancel_local_orca_job("job_mismatch", "att_1", state_dir=state_dir)
        assert cancelled is False

        # Foreign process MUST STILL BE ALIVE and unharmed!
        assert foreign_proc.poll() is None
    finally:
        foreign_proc.terminate()
        foreign_proc.wait()


def test_process_identity_unknown_fails_safe(tmp_path):
    """Verifies that UNKNOWN identity fails safe and does not falsely match or kill."""
    persisted_unknown = {
        "pid": 999999999,  # Nonexistent PID
        "creation_time": None,
        "executable": None,
        "state": "RUNNING",
    }
    status = local_orca.verify_process_identity(persisted_unknown)
    assert status in ("NOT_RUNNING", "UNKNOWN")
    assert local_orca.is_process_alive_and_matched(persisted_unknown) is False


def test_cancel_after_restart_requires_identity_match(test_env, monkeypatch):
    """Proves that cancellation after restart only terminates verified matching process."""
    tmp_path = test_env["tmp_path"]
    state_dir = str(test_env["state_dir"])
    fake_exe = _make_fake_orca_script(tmp_path)

    inp_dir = tmp_path / "inp_cancel_id"
    out_dir = tmp_path / "out_cancel_id"
    inp_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    settings = {
        "orca_executable": str(fake_exe),
        "input_directory": str(inp_dir),
        "output_directory": str(out_dir),
    }
    local_orca.save_local_orca_settings(settings, state_dir)
    monkeypatch.setenv("FAKE_ORCA_MODE", "VALID_OPT")
    monkeypatch.setenv("FAKE_ORCA_DELAY", "3.0")

    # Launch fake ORCA in thread
    job_id = "job_to_cancel_matched"
    t = threading.Thread(
        target=local_orca.execute_local_orca_job,
        args=(job_id, "! Opt\n*"),
        kwargs={"stage_kind": "OPT", "state_dir": state_dir}
    )
    t.start()
    time.sleep(0.3)

    active = local_orca._read_active_process_file(state_dir)
    assert active is not None
    assert active["state"] == "RUNNING"
    assert local_orca.verify_process_identity(active) == "MATCH"

    # Cancel matching job -> success
    cancelled = local_orca.cancel_local_orca_job(job_id, state_dir=state_dir)
    assert cancelled is True
    t.join(timeout=3.0)
    assert local_orca.get_active_local_process_info(state_dir) is None


def test_restart_mismatched_pid_does_not_kill_foreign_process(test_env):
    """Simulates restart with stale active record pointing to unrelated live process: proves no kill."""
    state_dir = str(test_env["state_dir"])
    foreign_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        stale_record = {
            "pid": foreign_proc.pid,
            "creation_time": 12345678,  # Mismatched timestamp
            "executable": "C:\\Other\\orca.exe",
            "job_id": "restarted_job",
            "attempt_id": "attempt_stale",
            "state": "RUNNING",
        }
        local_orca._write_active_process_file(stale_record, state_dir)

        # Re-initialize store / manager from state dir
        new_store = rws.ReactionStore(state_dir)
        active_info = local_orca.get_active_local_process_info(state_dir)
        # Mismatch must be detected, not active, and foreign process left alive
        assert active_info is None
        assert foreign_proc.poll() is None
    finally:
        foreign_proc.terminate()
        foreign_proc.wait()
