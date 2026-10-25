/* Browser-only native Open Babel/WASM. No molecule data leaves this worker. */
'use strict';
const graphURL=new URL('molecule-graph.js',self.location.href);graphURL.search=new URL(self.location.href).search;
importScripts(graphURL.href);
const symbols='* H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og'.split(' ');
let enginePromise;
async function engine(){
  if(!enginePromise)enginePromise=(async()=>{
    const base=new URL('../vendor/openbabel/',self.location.href);
    const asset=name=>{const url=new URL(name,base);url.search=new URL(self.location.href).search;return url;};
    const load=async name=>{const r=await fetch(asset(name));if(!r.ok)throw Error('Browser chemistry engine is unavailable. Reload after deployment.');return r.arrayBuffer();};
    const [wasm,data]=await Promise.all([load('openbabel.wasm'),load('openbabel.data')]);
    importScripts(asset('openbabel.js').href);
    const ob=OpenBabelModule({wasmBinary:wasm,getPreloadedPackage:()=>data,print:()=>{},printErr:()=>{}});
    await new Promise(resolve=>ob.then(()=>resolve()));return {ob};
  })();
  try{return await enginePromise;}catch(error){enginePromise=null;throw error;}
}
function read(ob,text,format){
  const c=new ob.ObConversionWrapper(),m=new ob.OBMol();
  try{if(!c.setInFormat('',format)||!c.readString(m,text)||!m.NumAtoms())throw Error('No valid molecule found in this file.');if(m.NumAtoms()>1000)throw Error('Local editor limit: 1000 atoms.');return m;}catch(e){m.delete();throw e;}finally{c.delete();}
}
function extract(m){
  const atoms=[],bonds=[];
  for(let i=1;i<=m.NumAtoms();i++){const a=m.GetAtom(i);atoms.push({id:i,elem:symbols[a.GetAtomicNum()],x:a.GetX(),y:a.GetY(),z:a.GetZ(),charge:a.GetFormalCharge()});}
  for(let i=0;i<m.NumBonds();i++){const b=m.GetBond(i);bonds.push({a1:b.GetBeginAtomIdx(),a2:b.GetEndAtomIdx(),order:b.GetBondOrder(),aromatic:b.IsAromatic()});}
  MoleculeGraph.validate(atoms,bonds);return {atoms,bonds,coords:MoleculeGraph.xyz(atoms),formula:m.GetFormula()};
}
// A verified 3D V2000 record needs no native toolkit or geometry generation.
// Unusual/V3000/2D records still use Open Babel, preserving its chemistry rules.
function readReady3D(text,name){
  const lines=text.replace(/\r/g,'').split('\n');
  if(!lines[3]?.includes('V2000')||!lines[1]?.includes('3D'))return null;
  if(lines.some(l=>/^M  (ISO|RAD)/.test(l)))return null;
  const count=Number(lines[3].slice(0,3)),nb=Number(lines[3].slice(3,6));
  if(!Number.isInteger(count)||count<1||count>1000||!Number.isInteger(nb)||nb<0)return null;
  const charges={1:3,2:2,3:1,5:-1,6:-2,7:-3},atoms=[],bonds=[];
  for(let i=0;i<count;i++){
    const l=lines[4+i]||'',elem=l.slice(31,34).trim(),code=Number(l.slice(36,39));
    if(!symbols.includes(elem)||Number(l.slice(34,36))||code===4)return null;
    const a={id:i+1,elem,x:Number(l.slice(0,10)),y:Number(l.slice(10,20)),z:Number(l.slice(20,30)),charge:charges[code]||0};
    if(l.length<39||![a.x,a.y,a.z].every(Number.isFinite))return null;atoms.push(a);
  }
  for(let i=0;i<nb;i++){const l=lines[4+count+i]||'',order=Number(l.slice(6,9));if(![1,2,3,4].includes(order))return null;bonds.push({a1:Number(l.slice(0,3)),a2:Number(l.slice(3,6)),order:order===4?1.5:order,aromatic:order===4});}
  const end=lines.findIndex((l,i)=>i>=4+count+nb&&l.trim()==='M  END');if(end<0)return null;
  for(const l of lines.slice(4+count+nb,end))if(l.startsWith('M  CHG')){const tokens=l.trim().split(/\s+/).slice(2),n=Number(tokens.shift());if(tokens.length!==n*2)return null;for(let i=0;i<n;i++){const atom=atoms[Number(tokens[i*2])-1],charge=Number(tokens[i*2+1]);if(!atom||!Number.isInteger(charge))return null;atom.charge=charge;}}
  try{MoleculeGraph.validate(atoms,bonds);}catch(_){return null;}
  const counts={};for(const a of atoms)counts[a.elem]=(counts[a.elem]||0)+1;
  const order=Object.keys(counts).sort();if(counts.C){order.splice(order.indexOf('C'),1);if(counts.H)order.splice(order.indexOf('H'),1);order.unshift(...(counts.H?['C','H']:['C']));}
  const net=atoms.reduce((n,a)=>n+a.charge,0),formula=order.map(e=>e+(counts[e]>1?counts[e]:'')).join('')+(net?(Math.abs(net)>1?Math.abs(net):'')+(net>0?'+':'-'):'');
  return {atoms,bonds,name:name||'Molecule',coords:MoleculeGraph.xyz(atoms),formula,generated3D:false,topology:'source'};
}
async function parse({text,format,name,generate3D=false}){
  if(typeof text!=='string'||text.length>5*1024*1024)throw Error('Structure file limit: 5 MB.');
  if(!['xyz','mol','sdf','pdb','out','log','txt','orca','g03','g09'].includes(format))throw Error('Unsupported structure format.');
  if(!generate3D&&['mol','sdf'].includes(format)){const ready=readReady3D(text,name);if(ready)return ready;}
  const {ob}=await engine();
  // Open Babel performs format parsing locally, including calculation outputs.
  const formats=['out','log','txt'].includes(format)?['orca','g09']: [format];
  let m;
  for(const f of formats){try{m=read(ob,text,f);break;}catch(e){if(f===formats.at(-1))throw e;}}
  try{
    const authoritative=['mol','sdf'].includes(format);
    const generated3D=generate3D||(authoritative&&m.GetDimension()===2);
    if(generated3D){const g=new ob.OB3DGenWrapper();try{if(g.generate3DStructure(m,'UFF')<0)throw Error('Cannot generate 3D geometry for this structure.');}finally{g.delete();}}
    const result=extract(m);
    if(generated3D){MoleculeGraph.packGeneratedComponents(result.atoms,result.bonds);result.coords=MoleculeGraph.xyz(result.atoms);}
    // Never guess higher bond orders from XYZ. Its connectivity is unverified.
    if(!authoritative){result.bonds=MoleculeGraph.infer(result.atoms);}
    return {...result,name:name||'Molecule',generated3D,topology:authoritative?'source':'unverified'};
  }finally{m.delete();}
}
let stopRequested=false;
async function optimize({atoms,bonds,forceField='UFF'},progress){
  if(!['UFF','MMFF94'].includes(forceField))throw Error('Unsupported force field.');
  stopRequested=false;
  MoleculeGraph.validate(atoms,bonds);
  if(atoms.length>500)throw Error('Browser UFF limit: 500 atoms per operation.');
  const {ob}=await engine(),out=atoms.map(a=>({...a})),pending=new Set(atoms.map(a=>a.id));
  let before=0,after=0,unit='',converged=true,totalSteps=0,components=0;
  // Optimize disconnected components independently so salts / staged fragments
  // cannot drift together. Preserve each component centroid and stable IDs.
  while(pending.size&&!stopRequested){
    const ids=new Set([pending.values().next().value]);let changed=true;
    while(changed){changed=false;for(const b of bonds)if(ids.has(b.a1)!==ids.has(b.a2)){ids.add(b.a1);ids.add(b.a2);changed=true;}}
    ids.forEach(id=>pending.delete(id));
    const aa=atoms.filter(a=>ids.has(a.id)),bb=bonds.filter(b=>ids.has(b.a1)&&ids.has(b.a2));
    if(aa.length===1)continue;
    const m=read(ob,MoleculeGraph.mol(aa,bb),'mol');
    try{
      const ff=ob.OBForceField.FindForceField(forceField);
      if(!ff||!ff.Setup(m))throw Error(forceField+' parameters are unavailable for this structure. Try another force field; coordinates preserved.');
      const e0=ff.Energy(false);unit=ff.GetUnit();
      if(!Number.isFinite(e0))throw Error('Non-finite starting energy. Coordinates preserved.');
      let bestEnergy=e0,bestCoordinates=extract(m).atoms,steps=0,running=true;
      // Each native batch is small; yield to process Stop and publish progress.
      // A high explicit safety cap bounds pathological non-convergent structures.
      ff.ConjugateGradientsInitialize(200000,1e-6,0);
      while(running&&!stopRequested&&steps<200000){
        running=ff.ConjugateGradientsTakeNSteps(20);steps+=20;
        ff.GetCoordinates(m);const energy=ff.Energy(false);
        if(!Number.isFinite(energy))throw Error('Force field returned non-finite energy. Coordinates preserved.');
        if(energy<=bestEnergy){bestEnergy=energy;bestCoordinates=extract(m).atoms;}
        progress({forceField,steps,energy,bestEnergy,initialEnergy:e0,delta:energy-e0,unit,component:atoms.length-pending.size,atomCount:aa.length});
        await new Promise(resolve=>setTimeout(resolve,0));
      }
      converged=converged&&!running;totalSteps+=steps;components++;
      const e1=bestEnergy;
      const next=bestCoordinates;
      if(next.length!==aa.length||next.some((a,i)=>a.elem!==aa[i].elem))throw Error('UFF atom mapping changed. Coordinates preserved.');
      const delta={x:0,y:0,z:0};for(let i=0;i<aa.length;i++)for(const k of ['x','y','z'])delta[k]+=(aa[i][k]-next[i][k])/aa.length;
      for(let i=0;i<aa.length;i++){const dest=out.find(a=>a.id===aa[i].id);for(const k of ['x','y','z'])dest[k]=next[i][k]+delta[k];}
      before+=e0;after+=e1;unit=ff.GetUnit();
    }finally{m.delete();}
  }
  return {atoms:out,before,after,unit,converged,stopped:stopRequested,forceField,totalSteps,components};
}
self.onmessage=async({data})=>{
  if(data.type==='stop'){stopRequested=true;return;}
  try{
    const progress=value=>self.postMessage({id:data.id,progress:value});
    const result=await (data.type==='parse'?parse(data.payload):data.type==='optimize'?optimize(data.payload,progress):Promise.reject(Error('Unknown chemistry operation.')));
    self.postMessage({id:data.id,result});
  }catch(e){self.postMessage({id:data.id,error:e.message||String(e)});}
};
