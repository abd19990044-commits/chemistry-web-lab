/* ChemDraw bot features adapted to ORCA Web Lab; no Telegram state or tokens. */
(()=>{
  'use strict';
  const byId=id=>document.getElementById(id);
  if(!byId('chemdraw-advanced-form'))return;
  const snapshots={};
  const preferences=['explorer-export-transparent','reaction-export-transparent','reaction-auto-balance','reaction-small-as-formula','reaction-arrow-style'];
  const defaults={'explorer-export-transparent':false,'reaction-export-transparent':false,'reaction-auto-balance':true,'reaction-small-as-formula':true,'reaction-arrow-style':'forward'};
  const prefKey='orca.chemdraw.drawing-preferences.v1';
  function applyPreferences(values){for(const id of preferences){const el=byId(id);if(!el)continue;const value=values[id]??defaults[id];if(el.type==='checkbox')el.checked=typeof value==='boolean'?value:defaults[id];else if(['forward','equilibrium','reversible'].includes(value))el.value=value;}}
  try{applyPreferences(JSON.parse(localStorage.getItem(prefKey)||'{}'));}catch(_){applyPreferences(defaults);}
  function savePreferences(){const values={};for(const id of preferences){const el=byId(id);if(el)values[id]=el.type==='checkbox'?el.checked:el.value;}try{localStorage.setItem(prefKey,JSON.stringify(values));}catch(_){}}
  for(const id of preferences)byId(id)?.addEventListener('change',savePreferences);
  byId('chemdraw-reset-preferences')?.addEventListener('click',()=>{applyPreferences(defaults);savePreferences();status('chemdraw-advanced-status','Drawing preferences reset. They apply to the next drawing.');});
  function status(id,message,error=false){const el=byId(id);if(el){el.textContent=message;el.classList.toggle('chemdraw-error',error);}}
  function dataLink(id,base64,mime,filename){const link=byId(id);if(!link)return;link.classList.toggle('hidden',!base64);if(base64){link.href='data:'+mime+';base64,'+base64;link.download=filename;}else link.removeAttribute('href');}
  for(const [event,kind] of [['compound','explorer'],['reaction','reaction']])window.addEventListener('chemdraw:'+event,({detail})=>{
    snapshots[kind]=structuredClone(detail.render_request);
    dataLink(kind+'-download-png',detail.image_png_base64,'image/png',kind+'-drawing.png');
    status(kind+'-export-status','');
    const canvas=byId(kind==='explorer'?'explorer-img-stage':'reaction-canvas-container');
    if(canvas)canvas.style.background=detail.render_request?.transparent_background?'repeating-conic-gradient(#e2e8f0 0% 25%, #fff 0% 50%) 50% / 16px 16px':'#ffffff';
  });
  async function post(path,payload,blob=false){
    // app.js attaches the current session's CSRF token to same-origin POSTs.
    const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),signal:AbortSignal.timeout(60000)});
    if(!response.ok){let message='Chemistry request failed (HTTP '+response.status+').';try{const data=await response.json();message=typeof data.error==='string'?data.error:message;}catch(_){}throw Error(message);}
    return blob?response.blob():response.json();
  }
  for(const kind of ['explorer','reaction'])byId(kind+'-download-tiff')?.addEventListener('click',async event=>{
    if(!snapshots[kind]){status(kind+'-export-status','Render a drawing first.',true);return;}
    const button=event.currentTarget;button.disabled=true;status(kind+'-export-status','Preparing TIFF from the displayed drawing...');
    try{
      const blob=await post('/api/drawing/export',{...snapshots[kind],format:'tiff'},true);
      const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=kind+'-drawing.tiff';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
      status(kind+'-export-status','TIFF saved at 600 dpi.');
    }catch(error){status(kind+'-export-status',error.message,true);}finally{button.disabled=false;}
  });
  const advanced=byId('chemdraw-advanced-form');
  advanced.addEventListener('submit',async event=>{
    event.preventDefault();
    const action=event.submitter?.value||'redox_acidic';
    const payload={action,reactants:byId('chemdraw-reactants').value.trim(),products:byId('chemdraw-products').value.trim()};
    const buttons=[...advanced.querySelectorAll('button')];buttons.forEach(b=>b.disabled=true);
    byId('chemdraw-advanced-result').classList.add('hidden');status('chemdraw-advanced-status','Calculating and checking the entered structures...');
    try{
      const data=await post('/api/drawing/advanced',payload);
      byId('chemdraw-advanced-text').textContent=data.text;
      byId('chemdraw-advanced-note').textContent=data.note||'';
      byId('chemdraw-spectators').textContent=data.spectators?.length?'Cancelled spectator ions: '+data.spectators.join(', '):'';
      byId('chemdraw-advanced-result').classList.remove('hidden');status('chemdraw-advanced-status','Result ready.');
    }catch(error){status('chemdraw-advanced-status',error.message,true);}finally{buttons.forEach(b=>b.disabled=false);}
  });
  byId('chemdraw-copy-result').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(byId('chemdraw-advanced-text').textContent);status('chemdraw-advanced-status','Copied.');}catch(_){status('chemdraw-advanced-status','Select the result text and copy it manually.',true);}});
  byId('chemdraw-use-reaction').addEventListener('click',()=>{byId('chemdraw-reactants').value=byId('reaction-reactants').value;byId('chemdraw-products').value=byId('reaction-products').value;});
  byId('chemdraw-ionic-example').addEventListener('click',()=>{byId('chemdraw-reactants').value='Cl + [Na+].[OH-]';byId('chemdraw-products').value='[Na+].[Cl-] + O';});
  const mechanism=byId('chemdraw-mechanism-form');
  mechanism.addEventListener('submit',async event=>{
    event.preventDefault();const button=mechanism.querySelector('button');button.disabled=true;
    byId('chemdraw-mechanism-result').classList.add('hidden');status('chemdraw-mechanism-status','Drawing numbered atoms and the requested arrows...');
    try{
      const data=await post('/api/drawing/mechanism',{smiles:byId('chemdraw-mechanism-smiles').value.trim(),arrows:byId('chemdraw-mechanism-arrows').value.trim()});
      byId('chemdraw-mechanism-image').src='data:image/svg+xml;base64,'+data.image_svg_base64;
      dataLink('chemdraw-mechanism-download',data.image_svg_base64,'image/svg+xml','mechanism.svg');
      byId('chemdraw-mechanism-result').classList.remove('hidden');status('chemdraw-mechanism-status','Ready. Arrow numbers refer to this exact SMILES atom order.');
    }catch(error){status('chemdraw-mechanism-status',error.message,true);}finally{button.disabled=false;}
  });
})();
