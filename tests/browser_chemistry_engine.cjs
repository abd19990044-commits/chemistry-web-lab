const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict'),path=require('path');
const root=process.cwd();let active,progress=[],stopAt=Infinity;
const context=vm.createContext({ArrayBuffer,Uint8Array,URL,setTimeout,clearTimeout,WebAssembly,console:{log(){},error(){}},location:{href:'http://local/static/js/chemistry-worker.js',pathname:'/static/js/chemistry-worker.js'},fetch:async url=>({ok:true,arrayBuffer:async()=>{const b=fs.readFileSync(path.join(root,new URL(url).pathname));return b.buffer.slice(b.byteOffset,b.byteOffset+b.byteLength);}})});
context.self=context;context.importScripts=(...urls)=>urls.forEach(url=>{const file=url.startsWith('http')?new URL(url).pathname:'static/js/'+url;vm.runInContext(fs.readFileSync(path.join(root,file),'utf8'),context,{filename:path.basename(file)});});
context.postMessage=data=>{if(data.progress){progress.push(data.progress);if(progress.length===stopAt)context.onmessage({data:{type:'stop'}});}else{data.error?active.reject(Error(data.error)):active.resolve(data.result);}};
vm.runInContext(fs.readFileSync('static/js/chemistry-worker.js','utf8'),context,{filename:'chemistry-worker.js'});
const run=(type,payload)=>new Promise((resolve,reject)=>{active={resolve,reject};context.onmessage({data:{id:1,type,payload}});});
(async()=>{
const ethanolInput=context.MoleculeGraph.mol([{id:1,elem:'C',x:0,y:0,z:0},{id:2,elem:'C',x:1.5,y:0,z:0},{id:3,elem:'O',x:2.2,y:1.2,z:0}],[{a1:1,a2:2,order:1},{a1:2,a2:3,order:1}],'Drawn ethanol');
const drawn=await run('parse',{text:ethanolInput,format:'mol',name:'Drawn ethanol',generate3D:true,addHydrogens:true});
assert.equal(drawn.atoms.length,9);assert.equal(drawn.atoms.filter(a=>a.elem==='H').length,6);assert.equal(drawn.bonds.length,8);assert.equal(drawn.topology,'source');assert(drawn.generated3D);
assert(drawn.atoms.some(a=>Math.abs(a.z-drawn.atoms[0].z)>.1));
console.log('2D drawing to hydrogen-complete 3D ethanol',drawn.formula,drawn.atoms.length);
const acidInput=context.MoleculeGraph.mol([{id:1,elem:'C',x:0,y:0,z:0},{id:2,elem:'C',x:1.5,y:0,z:0},{id:3,elem:'O',x:2.2,y:1.2,z:0},{id:4,elem:'O',x:2.2,y:-1.2,z:0}],[{a1:1,a2:2,order:1},{a1:2,a2:3,order:2},{a1:2,a2:4,order:1}],'Drawn acetic acid');
const acid=await run('parse',{text:acidInput,format:'mol',generate3D:true,addHydrogens:true});
assert.equal(acid.atoms.length,8);assert.equal(acid.atoms.filter(a=>a.elem==='H').length,4);assert.equal(acid.atoms.reduce((sum,a)=>sum+a.charge,0),0);assert.equal(acid.bonds.filter(b=>b.order===2).length,1);
console.log('Drawn acid preserves neutral protonation',acid.formula,acid.atoms.length);
const chargedAmine=context.MoleculeGraph.mol([{id:1,elem:'N',charge:1,x:0,y:0,z:0},{id:2,elem:'C',x:1.5,y:0,z:0}],[{a1:1,a2:2,order:1}],'Methylammonium');
const ammonium=await run('parse',{text:chargedAmine,format:'mol',generate3D:true,addHydrogens:true});
assert.equal(ammonium.atoms.length,8);assert.equal(ammonium.atoms.reduce((sum,a)=>sum+a.charge,0),1);
console.log('Drawn ammonium preserves positive charge',ammonium.formula);
const methylInput=context.MoleculeGraph.mol([{id:1,elem:'C',radical:2,x:0,y:0,z:0}],[],'Methyl radical');
const methyl=await run('parse',{text:methylInput,format:'mol',generate3D:true,addHydrogens:true});
assert.equal(methyl.atoms.length,4);assert.equal(methyl.atoms[0].radical,2);assert.equal(methyl.atoms[0].charge,0);
const methylSaved=context.MoleculeGraph.serialize(methyl.atoms,methyl.bonds,'sdf','Methyl radical').text;assert(methylSaved.includes('M  RAD'));
const methylReopened=await run('parse',{text:methylSaved,format:'sdf'});assert.equal(methylReopened.atoms.length,4);assert.equal(methylReopened.atoms[0].radical,2);
for(const charge of [-1,1]){const ion=await run('parse',{text:context.MoleculeGraph.mol([{id:1,elem:'C',charge,x:0,y:0,z:0}],[]),format:'mol',generate3D:true,addHydrogens:true});assert.equal(ion.atoms.length,4);assert.equal(ion.atoms[0].charge,charge);}
console.log('Methyl radical / ions preserve CH3, electron states and SDF round trip');
const mb=await run('parse',{text:fs.readFileSync('tests/fixtures/browser_chemistry/methylene-blue.sdf','utf8'),format:'sdf',name:'Methylene blue'});
const saved=context.MoleculeGraph.mol(mb.atoms,mb.bonds,'Methylene blue');
assert.equal(saved.split('\n')[1].slice(20,22),'3D');
const reopened=await run('parse',{text:saved,format:'mol',name:'Methylene blue reopened'});
assert.equal(reopened.generated3D,false);
assert.equal(reopened.bonds.length,mb.bonds.length);
assert(reopened.atoms.every((a,i)=>['x','y','z'].every(k=>Math.abs(a[k]-mb.atoms[i][k])<=.00005001)));
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
