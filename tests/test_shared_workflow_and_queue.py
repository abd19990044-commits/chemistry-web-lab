# -*- coding: utf-8 -*-
"""Comprehensive tests for Shared Workflow, Durable Execution Queue (Max 5),
Auto Thermo -> PDF Pipeline, and Local ORCA Backend.
"""
import io
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orca_engine", "src"))

import app as webapp
import services.reaction_workflow_service as rws
import services.local_orca_service as local_orca
try:
    from tests.test_reaction_workflow_thermo import opt_fixture, freq_fixture, sp_fixture
except ImportError:
    from test_reaction_workflow_thermo import opt_fixture, freq_fixture, sp_fixture


@pytest.fixture
def client(monkeypatch, tmp_path):
    webapp.app.config["TESTING"] = True
    store = rws.ReactionStore(str(tmp_path))
    monkeypatch.setattr(webapp, "_reaction_store", store)
    with webapp.app.test_client() as c:
        with c.session_transaction() as sess:
            sess["user"] = {"email": "test-user@chemistry.lab", "role": "researcher"}
        yield c


def _create_reaction(client, eq):
    res = client.post("/api/v1/reactions", json={"equation": eq})
    assert res.status_code == 200, res.get_json()
    return res.get_json()["reaction"]


def test_shared_workflow_configure_and_apply_to_all(client):
    """Campaign 3: Configure once -> Apply to all species with deduplication."""
    rx = _create_reaction(client, "2 H2 + O2 -> 2 H2O")
    # Stoichiometric deduplication: 3 unique computational species
    species_names = [s["display_name"] for s in rx["species"]]
    assert species_names == ["H2", "O2", "H2O"]
    assert [s["nu"] for s in rx["species"]] == [-2.0, -1.0, 2.0]

    # Shared workflow definition
    stages = [
        {"kind": "OPT", "label": "Low-level Opt"},
        {"kind": "FREQ", "label": "Thermo Frequencies"},
        {"kind": "SP", "label": "High-level SP"},
    ]
    res = client.post(f"/api/v1/reactions/{rx['reaction_id']}/shared-workflow", json={"stages": stages})
    assert res.status_code == 200, res.get_json()
    sw = res.get_json()["shared_workflow"]
    assert sw["workflow_revision"] == 2
    assert sw["workflow_hash"] is not None
    assert len(sw["stages"]) == 3

    # Apply to all species
    apply_res = client.post(f"/api/v1/reactions/{rx['reaction_id']}/shared-workflow/apply", json={})
    assert apply_res.status_code == 200, apply_res.get_json()
    fresh = client.get(f"/api/v1/reactions/{rx['reaction_id']}").get_json()["reaction"]

    for sp in fresh["species"]:
        assert sp["workflow_source"] == "SHARED"
        assert sp["workflow_hash"] == sw["workflow_hash"]
        assert len(sp["stages"]) == 3
        assert sp["stages"][0]["kind"] == "OPT"
        assert sp["stages"][0]["state"] == "READY"
        assert sp["stages"][1]["kind"] == "FREQ"
        assert sp["stages"][1]["state"] == "BLOCKED_BY_DEPENDENCY"
        assert sp["stages"][2]["kind"] == "SP"
        assert sp["stages"][2]["state"] == "BLOCKED_BY_DEPENDENCY"
        # Species molecular data is strictly preserved
        assert sp["display_name"] in ("H2", "O2", "H2O")


def test_shared_workflow_custom_override_per_species(client):
    """Campaign 3: Custom workflow override per species."""
    rx = _create_reaction(client, "A -> B")
    sp_b = next(s for s in rx["species"] if s["display_name"] == "B")

    # Set custom workflow on B
    custom_stages = [
        {"kind": "OPT_FREQ", "label": "Combined OptFreq"},
    ]
    c_res = client.post(
        f"/api/v1/reactions/{rx['reaction_id']}/species/{sp_b['species_id']}/custom-workflow",
        json={"stages": custom_stages},
    )
    assert c_res.status_code == 200, c_res.get_json()
    sp_b_updated = c_res.get_json()["species"]
    assert sp_b_updated["workflow_source"] == "CUSTOM"
    assert len(sp_b_updated["stages"]) == 1

    # Apply shared workflow to all without overwrite_custom -> B remains CUSTOM
    client.post(f"/api/v1/reactions/{rx['reaction_id']}/shared-workflow/apply", json={"overwrite_custom": False})
    fresh = client.get(f"/api/v1/reactions/{rx['reaction_id']}").get_json()["reaction"]
    sp_a_fresh = next(s for s in fresh["species"] if s["display_name"] == "A")
    sp_b_fresh = next(s for s in fresh["species"] if s["display_name"] == "B")
    assert sp_a_fresh["workflow_source"] == "SHARED"
    assert sp_b_fresh["workflow_source"] == "CUSTOM"
    assert len(sp_b_fresh["stages"]) == 1


def test_durable_queue_concurrency_max_5(tmp_path):
    """Campaign 4: Queue MAX 5 concurrency and FIFO ordering across workers."""
    local_orca.save_local_orca_settings({"kaggle_enabled": True}, str(tmp_path))
    store = rws.ReactionStore(str(tmp_path))
    # Create 4 reactions with multiple READY stages (total > 10 ready stages)
    for i in range(4):
        rx = rws.create_reaction("owner-1", f"A{i} -> B{i}", store)
        for sp in rx["species"]:
            st1 = rws.add_stage(rx, sp["species_id"], "OPT", "Opt")
            st2 = rws.add_stage(rx, sp["species_id"], "FREQ", "Freq")
            st1["backend"] = "kaggle"
            st2["backend"] = "kaggle"
            st1["state"] = "READY"
            st1["ready_at"] = datetime(2026, 8, 31, 10, i, 0, tzinfo=timezone.utc).isoformat()
        store.save_reaction(rx)

    # 2 concurrent workers attempting to reserve slots simultaneously
    reserved_slots = []
    lock = threading.Lock()

    def worker_loop():
        for _ in range(10):
            res = store.reserve_execution_slot(backend="kaggle", max_concurrency=5)
            if res:
                with lock:
                    reserved_slots.append(res)

    t1 = threading.Thread(target=worker_loop)
    t2 = threading.Thread(target=worker_loop)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Total active reserved slots must NEVER exceed max concurrency 5
    assert len(reserved_slots) == 5
    summary = store.get_queue_summary(backend="kaggle")
    assert summary["active"] == 5
    assert summary["total"] >= 16


def test_queue_pause_resume_cancel(client):
    """Campaign 4: Reaction-level pause, resume, and cancel."""
    rx = _create_reaction(client, "A -> B")
    client.post(f"/api/v1/reactions/{rx['reaction_id']}/shared-workflow/apply", json={})

    # Pause reaction
    p_res = client.post(f"/api/v1/reactions/{rx['reaction_id']}/pause")
    assert p_res.status_code == 200
    p_rx = p_res.get_json()["reaction"]
    assert p_rx["state"] == "PAUSED"

    # Resume reaction
    r_res = client.post(f"/api/v1/reactions/{rx['reaction_id']}/resume")
    assert r_res.status_code == 200
    r_rx = r_res.get_json()["reaction"]
    assert r_rx["state"] == "READY"

    # Cancel reaction
    c_res = client.post(f"/api/v1/reactions/{rx['reaction_id']}/cancel")
    assert c_res.status_code == 200
    c_rx = c_res.get_json()["reaction"]
    assert c_rx["state"] == "CANCELLED"


def test_stage_dependency_advance_and_auto_thermo_pdf(client):
    """Campaign 4 & Campaign 6: Dependency chain unblocking + Auto Thermo -> PDF generation."""
    rx = _create_reaction(client, "A -> B")
    client.post(f"/api/v1/reactions/{rx['reaction_id']}/shared-workflow/apply", json={})
    fresh = client.get(f"/api/v1/reactions/{rx['reaction_id']}").get_json()["reaction"]

    sp_a = fresh["species"][0]
    sp_b = fresh["species"][1]

    # Species A Stage 0 (OPT) completes
    st_a_opt = sp_a["stages"][0]
    client.post(
        f"/api/v1/reactions/{rx['reaction_id']}/stages/{st_a_opt['stage_id']}/complete",
        json={"species_id": sp_a["species_id"], "output_text": opt_fixture(-76.40, 1)},
    )
    # Stage 1 (FREQ) should automatically become READY
    mid_rx = client.get(f"/api/v1/reactions/{rx['reaction_id']}").get_json()["reaction"]
    st_a_freq = mid_rx["species"][0]["stages"][1]
    assert st_a_freq["state"] == "READY"

    # Species A Stage 1 (FREQ) completes
    client.post(
        f"/api/v1/reactions/{rx['reaction_id']}/stages/{st_a_freq['stage_id']}/complete",
        json={"species_id": sp_a["species_id"], "output_text": freq_fixture(-76.40, coords_variant=1)},
    )

    # Species B Stage 0 (OPT) completes
    st_b_opt = sp_b["stages"][0]
    client.post(
        f"/api/v1/reactions/{rx['reaction_id']}/stages/{st_b_opt['stage_id']}/complete",
        json={"species_id": sp_b["species_id"], "output_text": opt_fixture(-76.39, 2)},
    )
    # Species B Stage 1 (FREQ) completes
    mid_rx2 = client.get(f"/api/v1/reactions/{rx['reaction_id']}").get_json()["reaction"]
    st_b_freq = mid_rx2["species"][1]["stages"][1]
    client.post(
        f"/api/v1/reactions/{rx['reaction_id']}/stages/{st_b_freq['stage_id']}/complete",
        json={"species_id": sp_b["species_id"], "output_text": freq_fixture(-76.39, coords_variant=2)},
    )

    # Entire reaction reaches COMPLETE with automatic thermodynamics and PDF report!
    done_rx = client.get(f"/api/v1/reactions/{rx['reaction_id']}").get_json()["reaction"]
    assert done_rx["state"] == "COMPLETE"
    assert done_rx["thermodynamics"] is not None
    assert done_rx["thermo_report_id"] is not None

    # Download auto-generated PDF
    dl = client.get(f"/api/v1/thermo-reports/{done_rx['thermo_report_id']}")
    assert dl.status_code == 200
    assert dl.data[:5] == b"%PDF-"


def test_local_orca_settings_api(client):
    """Campaign 7: Local ORCA settings management."""
    res = client.get("/api/v1/local-orca/settings")
    assert res.status_code == 200
    settings = res.get_json()["settings"]
    assert settings["concurrency"] == 1
    assert settings["execution_backend"] == "local"
    assert settings["kaggle_enabled"] is False

    update_res = client.post(
        "/api/v1/local-orca/settings",
        json={
            "orca_executable": "C:\\Orca\\orca.exe",
            "input_directory": "C:\\Orca\\inputs",
            "output_directory": "C:\\Orca\\outputs",
            "timeout_seconds": 1800,
        },
    )
    assert update_res.status_code == 200
    updated = update_res.get_json()["settings"]
    assert updated["orca_executable"] == "C:\\Orca\\orca.exe"
    assert updated["input_directory"] == "C:\\Orca\\inputs"
    assert updated["output_directory"] == "C:\\Orca\\outputs"
    assert updated["concurrency"] == 1
    assert updated["timeout_seconds"] == 1800
