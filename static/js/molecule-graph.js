/* Stable chemical topology. Geometry changes never invent chemical bonds. */
(function(root){
  'use strict';
  const radii={H:.31,B:.84,C:.76,N:.71,O:.66,F:.57,Si:1.11,P:1.07,S:1.05,Cl:1.02,Br:1.2,I:1.39};
  const limits={H:1,He:0,Li:0,Be:2,B:3,C:4,N:3,O:2,F:1,Ne:0,Na:0,Mg:2,Al:3,Si:4,P:5,S:6,Cl:1,Ar:0,K:0,Ca:2,Br:1,Kr:0,I:1,Xe:0};
  const maxValence=a=>a.elem==='N'&&a.charge>0?4:a.elem==='O'&&a.charge<0?1:a.elem==='O'&&a.charge>0?3:a.elem==='B'&&a.charge<0?4:(limits[a.elem]??0);
  function validate(atoms,bonds){
    if(!Array.isArray(atoms)||!atoms.length||atoms.length>1000)throw Error('Use a structure with 1–1000 atoms.');
    const ids=new Set();
    for(const a of atoms){if(ids.has(a.id)||!a.elem||![a.x,a.y,a.z].every(Number.isFinite))throw Error('Invalid atom coordinates or identifiers.');ids.add(a.id);}
    const seen=new Set();
    for(const b of bonds){const k=[b.a1,b.a2].sort((a,b)=>a-b).join(':');if(!ids.has(b.a1)||!ids.has(b.a2)||b.a1===b.a2||seen.has(k)||![1,2,3,1.5].includes(b.order))throw Error('Invalid or duplicate chemical bond.');seen.add(k);}
    return {atoms,bonds};
  }
  // Coordinate-only input has no authoritative bond orders/charges. Use a
  // conservative initial single-bond graph, visibly marked as unverified.
  function infer(atoms){
    const pairs=[], bonds=[], used=new Map(atoms.map(a=>[a.id,0]));
    for(let i=0;i<atoms.length;i++)for(let j=i+1;j<atoms.length;j++){
      const a=atoms[i],b=atoms[j],r=(radii[a.elem]||0)+(radii[b.elem]||0);
      if(!r||!radii[a.elem]||!radii[b.elem]||(a.molId&&b.molId&&a.molId!==b.molId))continue;
      const d=Math.hypot(a.x-b.x,a.y-b.y,a.z-b.z);
      if(d>=.45&&d<=r+Math.min(.20,r*.15))pairs.push({a,b,score:d/r});
    }
    pairs.sort((a,b)=>a.score-b.score);
    for(const {a,b} of pairs)if(used.get(a.id)<maxValence(a)&&used.get(b.id)<maxValence(b)){
      bonds.push({a1:a.id,a2:b.id,order:1});used.set(a.id,used.get(a.id)+1);used.set(b.id,used.get(b.id)+1);
    }
    return bonds;
  }
  function apply(model,bonds){
    const atoms=model.selectedAtoms({}),index=new Map(atoms.map((a,i)=>[a.id??i+1,i]));
    atoms.forEach(a=>{a.bonds=[];a.bondOrder=[];});
    for(const b of bonds){const i=index.get(b.a1),j=index.get(b.a2);if(i===undefined||j===undefined)continue;atoms[i].bonds.push(j);atoms[i].bondOrder.push(b.order);atoms[j].bonds.push(i);atoms[j].bondOrder.push(b.order);}
  }
  function canAdd(atoms,bonds,a1,a2,order=1){
    if(a1===a2||bonds.some(b=>b.a1===a1&&b.a2===a2||b.a1===a2&&b.a2===a1))return false;
    return [a1,a2].every(id=>{const a=atoms.find(a=>a.id===id);return a&&bonds.filter(b=>b.a1===id||b.a2===id).reduce((n,b)=>n+b.order,order)<=maxValence(a);});
  }
  function xyz(atoms){return atoms.map(a=>`${a.elem} ${a.x.toFixed(8)} ${a.y.toFixed(8)} ${a.z.toFixed(8)}`).join('\n');}
  function mol(atoms,bonds,name='Molecule'){
    validate(atoms,bonds);
    if(atoms.length>999||bonds.length>999)throw Error('MOL V2000 supports up to 999 atoms and bonds.');
    const pad=(v,n)=>String(v).padStart(n,' '),index=new Map(atoms.map((a,i)=>[a.id,i+1]));
    const lines=[name.replace(/[\r\n]/g,' ').slice(0,80),'  Chemistry Lab  3D','',`${pad(atoms.length,3)}${pad(bonds.length,3)}  0  0  0  0            999 V2000`];
    for(const a of atoms)lines.push(`${pad(a.x.toFixed(4),10)}${pad(a.y.toFixed(4),10)}${pad(a.z.toFixed(4),10)} ${a.elem.padEnd(3)} 0  0  0  0  0  0  0  0  0  0  0  0`);
    for(const b of bonds)lines.push(`${pad(index.get(b.a1),3)}${pad(index.get(b.a2),3)}${pad(b.order===1.5?4:b.order,3)}  0  0  0  0`);
    const charged=atoms.filter(a=>a.charge);
    for(let i=0;i<charged.length;i+=8){const group=charged.slice(i,i+8);lines.push('M  CHG'+pad(group.length,3)+group.map(a=>pad(index.get(a.id),4)+pad(a.charge,4)).join(''));}
    lines.push('M  END');return lines.join('\n')+'\n';
  }
  root.MoleculeGraph={validate,infer,apply,canAdd,maxValence,xyz,mol};
})(globalThis);
