# -*- coding: utf-8 -*-
"""Comprehensive tests for IR Spectrum parsing, convolution, and FTIR overlay."""

import pytest
import io
import json
from pathlib import Path
import shutil
import subprocess
import zipfile
from orca_engine.parser import OrcaParser
from orca_engine.adapters.web_adapter import convolute_ir_spectrum, job_to_web_json
from app import app


SAMPLE_ORCA_IR_OUTPUT = """
-------------------------------------------------------------------------------
                                 IR SPECTRUM
-------------------------------------------------------------------------------

 Mode   freq (cm**-1)   T**2 (km/mol)    TX       TY       TZ
------------------------------------------------------------------
    6:     1595.23         12.345      0.123    0.456    0.789
    7:     3657.05         45.678      0.345    0.678    0.901
    8:     3755.93         89.123      0.567    0.890    0.123

-----------------------
VIBRATIONAL FREQUENCIES
-----------------------
   0:         0.00 cm**-1
   1:         0.00 cm**-1
   2:         0.00 cm**-1
   3:         0.00 cm**-1
   4:         0.00 cm**-1
   5:         0.00 cm**-1
   6:      1595.23 cm**-1
   7:      3657.05 cm**-1
   8:      3755.93 cm**-1

****ORCA TERMINATED NORMALLY****
"""

SAMPLE_FTIR_TEXT_TRANSMITTANCE = """# FTIR Experimental Spectrum (H2O vapor)
Wavenumber (cm-1)    Transmittance (%)
4000.0              98.5
3755.0              22.4
3655.0              35.1
2500.0              99.2
1595.0              18.7
500.0               97.8
"""

# ORCA 6 layout: deliberately different eps, Int and T**2 values.
MODERN_IR_OUTPUT = """
Program Version 6.1.0
VIBRATIONAL FREQUENCIES
  0: 0.0 cm**-1
  6: 1190.75 cm**-1
  7: 2954.25 cm**-1
IR SPECTRUM
Mode   freq       eps      Int      T**2         TX        TY        TZ
       cm**-1   L/(mol*cm) km/mol    a.u.
-------------------------------------------------------------------
  6:  1190.75   1.2D-4     7.80    0.000093  (-0.000000 -0.009640  0.000000)
  7:  2954.25   0.013    106.35    0.004363  ( 0.066052 -0.000000 -0.000000)
****ORCA TERMINATED NORMALLY****
"""


def test_modern_orca_reports_integrated_intensities_not_eps_or_dipole_derivatives():
    raw = MODERN_IR_OUTPUT.replace("****ORCA TERMINATED NORMALLY****",
                                   "RAMAN SPECTRUM\n  8: 3100.0 95.0 0.1\n****ORCA TERMINATED NORMALLY****")
    job, = OrcaParser(raw).parse()
    assert job.ir_intensities_km_mol == [7.8, 106.35]
    assert job.ir_frequencies_cm == [1190.75, 2954.25]
    assert job.vibrational_frequencies_cm == [0, 1190.75, 2954.25]
    assert job.terminated_normally is True


def test_frequency_only_output_does_not_invent_ir_intensities():
    raw = MODERN_IR_OUTPUT.split("IR SPECTRUM")[0]
    job, = OrcaParser(raw).parse()
    web = job_to_web_json(job, "frequencies-only", 1)
    assert web["vibrational_frequencies_cm"] == [0, 1190.75, 2954.25]
    assert web["ir_spectrum"] == []
    assert web["convoluted_ir_spectrum"] == []


@pytest.mark.parametrize("archive", [False, True])
def test_multistep_output_draws_earlier_ir_bands_and_exports_a_vector_figure(tmp_path, archive):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required")
    raw = (MODERN_IR_OUTPUT + "\n$new_job\nFINAL SINGLE POINT ENERGY -76.22\n"
           "****ORCA TERMINATED NORMALLY****\n").encode()
    if archive:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("water.out", raw)
            zf.writestr("ignore.txt", raw)
        name, raw = "water.zip", buf.getvalue()
    else:
        name = "water.out"
    response = app.test_client().post("/api/orca/engine/parse", data={"file": (io.BytesIO(raw), name)})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["latest_job"]["ir_spectrum"] == []
    if archive:
        assert [entry["basename"] for entry in payload["archive_entries"]] == ["water.out"]
    payload_file = tmp_path / "analyzed.json"
    payload_file.write_text(json.dumps(payload), encoding="utf-8")
    source = Path(__file__).resolve().parents[1] / "static" / "js"
    script = r'''
const fs = require('node:fs'), assert = require('node:assert/strict');
const {join} = require('node:path');
const [src, payload] = process.argv.slice(1);
const app = fs.readFileSync(join(src, 'app.js'), 'utf8');
const currentEngineData = JSON.parse(fs.readFileSync(payload, 'utf8'));
require(join(src, 'publication-style.js'));require(join(src, 'publication-figures.js'));
const functionBody = name => { const start = app.indexOf('  function ' + name + '(');
  assert.ok(start >= 0, name); return app.slice(start, app.indexOf('\n  }', start) + 4); };
const document = {getElementById: () => null};
let loadedTheoreticalIRSpectra = [];
const loadedExperimentalIRSpectra = [], THEO_IR_PALETTE = ['#2374a8'];
const irViewAllows = () => true, updateIRSpectrumLayersTray = () => {};
const irViewportMode = 'standard', irManualRangeEnabled = false;
const irCustomTitles = {}, showIRLegend = false, showTheoreticalIRSticks = false, currentIRTheme = 'light';
let irYAxisMode = 'theory_intensity';
const formatAxisTick = v => String(v);
let downloaded;
const downloadTextFile = (name,text) => { downloaded={name,text}; };
eval(['analyzerIRData', 'addTheoreticalIRCalculationToStudio', 'getIRBounds', 'getIRPadding',
  'computeIRConvolution', 'getIRSharedNormMax', 'renderIRSpectrumToCanvas', 'publicationSpectrumSpec', 'exportIRData', 'buildXYText'].map(functionBody).join('\n') +
`\nconst ir = analyzerIRData(currentEngineData, currentEngineData.latest_job);
assert.equal(ir.step, 1); assert.equal(ir.hasBands, true);
addTheoreticalIRCalculationToStudio('water · step 1', ir.modes, 'ORCA', '', {calculationStep:ir.step});
const spec = publicationSpectrumSpec('ir');
assert.deepEqual(spec.series[0].points, [{x:1190.75,y:7.8},{x:2954.25,y:106.35}]);
exportIRData('csv');
assert.ok(downloaded.text.includes('2954.25,"Integrated intensity (km/mol)",106.35'));
assert.ok(downloaded.text.includes('1190.75,"Integrated intensity (km/mol)",7.8'));
exportIRData('xy');
const xy = downloaded.text.trim().split('\\n').filter(line=>!line.startsWith('#'));
assert.deepEqual(xy.slice(1).map(line=>line.split('\\t').map(Number)), [[2954.25,106.35],[1190.75,7.8]]);
assert.equal(spec.reverseX, true); assert.equal(spec.metadata.calculated_layers[0].step, 1);
const svg = PublicationFigures.plot(spec, PublicationFigures.settings({widthMm:85,heightMm:65,peakLabels:true}));
assert.ok(svg.includes('1191</text>')); assert.ok(svg.includes('2954</text>'));
const paths = [];let path;
const ctx = new Proxy({beginPath(){path=[];},moveTo(...p){path.push(p);},lineTo(...p){path.push(p);},
  stroke(){paths.push({color:this.strokeStyle,path});}}, {get(t,k){return t[k] ?? (()=>{});}});
const canvas = {getContext:()=>ctx,width:1000,height:380};
renderIRSpectrumToCanvas(canvas);
const bands = paths.filter(p=>p.color==='#2374a8' && p.path.length===2);
assert.equal(bands.length,2); assert.ok(bands[1].path[1][0]<bands[0].path[1][0]);
assert.ok(bands[1].path[1][1]<bands[0].path[1][1]);
irYAxisMode='transmittance';
const spectrum=publicationSpectrumSpec('ir');
assert.ok(Math.min(...spectrum.series[0].points.map(p=>p.y))<11);
assert.match(spectrum.yLabel,/pseudo/);
`);
'''
    result = subprocess.run([node, "-e", script, str(source), str(payload_file)],
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr


def test_orca_ir_spectrum_parsing():
    """Verify ORCA IR spectrum table parsing with frequencies and intensities."""
    parser = OrcaParser(SAMPLE_ORCA_IR_OUTPUT)
    jobs = parser.parse()
    assert len(jobs) == 1
    job = jobs[0]
    
    assert len(job.ir_frequencies_cm) == 3
    assert job.ir_frequencies_cm[0] == 1595.23
    assert job.ir_frequencies_cm[1] == 3657.05
    assert job.ir_frequencies_cm[2] == 3755.93
    
    assert len(job.ir_intensities_km_mol) == 3
    assert job.ir_intensities_km_mol[0] == 12.345
    assert job.ir_intensities_km_mol[1] == 45.678
    assert job.ir_intensities_km_mol[2] == 89.123

    assert len(job.ir_spectrum) == 3
    assert job.ir_spectrum[0]["mode"] == 6


def test_convolute_ir_spectrum_lorentzian():
    """Verify Lorentzian line broadening and %T calculation."""
    freqs = [1595.23, 3657.05, 3755.93]
    intensities = [12.345, 45.678, 89.123]
    
    curve = convolute_ir_spectrum(
        frequencies_cm=freqs,
        intensities_km_mol=intensities,
        scaling_factor=1.0,
        fwhm_cm=15.0,
        start_cm=400.0,
        end_cm=4000.0,
        step_cm=5.0
    )
    
    assert len(curve) > 100
    assert "wavenumber_cm" in curve[0]
    assert "absorbance" in curve[0]
    assert "transmittance_pct" in curve[0]
    
    # Transmittance should be between 0% and 100%
    for pt in curve:
        assert 0.0 <= pt["transmittance_pct"] <= 100.0


def test_api_ir_experimental_parse_text():
    """Test API endpoint for experimental FTIR text parsing."""
    client = app.test_client()
    data = {
        "file": (io.BytesIO(SAMPLE_FTIR_TEXT_TRANSMITTANCE.encode("utf-8")), "water_ftir.txt"),
        "file_name": "water_ftir.txt"
    }
    response = client.post("/api/orca/engine/ir-spectrum/parse-experimental", data=data, content_type="multipart/form-data")
    assert response.status_code == 200
    res_json = response.get_json()
    assert res_json["success"] is True
    spectrum = res_json["spectrum"]
    assert spectrum["points_count"] == 6
    assert len(spectrum["points"]) == 6
