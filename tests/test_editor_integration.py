"""Browser editor integration without spending remote calculation resources."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def node(code):
    if not shutil.which('node'):
        pytest.skip('Node required')
    result = subprocess.run(['node', '-e', code], cwd=ROOT, text=True, encoding='utf-8', capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_cooh_template_contains_carboxyl_atoms_and_preserves_user_library():
    node(r'''
require('./static/js/editor-integration.js');const assert=require('node:assert/strict');
const values=new Map([['ketcher-tmpls',JSON.stringify([{struct:JSON.stringify({header:{moleculeName:'UserGroup'}}),props:{custom:true}}])]]);
const storage={getItem:k=>values.get(k)||null,setItem:(k,v)=>values.set(k,v)};
assert.equal(EditorIntegration.installGroups(storage),true);assert.equal(EditorIntegration.installGroups(storage),false);
const templates=JSON.parse(values.get('ketcher-tmpls'));assert.equal(templates.length,2);assert.equal(templates[0].props.custom,true);
const ket=JSON.parse(templates[1].struct),mol=ket.mol0;
assert.deepEqual(mol.atoms.map(a=>a.label),['C','O','O']);assert.deepEqual(mol.bonds.map(b=>b.type),[2,1]);assert.equal(mol.sgroups[0].name,'COOH');assert.equal(mol.sgroups[0].attachmentPoints[0].attachmentAtom,0);
const copy=EditorIntegration.group();copy.mol0.atoms[0].label='N';assert.equal(EditorIntegration.group().mol0.atoms[0].label,'C');
''')


def test_free_labels_are_explicitly_undefined_unless_backed_by_real_structure():
    node(r'''
require('./static/js/editor-integration.js');const assert=require('node:assert/strict');
const input=EditorIntegration.group();input.mol0.sgroups=[];const before=JSON.stringify(input);
const ket=EditorIntegration.makeGroup('My <arbitrary> group',input,true);assert.equal(JSON.stringify(input),before);assert.equal(ket.mol0.atoms.length,3);assert.equal(ket.mol0.sgroups[0].name,'My <arbitrary> group');assert(!EditorIntegration.hasUndefined(ket));
const unknown=EditorIntegration.makeGroup('أي تسمية يكتبها المستخدم',null,false);assert(EditorIntegration.hasUndefined(unknown));assert.equal(unknown.mol0.atoms[0].label,'A');
assert.equal(EditorIntegration.definition('COOH').smiles,'C(=O)O');assert.equal(EditorIntegration.definition('NMe2').smiles,'N(C)C');assert.equal(EditorIntegration.definition('Cl').smiles,'Cl');assert.equal(EditorIntegration.definition('Fe').smiles,'[Fe]');assert(!EditorIntegration.definition('CustomName').defined);assert.equal(EditorIntegration.definition('CustomName','CCO').smiles,'CCO');
assert.throws(()=>EditorIntegration.makeGroup('x',{root:{nodes:[]}}),/connected/);
const disconnected=EditorIntegration.group();disconnected.mol0.bonds=[];assert.throws(()=>EditorIntegration.makeGroup('x',disconnected),/connected/);
const query=EditorIntegration.group();query.mol0.atoms[0].label='A';assert.throws(()=>EditorIntegration.makeGroup('x',query),/concrete/);
''')


def test_pubchem_import_is_2d_cached_deduplicated_and_does_not_guess_ambiguous_records():
    node(r'''
require('./static/js/editor-integration.js');const assert=require('node:assert/strict');let calls=[];
const sdf='2244\n  PubChem 2D\n\n  1  0  0  0            999 V2000\nM  END\n> <PUBCHEM_COMPOUND_CID>\n2244\n\n$$$$\n';
global.fetch=async(url,options)=>{calls.push(url);assert(options.signal);return {ok:true,text:async()=>sdf};};
(async()=>{const [a,b]=await Promise.all([EditorIntegration.pubchem('Aspirin'),EditorIntegration.pubchem('aspirin')]);assert.equal(calls.length,1);assert.equal(a.cid,'2244');assert.equal(a.structure,b.structure);a.title='changed';assert.equal((await EditorIntegration.pubchem('aspirin')).title,'Aspirin');assert(calls[0].endsWith('/name/Aspirin/SDF?record_type=2d'));
await EditorIntegration.pubchem('2244');assert(calls[1].includes('/cid/2244/'));
global.fetch=async()=>({ok:true,text:async()=>sdf+sdf});await assert.rejects(()=>EditorIntegration.pubchem('ambiguous'),/multiple/);
global.fetch=async()=>({ok:false,status:429});await assert.rejects(()=>EditorIntegration.pubchem('busy'),/busy/);
global.fetch=async()=>({ok:true,text:async()=>'not a structure'});await assert.rejects(()=>EditorIntegration.pubchem('invalid'),/valid molecular/);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


def test_image_sizes_stay_within_memory_budget_and_word_html_is_escaped():
    node(r'''
require('./static/js/editor-integration.js');const assert=require('node:assert/strict');
assert.deepEqual(EditorIntegration.imageSize(800,400,4096),{width:4096,height:2048});
const size=EditorIntegration.imageSize(100,100,6000);assert(size.width*size.height<=24010000);assert.throws(()=>EditorIntegration.imageSize(0,100),/dimensions/);
assert.deepEqual(EditorIntegration.maxImageBudget(8),{longEdge:12000,pixels:64000000});assert.deepEqual(EditorIntegration.maxImageBudget(2),{longEdge:6000,pixels:16000000});const maximum=EditorIntegration.imageSize(800,400,12000);assert(maximum.width>4096);assert(maximum.width*maximum.height<=64000000);
const html=EditorIntegration.wordHTML('data:image/png;base64,AA',4096,2048,'<script>');assert(html.includes('&lt;script&gt;'));assert(!html.includes('<script>'));assert(html.includes('width="640"'));
''')


def test_word_clipboard_starts_during_user_activation_and_uses_promised_png_and_html():
    node(r'''
require('./static/js/editor-integration.js');const assert=require('node:assert/strict');let captured,started=false;
global.ClipboardItem=class{constructor(data){this.data=data;}};global.FileReader=class{readAsDataURL(){this.result='data:image/png;base64,AAAA';queueMicrotask(()=>this.onload());}};
Object.defineProperty(global,'navigator',{configurable:true,value:{clipboard:{write(items){captured=items[0].data;started=true;return Promise.all([captured['image/png'],captured['text/html']]);}}}});
let resolve;const figure=new Promise(r=>resolve=r);const copied=EditorIntegration.copyForWord(figure,'Example');assert(started);assert(captured['image/png'] instanceof Promise);resolve({blob:new Blob(['PNG'],{type:'image/png'}),width:4096,height:2048});
copied.then(async()=>{assert.equal((await captured['image/png']).type,'image/png');assert((await (await captured['text/html']).text()).includes('data:image/png'));}).catch(e=>{console.error(e);process.exitCode=1;});
''')


def test_svg_blob_from_editor_frame_can_be_rasterized():
    node(r'''
require('./static/js/editor-integration.js');const assert=require('node:assert/strict');
const foreign={size:6,arrayBuffer:async()=>new TextEncoder().encode('<svg/>').buffer};assert(!(foreign instanceof Blob));
global.Image=class{set src(value){this.naturalWidth=200;this.naturalHeight=100;queueMicrotask(()=>this.onload());}};
let drawn=false,filled=false;global.document={createElement:()=>({getContext:()=>({fillRect(){filled=true;},drawImage(){drawn=true;}}),toBlob:fn=>fn(new Blob(['PNG'],{type:'image/png'}))})};
global.PublicationFigures={pngMetadata:bytes=>bytes};
(async()=>{const result=await EditorIntegration.rasterize(foreign,{transparent:true,longEdge:4096});assert(drawn);assert(!filled);assert.equal(result.width,4096);assert.equal(result.height,2048);assert.equal(result.blob.type,'image/png');await assert.rejects(()=>EditorIntegration.rasterize({size:20e6,arrayBuffer(){}}),/limit/);})().catch(e=>{console.error(e);process.exitCode=1;});
''')


def test_maximum_export_retries_a_lower_canvas_limit_without_upscaling_again():
    node(r'''
require('./static/js/editor-integration.js');const assert=require('node:assert/strict');
global.Image=class{set src(value){this.naturalWidth=800;this.naturalHeight=400;queueMicrotask(()=>this.onload());}};
let attempts=0;global.document={createElement:()=>({getContext:()=>({fillRect(){},drawImage(){}}),toBlob:fn=>fn(++attempts===1?null:new Blob(['PNG'],{type:'image/png'}))})};global.PublicationFigures={pngMetadata:bytes=>bytes};
(async()=>{const result=await EditorIntegration.rasterize(new Blob(['<svg/>']),{longEdge:12000});assert.equal(attempts,2);assert(result.width*result.height<=16000000);assert(result.width*result.height>4000000);})().catch(e=>{console.error(e);process.exitCode=1;});
''')
