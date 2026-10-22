const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict'),path=require('path');
const root=process.cwd();let active,progress=[],stopAt=Infinity;
const context=vm.createContext({ArrayBuffer,Uint8Array,URL,setTimeout,clearTimeout,WebAssembly,console:{log(){},error(){}},location:{href:'http://local/static/js/chemistry-worker.js',pathname:'/static/js/chemistry-worker.js'},fetch:async url=>({ok:true,arrayBuffer:async()=>{const b=fs.readFileSync(path.join(root,new URL(url).pathname));return b.buffer.slice(b.byteOffset,b.byteOffset+b.byteLength);}})});
context.self=context;context.importScripts=(...urls)=>urls.forEach(url=>{const file=url.startsWith('http')?new URL(url).pathname:'static/js/'+url;vm.runInContext(fs.readFileSync(path.join(root,file),'utf8'),context,{filename:path.basename(file)});});
context.postMessage=data=>{if(data.progress){progress.push(data.progress);if(progress.length===stopAt)context.onmessage({data:{type:'stop'}});}else{data.error?active.reject(Error(data.error)):active.resolve(data.result);}};
vm.runInContext(fs.readFileSync('static/js/chemistry-worker.js','utf8'),context,{filename:'chemistry-worker.js'});
const run=(type,payload)=>new Promise((resolve,reject)=>{active={resolve,reject};context.onmessage({data:{id:1,type,payload}});});
(async()=>{
const mb=await run('parse',{text:fs.readFileSync('tests/fixtures/browser_chemistry/methylene-blue.sdf','utf8'),format:'sdf',name:'Methylene blue'});
console.log('Methylene blue',mb.atoms.length,mb.bonds.length,mb.formula,mb.atoms.filter(a=>a.charge).map(a=>[a.elem,a.charge]));
assert.equal(mb.generated3D,true);assert.equal(mb.atoms.length,39);assert.equal(mb.bonds.length,40);assert.equal(mb.bonds.filter(b=>b.order===2).length,7);
assert.equal(mb.bonds.filter(b=>b.a1===1||b.a2===1).length,0);
assert.ok(mb.atoms.find(a=>a.elem==='Cl').x-Math.max(...mb.atoms.filter(a=>a.elem!=='Cl').map(a=>a.x))>=2.999);
for(const ff of ['UFF','MMFF94']){
 progress=[];const res=await run('optimize',{atoms:mb.atoms,bonds:mb.bonds,forceField:ff});
 console.log(ff,res.before,res.after,res.unit,res.converged,progress.length,progress.at(-1)?.steps);
 assert(res.after<=res.before);assert(progress.length);assert.equal(res.atoms.length,39);assert(res.atoms.every(a=>Number.isFinite(a.x)));
}
progress=[];stopAt=1;const stopped=await run('optimize',{atoms:mb.atoms,bonds:mb.bonds,forceField:'MMFF94'});assert(stopped.stopped);assert(stopped.after<=stopped.before);console.log('stop best energy',stopped.after);
assert.equal(stopped.atoms.find(a=>a.elem==='Cl').x,mb.atoms.find(a=>a.elem==='Cl').x);
stopAt=Infinity;progress=[];
const water=await run('parse',{text:'3\nwater\nO 0 0 0\nH 1.4 0 0\nH 0 1.4 0\n',format:'xyz'});
assert.equal(water.topology,'unverified');assert.equal(water.bonds.length,0); // conservative cutoff rejects stretched O–H
const explicitWater={atoms:water.atoms,bonds:[{a1:1,a2:2,order:1},{a1:1,a2:3,order:1}]};
const initial=JSON.stringify(explicitWater);
for(const ff of ['UFF','MMFF94']){
 const res=await run('optimize',{...explicitWater,forceField:ff});assert(res.after<res.before-1);assert(res.converged);
 assert.equal(JSON.stringify(explicitWater),initial);console.log('Distorted water',ff,res.before,res.after);
}
await assert.rejects(()=>run('optimize',{atoms:[{id:1,elem:'Xe',x:0,y:0,z:0},{id:2,elem:'O',x:1.7,y:0,z:0}],bonds:[{a1:1,a2:2,order:1}],forceField:'MMFF94'}),/parameters are unavailable/);
await assert.rejects(()=>run('parse',{text:'no atoms',format:'xyz'}),/No valid molecule/);
console.log('PASS: actual browser worker + pinned WASM, source bonds/charges, UFF/MMFF94 convergence, progress, stop, invalid input, unsupported parameters.');
})().catch(e=>{console.error(e);process.exitCode=1;});
