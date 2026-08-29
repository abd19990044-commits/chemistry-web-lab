# -*- coding: utf-8 -*-
"""Unified ORCA multi-output import regression tests (IR FREQ + UV TD-DFT).

Covers the /api/orca/engine/import-orca-output endpoint:
- genuine FREQ/IR outputs load with immutable raw modes;
- SP-only / Opt-only / malformed / empty outputs are rejected per file;
- mixed batches partially succeed (one bad file never rejects the batch);
- duplicate content is detected via SHA-256;
- imaginary frequencies are flagged, never flipped positive;
- TD-DFT outputs expose raw transitions (cm-1 preserved, wavelength derived);
- one file containing BOTH FREQ and TD-DFT serves both viewers;
- hostile filenames are sanitized (no traversal, no HTML injection).
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as webapp  # noqa: E402
from orca_engine.parser import OrcaParser  # noqa: E402


HEADER = """
  ------------------------------------------------------------------------------
  * O   R   C   A *
  ------------------------------------------------------------------------------
                                 Program Version 6.1.0

  Total Charge           Charge          ....    0
  Multiplicity           Mult            ....    1
Your calculation utilizes the basis: def2-TZVP
"""

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

IR_BLOCK_IMAG = IR_BLOCK.replace("   0:        0.00    0.000000        0.0000",
                                 "   0:     -500.00    0.000000        0.0000")

IR_BLOCK_B = IR_BLOCK.replace("   6:     1000.00    0.010000        1.0432",
                              "   6:     1050.00    0.080000        8.3456")
IR_BLOCK_B = IR_BLOCK_B.replace("   7:     1500.00    0.050000        5.2161",
                                "   7:     1600.00    0.020000        2.0864")
IR_BLOCK_B = IR_BLOCK_B.replace("   8:     3000.00    0.100000       10.4323",
                                "   8:     2950.00    0.060000        6.2593")

UV_BLOCK = """
------------------------------------------------------------------------------
         ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS
------------------------------------------------------------------------------
State   Energy  Wavelength   fosc
  1    23820.0000    419.5400    0.0100
  2    28500.0000    350.8700    0.1500
  3    35000.0000    285.7100    0.3000
"""

FOOTER = "\n****ORCA TERMINATED NORMALLY****\n"

FREQ_A = HEADER + IR_BLOCK + FOOTER
FREQ_B = HEADER + IR_BLOCK_B + FOOTER
FREQ_IMAG = HEADER + IR_BLOCK_IMAG + FOOTER
SP_ONLY = HEADER + FOOTER
UV_ONLY = HEADER + UV_BLOCK + FOOTER
FREQ_AND_UV = HEADER + IR_BLOCK + UV_BLOCK + FOOTER
MALFORMED = "\x00\x01\x02 not an orca output \x03{{{{}}}}"



@pytest.fixture
def client():
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        yield c


def _post(client, files):
    payload = {"files": []}
    for name, content in files:
        payload["files"].append((io.BytesIO(content.encode("utf-8") if isinstance(content, str) else content), name))
    return client.post("/api/orca/engine/import-orca-output", data=payload, content_type="multipart/form-data")


def _by_name(data, name):
    return next(r for r in data["results"] if r["file_name"] == name)


def test_single_valid_freq_output(client):
    res = _post(client, [("naproxen.out", FREQ_A)])
    assert res.status_code == 200
    data = res.get_json()
    assert data["ok"] is True and data["loaded"] == 1 and data["rejected"] == 0
    entry = _by_name(data, "naproxen.out")
    assert entry["status"] == "loaded"
    assert entry["capabilities"]["ir"] is True and entry["capabilities"]["uv"] is False
    modes = entry["ir"]["modes"]
    by_freq = {m["frequency_cm"]: m["intensity_km_mol"] for m in modes}
    assert by_freq[1000.0] == 0.01 and by_freq[1500.0] == 0.05 and by_freq[3000.0] == 0.1
    assert entry["ir"]["imaginary_frequency_count"] == 0
    assert entry["ir"]["mode_count"] == len(modes)


def test_multiple_valid_freq_outputs(client):
    res = _post(client, [("a.out", FREQ_A), ("b.out", FREQ_B)])
    data = res.get_json()
    assert data["loaded"] == 2 and data["rejected"] == 0
    a = _by_name(data, "a.out")["ir"]["modes"]
    b = _by_name(data, "b.out")["ir"]["modes"]
    assert max(m["intensity_km_mol"] for m in a if m["frequency_cm"] > 0) == 0.1
    assert max(m["intensity_km_mol"] for m in b if m["frequency_cm"] > 0) == 0.08


def test_mixed_valid_and_invalid_batch(client):
    res = _post(client, [("good1.out", FREQ_A), ("sp_only.out", SP_ONLY),
                         ("good2.out", FREQ_B), ("malformed.out", MALFORMED)])
    data = res.get_json()
    assert data["loaded"] == 2 and data["rejected"] == 2
    assert _by_name(data, "sp_only.out")["status"] == "rejected"
    assert "no usable FREQ/IR" in _by_name(data, "sp_only.out")["reason"]
    assert _by_name(data, "malformed.out")["status"] == "rejected"
    assert _by_name(data, "good1.out")["status"] == "loaded"


def test_sp_only_rejected(client):
    data = _post(client, [("sp.out", SP_ONLY)]).get_json()
    entry = _by_name(data, "sp.out")
    assert entry["status"] == "rejected" and entry["capabilities"] == {"ir": False, "uv": False}


def test_opt_only_rejected(client):
    opt_only = (HEADER + "\nCARTESIAN COORDINATES (ANGSTROEM)\n"
                "---------------------------------\n"
                "  O      0.000000    0.000000    0.117269\n"
                "  H      0.000000    0.756968   -0.469076\n\n"
                "FINAL SINGLE POINT ENERGY      -76.421258412356\n" + FOOTER)
    data = _post(client, [("opt.out", opt_only)]).get_json()
    assert _by_name(data, "opt.out")["status"] == "rejected"


def test_malformed_and_empty_rejected(client):
    data = _post(client, [("bad.out", MALFORMED), ("empty.out", "")]).get_json()
    assert _by_name(data, "bad.out")["status"] == "rejected"
    assert _by_name(data, "empty.out")["reason"] == "file is empty"


def test_duplicate_content_detected(client):
    data = _post(client, [("first.out", FREQ_A), ("second.out", FREQ_A)]).get_json()
    first = _by_name(data, "first.out")
    second = _by_name(data, "second.out")
    assert first["status"] == "loaded"
    assert second["status"] == "duplicate"
    assert second["duplicate_of"] == "first.out"
    assert second["raw_hash"] == first["raw_hash"]


def test_imaginary_frequencies_flagged_not_flipped(client):
    data = _post(client, [("ts.out", FREQ_IMAG)]).get_json()
    entry = _by_name(data, "ts.out")
    assert entry["ir"]["imaginary_frequency_count"] == 1
    raw = [m["frequency_cm"] for m in entry["ir"]["modes"]]
    assert -500.0 in raw, "imaginary frequency must keep its negative sign in raw data"


def test_raw_modes_match_authoritative_parser(client):
    """The endpoint must reuse the SAME OrcaParser - raw values identical."""
    res = _post(client, [("naproxen.out", FREQ_A)])
    entry = _by_name(res.get_json(), "naproxen.out")
    direct = OrcaParser(io.StringIO(FREQ_A), source_name="naproxen").parse()[0]
    expected = list(zip(direct.ir_frequencies_cm, direct.ir_intensities_km_mol))
    got = [(m["frequency_cm"], m["intensity_km_mol"]) for m in entry["ir"]["modes"]]
    assert got == expected


def test_tddft_output_loads_with_raw_transitions(client):
    data = _post(client, [("tddft.out", UV_ONLY)]).get_json()
    entry = _by_name(data, "tddft.out")
    assert entry["status"] == "loaded" and entry["capabilities"]["uv"] is True
    trans = entry["uv"]["transitions"]
    assert len(trans) == 3
    t1 = next(t for t in trans if t["state_index"] == 1)
    assert t1["energy_cm"] == 23820.0
    assert t1["oscillator_strength"] == 0.01
    assert abs(t1["wavelength_nm"] - 1e7 / 23820.0) < 1e-6
    assert abs(t1["excitation_energy_ev"] - 23820.0 * 1.2398419843320026e-4) < 1e-9


def test_freq_and_tddft_same_file_serves_both(client):
    data = _post(client, [("both.out", FREQ_AND_UV)]).get_json()
    entry = _by_name(data, "both.out")
    assert entry["capabilities"] == {"ir": True, "uv": True}
    assert entry["ir"]["modes"] and entry["uv"]["transitions"]


def test_hostile_filenames_sanitized(client):
    res = _post(client, [("../evil/<img src=x onerror=alert(1)>.out", FREQ_A)])
    assert res.status_code == 200, "hostile filename must never produce a server error"
    data = res.get_json()
    entry = data["results"][0]
    assert entry["status"] == "loaded"
    assert ".." not in entry.get("display_name", "")
    assert "<img" not in entry.get("display_name", "")
    assert "<" not in entry.get("display_name", "")


def test_oversized_file_rejected(client, monkeypatch):
    monkeypatch.setattr(webapp, "ORCA_IMPORT_MAX_FILE_BYTES", 64)
    big = FREQ_A + " " * 512
    data = _post(client, [("big.out", big)]).get_json()
    entry = _by_name(data, "big.out")
    assert entry["status"] == "rejected" and "limit" in entry["reason"]


def test_same_name_different_content_stays_distinct(client):
    data = _post(client, [("a.out", FREQ_A), ("a.out", FREQ_B)]).get_json()
    first = data["results"][0]
    second = data["results"][1]
    assert first["status"] == "loaded" and second["status"] == "loaded"
    assert first["raw_hash"] != second["raw_hash"]
    assert first["ir"]["modes"] != second["ir"]["modes"]


def test_too_many_files_rejected(client):
    files = [("f%d.out" % i, FREQ_A) for i in range(25)]
    res = _post(client, files)
    assert res.status_code == 413
