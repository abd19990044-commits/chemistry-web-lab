"""Exercise browser requests and real workflow transitions without cloud jobs."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / 'static/js/app.js').read_text(encoding='utf-8')


def node(script):
    if not shutil.which('node'):
        pytest.skip('Node.js required')
    result = subprocess.run(['node', '-e', script], cwd=ROOT, text=True, encoding='utf-8', capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_pubchem_ready_3d_returns_without_waiting_for_2d_and_reuses_isolated_cache():
    node(r'''
const assert=require('node:assert/strict');let requests=0,parses=0;
global.document={currentScript:{src:'https://lab.test/static/js/browser-chemistry.js?v=test'}};
global.Worker=class {postMessage({id}){parses++;queueMicrotask(()=>this.onmessage({data:{id,result:{atoms:[{x:1}],bonds:[{order:2}]}}}));}terminate(){}};
global.fetch=(url,{signal})=>{requests++;if(url.endsWith('3d'))return Promise.resolve({ok:true,text:async()=> '3D SDF'});return new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(Error('cancelled')),{once:true}));};
require('./static/js/browser-chemistry.js');
(async()=>{
const [a,b]=await Promise.all([BrowserChemistry.lookup('water'),BrowserChemistry.lookup('Water')]);
assert.equal(requests,2);assert.equal(parses,1);a.atoms[0].x=99;assert.equal(b.atoms[0].x,1);
const c=await BrowserChemistry.lookup('water');assert.equal(c.atoms[0].x,1);assert.equal(requests,2);assert.equal(parses,1);
})().catch(e=>{console.error(e);process.exit(1);});
''')


def test_ready_3d_mol_preserves_graph_charges_without_loading_wasm():
    node(r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
require('./static/js/molecule-graph.js');
const input=fs.readFileSync('tests/fixtures/browser_chemistry/methylene-blue.sdf','utf8').split(/\r?\n/),n=Number(input[3].slice(0,3)),nb=Number(input[3].slice(3,6));
const atoms=input.slice(4,4+n).map((l,i)=>({id:i+1,elem:l.slice(31,34).trim(),x:Number(l.slice(0,10)),y:Number(l.slice(10,20)),z:Number(l.slice(20,30))+.001*i,charge:i===0?-1:i===2?1:0}));
const bonds=input.slice(4+n,4+n+nb).map(l=>({a1:Number(l.slice(0,3)),a2:Number(l.slice(3,6)),order:Number(l.slice(6,9))}));
const mol=MoleculeGraph.mol(atoms,bonds,'Explicit 3D fixture');let result;
const context=vm.createContext({URL,ArrayBuffer,Uint8Array,setTimeout,clearTimeout,WebAssembly,fetch(){throw Error('Ready 3D must not fetch WASM');},location:{href:'https://lab.test/static/js/chemistry-worker.js'},postMessage:v=>{result=v;}});
context.self=context;context.importScripts=()=>vm.runInContext(fs.readFileSync('static/js/molecule-graph.js','utf8'),context);
vm.runInContext(fs.readFileSync('static/js/chemistry-worker.js','utf8'),context);
(async()=>{await context.onmessage({data:{id:1,type:'parse',payload:{text:mol+'$$$$\nSecond record\nM  CHG  1   1   3\nM  END\n',format:'sdf',name:'Fixture'}}});
assert.equal(result.error,undefined);assert.equal(result.result.generated3D,false);assert.equal(result.result.atoms.length,39);assert.equal(result.result.bonds.length,40);
assert.equal(result.result.bonds.filter(b=>b.order===2).length,7);assert.equal(result.result.atoms[0].charge,-1);assert.equal(result.result.atoms[2].charge,1);assert.equal(result.result.atoms[10].z,atoms[10].z);
})().catch(e=>{console.error(e);process.exit(1);});
''')


WORKFLOW = APP[APP.index('  async function workflowGeometry('):APP.index('  let isProcessingQueue = false;')]
BASE = r'''
const assert=require('node:assert/strict');let clock=1000;Date.now=()=>clock;
let stored=[{jobId:'chem-tools-parent',workflowId:'chain',stage:1,stageType:'opt',status:'complete',name:'Opt'},
{jobId:'queued-child',workflowId:'chain',dependsOnWorkflow:'chain',stage:2,status:'waiting_dependency',name:'SP',stageConfig:{calc_type:'sp',theory:'B3LYP',coords:'OLD'}}];
const loadJobs=()=>structuredClone(stored),updateJob=(id,patch)=>{stored=stored.map(j=>j.jobId===id?{...j,...patch}:j);};
const credsFor=()=>({}),showToast=()=>{},isOptimizationJob=j=>j.stageType==='opt',extractCoordsFromInp=()=>'';
let fetchCount=0,generated=0,mode='delayed',release;
const fetch=async()=>{fetchCount++;if(mode==='blocked')return new Promise(resolve=>{release=()=>resolve({ok:true,status:200,json:async()=>({ok:true,converged:true,coords:'O 0 0 1'})});});if(mode==='delayed')return {ok:false,status:404,json:async()=>({ok:false,error:'Not published yet'})};if(mode==='unconverged')return {ok:false,status:422,json:async()=>({ok:false,error:'Optimization did not converge',code:'optimization_not_complete'})};return {ok:true,status:200,json:async()=>({ok:true,converged:true,coords:'O 0 0 1'})};};
const postJSON=async(url,config)=>{generated++;assert.equal(config.coords,'O 0 0 1');return {input_text:'! SP\n* xyz 0 1\n'+config.coords+'\n*'};};
'''


def test_step_two_retries_delayed_outputs_then_uses_verified_final_coordinates():
    node(BASE + WORKFLOW + r'''
(async()=>{
await prepareWorkflowStage(loadJobs()[1]);assert.equal(stored[1].status,'waiting_dependency');assert.equal(generated,0);assert.ok(stored[1].dependencyRetryAt>clock);
await prepareWorkflowStage(loadJobs()[1]);assert.equal(fetchCount,1,'Backoff must suppress duplicate downloads');
clock=stored[1].dependencyRetryAt+1;mode='ready';await prepareWorkflowStage(loadJobs()[1]);
assert.equal(stored[1].status,'queued');assert.equal(generated,1);assert.ok(stored[1].input_content.includes('O 0 0 1'));assert.ok(!stored[1].input_content.includes('OLD'));assert.equal(stored[1].parentJobId,'chem-tools-parent');
})().catch(e=>{console.error(e);process.exit(1);});
''')


def test_step_transition_is_single_flight_and_respects_cancellation_during_download():
    node(BASE + WORKFLOW + r'''
(async()=>{
mode='blocked';const a=prepareWorkflowStage(loadJobs()[1]),b=prepareWorkflowStage(loadJobs()[1]);assert.equal(fetchCount,1);
updateJob('queued-child',{status:'cancelled'});release();await Promise.all([a,b]);assert.equal(stored[1].status,'cancelled');assert.equal(generated,0);
})().catch(e=>{console.error(e);process.exit(1);});
''')


def test_unconverged_optimization_stops_chain_without_using_older_geometry():
    node(BASE + WORKFLOW + r'''
(async()=>{
stored[0].stage=2;stored[1].stage=3;stored.unshift({jobId:'old',workflowId:'chain',stage:1,stageType:'opt',status:'complete',optimizedCoords:'OLD'});mode='unconverged';
await prepareWorkflowStage(loadJobs()[2]);assert.equal(stored[2].status,'error');assert.equal(generated,0);assert.ok(stored[2].warning.includes('did not converge'));
})().catch(e=>{console.error(e);process.exit(1);});
''')


def test_explicit_retry_recovers_only_the_unsubmitted_stage_and_revalidates_geometry():
    node(BASE + WORKFLOW + r'''
(async()=>{
let dispatched=0;globalThis.processKaggleQueue=async()=>{dispatched++;};
stored[0].optimizedCoords='OLD';stored[1].status='error';mode='ready';
assert.equal(await retryWorkflowDependency('queued-child'),true);
assert.equal(stored[0].status,'complete');assert.equal(stored[0].jobId,'chem-tools-parent');assert.equal(stored[1].status,'queued');assert.equal(fetchCount,1);assert.equal(generated,1);assert.equal(dispatched,1);
assert.equal(await retryWorkflowDependency('queued-child'),false);assert.equal(dispatched,1);
})().catch(e=>{console.error(e);process.exit(1);});
''')
