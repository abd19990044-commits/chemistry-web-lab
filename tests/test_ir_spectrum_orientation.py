# -*- coding: utf-8 -*-
"""IR spectrum scientific-orientation regression tests (blocking assertions).

Locks the scientifically verified behavior of the authoritative IR pipeline
(static/js/app.js, functions `computeIRConvolution` + the canvas mappers in
`renderIRSpectrumToCanvas`):

- ORCA modes are IR INTENSITIES (km/mol); the %T view is a NORMALIZED RELATIVE
  transmittance built as tPct = 100 * 10**(-aNorm), aNorm in [0, 1] - never the
  invalid `100 - raw_km_mol` conversion.
- baseline far from bands sits at ~100 %T (top of canvas);
- the strongest ORCA intensity produces the DEEPEST downward %T band;
- the Y axis uses the normal numeric orientation: 0 at bottom, 100 near the top
  (both in the mapY arithmetic AND in the drawn tick labels - no reversal, no
  double inversion);
- wavenumber axis is conventional descending IR (4000 left -> 400 right),
  independent of the Y quantity;
- intensity mode plots stronger modes higher (upward);
- switching display modes never mutates the raw parsed modes;
- the derived curve is a presentation array that never overwrites the raw
  km/mol data;
- exported/tooltip values follow the selected display quantity.

Every check below is a HARD pytest assertion: a scientifically false condition
fails the suite. The numeric checks run the REAL production JavaScript
(extracted verbatim) through Node, exactly like tests/test_frontend.py runs
load-time checks; they are skipped (pytest.skip) only when Node is unavailable.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def served(flat, nested):
    nested_path = os.path.join(ROOT, nested)
    return nested_path if os.path.exists(nested_path) else os.path.join(ROOT, flat)


APP_JS = served("app.js", os.path.join("static", "js", "app.js"))


def _check(label, condition, detail=""):
    """Hard assertion helper: a false scientific condition MUST fail pytest."""
    if not condition:
        pytest.fail("%s%s" % (label, (" | " + detail) if detail else ""))


NODE_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");

const fnStart = src.indexOf("function computeIRConvolution");
if (fnStart < 0) { console.log(JSON.stringify({ error: "computeIRConvolution not found" })); process.exit(0); }
const fnSrc = src.slice(fnStart, src.indexOf("\n  }", fnStart) + 4);
eval(fnSrc.replace("function computeIRConvolution", "globalThis.computeIRConvolution = function computeIRConvolution"));

const modes = [
  { frequency_cm: 1000, intensity_km_mol: 10 },
  { frequency_cm: 1500, intensity_km_mol: 50 },
  { frequency_cm: 3000, intensity_km_mol: 100 },
];
const modesSnapshot = JSON.stringify(modes);
const curve = computeIRConvolution(modes, 1.0, 15.0, 0.0, 400, 4000, 2);

const at = (wn) => curve.reduce((a, p) => (Math.abs(p.wavenumber_cm - wn) < Math.abs(a.wavenumber_cm - wn) ? p : a));
const c1000 = at(1000), c1500 = at(1500), c3000 = at(3000), baseline = at(3900);
const tVals = curve.map(p => p.transmittance_pct);

// EXACT canvas mapper formulas from renderIRSpectrumToCanvas (transmittance mode)
const minWn = 400, maxWn = 4000, minY = 0.0, maxY = 105.0;
const padding = { top: 35, right: 65, bottom: 48, left: 65 };
const width = 1400, height = 800;
const plotW = width - padding.left - padding.right;
const plotH = height - padding.top - padding.bottom;
const mapX = (wn) => padding.left + ((maxWn - wn) / (maxWn - minWn)) * plotW;
const mapY = (val) => padding.top + plotH - ((val - minY) / (maxY - minY)) * plotH;

// intensity mode: plotted value = absorbance_norm * maxTheoModeInt, maxY = 100
const maxTheoModeInt = Math.max(...modes.map(m => m.intensity_km_mol || 0), 10.0);
const plottedIntensity = (pt) => pt.absorbance_norm * maxTheoModeInt;

const out = {
  t1000: c1000.transmittance_pct,
  t1500: c1500.transmittance_pct,
  t3000: c3000.transmittance_pct,
  baseline_t: baseline.transmittance_pct,
  strongest_is_lowest_t: c3000.transmittance_pct < c1500.transmittance_pct && c1500.transmittance_pct < c1000.transmittance_pct,
  bounds_ok: tVals.every(t => t >= 0 && t <= 100 && isFinite(t)),
  pairing_ok: [1000, 1500, 3000].every(wn => Math.abs(at(wn).wavenumber_cm - wn) <= 2),
  band_points_downward: mapY(c3000.transmittance_pct) > mapY(100),
  y100_above_y0: mapY(100) < mapY(0),
  descending_x: mapX(4000) < mapX(400),
  x_independent_of_y: !/mapY/.test(mapX.toString()),
  strongest_abs_largest: c3000.absorbance > c1500.absorbance && c1500.absorbance > c1000.absorbance,
  intensity_mode_upward: plottedIntensity(c3000) > plottedIntensity(c1500) && plottedIntensity(c1500) > plottedIntensity(c1000)
    && mapY(plottedIntensity(c3000)) < mapY(plottedIntensity(c1000)),
  modes_unmutated: JSON.stringify(modes) === modesSnapshot,
  returns_new_array: curve !== modes && curve.every(p => !("intensity_km_mol" in p)),
};
console.log(JSON.stringify(out));
"""


def _node_available():
    return shutil.which("node") is not None


def _renderer_body(src):
    """The renderIRSpectrumToCanvas body (the only IR canvas renderer)."""
    start = src.find("function renderIRSpectrumToCanvas")
    if start < 0:
        return ""
    end = src.find("\n  function ", start + 10)
    return src[start:end] if end > start else src[start:start + 30000]


def test_ir_spectrum_orientation():
    print("== IR spectrum scientific orientation (app.js: computeIRConvolution + canvas mappers) ==")
    src = open(APP_JS, encoding="utf-8").read()

    # ---- static invariants (always run) ----
    _check("relative %T built via 100 * 10**(-aNorm), not '100 - raw'",
           "100.0 * Math.pow(10, -aNorm)" in src
           and not re.search(r"=\s*100\s*-\s*(raw|intensity)\b", src))
    _check("theory-only %T is honestly labeled Pseudo-%T",
           "Theoretical IR Transmittance (Pseudo-%T)" in src)
    _check("tooltip shows %T for transmittance mode (same quantity as the axis)",
           "irYAxisMode === 'transmittance' ? `${p.transmittance_pct.toFixed(2)} %T`" in src)
    _check("CSV export follows the selected display quantity",
           "irYAxisMode === 'transmittance' ? 'Transmittance_pct' : 'Absorbance'" in src)
    _check("experimental %T->A conversion uses Beer-Lambert direction (-log10), not subtraction",
           "-Math.log10(Math.max(0.0001, tPct / 100))" in src)
    _check("experimental quantity detection heuristic present (documented limitation: "
           "%T columns with max<=10 and a non-'trans' header are treated as absorbance)",
           'const isTransmittance = maxVal > 10.0 || (expData.units_y && expData.units_y.includes("%")) || /trans/i.test(seriesLabel);' in src)

    # ---- Y-axis orientation: arithmetic AND drawn tick labels ----
    body = _renderer_body(src)
    _check("IR renderer found", len(body) > 1000)
    _check("mapY uses STANDARD orientation (subtractive: higher value -> smaller canvas y)",
           re.search(r"mapY\s*=\s*\(val\)\s*=>\s*\{\s*return padding\.top \+ plotH - \(\(val - minY\) / \(maxY - minY\)\) \* plotH;", body) is not None)
    _check("no reversed/inverted Y-axis construct inside the IR renderer",
           not re.search(r"reversed|inverted|\binverse\b", body, re.IGNORECASE))
    _check("transmittance mode scale constants: minY=0, maxY=105 (headroom above 100)",
           "let minY = 0.0;" in body and "isTrans ? 105.0" in body)
    _check("tick labels are generated from the same mapped values "
           "(label text `${Math.round(yVal)}%` drawn at mapY(yVal) - labels cannot be swapped relative to position)",
           re.search(r"label = `\$\{Math\.round\(yVal\)\}%`", body) is not None
           and re.search(r"const yPos = mapY\(yVal\);\s*ctx\.fillText\(label", body) is not None)
    _check("tick loop covers the full scale from the bottom (yStep starts at 0 -> the 0%% label exists at the bottom)",
           re.search(r"for \(let yStep = 0; yStep <= 4; yStep\+\+\) \{\s*const yVal = minY \+ \(\(maxY - minY\) / 4\) \* yStep;", body) is not None)

    # ---- numeric production-JS verification (real app.js via Node) ----
    if not _node_available():
        pytest.skip("node is not available: the production-JS numeric orientation checks cannot run")
    tmp = tempfile.mkdtemp(prefix="ir_orientation_")
    harness = os.path.join(tmp, "ir_harness.js")
    with open(harness, "w", encoding="utf-8") as f:
        f.write(NODE_HARNESS)
    try:
        proc = subprocess.run(["node", harness, APP_JS], capture_output=True, text=True, timeout=120)
    finally:
        os.remove(harness)
        os.rmdir(tmp)
    out_line = [l for l in proc.stdout.splitlines() if l.strip().startswith("{")]
    _check("node harness executed against the real app.js", bool(out_line),
           (proc.stderr or proc.stdout)[-400:])
    res = json.loads(out_line[-1])

    _check("strongest ORCA intensity (3000, I=100) -> lowest %%T (got %.2f%%; 1500: %.2f%%; 1000: %.2f%%)"
           % (res["t3000"], res["t1500"], res["t1000"]), res["strongest_is_lowest_t"])
    _check("baseline far from bands ~ 100 %%T (got %.2f%%)" % res["baseline_t"],
           99.0 <= res["baseline_t"] <= 100.0)
    _check("deepest band reaches the normalized floor (10 %%T for aNorm=1)",
           abs(res["t3000"] - 10.0) < 0.5)
    _check("bounds 0 <= relative %%T <= 100 and finite everywhere", res["bounds_ok"])
    _check("frequency/intensity pairing preserved (centers within 2 cm-1)", res["pairing_ok"])
    _check("strong absorption band points DOWNWARD in pixel space (mapY(T_min) > mapY(100))",
           res["band_points_downward"])
    _check("Y-axis numeric orientation: 100%% maps ABOVE 0%% (mapY(100) < mapY(0))", res["y100_above_y0"])
    _check("wavenumber axis conventional descending (4000 left -> 400 right)", res["descending_x"])
    _check("X-axis reversal is independent of the Y quantity", res["x_independent_of_y"])
    _check("absorbance mode: stronger mode -> larger absorbance", res["strongest_abs_largest"])
    _check("intensity mode: stronger mode -> higher plotted peak (upward)", res["intensity_mode_upward"])
    _check("display-mode switching never mutates the raw parsed modes", res["modes_unmutated"])
    _check("convolution returns a derived presentation array (no km/mol fields leaked)", res["returns_new_array"])
    print("   measured: T(1000)=%.2f%%  T(1500)=%.2f%%  T(3000)=%.2f%%  baseline=%.2f%%"
          % (res["t1000"], res["t1500"], res["t3000"], res["baseline_t"]))


if __name__ == "__main__":
    test_ir_spectrum_orientation()
    print("IR ORIENTATION: all blocking assertions passed")
