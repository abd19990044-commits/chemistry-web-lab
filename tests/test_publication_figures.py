"""Publication exports preserve science, physical dimensions and raster metadata."""
import json
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / 'static/js/publication-figures.js'


def node(script, *args):
    if not shutil.which('node'):
        pytest.skip('Node.js required')
    result = subprocess.run(['node', '-e', f'require({str(MODULE)!r});const P=globalThis.PublicationFigures;const assert=require("node:assert/strict");\n'+script, *map(str,args)], cwd=ROOT, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_vector_plot_has_physical_size_safe_labels_and_exact_unmodified_coordinates():
    source = node(r'''
const spec={title:'<script>alert(1)</script>',xMin:400,xMax:4000,reverseX:true,yMin:0,yMax:100,xLabel:'Wavenumber (cm⁻¹)',yLabel:'Transmittance (%)',
series:[{name:'Experiment & reference',points:[{x:400,y:25},{x:4000,y:75}]}]};
const before=JSON.stringify(spec);const svg=P.plot(spec,P.settings({widthMm:85,heightMm:65}));
assert.equal(JSON.stringify(spec),before);assert.ok(!svg.includes('<script>'));
console.log(svg);
''')
    svg = ET.fromstring(source)
    assert svg.attrib['width'] == '85mm'
    assert svg.attrib['height'] == '65mm'
    assert svg.find('{*}metadata') is not None
    path = svg.find('.//{*}path')
    assert path is not None
    assert path.attrib['d'].startswith(f'M{85 * 72 / 25.4 - 15:.3f},')
    assert 'L52.000,' in path.attrib['d']  # 4000 cm^-1 is at the left, 400 at the right.
    assert svg.find('{*}g').attrib['font-size'] == '8'
    assert not svg.findall('.//{*}image')  # Real vector paths, no embedded screenshot.


def test_dual_axes_and_missing_data_are_explicit():
    node(r'''
const s=P.settings();
const base={xMin:200,xMax:800,yMin:0,yMax:10,xLabel:'nm',yLabel:'ε',y2Label:'Absorbance',y2Min:0,y2Max:1,
 series:[{name:'Calculated',points:[{x:300,y:5}]},{name:'Measured',axis:2,points:[{x:300,y:.5}]}]};
const svg=P.plot(base,s),paths=[...svg.matchAll(/<path d="([^"]+)"/g)].map(m=>m[1]);
assert.equal(paths[0],paths[1]);assert.ok(svg.includes('Absorbance'));
assert.throws(()=>P.plot({...base,series:[]},s),/No finite/);
assert.throws(()=>P.plot({...base,xMin:Infinity},s),/axis limits/);
assert.throws(()=>P.dimensions(P.settings({widthMm:180,heightMm:110,dpi:1200})),/32 megapixel/);
assert.doesNotThrow(()=>P.dimensions(P.settings({widthMm:180,heightMm:110,dpi:1200}),false));
''')


def test_orbital_axis_uses_actual_eigenvalues_and_no_fabricated_pairs():
    node(r'''
const svg=P.orbitals({alpha_homo_ev:-25,alpha_lumo_ev:-15,beta_homo_ev:-24,beta_lumo_ev:-14});
assert.ok(svg.includes('HOMO -25.000'));assert.ok(svg.includes('LUMO -14.000'));assert.ok(svg.includes('Δε = 10.000 eV'));
assert.ok(!svg.includes('Def2-TZVP'));assert.ok(!svg.includes('Gas Phase'));
assert.throws(()=>P.orbitals({homo_ev:null,lumo_ev:1}),/No complete/);
assert.ok(P.orbitals({homo_ev:0,lumo_ev:1}).includes('HOMO 0.000'));
assert.ok(P.orbitals({homo_ev:1,lumo_ev:0}).includes('Δε = -1.000 eV'));
''')


def test_tiff_rgb_resolution_and_alpha_compositing_are_readable_by_pillow(tmp_path):
    path = tmp_path / 'figure.tiff'
    node(r'''
const rgba=Uint8Array.from([255,0,0,255,0,255,0,255,0,0,255,128,0,0,0,0]);
require('node:fs').writeFileSync(process.argv[1],P.tiff(rgba,2,2,600,{source:'ORCA',label:'δ'}));
''', path)
    with Image.open(path) as im:
        assert im.format == 'TIFF'
        assert im.mode == 'RGB'
        assert im.size == (2, 2)
        assert im.info['dpi'] == (600, 600)
        assert im.getpixel((0, 0)) == (255, 0, 0)
        assert im.getpixel((1, 0)) == (0, 255, 0)
        assert im.getpixel((0, 1)) == (127, 127, 255)
        assert im.getpixel((1, 1)) == (255, 255, 255)
        assert 'ORCA' in im.tag_v2[270]
        assert json.loads(im.tag_v2[270])['label'] == 'δ'


def test_png_resolution_and_utf8_metadata_do_not_change_pixels(tmp_path):
    path = tmp_path / 'figure.png'
    Image.new('RGB', (3, 2), (12, 34, 56)).save(path, dpi=(96, 96))
    node(r'''
const fs=require('node:fs'),bytes=fs.readFileSync(process.argv[1]);
fs.writeFileSync(process.argv[1],P.pngMetadata(bytes,600,{axis:'δ (ppm)',source:'ORCA'}));
''', path)
    with Image.open(path) as im:
        assert im.size == (3, 2)
        assert im.getpixel((1, 1)) == (12, 34, 56)
        assert im.info['dpi'] == pytest.approx((600, 600), abs=.02)
        assert json.loads(im.info['Description']) == {'axis': 'δ (ppm)', 'source': 'ORCA'}


def test_geometry_capture_restores_camera_and_styles_on_gpu_failure():
    node(r'''
const original=[1,2,3,4,0,0,0,1];let view=original.slice(),restores=0,background,width;
const viewer={getView:()=>view,getRenderer:()=>({domElement:{width:800}}),
 setView:v=>{view=v;},setBackgroundColor:c=>{background=c;},setWidth:w=>{width=w;view=[9];},setHeight(){},resize(){},render(){},
 pngURI(){assert.equal(background,'#ffffff');assert.ok(width>4000);throw Error('GPU failure');}};
const viewport={getBoundingClientRect:()=>({width:800,height:400})};
(async()=>{
 for(let i=0;i<2;i++){
  await assert.rejects(P.saveMolecule(viewer,viewport,{restore(){restores++;background='#000000';}}),/GPU failure/);
  assert.deepEqual(view,original);assert.equal(background,'#000000');
 }
 assert.equal(restores,2);
})().catch(e=>{console.error(e);process.exit(1);});
''')


def test_spectrum_adapter_preserves_raw_samples_and_declares_all_transformations():
    app = (ROOT / 'static/js/app.js').read_text(encoding='utf-8')
    function = app[app.index('  function publicationSpectrumSpec(kind)'):app.index('  async function exportPublicationSpectrum(')]
    node(r'''
const document={getElementById:id=>({value:({'engine-sigma-slider':20,'engine-shift-slider':5,'engine-ir-scale-slider':1.1,'engine-ir-fwhm-slider':15,'engine-ir-shift-slider':5})[id]})};
const currentEngineData={name:'fixture'},uvViewAllows=()=>true,irViewAllows=()=>true;
const loadedTheoreticalSpectra=[{name:'TD-DFT',visible:true,transitions:[]}];
const loadedExperimentalSpectra=[{label:'Measured',visible:true,raw_data:[{wavelength_nm:200,absorbance:1},{wavelength_nm:300,absorbance:8}]}];
const getUVVisBounds=()=>({minX:180,maxX:800}),computeUvvisConvolution=()=>[{wavelength_nm:200,intensity:2},{wavelength_nm:300,intensity:4}];
const spectrumNormalizeMode='all',uvvisManualRangeEnabled=false,uvvisManualMinY=null,uvvisManualMaxY=null,uvCustomTitles={},showUVVisLegend=true;
const loadedTheoreticalIRSpectra=[{name:'DFT',visible:true,modes:[{frequency_cm:1000,intensity_km_mol:100}]}];
let loadedExperimentalIRSpectra=[{label:'FTIR',visible:true,raw_data:[{wavenumber_cm:1000,transmittance_pct:50,absorbance:.5}]}];
const getIRBounds=()=>({minWn:400,maxWn:4000}),getIRSharedNormMax=()=>1,computeIRConvolution=()=>[{wavenumber_cm:1105,transmittance_pct:40,absorbance:.4}];
let irYAxisMode='transmittance';const irManualRangeEnabled=false,irManualMinY=null,irManualMaxY=null,irCustomTitles={},showIRLegend=true;
const currentNMRNucleus='1H',currentNMRReference='none';
const currentNMRData={h1_spectrum:{min_ppm:0,max_ppm:10,is_reference_applied:false,peaks:[{position_ppm:3,intensity:2}]}};
''' + function + r'''
const before=JSON.stringify([loadedExperimentalSpectra,loadedExperimentalIRSpectra,currentNMRData]);
const uv=publicationSpectrumSpec('uvvis');assert.equal(uv.series[0].points[0].y,.5);assert.equal(uv.series[1].points[0].y,.125);assert.equal(uv.series[1].axis,2);assert.equal(uv.metadata.theoretical_shift_nm,5);
const ir=publicationSpectrumSpec('ir');assert.equal(ir.series[0].points[0].x,1105);assert.equal(ir.series[1].points[0].x,1000);assert.equal(ir.series[1].points[0].y,50);assert.equal(ir.reverseX,true);assert.ok(ir.yLabel.includes('pseudo'));
const nmr=publicationSpectrumSpec('nmr');assert.equal(nmr.reverseX,false);assert.ok(nmr.xLabel.includes('shielding'));assert.equal(nmr.series[0].points[0].x,3);
assert.equal(JSON.stringify([loadedExperimentalSpectra,loadedExperimentalIRSpectra,currentNMRData]),before);
irYAxisMode='theory_intensity';assert.throws(()=>publicationSpectrumSpec('ir'),/Integrated band intensities/);
loadedExperimentalIRSpectra=[];const bands=publicationSpectrumSpec('ir');assert.equal(bands.series[0].sticks,true);assert.equal(bands.series[0].points[0].y,100);
currentNMRData.h1_spectrum.is_reference_applied=true;assert.equal(publicationSpectrumSpec('nmr').reverseX,true);
''')
