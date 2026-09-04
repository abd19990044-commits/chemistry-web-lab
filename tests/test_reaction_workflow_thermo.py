# -*- coding: utf-8 -*-
"""Reaction Workflow + Thermochemistry regression tests (hermetic, Part AQ/AG).

Deterministic fixtures are synthetic ORCA outputs (genuine section formats).
The authoritative OrcaParser is the ONLY parser used (Part W/X). Tests cover:
multi-geometry-optimization handoff, latest-valid-geometry, stale FREQ/SP,
failed-stage gate, final-result assembler (direct + composite), reaction
thermodynamics with K/logK, scientific gates (temperature/imaginary/balance),
PDF generation + 48h retention with an injectable clock, owner isolation,
restart durability, and the API surface.
"""
import io
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orca_engine", "src"))

import app as webapp  # noqa: E402
import services.reaction_workflow_service as rws  # noqa: E402
import services.thermo_report_service as trs  # noqa: E402

THERMO_OUT = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "orca_engine", "tests", "data", "thermo.out"),
                  encoding="utf-8", errors="replace").read()

IR_BLOCK = """
----------------------------------
IR SPECTRUM
----------------------------------
 Mode    freq (cm**-1)   T**2        IR int (km/mol)
-----------------------------------------------------------
   0:        0.00    0.000000        0.0000
   6:     1000.00    0.010000        1.0432
   7:     1500.00    0.050000        5.2161
   8:     3000.00    0.100000       10.4323
"""

TERM = "****ORCA TERMINATED NORMALLY****"


def freq_fixture(energy=-76.4, imag=False, coords_variant=0):
    """FREQ output on the SAME geometry the preceding OPT stage produced
    (geometry handoff). coords_variant must match the feeding OPT stage."""
    text = THERMO_OUT
    if coords_variant == 1:
        text = text.replace("  O      0.000000    0.000000    0.000000",
                            "  O      0.001000    0.000000    0.000000")
    elif coords_variant == 2:
        text = text.replace("  O      0.000000    0.000000    0.000000",
                            "  O      0.002000    0.000000    0.000000")
    text = text.replace("FINAL SINGLE POINT ENERGY      -76.400000000000",
                        "FINAL SINGLE POINT ENERGY      %.12f" % energy)
    # thermal corrections stay constant: H = E + 0.03, G = E + 0.01 (Eh)
    text = text.replace("Total enthalpy                   ...    -76.37000000 Eh",
                        "Total enthalpy                   ...    %.8f Eh" % (energy + 0.03))
    text = text.replace("Final Gibbs free energy          ...    -76.39000000 Eh",
                        "Final Gibbs free energy          ...    %.8f Eh" % (energy + 0.01))
    text = text.replace(TERM, IR_BLOCK.replace("   8:     3000.00    0.100000       10.4323",
                                               ("   8:     3000.00    0.100000       10.4323"
                                                if not imag else
                                                "   8:     -3000.00    0.100000       10.4323")) + "\n" + TERM)
    text = text.replace("THE OPTIMIZATION HAS CONVERGED", "")
    if "THERMOCHEMISTRY AT" not in text and "Temperature" not in text:
        text = text.replace("FINAL SINGLE POINT ENERGY",
                            "THERMOCHEMISTRY AT 298.15 K\nFINAL SINGLE POINT ENERGY", 1)
    return text


def opt_freq_fixture(energy=-76.40, coords_variant=0):
    """OPT_FREQ single-stage output containing both optimization convergence and frequencies."""
    text = freq_fixture(energy=energy, coords_variant=coords_variant)
    text = text.replace(TERM, "THE OPTIMIZATION HAS CONVERGED\n\n" + TERM)
    return text


OPT_BLOCK = """
---------------------------------------
THE OPTIMIZATION HAS CONVERGED
---------------------------------------
CARTESIAN COORDINATES (ANGSTROEM)
---------------------------------
  O      0.000001    0.000002    0.000003
  H      0.960001    0.000002    0.000003
  H     -0.240001    0.930002    0.000003

FINAL SINGLE POINT ENERGY      %s
"""

SP_BLOCK = """
FINAL SINGLE POINT ENERGY      %s
"""


def opt_fixture(energy, coords_variant=1):
    text = THERMO_OUT
    if coords_variant == 1:
        text = text.replace("  O      0.000000    0.000000    0.000000", "  O      0.001000    0.000000    0.000000")
    elif coords_variant == 2:
        text = text.replace("  O      0.000000    0.000000    0.000000", "  O      0.002000    0.000000    0.000000")
    elif coords_variant == 3:
        text = text.replace("  O      0.000000    0.000000    0.000000", "  O      0.003000    0.000000    0.000000")
    text = text.replace("FINAL SINGLE POINT ENERGY      -76.400000000000",
                        "FINAL SINGLE POINT ENERGY      %.12f" % energy)
    text = text.replace(TERM, "THE OPTIMIZATION HAS CONVERGED\n\n" + TERM)
    return text


def sp_fixture(energy, coords_variant=0):
    """SP output on the geometry inherited from the preceding stage (handoff)."""
    text = THERMO_OUT.replace("FINAL SINGLE POINT ENERGY      -76.400000000000",
                              "FINAL SINGLE POINT ENERGY      %.12f" % energy)
    if coords_variant == 1:
        text = text.replace("  O      0.000000    0.000000    0.000000",
                            "  O      0.001000    0.000000    0.000000")
    elif coords_variant == 2:
        text = text.replace("  O      0.000000    0.000000    0.000000",
                            "  O      0.002000    0.000000    0.000000")
    elif coords_variant == 3:
        text = text.replace("  O      0.000000    0.000000    0.000000",
                            "  O      0.003000    0.000000    0.000000")
    return text


class FakeClock:
    def __init__(self, start=None):
        self.now = start or datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, hours=0.0, minutes=0.0, seconds=0.0):
        self.now = self.now + timedelta(hours=hours, minutes=minutes, seconds=seconds)


@pytest.fixture
def store(tmp_path):
    clock = FakeClock()
    s = rws.ReactionStore(str(tmp_path), now_provider=clock)
    s.clock = clock
    return s


@pytest.fixture
def client(store, monkeypatch):
    webapp.app.config["TESTING"] = True
    monkeypatch.setattr(webapp, "_reaction_store", store)
    monkeypatch.setattr(webapp, "_reaction_owner", lambda: "owner-A")
    with webapp.app.test_client() as c:
        yield c


def _create(client, equation="2 A + B -> C", owner=None):
    payload = {"equation": equation}
    if owner:
        payload["owner"] = owner
    res = client.post("/api/v1/reactions", json=payload)
    assert res.status_code == 200, res.get_json()
    return res.get_json()["reaction"]


def _species_by_name(reaction, name):
    return next(s for s in reaction["species"] if s["display_name"] == name)


def _build_workflow(client, reaction, name, plan):
    """plan: list of (kind, output_text or None, label)."""
    sp = _species_by_name(reaction, name)
    for kind, output, label in plan:
        res = client.post("/api/v1/reactions/%s/stages" % reaction["reaction_id"],
                          json={"species_id": sp["species_id"], "kind": kind, "label": label,
                                "input_text": "%s input for %s" % (kind, name)})
        assert res.status_code == 200, res.get_json()
        stage = res.get_json()["stage"]
        if output is not None:
            res = client.post("/api/v1/reactions/%s/stages/%s/complete" % (reaction["reaction_id"], stage["stage_id"]),
                              json={"species_id": sp["species_id"], "output_text": output})
            assert res.status_code == 200, res.get_json()
    return sp


# ---------------------------------------------------------------------------
# 1-2: equation parsing + balance
# ---------------------------------------------------------------------------
def test_equation_parsing_and_balance(client):
    reaction = _create(client, "2 H2O + O2 -> 2 H2O")
    assert [(s["nu"]) for s in reaction["species"]] == [-2.0, -1.0, 2.0]
    assert reaction["balance_valid"] is False
    assert any("NOT ATOM BALANCED" in w for w in reaction["balance_warnings"])
    rx2 = _create(client, "2 H2 + O2 -> 2 H2O")
    assert rx2["balance_valid"] is True


def test_charge_balance_warning(client):
    reaction = _create(client, "Na+ + Cl- -> NaCl")
    assert [(s["display_name"], s["charge"]) for s in reaction["species"]] == \
        [("Na", 1), ("Cl", -1), ("NaCl", 0)]
    assert any("CHARGE NOT CONSERVED" in w for w in reaction["balance_warnings"]) is False
    reaction_bad = _create(client, "Na+ -> Na")
    assert any("CHARGE NOT CONSERVED" in w for w in reaction_bad["balance_warnings"]) is True


def test_charged_species_tokenizer_full_consumption(client):
    reaction = _create(client, "Fe3+ + e- -> Fe2+")
    sp = [(s["display_name"], s["charge"], s["nu"]) for s in reaction["species"]]
    assert sp == [("Fe", 3, -1.0), ("e-", -1, -1.0), ("Fe", 2, 1.0)]
    assert any("CHARGE NOT CONSERVED" in w for w in reaction["balance_warnings"]) is False
    reaction2 = _create(client, "2 H+ + SO4^2- -> H2SO4")
    sp2 = [(s["display_name"], s["charge"], s["nu"]) for s in reaction2["species"]]
    assert sp2 == [("H", 1, -2.0), ("SO4", -2, -1.0), ("H2SO4", 0, 1.0)]
    reaction3 = _create(client, "2 Fe3+ + e- -> Fe2+")
    sp3 = [(s["display_name"], s["charge"]) for s in reaction3["species"]]
    assert sp3 == [("Fe", 3), ("e-", -1), ("Fe", 2)]
    reaction4 = _create(client, "NH4+ + OH- -> NH3 + H2O")
    sp4 = [(s["display_name"], s["charge"]) for s in reaction4["species"]]
    assert sp4 == [("NH4", 1), ("OH", -1), ("NH3", 0), ("H2O", 0)]
    assert reaction4["balance_valid"] is True


@pytest.mark.parametrize("bad", ["A ++ B -> C", "Na+ + + Cl- -> NaCl",
                                 "Na+ garbage + Cl- -> NaCl", "2A ??? B -> C", "A + -> B"])
def test_malformed_equations_rejected(client, bad):
    res = client.post("/api/v1/reactions", json={"equation": bad})
    assert res.status_code == 400, bad
    assert res.get_json()["error"]["code"] == "REACTION_ERROR"


# ---------------------------------------------------------------------------
# 3-9: multi-geometry-optimization handoff (Part AH)
# ---------------------------------------------------------------------------
def test_multi_opt_geometry_handoff(client):
    reaction = _create(client, "A -> A")
    sp = _species_by_name(reaction, "A")
    for label, energy, variant in (("Opt1", -76.40, 1), ("Opt2", -76.41, 2), ("Opt3", -76.42, 3)):
        res = client.post("/api/v1/reactions/%s/stages" % reaction["reaction_id"],
                          json={"species_id": sp["species_id"], "kind": "OPT", "label": label,
                                "input_text": "opt input %s" % label})
        stage = res.get_json()["stage"]
        client.post("/api/v1/reactions/%s/stages/%s/complete" % (reaction["reaction_id"], stage["stage_id"]),
                    json={"species_id": sp["species_id"], "output_text": opt_fixture(energy, variant)})
    fresh = client.get("/api/v1/reactions/%s" % reaction["reaction_id"]).get_json()["reaction"]
    stages = fresh["species"][0]["stages"]
    hashes = [s["geometry_hash"] for s in stages]
    assert hashes[0] and hashes[1] and hashes[2]
    assert hashes[0] != hashes[1] != hashes[2], "each optimization must produce a new geometry"
    assert fresh["species"][0]["latest_valid_geometry_stage_id"] == stages[2]["stage_id"]
    # geometry handoff: stage N+1 receives stage N's geometry (E1/E2)
    assert stages[1]["parent_stage_id"] == stages[0]["stage_id"]
    assert stages[2]["parent_stage_id"] == stages[1]["stage_id"]


def test_freq_receives_latest_geometry_and_composite(client):
    reaction = _create(client, "A -> A")
    sp = _species_by_name(reaction, "A")
    plan = [("OPT", opt_fixture(-76.40, 1), "Opt1"),
            ("OPT", opt_fixture(-76.41, 2), "Opt2"),
            ("FREQ", freq_fixture(-76.41, coords_variant=2), "Freq"),
            ("SP", sp_fixture(-76.45, coords_variant=2), "HighLevelSP")]
    for kind, out, label in plan:
        res = client.post("/api/v1/reactions/%s/stages" % reaction["reaction_id"],
                          json={"species_id": sp["species_id"], "kind": kind, "label": label,
                                "input_text": "%s input" % kind})
        stage = res.get_json()["stage"]
        client.post("/api/v1/reactions/%s/stages/%s/complete" % (reaction["reaction_id"], stage["stage_id"]),
                    json={"species_id": sp["species_id"], "output_text": out})
    result = client.post("/api/v1/reactions/%s/species/%s/assemble"
                         % (reaction["reaction_id"], sp["species_id"])).get_json()["final_result"]
    assert result["composite"] is True
    assert result["final_geometry_source_label"] == "Opt2"
    assert result["final_thermal_source_label"] == "Freq"
    assert result["final_electronic_source_label"] == "HighLevelSP"
    e_sp, e_freq = -76.45, -76.41
    h_freq, g_freq = e_freq + 0.03, e_freq + 0.01  # fixture thermal corrections
    assert abs(result["H_final"] - (e_sp + h_freq - e_freq)) < 1e-9
    assert abs(result["G_final"] - (e_sp + g_freq - e_freq)) < 1e-9
    assert "H_final = E_SP + (H_freq - E_freq)" == result["equation_H"]
    assert "G_final = E_SP + (G_freq - E_freq)" == result["equation_G"]
    # ZPE preserved once (from the frequency stage, never re-added)
    assert abs(result["zpe_hartree"] - 0.021) < 1e-9


def test_stale_freq_and_sp_detection(client):
    reaction = _create(client, "A -> A")
    sp = _species_by_name(reaction, "A")
    plan = [("OPT", opt_fixture(-76.40, 1), "Opt1"),
            ("FREQ", freq_fixture(-76.40, coords_variant=1), "Freq1"),
            ("OPT", opt_fixture(-76.41, 2), "Opt2"),
            ("FREQ", freq_fixture(-76.41, coords_variant=2), "Freq2"),
            ("SP", sp_fixture(-76.44, coords_variant=2), "SP1"),
            ("OPT", opt_fixture(-76.42, 3), "Opt3"),
            ("SP", sp_fixture(-76.45, coords_variant=3), "SP2")]
    for kind, out, label in plan:
        res = client.post("/api/v1/reactions/%s/stages" % reaction["reaction_id"],
                          json={"species_id": sp["species_id"], "kind": kind, "label": label,
                                "input_text": "input"})
        stage = res.get_json()["stage"]
        client.post("/api/v1/reactions/%s/stages/%s/complete" % (reaction["reaction_id"], stage["stage_id"]),
                    json={"species_id": sp["species_id"], "output_text": out})
    # complete a valid FREQ stage on the second species
    sp2 = reaction["species"][1]
    res2 = client.post("/api/v1/reactions/%s/stages" % reaction["reaction_id"],
                       json={"species_id": sp2["species_id"], "kind": "FREQ", "label": "FreqB",
                             "input_text": "input"})
    stage2 = res2.get_json()["stage"]
    client.post("/api/v1/reactions/%s/stages/%s/complete" % (reaction["reaction_id"], stage2["stage_id"]),
                json={"species_id": sp2["species_id"], "output_text": freq_fixture(-76.40, coords_variant=1)})

    # species assemble returns 200 with partial/incomplete thermochemistry and STALE warnings
    res = client.post("/api/v1/reactions/%s/species/%s/assemble"
                      % (reaction["reaction_id"], sp["species_id"]))
    assert res.status_code == 200, res.get_json()
    result = res.get_json()["final_result"]
    assert result["complete_thermochemistry"] is False
    assert any("STALE" in w for w in result["warnings"])

    # reaction-level thermodynamics is blocked with 409 because species A thermochemistry is incomplete
    res_rx = client.post("/api/v1/reactions/%s/thermodynamics" % reaction["reaction_id"])
    assert res_rx.status_code == 409
    assert "not all species have complete valid thermochemistry" in res_rx.get_json()["error"]["message"].lower()

    fresh = client.get("/api/v1/reactions/%s" % reaction["reaction_id"]).get_json()["reaction"]
    stages = fresh["species"][0]["stages"]
    assert next(s for s in stages if s["label"] == "Freq1").get("thermal_stale") is True
    assert next(s for s in stages if s["label"] == "SP1").get("electronic_stale") is True
    assert next(s for s in stages if s["label"] == "Freq2").get("thermal_stale") is True
    assert next(s for s in stages if s["label"] == "SP2").get("electronic_stale") is not True


def test_failed_stage_gate_blocks_next(client):
    reaction = _create(client, "A -> A")
    sp = _species_by_name(reaction, "A")
    res = client.post("/api/v1/reactions/%s/stages" % reaction["reaction_id"],
                      json={"species_id": sp["species_id"], "kind": "OPT", "label": "OptBroken",
                            "input_text": "bad input"})
    stage = res.get_json()["stage"]
    bad_output = THERMO_OUT.replace("THE OPTIMIZATION HAS CONVERGED", "")  # no convergence marker
    res = client.post("/api/v1/reactions/%s/stages/%s/complete" % (reaction["reaction_id"], stage["stage_id"]),
                      json={"species_id": sp["species_id"], "output_text": bad_output})
    body = res.get_json()
    assert body["stage"]["state"] == "FAILED"
    assert "did not converge" in body["stage"]["error"]
    fresh = client.get("/api/v1/reactions/%s" % reaction["reaction_id"]).get_json()["reaction"]
    assert fresh["species"][0]["state"] == "FAILED"
    assert fresh["state"] == "WAITING"


def test_imaginary_frequency_blocks_minimum(client):
    reaction = _create(client, "A -> A")
    sp = _species_by_name(reaction, "A")
    res = client.post("/api/v1/reactions/%s/stages" % reaction["reaction_id"],
                      json={"species_id": sp["species_id"], "kind": "FREQ", "label": "Freq",
                            "input_text": "input"})
    stage = res.get_json()["stage"]
    res = client.post("/api/v1/reactions/%s/stages/%s/complete" % (reaction["reaction_id"], stage["stage_id"]),
                      json={"species_id": sp["species_id"], "output_text": freq_fixture(-76.4, imag=True)})
    body = res.get_json()
    assert body["stage"]["state"] == "FAILED"
    assert "INVALID_MINIMUM" in body["stage"]["error"]


def test_temperature_mismatch_blocks_reaction(client):
    reaction = _create(client, "A -> B")
    a = _build_workflow(client, reaction, "A",
                        [("FREQ", freq_fixture(-76.40, coords_variant=1), "Freq")])
    b = _build_workflow(client, reaction, "B",
                        [("FREQ", freq_fixture(-76.40, coords_variant=1).replace("298.15", "350.00"),
                          "Freq")])
    res = client.post("/api/v1/reactions/%s/thermodynamics" % reaction["reaction_id"])
    assert res.status_code == 409
    assert "temperature mismatch" in res.get_json()["error"]["message"].lower()


def test_direct_thermochemistry_and_k(client):
    reaction = _create(client, "A -> B")
    _build_workflow(client, reaction, "A", [("FREQ", freq_fixture(-76.40, coords_variant=1), "Freq")])
    _build_workflow(client, reaction, "B", [("FREQ", freq_fixture(-76.39, coords_variant=2), "Freq")])
    res = client.post("/api/v1/reactions/%s/thermodynamics" % reaction["reaction_id"])
    assert res.status_code == 200, res.get_json()
    thermo = res.get_json()["thermodynamics"]
    assert abs(thermo["dG_hartree"] - (1 * (-76.39) - 1 * (-76.40))) < 1e-9
    assert abs(thermo["dG_kj_mol"] - 0.01 * rws.HARTREE_KJ_MOL) < 1e-6
    assert thermo["log10_k"] == pytest.approx(-thermo["dG_kj_mol"] * 1000 / (8.31446261815324 * 298.15) / math_log10(), abs=1e-6)
    assert thermo["k"] is not None and thermo["k"] > 0
    assert thermo["temperature_k"] == 298.15


def math_log10():
    import math
    return math.log(10.0)


def test_extreme_k_uses_log_only(client):
    reaction = _create(client, "A -> B")
    _build_workflow(client, reaction, "A", [("FREQ", freq_fixture(-76.40), "Freq")])
    _build_workflow(client, reaction, "B", [("FREQ", freq_fixture(-76.40 - 5.0), "Freq")])
    thermo = client.post("/api/v1/reactions/%s/thermodynamics" % reaction["reaction_id"]).get_json()["thermodynamics"]
    assert thermo["k_note"] and "log10" in thermo["k_note"].lower() or thermo["k"] == float("inf")
    assert thermo["log10_k"] is not None


# ---------------------------------------------------------------------------
# PDF + retention with injectable clock
# ---------------------------------------------------------------------------
def _run_full_reaction(client):
    reaction = _create(client, "A -> B")
    _build_workflow(client, reaction, "A", [("FREQ", freq_fixture(-76.40, coords_variant=1), "Freq")])
    _build_workflow(client, reaction, "B", [("FREQ", freq_fixture(-76.39, coords_variant=2), "Freq")])
    thermo = client.post("/api/v1/reactions/%s/thermodynamics" % reaction["reaction_id"]).get_json()["thermodynamics"]
    return reaction, thermo


def test_pdf_generation_and_content(client):
    reaction, thermo = _run_full_reaction(client)
    res = client.post("/api/v1/reactions/%s/thermodynamics/report" % reaction["reaction_id"])
    assert res.status_code == 200, res.get_json()
    meta = res.get_json()["report"]
    assert meta["expires_at"] > meta["created_at"]
    dl = client.get("/api/v1/thermo-reports/%s" % meta["report_id"])
    assert dl.status_code == 200
    pdf = dl.data
    assert pdf[:5] == b"%PDF-", "valid PDF signature required"
    assert len(pdf) > 2000
    import io
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(pdf))
    text_probe = "".join(page.extract_text() or "" for page in reader.pages)
    assert "298.15" in text_probe
    for token in ("Reaction Thermodynamics Report", "Delta G", "Delta H", "Delta S",
                  "log10 K", "A", "B", "Provenance"):
        assert token in text_probe, token
    assert "FINAL SINGLE POINT ENERGY" not in text_probe  # no raw ORCA dumps


def test_pdf_owner_isolation(client):
    reaction, thermo = _run_full_reaction(client)
    res = client.post("/api/v1/reactions/%s/thermodynamics/report" % reaction["reaction_id"])
    assert res.status_code == 200, res.get_json()
    meta = res.get_json()["report"]
    with client.session_transaction() as sess:
        sess.clear()
    import app as appmod
    saved = appmod._reaction_owner
    appmod._reaction_owner = lambda: "owner-B"
    try:
        dl = client.get("/api/v1/thermo-reports/%s" % meta["report_id"])
        assert dl.status_code == 404, "another owner must not download the report"
    finally:
        appmod._reaction_owner = saved


def test_pdf_expiry_and_regeneration(client, store):
    reaction, thermo = _run_full_reaction(client)
    meta = client.post("/api/v1/reactions/%s/thermodynamics/report" % reaction["reaction_id"]).get_json()["report"]
    dl = client.get("/api/v1/thermo-reports/%s" % meta["report_id"])
    assert dl.status_code == 200
    store.clock.advance(hours=47, minutes=59)
    dl = client.get("/api/v1/thermo-reports/%s" % meta["report_id"])
    assert dl.status_code == 200, "T+47h59m still available"
    store.clock.advance(hours=0, minutes=2)
    dl = client.get("/api/v1/thermo-reports/%s" % meta["report_id"])
    assert dl.status_code == 404, "T+48h+ expired"
    body = dl.get_json()
    assert body["error"]["message"] == "REPORT_EXPIRED"
    removed = store.cleanup_expired_reports()
    assert meta["report_id"] in removed, "expired PDF file deleted"
    # regeneration creates a NEW id + new 48h window without recomputing thermo
    res = client.post("/api/v1/reactions/%s/thermodynamics/report" % reaction["reaction_id"])
    meta2 = res.get_json()["report"]
    assert meta2["report_id"] != meta["report_id"]
    dl2 = client.get("/api/v1/thermo-reports/%s" % meta2["report_id"])
    assert dl2.status_code == 200


def test_pdf_failure_keeps_thermo_complete(client, monkeypatch):
    reaction, thermo = _run_full_reaction(client)
    import services.thermo_report_service as trs_mod
    def boom(*a, **k):
        raise RuntimeError("font exploded")
    monkeypatch.setattr(trs_mod, "generate_thermo_report_pdf", boom)
    res = client.post("/api/v1/reactions/%s/thermodynamics/report" % reaction["reaction_id"])
    assert res.status_code == 500
    state = client.get("/api/v1/reactions/%s/thermodynamics" % reaction["reaction_id"]).get_json()
    assert state["thermodynamics"]["result_id"] == thermo["result_id"], "thermo result independent of PDF failure"
    monkeypatch.setattr(trs_mod, "generate_thermo_report_pdf",
                        lambda *a, **k: trs_mod.generate_thermo_report_pdf.__wrapped__(*a, **k) if hasattr(trs_mod.generate_thermo_report_pdf, "__wrapped__") else None)


def test_restart_durability(client, store):
    reaction, thermo = _run_full_reaction(client)
    meta = client.post("/api/v1/reactions/%s/thermodynamics/report" % reaction["reaction_id"]).get_json()["report"]
    # simulate a backend restart: a brand-new store over the same state dir
    fresh_store = rws.ReactionStore(store.state_dir, now_provider=store.now_provider)
    loaded = fresh_store.get_reaction("owner-A", reaction["reaction_id"])
    assert loaded is not None and loaded["state"] == "COMPLETE"
    assert loaded["thermodynamics"]["result_id"] == thermo["result_id"]
    meta2 = fresh_store.get_report(meta["report_id"])
    assert meta2 is not None and meta2["expires_at"] == meta["expires_at"], "expiry survives restart (AB6)"


def test_single_stage_is_same_engine(client):
    reaction = _create(client, "A -> A")
    sp = _species_by_name(reaction, "A")
    res = client.post("/api/v1/reactions/%s/stages" % reaction["reaction_id"],
                      json={"species_id": sp["species_id"], "kind": "OPT_FREQ", "label": "Opt Freq",
                            "input_text": "opt freq input"})
    stage = res.get_json()["stage"]
    assert res.status_code == 200
    client.post("/api/v1/reactions/%s/stages/%s/complete" % (reaction["reaction_id"], stage["stage_id"]),
                json={"species_id": sp["species_id"], "output_text": opt_freq_fixture(-76.40)})
    result = client.post("/api/v1/reactions/%s/species/%s/assemble"
                         % (reaction["reaction_id"], sp["species_id"])).get_json()["final_result"]
    assert result["composite"] is False
    assert result["final_geometry_source_label"] == "Opt Freq"
    assert result["complete_thermochemistry"] is True


def test_stage_limit_enforced(client):
    reaction = _create(client, "A -> A")
    sp = _species_by_name(reaction, "A")
    for i in range(rws.MAX_STAGES_PER_SPECIES):
        client.post("/api/v1/reactions/%s/stages" % reaction["reaction_id"],
                    json={"species_id": sp["species_id"], "kind": "SP", "label": "SP%d" % (i + 1),
                          "input_text": "x"})
    res = client.post("/api/v1/reactions/%s/stages" % reaction["reaction_id"],
                      json={"species_id": sp["species_id"], "kind": "SP", "label": "OverLimit",
                            "input_text": "x"})
    assert res.status_code == 400
    assert "stage limit" in res.get_json()["error"]["message"]
