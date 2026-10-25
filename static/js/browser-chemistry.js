/* Single lazy worker per browser tab; bounded jobs, cancellation, no server UFF. */
(function(root){
  'use strict';
  const scriptURL=new URL(document.currentScript.src),base=new URL('.',scriptURL);
  const workerURL=new URL('chemistry-worker.js',base);workerURL.search=scriptURL.search;
  let worker=null,pending=null,sequence=0,lookupTask=null,lookupAbort=null;
  const lookupCache=new Map();
  function cancel(){lookupAbort?.abort();lookupAbort=null;lookupTask=null;if(worker)worker.terminate();worker=null;if(pending){clearTimeout(pending.timer);pending.reject(Error('Local chemistry operation cancelled.'));pending=null;}}
  function run(type,payload,onProgress){
    if(pending)return Promise.reject(Error('Another local chemistry operation is running.'));
    if(!root.Worker||!root.WebAssembly)return Promise.reject(Error('This browser requires Web Workers and WebAssembly for local chemistry.'));
    if(!worker){worker=new Worker(workerURL);worker.onmessage=({data})=>{if(!pending||pending.id!==data.id)return;const job=pending;
      if(data.progress){clearTimeout(job.timer);job.timer=setTimeout(cancel,90000);job.onProgress?.(data.progress);return;}
      pending=null;clearTimeout(job.timer);data.error?job.reject(Error(data.error)):job.resolve(data.result);};worker.onerror=()=>{cancel();};}
    return new Promise((resolve,reject)=>{const id=++sequence;pending={id,resolve,reject,onProgress,timer:setTimeout(cancel,90000)};worker.postMessage({id,type,payload});});
  }
  async function file(file){if(file.size>5*1024*1024)throw Error('Structure file limit: 5 MB.');const format=file.name.split('.').pop().toLowerCase();return run('parse',{text:await file.text(),format,name:file.name.replace(/\.[^.]+$/,'')});}
  async function lookup(name){
    name=String(name||'').trim();if(!name)throw Error('Enter a compound name.');
    const key=name.toLowerCase(),cached=lookupCache.get(key);
    if(lookupTask){if(lookupTask.key!==key)throw Error('Another local chemistry operation is running.');return structuredClone(await lookupTask.promise);}
    if(pending)throw Error('Another local chemistry operation is running.');
    if(cached&&Date.now()-cached.at<15*60*1000)return structuredClone(cached.value);
    const controller=new AbortController(),fallbackController=new AbortController();lookupAbort=controller;
    const promise=(async()=>{
      const url='https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/'+encodeURIComponent(name)+'/SDF?record_type=';
      const request=async type=>{const response=await fetch(url+type,{signal:AbortSignal.any([controller.signal,...(type==='2d'?[fallbackController.signal]:[]),AbortSignal.timeout(type==='3d'?10000:15000)])});if(!response.ok)throw Error('PubChem HTTP '+response.status);return response.text();};
      // Both records are small. Fetch together so salts with no 3D conformer
      // do not pay for two sequential network round trips.
      const settle=promise=>promise.then(value=>({status:'fulfilled',value}),reason=>({status:'rejected',reason}));
      const threeDRequest=settle(request('3d')),twoDRequest=settle(request('2d'));
      const preferred=await threeDRequest;
      if(controller.signal.aborted)throw Error('Local chemistry operation cancelled.');
      const threeD=preferred.status==='fulfilled',record=threeD?preferred:await twoDRequest;
      if(threeD)fallbackController.abort();
      if(record.status!=='fulfilled')throw Error('PubChem could not return this compound. Try again or open an SDF/MOL file.');
      const result=await run('parse',{text:record.value,format:'sdf',name,generate3D:!threeD});
      if(controller.signal.aborted)throw Error('Local chemistry operation cancelled.');
      lookupCache.delete(key);lookupCache.set(key,{at:Date.now(),value:structuredClone(result)});
      while(lookupCache.size>20)lookupCache.delete(lookupCache.keys().next().value);
      return result;
    })();
    const task={key,promise};lookupTask=task;
    try{return structuredClone(await promise);}finally{if(lookupTask===task){lookupTask=null;lookupAbort=null;}}
  }
  root.BrowserChemistry={file,lookup,parse:(text,format='xyz',name='Molecule')=>run('parse',{text,format,name}),optimize:(payload,onProgress)=>run('optimize',payload,onProgress),stop:()=>worker?.postMessage({type:'stop'}),cancel};
})(globalThis);
