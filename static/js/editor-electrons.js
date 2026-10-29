/* Explicit electron states and hydrogen-preserving deletion for Ketcher 3.18. */
(function(root){
  'use strict';
  const electrons=radical=>Number(radical)===2?1:[1,3].includes(Number(radical))?2:0;
  function snapshot(native){
    const struct=native.struct(),atoms=new Map(),bonds=new Map();
    struct.atoms.forEach((a,id)=>atoms.set(id,{label:a.label,charge:a.charge||0,radical:a.radical||0,h:Math.max(0,a.implicitH||0),fixedH:a.implicitHCount??null}));
    struct.bonds.forEach((b,id)=>bonds.set(id,{begin:b.begin,end:b.end,type:b.type}));
    return {atoms,bonds};
  }
  function deletionEdits(before,after,mode='radical'){
    if(!before||mode==='cap'||after.atoms.size>before.atoms.size)return [];
    const affected=new Set();let removed=false;
    for(const [id,b] of before.bonds){const now=after.bonds.get(id);if(!now){removed=true;affected.add(b.begin);affected.add(b.end);}else if(now.begin!==b.begin||now.end!==b.end||now.type!==b.type)return [];}
    if([...after.bonds.keys()].some(id=>!before.bonds.has(id)))return [];
    if(!removed)return [];
    // Replacement, undo and redo must never be interpreted as fresh cleavage.
    for(const [id,a] of after.atoms){const old=before.atoms.get(id);if(!old||old.label!==a.label)return [];}
    const edits=[];
    for(const id of affected){
      const old=before.atoms.get(id),now=after.atoms.get(id);if(!old||!now||old.label==='H'||old.charge!==now.charge||old.radical!==now.radical||old.fixedH!==now.fixedH)continue;
      // Once an explicit H count exists, cleavage still opens an electron state.
      let lost=0;
      for(const [bid,b] of before.bonds)if(!after.bonds.has(bid)&&(b.begin===id||b.end===id))lost+=b.type===4?0:b.type;
      if(!Number.isInteger(lost)||lost<1)continue;
      const count=electrons(old.radical)+lost;
      const attrs={implicitHCount:old.h};
      if(mode==='radical'&&count<=2)attrs.radical=count===1?2:3;
      edits.push({id,attrs});
    }
    return edits;
  }
  function atomAttrs(atom,charge,radical,hydrogens){
    if(!Number.isInteger(charge)||Math.abs(charge)>15||![0,1,2,3].includes(radical))throw Error('Choose an integer charge and a supported radical state.');
    if(hydrogens!==null&&(!Number.isInteger(hydrogens)||hydrogens<0||hydrogens>8))throw Error('Choose automatic hydrogens or a count from 0 to 8.');
    return Object.assign(atom.clone(),{charge,radical,implicitHCount:hydrogens,explicitValence:-1});
  }
  function attach(api,{mode,isImport,note}){
    const native=api.editor;let before=snapshot(native),ptr=native.historyPtr,updating=false;
    const replace=(id,attrs)=>{const atom=native.struct().atoms.get(id);if(!atom||atom.sgs?.size)throw Error('Expand a collapsed group before editing its atom state.');native.selection({atoms:[id]});native.tool('atom',Object.assign(atom.clone(),attrs));};
    const onChange=()=>{
      if(updating)return;
      const after=snapshot(native),currentPtr=native.historyPtr;
      if(isImport()||currentPtr<=ptr){before=after;ptr=currentPtr;return;}
      const edits=deletionEdits(before,after,mode());
      if(edits.length){
        updating=true;const first=native.historyPtr,original=native.historyStack[first-1];
        try{
          for(const {id,attrs} of edits)replace(id,attrs);
          // Keep cleavage plus electron correction in one native undo step.
          const corrections=native.historyStack.slice(first).reverse();
          if(corrections.length){const combined=corrections.shift();for(const undo of corrections)combined.mergeWith(undo);combined.mergeWith(original);native.historyStack.splice(first-1,native.historyStack.length-first+1,combined);native.historyPtr=first;}
          note(mode()==='radical'?'Deletion kept the existing hydrogens and open electron states. Use Radicals / ions to choose charge and spin.':'Deletion kept the existing hydrogen count. Set the charge and radical state explicitly.');
        }catch(error){note(error.message,true);}finally{updating=false;}
      }
      before=snapshot(native);ptr=native.historyPtr;
    };
    native.subscribe('change',onChange);
    function selected(){const ids=native.selection()?.atoms||[];if(ids.length!==1)throw Error('Select one atom on the canvas first. Expand a collapsed group to edit its atoms.');const atom=native.struct().atoms.get(ids[0]);if(!atom||atom.sgs?.size)throw Error('Expand the group and select one of its atoms.');return {id:ids[0],atom};}
    return {
      read(){const {atom}=selected();return {label:atom.label,charge:atom.charge||0,radical:atom.radical||0,hydrogens:atom.implicitHCount??null};},
      apply(charge,radical,hydrogens){const {id,atom}=selected();replace(id,atomAttrs(atom,charge,radical,hydrogens));native.selection({atoms:[id]});before=snapshot(native);ptr=native.historyPtr;},
      reset(){before=snapshot(native);ptr=native.historyPtr;}
    };
  }
  root.EditorElectrons={electrons,deletionEdits,atomAttrs,attach};
})(globalThis);
