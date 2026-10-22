/* Single lazy worker per browser tab; bounded jobs, cancellation, no server UFF. */
(function(root){
  'use strict';
  const base=new URL('.',document.currentScript.src);
  let worker=null,pending=null,sequence=0;
  function cancel(){if(worker)worker.terminate();worker=null;if(pending){clearTimeout(pending.timer);pending.reject(Error('Local chemistry operation cancelled.'));pending=null;}}
  function run(type,payload,onProgress){
    if(pending)return Promise.reject(Error('Another local chemistry operation is running.'));
    if(!root.Worker||!root.WebAssembly)return Promise.reject(Error('This browser requires Web Workers and WebAssembly for local chemistry.'));
    if(!worker){worker=new Worker(new URL('chemistry-worker.js',base));worker.onmessage=({data})=>{if(!pending||pending.id!==data.id)return;const job=pending;
      if(data.progress){clearTimeout(job.timer);job.timer=setTimeout(cancel,90000);job.onProgress?.(data.progress);return;}
      pending=null;clearTimeout(job.timer);data.error?job.reject(Error(data.error)):job.resolve(data.result);};worker.onerror=()=>{cancel();};}
    return new Promise((resolve,reject)=>{const id=++sequence;pending={id,resolve,reject,onProgress,timer:setTimeout(cancel,90000)};worker.postMessage({id,type,payload});});
  }
  async function file(file){if(file.size>5*1024*1024)throw Error('Structure file limit: 5 MB.');const format=file.name.split('.').pop().toLowerCase();return run('parse',{text:await file.text(),format,name:file.name.replace(/\.[^.]+$/,'')});}
  async function lookup(name){
    const url='https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/'+encodeURIComponent(name)+'/SDF?record_type=';
    let response=await fetch(url+'3d',{signal:AbortSignal.timeout(20000)}),generate3D=false;
    if(response.status===404){response=await fetch(url+'2d',{signal:AbortSignal.timeout(20000)});generate3D=true;}
    if(!response.ok)throw Error('PubChem could not return this compound. Try an SDF/MOL file.');
    return run('parse',{text:await response.text(),format:'sdf',name,generate3D});
  }
  root.BrowserChemistry={file,lookup,parse:(text,format='xyz',name='Molecule')=>run('parse',{text,format,name}),optimize:(payload,onProgress)=>run('optimize',payload,onProgress),stop:()=>worker?.postMessage({type:'stop'}),cancel};
})(globalThis);
