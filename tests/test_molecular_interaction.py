"""Execute real molecular pointer transactions and coordinate invariants in Node."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / 'static/js/molecular-controls.js'
APP = (ROOT / 'static/js/app.js').read_text(encoding='utf-8')


def node(script):
    executable = shutil.which('node')
    if not executable:
        pytest.skip('Node.js required for molecular controller tests')
    result = subprocess.run([executable, '-e', script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


BASE = f"require({str(MODULE)!r}); const C=globalThis.MolecularControls; const assert=require('node:assert/strict');\n"
BASE += r'''
const close=(a,b,t=1e-8)=>assert.ok(Math.abs(a-b)<t, `${a} != ${b}`);
const position=(a)=>({x:a.x,y:a.y,z:a.z});
'''


def test_projection_and_rigid_geometry_at_arbitrary_camera_rotation_and_zoom():
    node(BASE + r'''
for (let i=0; i<80; i++) {
  const q=C.rotation({x:1+i*.1,y:3,z:-2},i*.117);
  const view=[0,0,0,0,...q], b=C.basis(view), zoom=4+i*3;
  const viewer={getView:()=>view, modelToScreen(p) {
    const r=C.rotate(p,q);return {x:900+zoom*r.x,y:700-zoom*r.y};
  }};
  const d=C.screenDelta(viewer,{x:2,y:1,z:4},40,-20);
  const p=C.rotate(d,q);
  close(p.x,40/zoom); close(p.y,20/zoom); close(p.z,0);
  const fine=C.screenDelta(viewer,{x:2,y:1,z:4},40,-20,{shiftKey:true});
  close(fine.x,d.x*.2);close(fine.y,d.y*.2);close(fine.z,d.z*.2);
  const depth=C.screenDelta(viewer,{x:2,y:1,z:4},0,-40,{altKey:true});
  close(C.dot(depth,b.right),0);close(C.dot(depth,b.up),0);
  for(const axis of ['x','y','z']) {
    const constrained=C.screenDelta(viewer,{x:2,y:1,z:4},25,45,{axis});
    assert.ok(Object.values(constrained).every(Number.isFinite));
    for(const a of ['x','y','z']) if(a!==axis) close(constrained[a],0);
  }
}
const atoms=[{id:1,x:0,y:0,z:0},{id:2,x:1,y:2,z:3},{id:3,x:7,y:8,z:9}];
const original=new Map(atoms.map(a=>[a.id,position(a)])), ids=new Set([1,2]);
const center=C.centroid(atoms.slice(0,2)), initial=Math.hypot(1,2,3);
C.applyTransform(atoms,original,ids,{x:0,y:0,z:0},C.rotation({x:1,y:2,z:3},1.2),center);
close(Math.hypot(atoms[1].x-atoms[0].x,atoms[1].y-atoms[0].y,atoms[1].z-atoms[0].z),initial);
assert.deepEqual(position(atoms[2]),original.get(3));
close(C.centroid(atoms.slice(0,2)).x,center.x);
''')


HARNESS = r'''
global.window=new EventTarget();
let nextFrame=0, frames=new Map();
global.requestAnimationFrame=callback=>{frames.set(++nextFrame,callback);return nextFrame;};
global.cancelAnimationFrame=id=>frames.delete(id);
const flush=()=>{const fs=[...frames.values()];frames.clear();fs.forEach(f=>f());};
class Surface extends EventTarget {
  constructor(){super();this.style={};this.clientHeight=500;this.captures=new Set();this.classList={add(){},remove(){}};}
  focus(){} setPointerCapture(id){this.captures.add(id);} hasPointerCapture(id){return this.captures.has(id);} releasePointerCapture(id){this.captures.delete(id);}
}
const surface=new Surface();
let view=[0,0,0,0,0,0,0,1], pans=0, zooms=0, starts=0, commits=0, cancels=0, clicks=0, redraws=0;
const state={activeTool:'move',transformAxis:'free',atoms:[{id:1,molId:1,x:0,y:0,z:0},{id:2,molId:1,x:1,y:0,z:0},{id:3,molId:2,x:6,y:0,z:0}]};
const viewer={getView:()=>view,setView:v=>view=v,translate(){pans++;},zoom(){zooms++;},modelToScreen:p=>({x:p.x*40+400,y:300-p.y*40})};
const controller=C.createController({element:surface,getViewer:()=>viewer,getState:()=>state,
  pick:()=>state.atoms[0],select(){},click(){clicks++;},redraw(){redraws++;},
  begin(){starts++;},commit(){commits++;},cancel(){cancels++;}});
function send(type,x=100,y=100,extra={}) {
  const e=new Event(type,{cancelable:true});
  Object.assign(e,{pointerId:1,pointerType:'mouse',button:0,clientX:x,clientY:y,shiftKey:false,altKey:false,deltaY:30,deltaMode:0,...extra});
  surface.dispatchEvent(e);return e;
}
const reset=()=>{state.atoms.forEach((a,i)=>Object.assign(a,{x:[0,1,6][i],y:0,z:0}));};
'''


def test_pointer_drag_is_rigid_coalesced_and_one_undo_transaction():
    node(BASE + HARNESS + r'''
send('pointerdown');send('pointermove',120,100);send('pointermove',140,100);
assert.equal(frames.size,1);close(state.atoms[0].x,1);close(state.atoms[1].x,2);close(state.atoms[2].x,6);
assert.equal(starts,1);assert.equal(commits,0);flush();assert.equal(redraws,1);
send('pointerup',140,100);assert.equal(commits,1);assert.equal(clicks,0);
controller.destroy();
''')


def test_navigation_inspection_wheel_and_right_drag_never_edit_geometry():
    node(BASE + HARNESS + r'''
for(const tool of ['navigate','select','delete']) {
  state.activeTool=tool;
  const before=structuredClone(state.atoms);
  send('pointerdown',100,100,{button:2});send('pointermove',140,120,{button:2});send('pointerup',140,120,{button:2});
  assert.deepEqual(state.atoms,before);
}
assert.equal(pans,3);assert.equal(clicks,0);
state.activeTool='navigate';send('pointerdown');send('pointermove',135,130);send('pointerup',135,130);
assert.notDeepEqual(view,[0,0,0,0,0,0,0,1]);assert.equal(starts,0);assert.equal(commits,0);
state.activeTool='moveAtom';send('wheel');assert.equal(zooms,1);assert.equal(starts,0);
assert.deepEqual(state.atoms.map(a=>a.x),[0,1,6]);
controller.destroy();
''')


def test_click_threshold_and_cancel_restore_coordinates_without_history():
    node(BASE + HARNESS + r'''
send('pointerdown');send('pointermove',102,100);send('pointerup',102,100);
assert.equal(starts,0);assert.equal(commits,0);assert.equal(clicks,1);
send('pointerdown');send('pointermove',150,120);controller.finish(true);
assert.equal(cancels,1);assert.equal(commits,0);assert.deepEqual(state.atoms.map(a=>a.x),[0,1,6]);assert.equal(frames.size,0);
send('pointerdown');send('pointermove',140,100);window.dispatchEvent(new Event('blur'));
assert.equal(cancels,2);assert.deepEqual(state.atoms.map(a=>a.x),[0,1,6]);
controller.destroy();send('pointerdown');send('pointermove',200,200);assert.deepEqual(state.atoms.map(a=>a.x),[0,1,6]);
''')


def test_single_atom_axis_lock_and_fragment_rotation_invariants():
    node(BASE + HARNESS + r'''
state.activeTool='moveAtom';state.transformAxis='x';
send('pointerdown');send('pointermove',140,180);send('pointerup',140,180);
close(state.atoms[0].x,1);close(state.atoms[0].y,0);close(state.atoms[1].x,1);close(state.atoms[2].x,6);
reset();state.activeTool='rotate';state.transformAxis='free';
send('pointerdown');send('pointermove',150,180);send('pointerup',150,180);
const [a,b,other]=state.atoms;
close(Math.hypot(a.x-b.x,a.y-b.y,a.z-b.z),1);close((a.x+b.x)/2,.5);
assert.deepEqual(position(other),{x:6,y:0,z:0});
controller.destroy();
''')


def test_touch_pinch_cancels_geometry_edit_and_controls_camera():
    node(BASE + HARNESS + r'''
send('pointerdown',100,100,{pointerType:'touch'});send('pointermove',130,100,{pointerType:'touch'});
send('pointerdown',200,100,{pointerType:'touch',pointerId:2});
assert.equal(cancels,1);assert.deepEqual(state.atoms.map(a=>a.x),[0,1,6]);
send('pointermove',220,100,{pointerType:'touch',pointerId:2});send('pointermove',240,100,{pointerType:'touch',pointerId:2});
assert.equal(zooms,1);assert.equal(pans,1);assert.equal(commits,0);
controller.destroy();
''')


def test_picking_corrects_page_scroll_and_front_atom_depth():
    start = APP.index('    function findAtomUnderCursor(')
    end = APP.index('\n    function updateLiveGeometryInfo(', start)
    node(BASE + APP[start:end] + r'''
global.window={scrollX:40,scrollY:500};
const canvasEl={getBoundingClientRect:()=>({left:10,top:10,right:500,bottom:500,width:490,height:490})};
const MolecularControls=C;
const bState={atoms:[{id:1,x:0,y:0,z:-5},{id:2,x:0,y:0,z:5}],viewer:{
  getView:()=>[0,0,0,0,0,0,0,1],targetedObjects:()=>[],modelToScreen:()=>({x:140,y:600})}};
assert.equal(findAtomUnderCursor(100,100).id,2);
assert.equal(findAtomUnderCursor(600,100),null);
''')


def test_neighboring_molecules_do_not_form_accidental_bonds():
    start = APP.index('  function reconcileBondsAvogadro(')
    end = APP.index('\n  window.reconcileBondsAvogadro', start)
    node(BASE + APP[start:end] + r'''
const atoms=[{id:10,molId:1,elem:'C',x:0,y:0,z:0},{id:20,molId:2,elem:'C',x:1.4,y:0,z:0}];
const model={selectedAtoms:()=>atoms};
reconcileBondsAvogadro(model,{fragmentCheck:true,explicitBonds:[]});
assert.deepEqual(atoms.map(a=>a.bonds),[[],[]]);
reconcileBondsAvogadro(model,{fragmentCheck:true,explicitBonds:[{a1:10,a2:20,order:1}]});
assert.deepEqual(atoms.map(a=>a.bonds),[[1],[0]]);
''')
