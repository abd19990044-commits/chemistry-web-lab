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
assert.equal(text[0].color,'#c01234');assert.ok(text[0].font.includes('37.5px'));
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
