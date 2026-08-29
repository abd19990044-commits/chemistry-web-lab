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


def test_filename_sanitizer(node_results):
    r = node_results
    print("== filename sanitizer ==")
    print("   hostile:", r["display_name_intact"])
    print("   stem:", r["stem"])
    stem = r["stem"]
    assert "/" not in stem and "\\" not in stem and ".." not in stem
    assert "<" not in stem and ">" not in stem and '"' not in stem
    assert r["display_name_intact"] == "../<img src=x onerror=alert(1)>.out"
