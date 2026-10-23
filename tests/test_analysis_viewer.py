"""Read-only geometry inspector and measurement regression checks."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def node(script):
    if not shutil.which('node'):
        pytest.skip('Node.js required')
    prefix = ''.join(f'require({str(ROOT / "static/js" / name)!r});\n' for name in ['molecule-graph.js', 'molecular-controls.js', 'analysis-viewer.js'])
    result = subprocess.run(['node', '-e', prefix + 'const assert=require("node:assert/strict"),V=globalThis.AnalysisViewer;\n' + script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_distance_angle_and_signed_dihedral_with_degenerate_inputs():
    node('''
const close=(a,b)=>assert.ok(Math.abs(a-b)<1e-8,`${a} != ${b}`);
const p=(x,y,z)=>({x,y,z});
close(V.measure([p(0,0,0),p(1,2,2)]).value,3);
close(V.measure([p(1,0,0),p(0,0,0),p(0,1,0)]).value,90);
close(V.measure([p(1,0,0),p(0,0,0),p(0,1,0),p(1,1,0)]).value,0);
close(Math.abs(V.measure([p(1,0,0),p(0,0,0),p(0,1,0),p(-1,1,0)]).value),180);
close(V.measure([p(1,0,0),p(0,0,0),p(0,1,0),p(0,1,1)]).value,-90);
assert.equal(V.measure([p(0,0,0),p(0,0,0),p(0,1,0)]).value,null);
assert.equal(V.measure([p(0,0,0),p(1,0,0),p(2,0,0),p(3,0,0)]).value,null);
''')


def test_source_topology_mapping_preserves_calculated_coordinates():
    node('''
const atoms=[{id:1,elem:'C',x:5,y:2,z:1},{id:2,elem:'O',x:6,y:2,z:1}];
const molecule={atoms:[{id:40,elem:'C',x:0,y:0,z:0},{id:60,elem:'O',x:1.2,y:0,z:0}],bonds:[{a1:40,a2:60,order:2}]};
const before=JSON.stringify(atoms),b=V.attachTopology(atoms,molecule);
assert.deepEqual(b,[{a1:1,a2:2,order:2}]);assert.equal(JSON.stringify(atoms),before);
assert.throws(()=>V.attachTopology(atoms,{...molecule,atoms:[...molecule.atoms].reverse()}),/Atom order/);
''')


def test_analysis_style_never_reperceives_bonds_and_fallback_is_live_3d():
    app = (ROOT / 'static/js/app.js').read_text(encoding='utf-8')
    style = app.split('  function applyViewer3DStyle()', 1)[1].split('  function init3DViewer(', 1)[0]
    assert 'reconcileBondsAvogadro' not in style
    assert 'analysisStudio?.draw()' in style
    assert 'MolecularControls.createSoftwareViewer(viewport)' in app
    assert 'const vec = job.dipole_moment_vector ||' not in style
    assert 'Charge count (' in app


def test_canvas_fallback_renders_background_labels_shapes_and_png():
    node('''
global.window={devicePixelRatio:1,scrollX:0,scrollY:0};
let arrowSegments=0,textColor=null,background=null;
const context=new Proxy({createLinearGradient:()=>({addColorStop(){}}),createRadialGradient:()=>({addColorStop(){}}),measureText:()=>({width:20}),fillRect(){background=this.fillStyle;},fillText(){textColor=this.fillStyle;},lineTo(){arrowSegments++;}}, {get:(o,k)=>k in o?o[k]:(()=>{})});
const canvas={style:{},setAttribute(){},getContext:()=>context,toDataURL:()=>`data:image/png;size=${canvas.width}`};
global.document={createElement:()=>canvas};
const element={appendChild(){},getBoundingClientRect:()=>({width:800,height:500,left:0,top:0})};
const viewer=MolecularControls.createSoftwareViewer(element),model=viewer.addModel('2\\nCO\\nC 0 0 0\\nO 1.2 0 0');
assert.equal(viewer.getModel(),model);viewer.setStyle({},{sphere:{scale:.3}});viewer.setBackgroundColor('#ffffff',1);
viewer.addArrow({start:{x:0,y:0,z:0},end:{x:1,y:1,z:0},color:'#f00'});viewer.addLabel('O2',{position:{x:1.2,y:0,z:0},fontColor:'#000000',showBackground:false});viewer.zoomTo();viewer.render();
assert.equal(background,'#ffffff');assert.equal(textColor,'#000000');assert.ok(arrowSegments>=3);
assert.equal(viewer.pngURI(2),'data:image/png;size=1600');assert.equal(canvas.width,800);
viewer.removeAllShapes();viewer.removeAllLabels();viewer.removeAllSurfaces();
''')


def test_readonly_inspector_camera_measurements_and_cleanup():
    node('''
global.window=new EventTarget();global.requestAnimationFrame=()=>1;global.cancelAnimationFrame=()=>{};
let disconnected=false;global.ResizeObserver=class {observe(){}disconnect(){disconnected=true;}};
class El extends EventTarget {constructor(){super();this.value='0';this.clientWidth=800;this.clientHeight=500;this.textContent='';this.style={};this.classList={add(){},remove(){},toggle(){}};}setAttribute(){}replaceChildren(){}focus(){}setPointerCapture(){}hasPointerCapture(){return false;}getBoundingClientRect(){return {left:0,top:0,right:800,bottom:500,width:800,height:500};}getClientRects(){return [this.getBoundingClientRect()];}}
const els=new Map();global.document={hidden:false,getElementById:id=>{if(!els.has(id))els.set(id,new El());return els.get(id);}};
const atoms=[{id:1,index:0,elem:'C',x:0,y:0,z:0,bonds:[],bondOrder:[]},{id:2,index:1,elem:'O',x:1.2,y:0,z:0,bonds:[],bondOrder:[]}];
const snapshot=JSON.stringify(atoms.map(a=>[a.x,a.y,a.z]));let hit=atoms[0],view=[0,0,0,5,0,0,0,1],renders=0;
const model={selectedAtoms:()=>atoms,setClickable(){}};
const viewer={getModel:()=>model,render(){renders++;},setProjection(){},getView:()=>view,setView:v=>{view=v;},zoomTo(){},zoom(){},translate(){},resize(){},targetedObjects:()=>[{clickable:hit}],addStyle(){},addLabel(){},addLine(){}};
const viewport=new El(),job={mulliken_charges:[.5,-.5]};let scene;
scene=V.create({viewer,viewport,job,restyle(){scene?.draw();}});
const event=(type,extra={})=>{const e=new Event(type,{cancelable:true});Object.assign(e,{pointerId:1,pointerType:'mouse',button:0,clientX:100,clientY:100,deltaY:40,deltaMode:0,...extra});viewport.dispatchEvent(e);};
event('pointerdown');event('pointerup');assert.match(document.getElementById('engine-3d-inspector').textContent,/Mulliken: \\+0.500000/);
const mode=document.getElementById('engine-3d-measure-mode');mode.value='2';mode.dispatchEvent(new Event('change'));
event('pointerdown');event('pointerup');hit=atoms[1];event('pointerdown');event('pointerup');assert.match(document.getElementById('engine-3d-inspector').textContent,/Distance: 1.2000 Å/);
event('pointerdown');event('pointermove',{clientX:150,clientY:130});event('pointerup',{clientX:150,clientY:130});event('wheel');
assert.equal(JSON.stringify(atoms.map(a=>[a.x,a.y,a.z])),snapshot);assert.notDeepEqual(view.slice(4),[0,0,0,1]);
scene.destroy();const before=renders;event('wheel');assert.equal(renders,before);assert(disconnected);
''')


def test_publication_preview_discards_old_renders_and_exports_manual_label_positions():
    node(r'''
(async()=>{
global.window=new EventTarget();global.requestAnimationFrame=()=>1;global.cancelAnimationFrame=()=>{};
global.ResizeObserver=class{observe(){}disconnect(){}};
class El extends EventTarget{
 constructor(){super();this.value='off';this.hidden=false;this.disabled=false;this.children=[];this.attributes={};this.style={};this.classList={add(){},remove(){},toggle(){}};}
 setAttribute(k,v){this.attributes[k]=v;}replaceChildren(...children){this.children=children;}
 getBoundingClientRect(){return {left:0,top:0,width:800,height:500};}getClientRects(){return [1];}
}
const els=new Map();global.document={hidden:false,getElementById:id=>{if(!els.has(id))els.set(id,new El());return els.get(id);}};
const el=id=>document.getElementById(id),atoms=[{id:1,index:0,elem:'C',x:0,y:0,z:0,bonds:[],bondOrder:[]},{id:2,index:1,elem:'N',x:1.3,y:0,z:0,bonds:[],bondOrder:[]}];
let view=[0,0,0,5,0,0,0,1];const before=JSON.stringify(atoms.map(a=>[a.x,a.y,a.z]));
const viewer={getModel:()=>({selectedAtoms:()=>atoms,setClickable(){}}),getView:()=>view,setView:v=>{view=v;},setProjection(){},render(){},addStyle(){},addLabel(){},addLine(){}};
const pending=[];let exported,moveLabel,destroyed=0;
global.PublicationFigures={renderMolecule:()=>new Promise(resolve=>pending.push(resolve)),saveMolecule:async(v,p,o)=>{exported=o;}};
global.PublicationMolecule={createLabelController(options){moveLabel=options.move;return {destroy(){destroyed++;}};}};
const studio=V.create({viewer,viewport:new El(),job:{},restyle(){}}),button=el('engine-3d-figure-preview-btn'),preview=el('engine-3d-figure-preview');
const click=id=>el(id).dispatchEvent(new Event('click')),tick=()=>new Promise(resolve=>setImmediate(resolve));
const result=()=>({canvas:Object.assign(new El(),{width:800,height:500}),labelBoxes:[],redrawLabels:()=>[]});
click('engine-3d-figure-preview-btn');assert.equal(button.disabled,true);
el('engine-3d-label-color').dispatchEvent(new Event('input'));const obsolete=result();pending.shift()(obsolete);await tick();
assert.equal(obsolete.canvas.width,1);assert.equal(preview.hidden,true,'Outdated async result must not replace the preview');
click('engine-3d-figure-preview-btn');pending.shift()(result());await tick();assert.equal(preview.hidden,false);assert.equal(preview.attributes['data-stale'],'false');
moveLabel(0,{x:.4,y:.2});click('engine-3d-download-png');await tick();assert.deepEqual(exported.labelPositions,{0:{x:.4,y:.2}});
el('engine-3d-label-color').dispatchEvent(new Event('change'));assert.equal(preview.attributes['data-stale'],'true');assert.equal(destroyed,1);
click('engine-3d-figure-preview-btn');pending.shift()(result());await tick();click('engine-3d-download-png');await tick();assert.deepEqual(exported.labelPositions,{0:{x:.4,y:.2}},'Changing color must preserve manual positions');
click('engine-3d-view-right');click('engine-3d-download-png');await tick();assert.deepEqual(exported.labelPositions,{},'New camera orientation invalidates old page positions');
assert.equal(JSON.stringify(atoms.map(a=>[a.x,a.y,a.z])),before);studio.destroy();
})().catch(e=>{console.error(e);process.exit(1);});
''')
