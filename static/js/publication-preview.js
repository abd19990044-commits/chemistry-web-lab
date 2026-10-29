/* MIT. The preview and exported figure share one renderer and numerical source. */
(function(root){
  'use strict';
  const el=id=>document.getElementById(id);let initialized=false,source=null,current=null,working=false;
  const note=message=>{el('figure-preview-status').textContent=message;};
  function refresh(){
    current=source();const doc=new DOMParser().parseFromString(current.svg,'image/svg+xml');
    if(doc.querySelector('parsererror')||doc.documentElement.localName!=='svg')throw Error('Could not prepare the vector preview.');
    el('figure-preview-art').replaceChildren(document.importNode(doc.documentElement,true));
    const s=root.PublicationFigures.settings(),d=root.PublicationFigures.dimensions(s,false);
    el('figure-preview-art').style.filter=s.format==='tiff-gray'?'grayscale(1)':'';
    el('figure-preview-name').textContent=current.name;
    note(`${s.widthMm} × ${s.heightMm} mm · ${s.dpi} dpi · ${d.width} × ${d.height} px for raster export. SVG keeps editable text and vector paths.`);
  }
  function open(getSource){source=getSource;sync();refresh();const labels=el('figure-preview-labels');labels.replaceChildren();for(const name of current.series||[]){const row=document.createElement('label');row.textContent='Legend label';const input=document.createElement('input');input.value=root.PublicationFigures.legendLabel(name);input.maxLength=160;input.addEventListener('change',()=>{try{root.PublicationFigures.setLegendLabel(name,input.value);refresh();}catch(error){note(error.message);}});row.append(input);labels.append(row);}el('figure-preview-legend-editor').hidden=!labels.childElementCount;el('figure-preview-dialog').showModal();}
  function sync(){for(const input of document.querySelectorAll('[data-figure-target]'))input.value=el(input.dataset.figureTarget).value;}
  function controls(){for(const b of el('figure-preview-dialog').querySelectorAll('button,input,select'))b.disabled=working;}
  async function save(){if(working)return;working=true;controls();try{refresh();await root.PublicationFigures.saveSVG(current.svg,current.name,root.PublicationFigures.settings(),current.metadata||{});note('Figure downloaded from the vector source at the selected print size.');}catch(error){note(error.message);}finally{working=false;controls();}}
  function copy(){
    if(working)return;if(!root.EditorIntegration){note('Image clipboard is unavailable. Download PNG instead.');return;}
    try{refresh();working=true;controls();const s=root.PublicationFigures.settings(),figure=root.PublicationFigures.pngImage(current.svg,s,current.metadata||{});
      // Clipboard write starts in the click handler, before asynchronous rendering.
      root.EditorIntegration.copyForWord(figure,current.name).then(async()=>{const f=await figure;note(`Copied ${f.width} × ${f.height} px at ${f.dpi} dpi. Paste the image into Word.`);}).catch(()=>note('Image copying was blocked or failed. Download PNG and insert it into Word.')).finally(()=>{working=false;controls();});
    }catch(error){working=false;controls();note(error.message);}
  }
  function setValue(id,value){const input=el(id);if(!input)return;input.value=value;input.dispatchEvent(new Event('change',{bubbles:true}));}
  function init(){
    if(initialized)return;initialized=true;
    el('figure-preview-close')?.addEventListener('click',()=>el('figure-preview-dialog').close());
    el('figure-preview-save')?.addEventListener('click',save);el('figure-preview-copy')?.addEventListener('click',copy);
    for(const input of document.querySelectorAll('[data-figure-target]'))input.addEventListener('change',()=>{try{setValue(input.dataset.figureTarget,input.value);sync();refresh();}catch(error){note(error.message);}});
    el('figure-style-preset')?.addEventListener('change',event=>{
      const compact=event.target.value==='compact',mono=event.target.value==='mono';
      setValue('figure-font','arial');setValue('figure-font-size',compact?'6.5':'8');setValue('figure-line-width',compact?'.8':'1.1');setValue('figure-legend','bottom');setValue('figure-frame','open');setValue('figure-palette','accessible');el('figure-monochrome').checked=mono;
      el('figure-grid').checked=false;el('figure-title').checked=false;
      if(compact){setValue('figure-width-mm','89');setValue('figure-height-mm','65');}
    });
    el('engine-3d-figure-preset')?.addEventListener('change',event=>{
      const preset=event.target.value;if(preset==='custom')return;
      for(const [id,value] of Object.entries({'engine-3d-style':preset==='sticks'?'stick':'ballAndStick','engine-3d-bg':'#ffffff','engine-3d-label-mode':preset==='numbered'?'elements':'hetero','engine-3d-label-color':'#111111','engine-3d-label-size':'8','engine-3d-label-badge':'transparent','engine-3d-atom-size':'.22','engine-3d-bond-radius':'.06','engine-3d-bond-spacing':'3.6','engine-3d-label-layout':'outside','engine-3d-label-hydrogens':'hide','engine-3d-figure-colors':'element'}))setValue(id,value);
      const bold=el('engine-3d-label-bold');if(bold?.classList.contains('btn-primary'))bold.click();
    });
  }
  root.PublicationPreview={open,init};
})(globalThis);
