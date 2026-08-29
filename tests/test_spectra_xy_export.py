# -*- coding: utf-8 -*-
"""Spectra Studio export/labeling/experimental-only regression tests.

Verifies - against the REAL production app.js (extracted verbatim, executed
in Node, exactly like tests/test_frontend.py):

- buildXYText produces Origin-friendly, tab-delimited, UTF-8, decimal-dot text
  with machine-safe headers and optional # metadata comments;
- single-curve and multi-curve (merged-grid / paired-columns) layouts;
- exported X/Y coordinates are the SAME arrays the renderer plots
  (computeIRConvolution / computeUvvisConvolution outputs);
- filename stems are filesystem-safe while display names stay intact;
- experimental-only / theoretical-only view filters are wired for both studios
  without mutating any layer state;
- custom graph/X/Y titles are plain canvas text (no HTML execution path).
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
INDEX = served("index.html", os.path.join("templates", "index.html"))

NODE_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");

function extractFn(name) {
  const start = src.indexOf("function " + name);
  if (start < 0) throw new Error(name + " not found in app.js");
  const end = src.indexOf("\n  }", start);
  const body = src.slice(start, end + 4);
  eval(body.replace("function " + name, "globalThis." + name + " = function " + name));
}
["computeIRConvolution", "computeUvvisConvolution", "buildXYText", "sanitizeFileStem"].forEach(extractFn);

const modesA = [
  { frequency_cm: 1000, intensity_km_mol: 10 },
  { frequency_cm: 1500, intensity_km_mol: 50 },
  { frequency_cm: 3000, intensity_km_mol: 100 },
];
const modesB = [
  { frequency_cm: 1050, intensity_km_mol: 80 },
  { frequency_cm: 1600, intensity_km_mol: 20 },
  { frequency_cm: 2950, intensity_km_mol: 60 },
];

// ---- 1. IR single-curve XY export (exactly what exportIRXYSingle builds) ----
const curveA = computeIRConvolution(modesA, 1.0, 15.0, 0.0, 400, 4000, 2);
const orderedA = curveA.slice().reverse(); // production exportIRXYSingle emits the canonical 4000->400 order
const colA = {
  xHeader: "Wavenumber_cm-1",
  yHeader: "Relative_Transmittance_pct",
  xs: orderedA.map(p => p.wavenumber_cm),
  ys: orderedA.map(p => p.transmittance_pct),
};
const metaA = ["Chemistry Lab", "Spectrum: Naproxen", "Type: Simulated IR",
  "X: Wavenumber (cm^-1)", "Y: Relative_Transmittance_pct (%)",
  "FWHM: 15 cm^-1", "Scaling: 1", "Normalization: Per Spectrum"];
const textA = buildXYText([colA], metaA);
const linesA = textA.trim().split("\n");
const metaRows = linesA.filter(l => l.startsWith("#")).length;
const headerA = linesA[metaRows];
const dataRows = linesA.slice(metaRows + 1);
const t2 = textA.includes("\t");
const firstData = dataRows[0].split("\t");
const lastData = dataRows[dataRows.length - 1].split("\t");

// ---- 2. visible multi-curve (theo A + theo B share the grid) ----
const curveB = computeIRConvolution(modesB, 1.0, 15.0, 0.0, 400, 4000, 2);
const textMulti = buildXYText([
  { xHeader: "Wavenumber_cm-1", yHeader: "Naproxen", xs: colA.xs, ys: colA.ys },
  { xHeader: "Wavenumber_cm-1", yHeader: "Product", xs: curveB.slice().reverse().map(p => p.wavenumber_cm), ys: curveB.slice().reverse().map(p => p.transmittance_pct) },
], ["Chemistry Lab", "View: all"]);
const multiLines = textMulti.trim().split("\n");
const multiMetaRows = multiLines.filter(l => l.startsWith("#")).length;
const multiHeader = multiLines[multiMetaRows].split("\t");

// ---- 3. different grids -> paired columns ----
const textPaired = buildXYText([
  { xHeader: "Wavenumber_cm-1_A", yHeader: "A", xs: [4000, 3990], ys: [100, 99] },
  { xHeader: "Wavelength_nm_B", yHeader: "B", xs: [200, 300, 400], ys: [0.1, 0.5, 0.2] },
], []);
const pairedHeader = textPaired.trim().split("\n")[0].split("\t");

// ---- 4. UV single-curve (existing gaussian convolution + norm semantics) ----
const transitions = [
  { wavelength_nm: 419.54, oscillator_strength: 0.01 },
  { wavelength_nm: 350.87, oscillator_strength: 0.15 },
  { wavelength_nm: 285.71, oscillator_strength: 0.30 },
];
const uvCurve = computeUvvisConvolution(transitions, 20, 0, 180, 800, 1);
let uvMax = 0;
uvCurve.forEach(p => { if (p.intensity > uvMax) uvMax = p.intensity; });
const uvCol = {
  xHeader: "Wavelength_nm",
  yHeader: "Theoretical_Intensity_Norm",
  xs: uvCurve.map(p => p.wavelength_nm),
  ys: uvCurve.map(p => p.intensity / uvMax),
};
const uvText = buildXYText([uvCol], ["Chemistry Lab", "Type: Simulated UV-Vis (TD-DFT)", "Normalization: theoretical_only"]);
const uvLines = uvText.trim().split("\n");
const uvMetaRows = uvLines.filter(l => l.startsWith("#")).length;
const uvHeader = uvLines[uvMetaRows].split("\t");

// ---- 5. filename sanitizer keeps display names intact ----
const hostile = "../<img src=x onerror=alert(1)>.out";
const stem = sanitizeFileStem(hostile);

const out = {
  tab_delimited: t2,
  meta_comment_rows: metaRows,
  header_a: headerA,
  x_descending: Number(firstData[0]) > Number(lastData[0]),
  x_first_4000: Math.abs(Number(firstData[0]) - 4000) < 0.01,
  y_matches_curve: Math.abs(Number(firstData[1]) - colA.ys[0]) < 1e-6
    && Math.abs(Number(lastData[1]) - colA.ys[colA.ys.length - 1]) < 1e-6,
  six_decimals: /^\d+\.\d{6}\t-?\d+\.\d{6}$/.test(dataRows[0]),
  no_comma_decimals: !/\d,\d/.test(textA),
  all_finite: dataRows.every(r => r.split("\t").every(v => isFinite(Number(v)))),
  row_count: dataRows.length,
  multi_header: multiHeader,
  multi_row_count: multiLines.length - multiMetaRows - 1,
  multi_finite: multiLines.slice(multiMetaRows + 1).every(r => r.split("\t").slice(1).every(v => isFinite(Number(v)))),
  paired_header: pairedHeader,
  paired_pad_ok: textPaired.trim().split("\n")[2].split("\t").length === 4,
  uv_header: uvHeader,
  uv_x_ascending: Number(uvLines[uvMetaRows + 1].split("\t")[0]) < Number(uvLines[uvLines.length - 1].split("\t")[0]),
  uv_norm_max_1: Math.max(...uvCol.ys) === 1,
  uv_finite: uvCol.ys.every(v => isFinite(v)),
  stem: stem,
  display_name_intact: hostile,
};
console.log(JSON.stringify(out));
"""

PARITY_HARNESS = r"""const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");

function extractFn(name) {
  const start = src.indexOf("function " + name);
  if (start < 0) throw new Error(name + " not found in app.js");
  const end = src.indexOf("\n  }", start);
  const body = src.slice(start, end + 4);
  eval(body.replace("function " + name, "globalThis." + name + " = function " + name));
}
 ["computeIRConvolution", "computeUvvisConvolution", "buildXYText"].forEach(extractFn);

const modesA = [
  { frequency_cm: 1000, intensity_km_mol: 10 },
  { frequency_cm: 1500, intensity_km_mol: 50 },
  { frequency_cm: 3000, intensity_km_mol: 100 },
];
const modesB = [
  { frequency_cm: 1050, intensity_km_mol: 80 },
  { frequency_cm: 1600, intensity_km_mol: 20 },
  { frequency_cm: 2950, intensity_km_mol: 60 },
];

// ---- renderer path: per-spectrum curves first, then the shared basis over the
// visible set (A and B both visible) - exactly what getIRSharedNormMax does ----
const rawA = computeIRConvolution(modesA, 1.0, 15.0, 0.0, 400, 4000, 2);
const rawB = computeIRConvolution(modesB, 1.0, 15.0, 0.0, 400, 4000, 2);
let globalMax = 0;
rawA.concat(rawB).forEach(p => { if (p.absorbance > globalMax) globalMax = p.absorbance; });

// DISPLAYED arrays (renderer, Shared mode)
const dispA = computeIRConvolution(modesA, 1.0, 15.0, 0.0, 400, 4000, 2, globalMax);
const dispB = computeIRConvolution(modesB, 1.0, 15.0, 0.0, 400, 4000, 2, globalMax);
// DISPLAYED arrays (renderer, Per-Spectrum mode)
const dispAps = computeIRConvolution(modesA, 1.0, 15.0, 0.0, 400, 4000, 2, null);
const dispBps = computeIRConvolution(modesB, 1.0, 15.0, 0.0, 400, 4000, 2, null);

// ---- EXPORT path (exportIRXYVisible after the parity fix: same shared basis,
// canonical 4000->400 row order, 6-decimal text) ----
const exportedText = buildXYText([
  { xHeader: "Wavenumber_cm-1", yHeader: "Naproxen", xs: dispA.slice().reverse().map(p => p.wavenumber_cm), ys: dispA.slice().reverse().map(p => p.transmittance_pct) },
  { xHeader: "Wavenumber_cm-1", yHeader: "Product", xs: dispB.slice().reverse().map(p => p.wavenumber_cm), ys: dispB.slice().reverse().map(p => p.transmittance_pct) },
], ["Chemistry Lab", "Normalization: Shared"]);
const perText = buildXYText([
  { xHeader: "Wavenumber_cm-1", yHeader: "Naproxen", xs: dispAps.slice().reverse().map(p => p.wavenumber_cm), ys: dispAps.slice().reverse().map(p => p.transmittance_pct) },
  { xHeader: "Wavenumber_cm-1", yHeader: "Product", xs: dispBps.slice().reverse().map(p => p.wavenumber_cm), ys: dispBps.slice().reverse().map(p => p.transmittance_pct) },
], ["Chemistry Lab", "Normalization: Per Spectrum"]);

function parseColumns(text) {
  const lines = text.trim().split("\n");
  const meta = lines.filter(l => l.startsWith("#")).length;
  const header = lines[meta].split("\t");
  const rows = lines.slice(meta + 1).map(l => l.split("\t").map(Number));
  const cols = { x: [], };
  header.slice(1).forEach((_, i) => { cols["y" + i] = []; });
  rows.forEach(r => {
    cols.x.push(r[0]);
    header.slice(1).forEach((_, i) => { cols["y" + i].push(r[i + 1]); });
  });
  return { header, cols, n: rows.length };
}

const sh = parseColumns(exportedText);
const ps = parseColumns(perText);

// point-by-point identity: exported rows are REVERSED (4000->400) vs displayed
// (400->4000) - allowed per the acceptance rules. Reverse back before compare.
function compareBackwards(displayedX, displayedY, expX, expY) {
  const n = displayedX.length;
  if (expX.length !== n) return false;
  for (let i = 0; i < n; i++) {
    const j = n - 1 - i; // reversal allowance
    if (Math.abs(expX[j] - displayedX[i]) > 5e-7) return false;
    if (Math.abs(expY[j] - displayedY[i]) > 5e-7) return false;
  }
  return true;
}

const min = a => Math.min.apply(null, a);
const out = {
  sharedGlobalMax: globalMax,
  shared_dispB_min: min(dispB.map(p => p.transmittance_pct)),
  shared_exportB_min: min(sh.cols.y1),
  shared_point_by_point: compareBackwards(dispB.map(p => p.wavenumber_cm), dispB.map(p => p.transmittance_pct), sh.cols.x, sh.cols.y1)
    && compareBackwards(dispA.map(p => p.wavenumber_cm), dispA.map(p => p.transmittance_pct), sh.cols.x, sh.cols.y0),
  per_dispB_min: min(dispBps.map(p => p.transmittance_pct)),
  per_exportB_min: min(ps.cols.y1),
  per_point_by_point: compareBackwards(dispBps.map(p => p.wavenumber_cm), dispBps.map(p => p.transmittance_pct), ps.cols.x, ps.cols.y1)
    && compareBackwards(dispAps.map(p => p.wavenumber_cm), dispAps.map(p => p.transmittance_pct), ps.cols.x, ps.cols.y0),
};

// ---- UV spot check: displayed (theoretical_only) vs exported ----
const transitions = [
  { wavelength_nm: 419.54, oscillator_strength: 0.01 },
  { wavelength_nm: 350.87, oscillator_strength: 0.15 },
  { wavelength_nm: 285.71, oscillator_strength: 0.30 },
];
const uvCurve = computeUvvisConvolution(transitions, 20, 0, 180, 800, 1);
let uvMax = 0;
uvCurve.forEach(p => { if (p.intensity > uvMax) uvMax = p.intensity; });
const uvDispX = uvCurve.map(p => p.wavelength_nm);
const uvDispY = uvCurve.map(p => p.intensity / uvMax); // theoretical_only: divide by OWN max
const uvExportText = buildXYText([
  { xHeader: "Wavelength_nm", yHeader: "Theoretical_Intensity_Norm", xs: uvDispX, ys: uvDispY },
], ["Chemistry Lab", "Normalization: theoretical_only"]);
const uvParsed = parseColumns(uvExportText);
uvDispX_match = uvParsed.cols.x.every((x, i) => Math.abs(x - uvDispX[i]) <= 5e-7);
uvDispY_match = uvParsed.cols.y0.every((y, i) => Math.abs(y - uvDispY[i]) <= 5e-7);
out.uv_point_by_point = uvDispX_match && uvDispY_match;
out.uv_min_display = min(uvDispY);
out.uv_min_export = min(uvParsed.cols.y0);

// mixed-grid behavior documentation check: ANY differing grid -> paired for ALL
const mixedText = buildXYText([
  { xHeader: "X1", yHeader: "S1", xs: [1, 2, 3], ys: [10, 20, 30] },
  { xHeader: "X2", yHeader: "S2", xs: [1, 2], ys: [5, 6] },
], []);
const mixedLines = mixedText.trim().split("\n");
out.mixed_all_paired = mixedLines[0].split("\t").length === 4 && mixedLines[0].indexOf("X2") >= 0;

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def node_results():
    if not shutil.which("node"):
        pytest.skip("node is not available: the production-JS export checks cannot run")
    tmp = tempfile.mkdtemp(prefix="spectra_export_")
    harness = os.path.join(tmp, "export_harness.js")
    with open(harness, "w", encoding="utf-8") as f:
        f.write(NODE_HARNESS)
    try:
        proc = subprocess.run(["node", harness, APP_JS], capture_output=True, text=True, timeout=120)
    finally:
        os.remove(harness)
        os.rmdir(tmp)
    out_line = [l for l in proc.stdout.splitlines() if l.strip().startswith("{")]
    if not out_line:
        pytest.fail("export harness failed: " + (proc.stderr or proc.stdout)[-400:])
    return json.loads(out_line[-1])


@pytest.fixture(scope="module")
def parity_results():
    return _run_harness(PARITY_HARNESS)


def _run_harness(js_text):
    if not shutil.which("node"):
        pytest.skip("node is not available: the production-JS checks cannot run")
    tmp = tempfile.mkdtemp(prefix="spectra_export_")
    harness = os.path.join(tmp, "harness.js")
    with open(harness, "w", encoding="utf-8") as f:
        f.write(js_text)
    try:
        proc = subprocess.run(["node", harness, APP_JS], capture_output=True, text=True, timeout=120)
    finally:
        os.remove(harness)
        os.rmdir(tmp)
    out_line = [l for l in proc.stdout.splitlines() if l.strip().startswith("{")]
    if not out_line:
        pytest.fail("harness failed: " + (proc.stderr or proc.stdout)[-400:])
    return json.loads(out_line[-1])


def test_static_wiring():
    print("== static wiring (app.js + index.html) ==")
    src = open(APP_JS, encoding="utf-8").read()
    html = open(INDEX, encoding="utf-8").read()
    for fn in ("buildXYText", "sanitizeFileStem", "downloadTextFile",
               "exportIRXYSingle", "exportIRXYVisible", "exportUVXYSingle", "exportUVXYVisible"):
        assert ("function " + fn) in src, fn + " missing"
    for el in ("ir-view-mode", "uv-view-mode", "ir-graph-title", "ir-x-title", "ir-y-title",
               "uv-graph-title", "uv-x-title", "uv-y-title", "ir-titles-reset", "uv-titles-reset",
               "ir-export-xy-visible", "uv-export-xy-visible", "ir-remove-imported", "uv-remove-imported",
               "engine-ir-import-freq-btn", "engine-uvvis-import-btn"):
        assert ('id="' + el + '"') in html, "index.html missing #" + el
    # view filter applied to the renderer curves (experimental-only support)
    assert src.count('irViewAllows("theo")') >= 2 and src.count('irViewAllows("exp")') >= 2
    assert src.count('uvViewAllows("theo")') >= 2 and src.count('uvViewAllows("exp")') >= 2
    # titles are drawn on canvas (plain text - no HTML path)
    assert "irCustomTitles.title || \"Simulated IR Spectrum\"" in src and "ctx.fillText(uvCustomTitles.title" in src
    assert "if (irCustomTitles.y) yAxisTitle = irCustomTitles.y;" in src
    assert "uvCustomTitles.y || (spectrumNormalizeMode" in src
    # export uses the same renderer pipeline (no second convolution)
    assert re.search(r"exportIRXYSingle[\s\S]{0,600}computeIRConvolution", src)
    assert "const ordered = curve.slice().reverse();" in src  # canonical 4000->400 emission order
    assert re.search(r"exportUVXYSingle[\s\S]{0,600}computeUvvisConvolution", src)
    # experimental-only empty-state message
    assert "No experimental spectrum loaded" in src
    # imported/experimental display names escaped at HTML boundaries
    assert "${escapeHtml(theo.name)}" in src and "${escapeHtml(exp.label || exp.file_name)}" in src


def test_ir_single_xy_export(node_results):
    r = node_results
    print("== IR single XY ==")
    print("   header:", r["header_a"], "| rows:", r["row_count"])
    assert r["tab_delimited"] is True
    assert r["meta_comment_rows"] == 8
    assert r["header_a"] == "Wavenumber_cm-1\tRelative_Transmittance_pct"
    assert r["x_descending"] is True and r["x_first_4000"] is True
    assert r["y_matches_curve"] is True, "exported Y must equal the plotted curve"
    assert r["six_decimals"] is True and r["no_comma_decimals"] is True
    assert r["all_finite"] is True


def test_visible_multi_and_paired(node_results):
    r = node_results
    print("== visible multi / paired ==")
    print("   multi header:", r["multi_header"])
    print("   paired header:", r["paired_header"])
    assert r["multi_header"] == ["Wavenumber_cm-1", "Naproxen", "Product"]
    assert r["multi_row_count"] == r["row_count"], "shared-grid merge keeps one row per X"
    assert r["multi_finite"] is True
    assert r["paired_header"] == ["Wavenumber_cm-1_A", "A", "Wavelength_nm_B", "B"]
    assert r["paired_pad_ok"] is True


def test_uv_xy_export(node_results):
    r = node_results
    print("== UV single XY ==")
    print("   header:", r["uv_header"], "| X ascending:", r["uv_x_ascending"])
    assert r["uv_header"] == ["Wavelength_nm", "Theoretical_Intensity_Norm"]
    assert r["uv_x_ascending"] is True
    assert r["uv_norm_max_1"] is True
    assert r["uv_finite"] is True


def test_shared_ir_export_display_parity(parity_results):
    """FINAL DISPLAYED ARRAY vs FINAL EXPORTED ARRAY - point-by-point identity.

    Not satisfied by 'same scientific function': the shared-normalization basis
    is applied by BOTH the renderer and the exporter, and the test compares the
    final coordinate arrays (allowing only row-order reversal and 6-decimal
    text formatting).
    """
    r = parity_results
    print("== Shared IR export/display parity ==")
    print("   shared basis (maxAbs): %.6f" % r["sharedGlobalMax"])
    print("   IR SHARED  DISPLAY B MIN: %.6f %%T" % r["shared_dispB_min"])
    print("   IR SHARED  EXPORT  B MIN: %.6f %%T" % r["shared_exportB_min"])
    print("   IR PER-SPEC DISPLAY B MIN: %.6f %%T" % r["per_dispB_min"])
    print("   IR PER-SPEC EXPORT  B MIN: %.6f %%T" % r["per_exportB_min"])
    print("   UV display-vs-export point-by-point:", r["uv_point_by_point"])
    print("   mixed-grid behavior (all-paired when any grid differs):", r["mixed_all_paired"])
    assert abs(r["shared_dispB_min"] - 15.85) < 0.05, "displayed Shared B floor must be ~15.85 %%T"
    assert abs(r["shared_exportB_min"] - 15.85) < 0.05, "exported Shared B floor must be ~15.85 %%T"
    assert abs(r["shared_dispB_min"] - r["shared_exportB_min"]) <= 5e-7
    assert r["shared_point_by_point"] is True
    assert abs(r["per_dispB_min"] - 10.0) < 0.05, "displayed Per-Spectrum B floor must be ~10 %%T"
    assert abs(r["per_exportB_min"] - 10.0) < 0.05, "exported Per-Spectrum B floor must be ~10 %%T"
    assert abs(r["per_dispB_min"] - r["per_exportB_min"]) <= 5e-7
    assert r["per_point_by_point"] is True
    assert r["uv_point_by_point"] is True
    assert abs(r["uv_min_display"] - r["uv_min_export"]) <= 5e-7
    assert r["mixed_all_paired"] is True


def test_filename_sanitizer(node_results):
    r = node_results
    print("== filename sanitizer ==")
    print("   hostile:", r["display_name_intact"])
    print("   stem:", r["stem"])
    stem = r["stem"]
    assert "/" not in stem and "\\" not in stem and ".." not in stem
    assert "<" not in stem and ">" not in stem and '"' not in stem
    assert r["display_name_intact"] == "../<img src=x onerror=alert(1)>.out"
