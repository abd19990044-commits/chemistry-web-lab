/* Self-hosted Ketcher bridge. Structures stay local until a user transfers them. */
(()=>{
  'use strict';
  const byId=id=>document.getElementById(id),frame=byId('sketch-frame');
  if(!frame)return;
  const draftKey='orca.chemdraw.editor-draft.v1';
  let readyTask=null,editor=null,dirty=false,revision=0,saveTimer=null,loading=false,busy=false;
  const sources={};
  const note=(message,error=false)=>{const el=byId('sketch-status');el.textContent=message;el.classList.toggle('chemdraw-error',error);};
  const decode=base64=>new TextDecoder().decode(Uint8Array.from(atob(base64),c=>c.charCodeAt(0)));
  function download(content,mime,name){const blob=content instanceof Blob?content:new Blob([content],{type:mime+';charset=utf-8'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=name;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  function activate(){document.querySelector('.ws-tab[data-target="sketcher"]')?.click();}
  function changed(){
    if(loading)return;dirty=true;const current=++revision;clearTimeout(saveTimer);
    saveTimer=setTimeout(async()=>{try{const ket=await editor.getKet();if(current!==revision)return;if(ket.length>2*1024*1024)throw Error('Draft too large');localStorage.setItem(draftKey,ket);}catch(_){note('Draft could not be saved in this browser. Use Save drawing to keep a file.',true);}},1000);
  }
  async function ready(){
    if(editor)return editor;
    if(readyTask)return readyTask;
    note('Loading standalone 2D editor and browser chemistry engine...');
    readyTask=new Promise((resolve,reject)=>{
      let timer,deadline,checking=false;
      const finish=()=>{clearInterval(timer);clearTimeout(deadline);window.removeEventListener('message',message);};
      const check=async()=>{
        if(checking)return;
        let candidate;try{candidate=frame.contentWindow?.ketcher;}catch(_){}
        if(!candidate?.setMolecule)return;checking=true;finish();editor=candidate;
        try{
          let draft;try{draft=localStorage.getItem(draftKey);}catch(_){}
          if(draft&&draft.length<=2*1024*1024){loading=true;await editor.setMolecule(draft);loading=false;dirty=true;note('Your local draft has been restored.');}
          else note('Editor ready. Use the left toolbar for atoms, bonds, rings and reaction tools.');
          editor.editor.subscribe('change',changed);resolve(editor);
        }catch(error){loading=false;editor=null;reject(error);}
      };
      const message=event=>{if(event.origin===location.origin&&event.source===frame.contentWindow&&event.data?.eventType==='init')check();};
      window.addEventListener('message',message);timer=setInterval(check,200);
      deadline=setTimeout(()=>{finish();reject(Error('The editor did not finish loading. Open the tab again to retry.'));},60000);
      frame.src=frame.dataset.src;check();
    });
    try{return await readyTask;}catch(error){readyTask=null;note(error.message,true);throw error;}
  }
  async function canReplace(){
    if(!dirty)return true;
    const dialog=byId('sketch-replace-dialog');
    return new Promise(resolve=>{
      const done=value=>{dialog.removeEventListener('close',closed);byId('sketch-replace-confirm').removeEventListener('click',replace);byId('sketch-replace-cancel').removeEventListener('click',cancel);dialog.close();resolve(value);};
      const replace=()=>done(true),cancel=()=>done(false),closed=()=>done(false);
      byId('sketch-replace-confirm').addEventListener('click',replace);byId('sketch-replace-cancel').addEventListener('click',cancel);dialog.addEventListener('close',closed);dialog.showModal();
    });
  }
  async function load(text){
    activate();const api=await ready();
    if(!await canReplace())return false;
    if(typeof text!=='string'||!text.trim()||text.length>5*1024*1024)throw Error('Open a nonempty chemical drawing file of at most 5 MB.');
    loading=true;revision++;clearTimeout(saveTimer);
    try{await api.setMolecule(text);dirty=false;note('Drawing loaded. Edit directly on the canvas; save KET to keep text, arrows and layout.');}
    finally{loading=false;}
    changed();return true;
  }
  async function action(fn){if(busy){note('Another editor operation is running.');return;}busy=true;try{await fn();}catch(error){note(error.message||'Editor operation failed.',true);}finally{busy=false;}}
  document.querySelector('.ws-tab[data-target="sketcher"]')?.addEventListener('click',()=>{ready().catch(()=>{});});
  for(const [event,kind] of [['compound','explorer'],['reaction','reaction']]){
    window.addEventListener('chemdraw:'+event,({detail})=>{sources[kind]=structuredClone(detail);});
    byId(kind+'-edit-2d')?.addEventListener('click',()=>action(async()=>{
      const source=sources[kind];if(!source)throw Error('Render a structure or reaction first.');
      if(kind==='explorer')await load(decode(source.mol_file_base64));
      else if(source.cdxml_file_base64){
        try{await load(decode(source.cdxml_file_base64));}
        catch(error){note('The ChemDraw layout could not be imported. Load the RXN chemistry using Open file, or save the CDXML for ChemDraw. '+error.message,true);}
      }else await load(decode(source.rxn_file_base64));
    }));
  }
  byId('sketch-import-file').addEventListener('change',event=>action(async()=>{const file=event.target.files?.[0];event.target.value='';if(!file)return;if(file.size>5*1024*1024)throw Error('Drawing file limit: 5 MB.');await load(await file.text());}));
  byId('sketch-load-text').addEventListener('click',()=>action(()=>load(byId('sketch-import-text').value)));
  byId('sketch-save-before-replace').addEventListener('click',async event=>{const button=event.currentTarget;button.disabled=true;try{download(await editor.getKet(),'application/json','chemical-drawing.ket');}catch(error){note(error.message,true);}finally{button.disabled=false;}});
  byId('sketch-save').addEventListener('click',()=>action(async()=>{
    const api=await ready(),format=byId('sketch-export-format').value,current=revision;
    if(format==='ket')download(await api.getKet(),'application/json','chemical-drawing.ket');
    else if(format==='mol'||format==='sdf'){const mol=await api.getMolfile('v2000');download(mol+(format==='sdf'?'$$$$\n':''),format==='sdf'?'chemical/x-mdl-sdfile':'chemical/x-mdl-molfile','chemical-structure.'+format);}
    else if(format==='rxn')download(await api.getRxn('v2000'),'chemical/x-mdl-rxnfile','chemical-reaction.rxn');
    else if(format==='smiles')download(await api.getSmiles(false),'chemical/x-daylight-smiles','chemical-drawing.smi');
    else if(format==='svg'||format==='png')download(await api.generateImage(await api.getKet(),{outputFormat:format,backgroundColor:'#FFFFFF'}),'image/'+(format==='svg'?'svg+xml':'png'),'chemical-drawing.'+format);
    if(format==='ket'&&revision===current)dirty=false;note(format==='ket'?'Editable drawing saved.':'Export saved. Save KET as well to retain the complete editable canvas.');
  }));
  byId('sketch-to-3d').addEventListener('click',()=>action(async()=>{
    const api=await ready();if(api.containsReaction())throw Error('Transfer a molecule without reaction arrows to 3D. Save the reaction as KET or RXN.');
    const mol=await api.getMolfile('v2000');note('Generating a hydrogen-complete 3D starting geometry locally...');
    const molecule=await BrowserChemistry.fromDrawing(mol,'Drawn molecule');
    window.openOrcaWizardWithCoords(molecule.coords,molecule.name,{molecule,formula:molecule.formula});
    note('Molecule opened in Molecular Studio. Review its 3D geometry and charge before calculation.');
  }));
  byId('sketch-to-reaction').addEventListener('click',()=>action(async()=>{
    const api=await ready();if(!api.containsReaction())throw Error('Draw a reaction arrow between reactants and products first.');
    const smiles=await api.getSmiles(false),parts=smiles.split('>');
    if(parts.length!==3||!parts[0]||!parts[2])throw Error('Use one reaction with species on both sides of the arrow.');
    byId('reaction-reactants').value=parts[0].split('.').map(s=>'smiles:'+s).join(' + ');byId('reaction-products').value=parts[2].split('.').map(s=>'smiles:'+s).join(' + ');
    if(parts[1])byId('reaction-arrow-top').value=parts[1];
    document.querySelector('.ws-tab[data-target="reaction"]')?.click();
    note('Reaction chemistry transferred. KET keeps the original canvas layout and annotations.');
  }));
  byId('sketch-expand').addEventListener('click',()=>action(async()=>{const wrapper=byId('sketch-frame-wrap');if(document.fullscreenElement)await document.exitFullscreen();else await wrapper.requestFullscreen();}));
})();
