/* Self-hosted Ketcher integration. Editing, groups, images and drafts are local. */
(()=>{
  'use strict';
  const byId=id=>document.getElementById(id),frame=byId('sketch-frame'),integration=window.EditorIntegration;
  if(!frame||!integration)return;
  const draftKey='orca.chemdraw.editor-draft.v1',sources={};
  let readyTask=null,editor=null,dirty=false,revision=0,saveTimer=null,loading=false,busy=false,figureCache=null,previewURL=null;
  const note=(message,error=false)=>{const el=byId('sketch-status');el.textContent=message;el.classList.toggle('chemdraw-error',error);};
  const decode=base64=>new TextDecoder().decode(Uint8Array.from(atob(base64),c=>c.charCodeAt(0)));
  const title=()=>byId('sketch-drawing-title').value.trim()||'Chemical drawing';
  const basename=()=>title().replace(/[^a-zA-Z0-9_-]+/g,'-').slice(0,70)||'chemical-drawing';
  function download(content,mime,name){const blob=content instanceof Blob?content:new Blob([content],{type:mime+';charset=utf-8'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=name;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  function activate(){window.showChemistryView?.('draw');document.querySelector('.ws-tab[data-target="sketcher"]')?.click();}
  function panel(name){for(const item of document.querySelectorAll('[data-sketch-panel]')){const selected=item.dataset.sketchPanel===name;item.setAttribute('aria-expanded',String(selected));byId('sketch-panel-'+item.dataset.sketchPanel).classList.toggle('hidden',!selected);}}
  function controls(){for(const button of document.querySelectorAll('[data-sketch-command]'))button.disabled=busy;byId('sketch-use-explorer').disabled=busy||!sources.explorer;byId('sketch-use-reaction').disabled=busy||!sources.reaction;}
  function clearFigure(){figureCache=null;if(previewURL){URL.revokeObjectURL(previewURL);previewURL=null;}byId('sketch-preview-wrap').classList.add('hidden');}
  function changed(){
    if(loading)return;dirty=true;const current=++revision;clearFigure();clearTimeout(saveTimer);
    saveTimer=setTimeout(async()=>{try{const ket=await editor.getKet();if(current!==revision)return;if(ket.length>2*1024*1024)throw Error('Draft too large');localStorage.setItem(draftKey,ket);}catch(_){note('Draft could not be saved in this browser. Save KET to keep your drawing.',true);}},1000);
  }
  async function action(fn){if(busy){note('Another editor operation is running.');return false;}busy=true;controls();try{return await fn();}catch(error){note(error.message||'Editor operation failed.',true);return false;}finally{busy=false;controls();}}
  function nativeButton(name){const button=[...frame.contentDocument.querySelectorAll('button')].find(b=>(b.getAttribute('title')||b.getAttribute('aria-label')||'').startsWith(name));if(!button||button.disabled)throw Error('This editor tool is not ready.');button.click();}
  function nativeKeyboard(event){
    if(event.key!=='Enter'||event.isComposing||busy||!event.target.matches?.('[data-testid="AbbreviationLookup-input"]'))return;
    const label=event.target.value.trim();if(!label)return;
    const exact=[...frame.contentDocument.querySelectorAll('[role="option"]')].find(el=>{const text=(el.title||el.textContent).trim().toLowerCase();return text===label.toLowerCase()||text.split(' (')[0]===label.toLowerCase();});
    if(exact)return; // native known groups keep their native placement/undo tool
    event.preventDefault();event.stopImmediatePropagation();
    event.target.dispatchEvent(new frame.contentWindow.KeyboardEvent('keydown',{key:'Escape',code:'Escape',bubbles:true,cancelable:true}));
    action(()=>addGroup(label));
  }
  async function ready(){
    if(editor)return editor;if(readyTask)return readyTask;
    const initialScroll={top:window.scrollY,left:window.scrollX};let groupWarning='';
    try{integration.installGroups(localStorage);}catch(_){groupWarning=' Keyboard template storage is unavailable; use Write labels & groups to insert COOH.';}
    note('Loading standalone editor...');
    readyTask=new Promise((resolve,reject)=>{
      let timer,deadline,checking=false;
      const finish=()=>{clearInterval(timer);clearTimeout(deadline);window.removeEventListener('message',message);};
      const check=async()=>{
        if(checking)return;let candidate;try{candidate=frame.contentWindow?.ketcher;}catch(_){}
        if(!candidate?.setMolecule)return;checking=true;finish();editor=candidate;
        try{
          let draft;try{draft=localStorage.getItem(draftKey);}catch(_){}
          if(draft&&draft.length<=2*1024*1024){loading=true;await editor.setMolecule(draft);loading=false;dirty=true;note('Local draft restored.'+groupWarning);}
          else note('Editor ready. Draw, import a compound, or write a label.'+groupWarning);
          editor.editor.subscribe('change',changed);frame.contentDocument.addEventListener('keydown',escapeExpanded);frame.contentDocument.addEventListener('keydown',nativeKeyboard,true);
          if(byId('sketcher').classList.contains('is-active'))window.scrollTo({...initialScroll,behavior:'instant'});
          resolve(editor);
        }catch(error){loading=false;editor=null;reject(error);}
      };
      const message=event=>{if(event.origin===location.origin&&event.source===frame.contentWindow&&event.data?.eventType==='init')check();};
      window.addEventListener('message',message);timer=setInterval(check,200);deadline=setTimeout(()=>{finish();reject(Error('The editor did not finish loading. Open the tab again to retry.'));},60000);frame.src=frame.dataset.src;check();
    });
    try{return await readyTask;}catch(error){readyTask=null;note(error.message,true);throw error;}
  }
  async function canReplace(){
    if(!dirty)return true;const dialog=byId('sketch-replace-dialog');
    return new Promise(resolve=>{
      const done=value=>{dialog.removeEventListener('close',closed);byId('sketch-replace-confirm').removeEventListener('click',replace);byId('sketch-replace-cancel').removeEventListener('click',cancel);dialog.close();resolve(value);};
      const replace=()=>done(true),cancel=()=>done(false),closed=()=>done(false);
      byId('sketch-replace-confirm').addEventListener('click',replace);byId('sketch-replace-cancel').addEventListener('click',cancel);dialog.addEventListener('close',closed);dialog.showModal();
    });
  }
  async function load(structure,options={}){
    if(typeof structure!=='string'||!structure.trim()||structure.length>5*1024*1024)throw Error('Open a nonempty chemical drawing of at most 5 MB.');
    activate();const api=await ready(),mode=options.mode||byId('sketch-import-mode').value;
    if(!['replace','add'].includes(mode))throw Error('Choose Replace or Add import mode.');
    if(mode==='replace'&&!await canReplace())return false;
    loading=true;revision++;clearFigure();clearTimeout(saveTimer);note(mode==='add'?'Adding to the current drawing...':'Loading drawing...');
    try{
      if(options.reactionOptions&&mode==='add'){
        const data=await api.structService.layout({struct:structure,output_format:'chemical/x-indigo-ket'},{});
        await api.addFragment(JSON.stringify(ChemicalDrawing.decorateReaction(data.struct,options.reactionOptions)));
      }else{
        await (mode==='add'?api.addFragment(structure):api.setMolecule(structure));
        if(options.layout&&mode==='replace')await api.layout();
        if(options.reactionOptions)await api.setMolecule(JSON.stringify(ChemicalDrawing.decorateReaction(await api.getKet(),options.reactionOptions)));
      }
      if(mode==='replace'&&options.title)byId('sketch-drawing-title').value=options.title.slice(0,100);
      byId('sketch-origin').textContent=options.source||(mode==='add'?'Additional editable fragment':'Imported editable drawing');
      document.querySelector('.chemdraw-sketch-toolbar').scrollIntoView({block:'start',behavior:'instant'});
    }finally{loading=false;}
    changed();note(mode==='add'?'Added to the current drawing. Select and drag to arrange or connect it.':'Drawing loaded. Edit the canvas, then save KET or export an image.');return true;
  }
  async function loadSource(kind){const source=sources[kind];if(!source)throw Error('Render a molecule or reaction in its drawing tab first.');return load(decode(kind==='explorer'?source.mol_file_base64:source.rxn_file_base64),{title:kind==='explorer'?source.title||source.query_name:'Edited reaction',source:kind==='explorer'?'Molecule Explorer':'Reaction Drawing',...(kind==='reaction'?{reactionOptions:source.render_request||{}}:{})});}
  async function addGroup(value,smiles=''){
    const api=await ready(),def=integration.definition(value,smiles);let ket;
    if(def.defined){
      const converted=await api.structService.convert({struct:def.smiles,input_format:'chemical/x-daylight-smiles',output_format:'chemical/x-indigo-ket'},{'ignore-stereochemistry-errors':false});
      const layout=await api.structService.layout({struct:converted.struct,output_format:'chemical/x-indigo-ket'},{});ket=integration.makeGroup(def.label,layout.struct,true);
    }else ket=integration.makeGroup(def.label,null,false);
    await api.addFragment(JSON.stringify(ket));
    try{integration.registerGroup(localStorage,ket,def.defined);}catch(_){}
    note(def.defined?'Group '+def.label+' added with its real atoms and bonds. Connect or move it with the editor tools.':'Label '+def.label+' added as an undefined group. Supply its structure before transferring to calculations.');
  }
  async function assertDefined(api){if(integration.hasUndefined(await api.getKet()))throw Error('This drawing contains an undefined group label. Give it a chemical structure or replace it before transferring chemistry. You can still save KET or export artwork.');}
  function imageOptions(){return {longEdge:Number(byId('sketch-image-quality').value),transparent:byId('sketch-image-transparent').checked,dpi:600,title:title()};}
  function figure(){
    if(!editor)return Promise.reject(Error('Wait for the editor to finish loading.'));
    const options=imageOptions(),key=JSON.stringify({revision,...options});if(figureCache?.key===key)return figureCache.promise;
    const captured=revision,promise=(async()=>{
      const ket=await editor.getKet();if(revision!==captured)throw Error('The drawing changed while preparing export. Please try again.');
      const svg=await editor.generateImage(ket,{outputFormat:'svg',backgroundColor:options.transparent?'':'#FFFFFF'});
      const image=await integration.rasterize(svg,options);return {...image,revision:captured,key};
    })();figureCache={key,promise};promise.catch(()=>{if(figureCache?.key===key)figureCache=null;});return promise;
  }
  async function savePNG(){note('Rendering the edited drawing as PNG...');const image=await figure();download(image.blob,'image/png',basename()+'.png');note('PNG saved: '+image.width+' × '+image.height+' px; '+image.dpi+' dpi.');}
  function copyWord(){
    if(busy){note('Wait for the current editor operation.');return;}
    note('Preparing the edited drawing for Word...');const promise=figure();
    try{integration.copyForWord(promise,title()).then(()=>note('Image copied. Paste into Microsoft Word with Ctrl+V.')).catch(()=>note('Image copy was blocked or could not be prepared. Use Save PNG and insert the image into Word.',true));}catch(error){promise.catch(()=>{});note(error.message,true);}
  }
  async function preview(){note('Preparing image preview...');const image=await figure();if(image.revision!==revision)return;if(previewURL)URL.revokeObjectURL(previewURL);previewURL=URL.createObjectURL(image.blob);byId('sketch-preview-image').src=previewURL;byId('sketch-preview-size').textContent=image.width+' × '+image.height+' px · '+image.dpi+' dpi';byId('sketch-preview-wrap').classList.remove('hidden');note('Image preview ready. PNG and Word use this same drawing and image settings.');}
  document.querySelector('.ws-tab[data-target="sketcher"]')?.addEventListener('click',()=>{ready().catch(()=>{});});
  for(const button of document.querySelectorAll('[data-sketch-panel]'))button.addEventListener('click',()=>panel(button.dataset.sketchPanel));
  for(const button of document.querySelectorAll('[data-sketch-action]'))button.addEventListener('click',()=>{const command=button.dataset.sketchAction;if(command==='png')action(savePNG);else if(command==='word')copyWord();else if(command==='labels'){collapseExpanded();panel('labels');byId('sketch-panel-labels').scrollIntoView({block:'start'});byId('sketch-group-label').focus();}});
  for(const [event,kind] of [['compound','explorer'],['reaction','reaction']]){
    window.addEventListener('chemdraw:'+event,({detail})=>{sources[kind]=structuredClone(detail);controls();});
    byId(kind+'-edit-2d')?.addEventListener('click',()=>action(()=>loadSource(kind)));
    byId('sketch-use-'+kind).addEventListener('click',()=>action(()=>loadSource(kind)));
  }
  byId('sketch-pubchem-form').addEventListener('submit',event=>{event.preventDefault();action(async()=>{note('Fetching the 2D structure directly from PubChem...');const result=await integration.pubchem(byId('sketch-pubchem-query').value);await load(result.structure,{title:result.title,source:'PubChem'+(result.cid?' · CID '+result.cid:'')});});});
  byId('sketch-import-file').addEventListener('change',event=>action(async()=>{const file=event.target.files?.[0];event.target.value='';if(!file)return;if(file.size>5*1024*1024)throw Error('Drawing file limit: 5 MB.');await load(await file.text(),{title:file.name.replace(/\.[^.]+$/,''),source:'Local file'});}));
  byId('sketch-load-text').addEventListener('click',()=>action(()=>load(byId('sketch-import-text').value,{source:'Pasted structure / reaction'})));
  byId('sketch-label-form').addEventListener('submit',event=>{event.preventDefault();action(()=>addGroup(byId('sketch-group-label').value,byId('sketch-group-smiles').value));});
  byId('sketch-group-library').addEventListener('click',()=>action(async()=>{await ready();nativeButton('Structure Library');note('Search the library for a group, select it, then place it on the canvas.');}));
  byId('sketch-text-tool').addEventListener('click',()=>action(async()=>{await ready();nativeButton('Add text');note('Click the canvas and write any text, caption or equation. Use the native text controls to format it.');}));
  byId('sketch-image-preview').addEventListener('click',()=>action(preview));
  for(const id of ['sketch-image-quality','sketch-image-transparent','sketch-drawing-title'])byId(id).addEventListener('change',clearFigure);
  byId('sketch-save-before-replace').addEventListener('click',async event=>{const button=event.currentTarget;button.disabled=true;try{download(await editor.getKet(),'application/json',basename()+'.ket');}catch(error){note(error.message,true);}finally{button.disabled=false;}});
  byId('sketch-save').addEventListener('click',()=>action(async()=>{
    const api=await ready(),format=byId('sketch-export-format').value,current=revision;
    if(format==='ket')download(await api.getKet(),'application/json',basename()+'.ket');
    else if(format==='mol'||format==='sdf'){const mol=await api.getMolfile('v2000');download(mol+(format==='sdf'?'$$$$\n':''),format==='sdf'?'chemical/x-mdl-sdfile':'chemical/x-mdl-molfile',basename()+'.'+format);}
    else if(format==='rxn')download(await api.getRxn('v2000'),'chemical/x-mdl-rxnfile',basename()+'.rxn');
    else if(format==='smiles')download(await api.getSmiles(false),'chemical/x-daylight-smiles',basename()+'.smi');
    else if(format==='png'){await savePNG();return;}
    else if(format==='svg')download(await api.generateImage(await api.getKet(),{outputFormat:'svg',backgroundColor:byId('sketch-image-transparent').checked?'':'#FFFFFF'}),'image/svg+xml',basename()+'.svg');
    if(format==='ket'&&revision===current)dirty=false;note(format==='ket'?'Complete editable drawing saved.':'File exported. Save KET to retain the complete editable canvas.');
  }));
  byId('sketch-to-3d').addEventListener('click',()=>action(async()=>{
    const api=await ready();if(api.containsReaction())throw Error('Transfer a molecule without reaction arrows to 3D.');await assertDefined(api);
    note('Generating a hydrogen-complete 3D starting geometry on your device...');const molecule=await BrowserChemistry.fromDrawing(await api.getMolfile('v2000'),title());collapseExpanded();window.openOrcaWizardWithCoords(molecule.coords,molecule.name,{molecule,formula:molecule.formula});note('Molecule opened in Molecular Studio. Review its geometry, charge and multiplicity.');
  }));
  byId('sketch-to-reaction').addEventListener('click',()=>action(async()=>{
    const api=await ready();if(!api.containsReaction())throw Error('Draw a reaction arrow between reactants and products first.');await assertDefined(api);const parts=(await api.getSmiles(false)).split('>');
    if(parts.length!==3||!parts[0]||!parts[2])throw Error('Use one reaction with species on both sides.');byId('reaction-reactants').value=parts[0].split('.').map(s=>'smiles:'+s).join(' + ');byId('reaction-products').value=parts[2].split('.').map(s=>'smiles:'+s).join(' + ');if(parts[1])byId('reaction-arrow-top').value=parts[1];collapseExpanded();document.querySelector('.ws-tab[data-target="reaction"]')?.click();note('Edited reaction chemistry transferred. KET retains its exact artwork and captions.');
  }));
  byId('sketch-to-explorer').addEventListener('click',()=>action(async()=>{const api=await ready();if(api.containsReaction())throw Error('Send a molecule to Explorer, or use Reaction Drawing for a reaction.');await assertDefined(api);const smiles=await api.getSmiles(false);if(!smiles)throw Error('Draw a molecule first.');collapseExpanded();document.querySelector('.ws-tab[data-target="explorer"]')?.click();byId('explorer-query').value=smiles;byId('explorer-form').requestSubmit();}));
  function collapseExpanded(){byId('sketch-frame-wrap').classList.remove('is-expanded');byId('sketch-collapse').classList.add('hidden');byId('sketch-expand').setAttribute('aria-expanded','false');}
  function escapeExpanded(event){if(event.key==='Escape')collapseExpanded();}
  byId('sketch-expand').addEventListener('click',()=>{byId('sketch-frame-wrap').classList.add('is-expanded');byId('sketch-collapse').classList.remove('hidden');byId('sketch-expand').setAttribute('aria-expanded','true');});byId('sketch-collapse').addEventListener('click',collapseExpanded);document.addEventListener('keydown',escapeExpanded);
  window.OrcaDrawingEditor={open:({structure,title,mode='replace',layout=false,source='Transferred drawing'}={})=>action(()=>load(structure,{title,mode,layout,source}))};
  window.addEventListener('orca:edit-2d',({detail})=>{if(detail&&typeof detail==='object')window.OrcaDrawingEditor.open(detail);});controls();
})();
