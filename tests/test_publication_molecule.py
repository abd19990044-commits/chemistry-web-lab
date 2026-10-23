"""Verify actual molecular pixels, occlusion, multiplicity and label placement."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def node(script):
    if not shutil.which('node'):
        pytest.skip('Node.js required')
    prefix = 'const assert=require("node:assert/strict");\n'
    prefix += ''.join(f'require({str(ROOT / "static/js" / name)!r});\n' for name in ['publication-molecule.js', 'publication-figures.js'])
    result = subprocess.run(['node', '-e', prefix + 'const M=global.PublicationMolecule,P=global.PublicationFigures;\n' + script], cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_source_bond_orders_are_visible_as_separate_pixel_lanes():
    node(r'''
(async()=>{
for(const order of [1,2,3]){
 const atoms=[{id:1,elem:'C',x:-1.2,y:0,z:0,bonds:[1],bondOrder:[order]},{id:2,elem:'N',x:1.2,y:0,z:0,bonds:[0],bondOrder:[order]}];
 const before=JSON.stringify(atoms),s=M.scene(atoms,[0,0,0,1,0,0,0,1]);
 assert.equal(s.primitives.filter(p=>p.type==='cylinder').length,order);
 const r=await M.raster(s,600,350);let bands=0,inBand=false;
 for(let y=0;y<350;y++){const offset=(y*600+300)*4,dark=r.data[offset]<220;if(dark&&!inBand)bands++;inBand=dark;}
 assert.equal(bands,order,`Source order ${order} must produce ${order} distinct bands, got ${bands}`);
 assert.equal(JSON.stringify(atoms),before);
}
})().catch(e=>{console.error(e);process.exit(1);});
''')


def test_depth_buffer_is_independent_of_atom_order_and_transparency_is_real():
    node(r'''
(async()=>{
const atoms=[{elem:'O',x:0,y:0,z:1},{elem:'N',x:0,y:0,z:-1}];
const render=async atoms=>M.raster(M.scene(atoms,[0,0,0,1,0,0,0,1]),120,100,{background:'transparent'});
const a=await render(atoms),b=await render([...atoms].reverse());
assert.deepEqual(a.data,b.data);assert.equal(a.data[3],0);
const at=(50*120+60)*4;assert.equal(a.data[at+3],255);assert.ok(a.data[at]>a.data[at+2]*2,'Front oxygen must occlude rear nitrogen');
const cylinder={a:{x:0,y:0,z:-1},b:{x:0,y:0,z:1},r:.1};
const hit=M.cylinderHit(cylinder,0,0);assert.equal(hit.z,1);assert.equal(hit.nz,1);
assert.equal(M.cylinderHit(cylinder,1,0),null);
})().catch(e=>{console.error(e);process.exit(1);});
''')


def test_aromatic_source_is_dashed_and_disconnected_ions_stay_disconnected():
    node(r'''
const atoms=[{elem:'C',x:0,y:0,z:0,bonds:[1],bondOrder:[1.5]},{elem:'C',x:1.4,y:0,z:0,bonds:[0],bondOrder:[1.5]},{elem:'Cl',x:4,y:0,z:0,bonds:[],bondOrder:[]}];
const before=JSON.stringify(atoms),s=M.scene(atoms,[0,0,0,1,0,0,0,1]);
assert.equal(s.bonds.length,1);assert.equal(s.bonds[0].order,1.5);
assert.equal(s.primitives.filter(p=>p.type==='cylinder').length,4);
assert.equal(JSON.stringify(atoms),before);
const f=M.frame(s,800,500);for(const p of s.points){assert.ok(f.x+(p.x-p.r)*f.scale>0);assert.ok(f.x+(p.x+p.r)*f.scale<800);}
''')


def test_outside_labels_fit_page_without_overlapping_each_other():
    node(r'''
const atoms=Array.from({length:12},(_,i)=>({elem:'C',x:Math.cos(i*Math.PI/6)*2,y:Math.sin(i*Math.PI/6)*2,z:0}));
const s=M.scene(atoms,[0,0,0,1,0,0,0,1]),f=M.frame(s,900,600,.15),labels=atoms.map((a,i)=>`C${i+1}`);
const boxes=M.layoutLabels(s,f,900,600,labels,18,text=>text.length*10);
assert.equal(boxes.length,12);
assert.throws(()=>M.layoutLabels(s,f,100,60,["A".repeat(80)],18,text=>text.length*10),/Labels do not fit/);
for(const [i,a] of boxes.entries()){
 assert.ok(a.x>=0&&a.y>=0&&a.x+a.w<=900&&a.y+a.h<=600);
 for(const b of boxes.slice(i+1))assert.ok(a.x+a.w<=b.x||b.x+b.w<=a.x||a.y+a.h<=b.y||b.y+b.h<=a.y,'Labels must not overlap');
}
''')


def test_print_render_respects_ui_settings_and_never_captures_or_changes_viewer():
    result = node(r'''
(async()=>{
const values={'figure-width-mm':'85','figure-height-mm':'65','figure-dpi':'300','engine-3d-label-size':'9','engine-3d-label-color':'#c01234','engine-3d-bg':'custom','engine-3d-bg-color':'#aabbcc','engine-3d-label-layout':'outside'};
const text=[];let image;
const ctx={createImageData:(w,h)=>({data:new Uint8ClampedArray(w*h*4)}),putImageData:i=>{image=i;},measureText:s=>({width:s.length*10}),beginPath(){},moveTo(){},lineTo(){},stroke(){},strokeText(){},fillText(t){text.push({t,color:this.fillStyle,font:this.font});}};
global.document={getElementById:id=>values[id]?{value:values[id]}:null,createElement:()=>({getContext:()=>ctx})};
const atoms=[{elem:'N',x:1,y:2,z:3,bonds:[],bondOrder:[]}],view=[0,0,0,5,0,0,0,1],before=JSON.stringify({atoms,view});
const viewer={getModel:()=>({selectedAtoms:()=>atoms}),getView:()=>view,pngURI(){throw Error('Screenshot must not be used');}};
const r=await P.renderMolecule(viewer,{labels:['N1']},true);
assert.equal(JSON.stringify({atoms,view}),before);assert.equal(r.metadata.background,'#aabbcc');assert.equal(r.metadata.label_size_pt,9);assert.equal(r.metadata.label_color,'#c01234');
assert.equal(text[0].color,'#c01234');assert.ok(Math.abs(Number(text[0].font.match(/([0-9.]+)px/)[1])-37.5)<.02);
assert.deepEqual([...image.data.slice(0,4)],[170,187,204,255]);
console.log(JSON.stringify({width:r.canvas.width,height:r.canvas.height}));
})().catch(e=>{console.error(e);process.exit(1);});
''')
    assert json.loads(result) == {'width': 1004, 'height': 768}


def test_methylene_blue_keeps_all_seven_double_bonds_and_the_isolated_counterion():
    node(r'''
const fs=require('node:fs'),lines=fs.readFileSync('tests/fixtures/browser_chemistry/methylene-blue.sdf','utf8').split(/\r?\n/),n=Number(lines[3].slice(0,3)),nb=Number(lines[3].slice(3,6));
const atoms=lines.slice(4,4+n).map((l,i)=>({id:i+1,elem:l.slice(31,34).trim(),x:Number(l.slice(0,10)),y:Number(l.slice(10,20)),z:Number(l.slice(20,30)),bonds:[],bondOrder:[]}));
for(const l of lines.slice(4+n,4+n+nb)){const a=Number(l.slice(0,3))-1,b=Number(l.slice(3,6))-1,order=Number(l.slice(6,9));atoms[a].bonds.push(b);atoms[a].bondOrder.push(order);atoms[b].bonds.push(a);atoms[b].bondOrder.push(order);}
const before=JSON.stringify(atoms),scene=M.scene(atoms,[0,0,0,5,0,0,0,1]);
assert.equal(atoms.length,39);assert.equal(scene.bonds.length,40);assert.equal(scene.bonds.filter(b=>b.order===2).length,7);
assert.equal(scene.primitives.filter(p=>p.type==='cylinder').length,47);
assert.equal(atoms.find(a=>a.elem==='Cl').bonds.length,0);assert.equal(JSON.stringify(atoms),before);
''')


def test_label_drag_handles_css_scaling_keyboard_cancel_and_cleanup():
    node(r'''
class Canvas extends EventTarget {constructor(){super();this.style={};this.captured=false;}getBoundingClientRect(){return {left:100,top:50,width:500,height:300};}focus(){}setPointerCapture(){this.captured=true;}hasPointerCapture(){return this.captured;}releasePointerCapture(){this.captured=false;}}
const canvas=new Canvas();let position={x:.3,y:.4},updates=0;
const boxes=()=>[{atom:2,x:position.x-.05,y:position.y-.05,w:.1,h:.1}];
const controller=M.createLabelController({canvas,getBoxes:boxes,move(atom,p){assert.equal(atom,2);position=p;updates++;}});
const event=(type,extra={})=>{const e=new Event(type,{cancelable:true});Object.assign(e,{button:0,pointerId:1,clientX:250,clientY:170,...extra});canvas.dispatchEvent(e);};
event('pointerdown');event('pointermove',{clientX:350,clientY:200});assert.ok(Math.abs(position.x-.5)<1e-10);assert.ok(Math.abs(position.y-.5)<1e-10);
event('keydown',{key:'Escape'});assert.deepEqual(position,{x:.3,y:.4});assert.equal(canvas.captured,false);
event('pointerdown');event('pointermove',{clientX:300});event('pointerup');assert.ok(Math.abs(position.x-.4)<1e-10);
event('keydown',{key:'ArrowRight'});assert.ok(Math.abs(position.x-.402)<1e-10);
event('keydown',{key:'ArrowDown',shiftKey:true});assert.ok(Math.abs(position.y-(.4+10/300))<1e-10);
controller.destroy();const before=updates;event('pointerdown');event('pointermove');assert.equal(updates,before);
''')


def test_preview_and_print_keep_exactly_the_same_normalized_label_positions():
    node(r'''
(async()=>{
const values={'figure-width-mm':'180','figure-height-mm':'110','figure-dpi':'300','engine-3d-label-size':'8'};
const ctx={createImageData:(w,h)=>({data:new Uint8ClampedArray(w*h*4)}),putImageData(){},measureText:s=>({width:s.length*4.7}),beginPath(){},moveTo(){},lineTo(){},stroke(){},strokeText(){},fillText(){}};
global.document={getElementById:id=>values[id]?{value:values[id]}:null,createElement:()=>({getContext:()=>ctx})};
const atoms=Array.from({length:8},(_,i)=>({elem:'N',x:Math.cos(i*Math.PI/4)*2,y:Math.sin(i*Math.PI/4)*2,z:i*.01})),view=[0,0,0,5,0,0,0,1];
const viewer={getModel:()=>({selectedAtoms:()=>atoms}),getView:()=>view};
const options={labels:atoms.map((a,i)=>`N${i+1}`),labelPositions:{0:{x:.22,y:.25},3:{x:.6,y:.73}}};
const before=JSON.stringify({atoms,view,options}),preview=await P.renderMolecule(viewer,options,true);
values['figure-dpi']='600';const print=await P.renderMolecule(viewer,options);
for(let i=0;i<preview.labelBoxes.length;i++)for(const key of ['x','y','w','h'])assert.ok(Math.abs(preview.labelBoxes[i][key]-print.labelBoxes[i][key])<1e-12,`DPI changed label ${i} ${key}`);
assert.equal(JSON.stringify({atoms,view,options}),before);
assert.equal(print.metadata.label_positions[0].manual,true);assert.equal(print.metadata.label_positions[1].manual,false);
assert.ok(Math.abs(print.metadata.label_positions[0].x-.22)<1e-12);
const clamped=M.positionLabels([{atom:1,x:1,y:1,w:20,h:10,anchor:{x:2,y:3,r:1}}],200,100,{1:{x:-1,y:2}})[0];assert.ok(clamped.x>=0&&clamped.y+clamped.h<=100);
})().catch(e=>{console.error(e);process.exit(1);});
''')
