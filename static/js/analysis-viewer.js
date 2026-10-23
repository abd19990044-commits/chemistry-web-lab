/* MIT. Read-only geometry inspection; view gestures never edit QM coordinates. */
(function(root){
  'use strict';
  const sub=(a,b)=>({x:a.x-b.x,y:a.y-b.y,z:a.z-b.z});
  const dot=(a,b)=>a.x*b.x+a.y*b.y+a.z*b.z;
  const cross=(a,b)=>({x:a.y*b.z-a.z*b.y,y:a.z*b.x-a.x*b.z,z:a.x*b.y-a.y*b.x});
  const length=a=>Math.hypot(a.x,a.y,a.z);
  function measure(points){
    if(points.length===2)return {label:'Distance',value:length(sub(points[0],points[1])),unit:'Å'};
    if(points.length===3){const a=sub(points[0],points[1]),b=sub(points[2],points[1]),n=length(a)*length(b);return {label:'Angle',value:n>1e-10?Math.acos(Math.max(-1,Math.min(1,dot(a,b)/n)))*180/Math.PI:null,unit:'°'};}
    if(points.length===4){const a=sub(points[0],points[1]),b=sub(points[2],points[1]),c=sub(points[3],points[2]);const n1=cross(b,a),n2=cross(b,c),n=length(n1)*length(n2),bn=length(b);return {label:'Dihedral',value:n>1e-10&&bn>1e-10?Math.atan2(dot(cross(n1,n2),b)/bn,dot(n1,n2))*180/Math.PI:null,unit:'°'};}
    return null;
  }
  function attachTopology(atoms,molecule){
    if(molecule.atoms.length!==atoms.length||molecule.atoms.some((a,i)=>a.elem!==atoms[i].elem))throw Error('Atom order or elements differ from the calculated geometry. Use an SDF/MOL with the same atom order.');
    MoleculeGraph.validate(molecule.atoms,molecule.bonds);
    const mapping=new Map(molecule.atoms.map((a,i)=>[a.id,atoms[i].id]));
    return molecule.bonds.map(b=>({...b,a1:mapping.get(b.a1),a2:mapping.get(b.a2)}));
  }
  function create(options){
    const {viewer,viewport,job,restyle}=options;
    const model=viewer.getModel(),atoms=model.selectedAtoms({});
    atoms.forEach((a,i)=>{a.id=i+1;a.molId=1;});
    let source=false,bonds=[],picks=[],selected=null,orthographic=false,spinning=false,spinFrame=0,lastFrame=0,disposed=false;
    if(job.molecular_topology?.topology==='source'){
      try{bonds=attachTopology(atoms,job.molecular_topology);source=true;}catch(e){options.warn?.(e.message);}
    }
    if(!source)bonds=atoms.length<=2000?MoleculeGraph.infer(atoms):[];
    MoleculeGraph.apply(model,bonds);model.setClickable({},true,()=>{});
    const events=new AbortController(),signal=events.signal;
    const el=id=>document.getElementById(id),listen=(node,type,fn)=>node?.addEventListener(type,fn,{signal});
    const mode=el('engine-3d-measure-mode'),details=el('engine-3d-inspector'),topology=el('engine-3d-topology-status');
    const notice=()=>{if(topology)topology.textContent=source?`${bonds.length} source bonds · bond orders preserved`:(bonds.length?`${bonds.length} estimated bonds · XYZ has no bond orders / formal charges`:'No verified bonds · coordinates only');};
    notice();if(details)details.textContent='Click an atom to inspect its coordinates and available partial charges.';
    viewer.setProjection('perspective');const projection=el('engine-3d-projection');if(projection){projection.textContent='Perspective';projection.setAttribute('aria-pressed','false');}
    function inspect(atom){
      selected=atom;
      const lines=[`${atom.elem}${atom.id} · X ${atom.x.toFixed(6)} · Y ${atom.y.toFixed(6)} · Z ${atom.z.toFixed(6)} Å`];
      for(const [name,values] of [['Mulliken',job.mulliken_charges||job.charges],['Hirshfeld',job.hirshfeld_charges||job.chg_charges]]){
        const q=values?.[atom.id-1];if(values?.length===atoms.length&&Number.isFinite(q))lines.push(`${name}: ${q>=0?'+':''}${q.toFixed(6)} e`);
      }
      if(details)details.textContent=lines.join('\n');
    }
    function draw(){
      if(disposed)return;
      const marked=new Set(picks.map(a=>a.id));if(selected)marked.add(selected.id);
      marked.forEach(id=>viewer.addStyle({index:id-1},{sphere:{scale:.34,color:'#f6c85f'}}));
      picks.forEach((a,i)=>{viewer.addLabel(`${i+1}: ${a.elem}${a.id}`,{position:a,fontColor:'#fff2b3',backgroundColor:'#283044',fontSize:13,showBackground:true,inFront:true});if(i)viewer.addLine?.({start:picks[i-1],end:a,color:'#f6c85f',linewidth:3,dashed:true});});
      const m=measure(picks);
      if(picks.length){const required=Number(mode?.value)||0;const line=picks.map(a=>`${a.elem}${a.id}`).join(' → ');if(details)details.textContent=`${line}\n${m?`${m.label}: ${m.value===null?'undefined (degenerate geometry)':m.value.toFixed(4)+' '+m.unit}`:'Pick the next atom.'}${picks.length<required?` · ${picks.length}/${required} selected`:''}`;}
      viewer.render();
    }
    function clear(){picks=[];selected=null;if(details)details.textContent='Click an atom to inspect its coordinates and available partial charges.';restyle();}
    function pick(x,y){const r=viewport.getBoundingClientRect();if(!r.width||!r.height||x<r.left||x>r.right||y<r.top||y>r.bottom)return null;const hit=viewer.targetedObjects?.((x-r.left)*2/r.width-1,1-(y-r.top)*2/r.height,{})?.[0]?.clickable;return hit?atoms.find(a=>a.id===hit.id):null;}
    function fit(){viewer.zoomTo();viewer.render();}
    viewport.tabIndex=0;viewport.setAttribute('aria-label','Calculated molecular geometry. Drag to orbit, right drag to pan, scroll to zoom. F fits; 1, 2, 3 select views.');
    const controls=MolecularControls.createController({element:viewport,getViewer:()=>viewer,getState:()=>({activeTool:'navigate',atoms}),pick,
      select:inspect,click(atom){inspect(atom);const needed=Number(mode?.value)||0;if(needed){if(picks.length>=needed)picks=[];if(!picks.some(a=>a.id===atom.id))picks.push(atom);}restyle();},clearSelection:clear,redraw:()=>viewer.render(),fit,viewChanged:()=>viewer.render()});
    const observer=new ResizeObserver(()=>{if(!disposed&&viewport.clientWidth&&viewport.clientHeight){viewer.resize();viewer.render();}});observer.observe(viewport);
    function preset(name){const v=viewer.getView().slice(),q=name==='right'?MolecularControls.rotation({x:0,y:1,z:0},Math.PI/2):name==='top'?MolecularControls.rotation({x:1,y:0,z:0},Math.PI/2):[0,0,0,1];v.splice(4,4,...q);viewer.setView(v);viewer.render();}
    for(const name of ['front','right','top'])listen(el('engine-3d-view-'+name),'click',()=>preset(name));
    listen(el('engine-3d-projection'),'click',e=>{orthographic=!orthographic;viewer.setProjection(orthographic?'orthographic':'perspective');e.currentTarget.textContent=orthographic?'Orthographic':'Perspective';e.currentTarget.setAttribute('aria-pressed',String(orthographic));viewer.render();});
    listen(el('engine-3d-clear-selection'),'click',clear);listen(mode,'change',clear);
    listen(viewport,'keydown',e=>{if(e.key.toLowerCase()==='f'){e.preventDefault();fit();}else if(e.key==='Escape'){e.preventDefault();clear();}else if(['1','2','3'].includes(e.key)){e.preventDefault();preset({1:'front',2:'right',3:'top'}[e.key]);}});
    function spin(){if(!spinning||disposed)return;if(document.hidden||!viewport.getClientRects().length){spinning=false;el('engine-3d-spin')?.classList.remove('btn-primary');return;}if(!document.hidden&&viewport.getBoundingClientRect().width&&viewport.getClientRects().length){const now=performance.now(),dt=lastFrame?Math.min(40,now-lastFrame):16;const v=viewer.getView().slice();v.splice(4,4,...MolecularControls.multiply(MolecularControls.rotation({x:0,y:1,z:0},dt*.00035),v.slice(4,8)));viewer.setView(v);viewer.render();lastFrame=now;}else lastFrame=0;spinFrame=requestAnimationFrame(spin);}
    function toggleSpin(){spinning=!spinning;el('engine-3d-spin')?.classList.toggle('btn-primary',spinning);if(spinning){lastFrame=0;spin();}else cancelAnimationFrame(spinFrame);}
    listen(viewport,'pointerdown',()=>{if(spinning)toggleSpin();});
    const topologyInput=el('engine-3d-topology-file');
    listen(el('engine-3d-topology-open'),'click',()=>topologyInput.click());
    listen(topologyInput,'change',async()=>{const file=topologyInput.files?.[0];if(!file)return;try{const molecule=await BrowserChemistry.file(file);if(disposed)return;if(molecule.topology!=='source')throw Error('Use SDF/MOL to supply bond orders.');const next=attachTopology(atoms,molecule);bonds=next;source=true;job.molecular_topology={...molecule,atoms:atoms.map(a=>({id:a.id,elem:a.elem,x:a.x,y:a.y,z:a.z,charge:molecule.atoms[a.id-1].charge||0})),bonds};MoleculeGraph.apply(model,bonds);notice();restyle();}catch(e){options.warn?.(e.message);}finally{topologyInput.value='';}});
    listen(el('engine-3d-download-png'),'click',async event=>{
      const button=event.currentTarget,wasSpinning=spinning;button.disabled=true;if(wasSpinning)toggleSpin();
      try{
        await PublicationFigures.saveMolecule(viewer,viewport,{
          prepare:()=>restyle(true),
          restore:()=>{const bg=el('engine-3d-bg')?.value||'#090e17';viewer.setBackgroundColor(bg==='transparent'?0:bg,bg==='transparent'?0:1);restyle();},
          metadata:{source_name:job.molecule||job.name||null,orca_version:job.orca_version||null,method:job.method||null,basis_set:job.basis_set||null,atoms:atoms.length,topology:source?'source':'estimated / unverified',geometry_coordinates:'unchanged'}
        });
      }catch(e){options.warn?.('Could not export molecular figure: '+e.message);}
      finally{button.disabled=false;if(wasSpinning&&!disposed)toggleSpin();}
    });
    return {draw,fit,toggleSpin,topology:()=>({atoms:atoms.map(a=>({id:a.id,elem:a.elem,x:a.x,y:a.y,z:a.z,charge:job.molecular_topology?.atoms?.[a.id-1]?.charge||0})),bonds:structuredClone(bonds),topology:source?'source':'unverified'}),destroy(){disposed=true;spinning=false;cancelAnimationFrame(spinFrame);controls.destroy();observer.disconnect();events.abort();el('engine-3d-spin')?.classList.remove('btn-primary');}};
  }
  root.AnalysisViewer={create,measure,attachTopology};
})(globalThis);
