/* Browser-only native Open Babel/WASM. No molecule data leaves this worker. */
'use strict';
importScripts('molecule-graph.js');
const symbols='* H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og'.split(' ');
let enginePromise;
async function engine(){
  if(!enginePromise)enginePromise=(async()=>{
    const base=new URL('../vendor/openbabel/',self.location.href);
    const load=async name=>{const r=await fetch(new URL(name,base));if(!r.ok)throw Error('Browser chemistry engine is unavailable. Reload after deployment.');return r.arrayBuffer();};
    const [wasm,data]=await Promise.all([load('openbabel.wasm'),load('openbabel.data')]);
    importScripts(new URL('openbabel.js',base).href);
    const ob=OpenBabelModule({wasmBinary:wasm,getPreloadedPackage:()=>data,print:()=>{},printErr:()=>{}});
    await new Promise(resolve=>ob.then(()=>resolve()));return {ob};
  })();
  return await enginePromise;
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
async function parse({text,format,name,generate3D=false}){
  if(typeof text!=='string'||text.length>5*1024*1024)throw Error('Structure file limit: 5 MB.');
  if(!['xyz','mol','sdf','pdb','out','log','txt','orca','g03','g09'].includes(format))throw Error('Unsupported structure format.');
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
