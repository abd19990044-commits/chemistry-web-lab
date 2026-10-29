/* MIT. Publication figures are rendered from numerical data on the user's device. */
(function(root){
  'use strict';
  const palette=['#0072B2','#D55E00','#009E73','#CC79A7','#000000','#8C6500'];
  const dashes=['','5 2','2 2','6 2 1 2','1 2','8 3','3 1 1 1','9 2 2 2','4 3','2 4','10 2 1 2 1 2','7 1'];
  const finite=v=>typeof v==='number'&&Number.isFinite(v);
  const esc=v=>String(v??'').replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g,'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&apos;'}[c]));
  const el=id=>typeof document==='undefined'?null:document.getElementById(id);
  let busy=false;
  function settings(overrides={}){
    return {widthMm:Number(el('figure-width-mm')?.value||180),heightMm:Number(el('figure-height-mm')?.value||110),
      dpi:Number(el('figure-dpi')?.value||1200),format:el('figure-format')?.value||'png',
      monochrome:el('figure-monochrome')?.checked||false,includeTitle:el('figure-title')?.checked||false,
      panel:el('figure-panel')?.value||'',fontFamily:el('figure-font')?.value||'arial',fontSize:Number(el('figure-font-size')?.value||8),lineWidth:Number(el('figure-line-width')?.value||1.1),palette:el('figure-palette')?.value||'accessible',legend:el('figure-legend')?.value||'bottom',frame:el('figure-frame')?.value||'open',grid:el('figure-grid')?.checked||false,peakLabels:el('figure-peaks')?.checked||false,...overrides};
  }
  function dimensions(s,raster=true){
    if(!finite(s.widthMm)||!finite(s.heightMm)||s.widthMm<50||s.widthMm>190||s.heightMm<40||s.heightMm>240)throw Error('Choose a width of 50-190 mm and a height of 40-240 mm.');
    if(![300,600,1000,1200].includes(s.dpi))throw Error('Choose 300, 600, 1000 or 1200 dpi.');
    const width=Math.round(s.widthMm*s.dpi/25.4),height=Math.round(s.heightMm*s.dpi/25.4);
    if(raster&&(width>12000||height>12000||width*height>64000000))throw Error('This size exceeds the 64 megapixel export limit. Use 600 dpi, a smaller size, or SVG for plots.');
    return {width,height,w:s.widthMm*72/25.4,h:s.heightMm*72/25.4};
  }
  function ticks(lo,hi,count=5){
    if(!finite(lo)||!finite(hi)||hi<=lo)throw Error('The axis limits must be finite and increasing.');
    const raw=(hi-lo)/count,power=10**Math.floor(Math.log10(raw)),step=([1,2,2.5,5,10].find(n=>n*power>=raw)||10)*power;
    const values=[];for(let i=Math.ceil(lo/step);i*step<=hi+step*1e-8&&values.length<30;i++)values.push(i*step);
    return values;
  }
  const legendLabels=new Map();
  const legendLabel=name=>legendLabels.get(name)||name;
  function setLegendLabel(name,label){label=String(label).trim();if(!label||label.length>160)throw Error('Use a legend label of 1-160 characters.');legendLabels.delete(name);legendLabels.set(name,label);while(legendLabels.size>200)legendLabels.delete(legendLabels.keys().next().value);}
  function plot(spec,s=settings()){if(!root.PublicationStyle)throw Error('Reload the page to load publication layouts.');return root.PublicationStyle.plot({...spec,series:(spec.series||[]).map(item=>({...item,displayName:legendLabels.get(item.name)||item.displayName}))},s);}
  function orbitals(job,s=settings()){if(!root.PublicationStyle)throw Error('Reload the page to load publication layouts.');return root.PublicationStyle.orbitals(job,s);}
  const crcTable=Array.from({length:256},(_,n)=>{for(let k=0;k<8;k++)n=n&1?0xedb88320^(n>>>1):n>>>1;return n>>>0;});
  function crc(bytes){let n=0xffffffff;for(const b of bytes)n=crcTable[(n^b)&255]^(n>>>8);return (n^0xffffffff)>>>0;}
  function concat(parts){const out=new Uint8Array(parts.reduce((n,p)=>n+p.length,0));let pos=0;for(const p of parts){out.set(p,pos);pos+=p.length;}return out;}
  function chunk(type,data){const out=new Uint8Array(data.length+12),v=new DataView(out.buffer);v.setUint32(0,data.length);out.set(new TextEncoder().encode(type),4);out.set(data,8);v.setUint32(out.length-4,crc(out.subarray(4,out.length-4)));return out;}
  function pngMetadata(bytes,dpi,metadata={}){
    if(bytes.length<20||bytes[0]!==137||bytes[1]!==80)throw Error('Invalid PNG data.');
    const phys=new Uint8Array(9),view=new DataView(phys.buffer),ppm=Math.round(dpi/.0254);view.setUint32(0,ppm);view.setUint32(4,ppm);phys[8]=1;
    const parts=[bytes.slice(0,8)];let inserted=false,offset=8;
    while(offset+12<=bytes.length){
      const size=new DataView(bytes.buffer,bytes.byteOffset+offset,4).getUint32(0),end=offset+12+size;
      if(end>bytes.length)throw Error('Truncated PNG data.');
      const type=String.fromCharCode(...bytes.subarray(offset+4,offset+8));
      if(!inserted&&(type==='IDAT'||type==='IEND')){parts.push(chunk('pHYs',phys),chunk('iTXt',new TextEncoder().encode('Description\0\0\0\0\0'+JSON.stringify(metadata))));inserted=true;}
      if(type!=='pHYs')parts.push(bytes.slice(offset,end));offset=end;if(type==='IEND')break;
    }
    if(!inserted)throw Error('PNG image data missing.');return concat(parts);
  }
  function tiff(rgba,width,height,dpi,metadata={},mode='rgb'){
    if(!Number.isInteger(width)||!Number.isInteger(height)||width<1||height<1||width*height>64000000||rgba.length!==width*height*4||!['rgb','gray','bitmap'].includes(mode))throw Error('Invalid TIFF raster dimensions or mode.');
    const ascii=JSON.stringify({color_space:mode==='rgb'?'sRGB':mode==='gray'?'grayscale':'1-bit line art',...metadata}).replace(/[\u007f-\uffff]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));
    const description=new TextEncoder().encode(ascii+'\0'),software=new TextEncoder().encode('Chemistry Web Lab\0'),align=n=>(n+1)&~1;
    const entries=15,ifdEnd=8+2+entries*12+4,bits=ifdEnd,xres=bits+6,yres=xres+8,desc=yres+8,soft=align(desc+description.length),pixels=align(soft+software.length);
    const samples=mode==='rgb'?3:1,bitDepth=mode==='bitmap'?1:8,rowBytes=mode==='bitmap'?Math.ceil(width/8):width*samples,byteCount=rowBytes*height;
    const out=new Uint8Array(pixels+byteCount),v=new DataView(out.buffer);
    out[0]=out[1]=73;v.setUint16(2,42,true);v.setUint32(4,8,true);v.setUint16(8,entries,true);
    const tags=[[256,4,1,width],[257,4,1,height],[258,3,samples,samples===3?bits:bitDepth],[259,3,1,1],[262,3,1,mode==='rgb'?2:1],[270,2,description.length,desc],[273,4,1,pixels],[277,3,1,samples],[278,4,1,height],[279,4,1,byteCount],[282,5,1,xres],[283,5,1,yres],[284,3,1,1],[296,3,1,2],[305,2,software.length,soft]];
    tags.forEach(([tag,type,count,value],i)=>{const p=10+i*12;v.setUint16(p,tag,true);v.setUint16(p+2,type,true);v.setUint32(p+4,count,true);if(type===3&&count===1)v.setUint16(p+8,value,true);else v.setUint32(p+8,value,true);});
    for(let i=0;i<3;i++)v.setUint16(bits+i*2,8,true);
    for(const p of [xres,yres]){v.setUint32(p,dpi,true);v.setUint32(p+4,1,true);}
    out.set(description,desc);out.set(software,soft);
    for(let i=0;i<width*height;i++){const alpha=rgba[i*4+3]/255,r=Math.round(rgba[i*4]*alpha+255*(1-alpha)),g=Math.round(rgba[i*4+1]*alpha+255*(1-alpha)),b=Math.round(rgba[i*4+2]*alpha+255*(1-alpha));
      if(mode==='rgb'){out[pixels+i*3]=r;out[pixels+i*3+1]=g;out[pixels+i*3+2]=b;}
      else{const gray=Math.round(.2126*r+.7152*g+.0722*b);if(mode==='gray')out[pixels+i]=gray;else if(gray>=160)out[pixels+Math.floor(i/width)*rowBytes+Math.floor((i%width)/8)]|=1<<(7-(i%width)%8);}
    }return out;
  }
  async function imageOf(blob){
    const url=URL.createObjectURL(blob);try{return await new Promise((resolve,reject)=>{const img=new Image();img.onload=()=>resolve(img);img.onerror=()=>reject(Error('Could not render the figure.'));img.src=url;});}finally{URL.revokeObjectURL(url);}
  }
  function download(blob,name){const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  async function saveRaster(canvas,s,name,metadata){
    const meta={...metadata,dpi:s.dpi,width_mm:s.widthMm,height_mm:s.heightMm,pixel_width:canvas.width,pixel_height:canvas.height,color_space:s.format==='tiff-gray'?'grayscale':s.format==='tiff-line'?'1-bit line art':'sRGB',figure_style:root.PublicationStyle?.style(s)};
    if(s.format.startsWith('tiff'))download(new Blob([tiff(canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data,canvas.width,canvas.height,s.dpi,meta,s.format==='tiff-gray'?'gray':s.format==='tiff-line'?'bitmap':'rgb')],{type:'image/tiff'}),name+'.tiff');
    else{const blob=await new Promise((resolve,reject)=>canvas.toBlob(b=>b?resolve(b):reject(Error('PNG encoding failed.')),'image/png'));download(new Blob([pngMetadata(new Uint8Array(await blob.arrayBuffer()),s.dpi,meta)],{type:'image/png'}),name+'.png');}
  }
  function safeName(value){return String(value||'figure').replace(/[^\p{L}\p{N}._-]+/gu,'_').slice(0,100);}
  async function rasterSVG(svg,s){
    const {width,height}=dimensions(s),canvas=document.createElement('canvas');canvas.width=width;canvas.height=height;
    try{const ctx=canvas.getContext('2d');if(!ctx)throw Error('Canvas export is unavailable.');ctx.fillStyle='#fff';ctx.fillRect(0,0,width,height);ctx.drawImage(await imageOf(new Blob([svg],{type:'image/svg+xml;charset=utf-8'})),0,0,width,height);
      if(s.format==='tiff-gray'){const image=ctx.getImageData(0,0,width,height);for(let i=0;i<image.data.length;i+=4){const gray=Math.round(.2126*image.data[i]+.7152*image.data[i+1]+.0722*image.data[i+2]);image.data[i]=image.data[i+1]=image.data[i+2]=gray;}ctx.putImageData(image,0,0);}
      return canvas;
    }catch(error){canvas.width=canvas.height=1;throw error;}
  }
  async function pngImage(svg,s=settings(),metadata={}){
    const canvas=await rasterSVG(svg,s);
    try{const raw=await new Promise((resolve,reject)=>canvas.toBlob(blob=>blob?resolve(blob):reject(Error('PNG encoding failed.')),'image/png'));
      const bytes=pngMetadata(new Uint8Array(await raw.arrayBuffer()),s.dpi,{...metadata,width_mm:s.widthMm,height_mm:s.heightMm,pixel_width:canvas.width,pixel_height:canvas.height,figure_style:root.PublicationStyle?.style(s)});
      return {blob:new Blob([bytes],{type:'image/png'}),width:canvas.width,height:canvas.height,dpi:s.dpi};
    }finally{canvas.width=canvas.height=1;}
  }
  async function saveSVG(svg,name,s,metadata){
    if(s.format==='tiff-line'&&s.dpi<1000)throw Error('Choose 1000 or 1200 dpi for 1-bit line art.');
    if(s.format==='svg'){download(new Blob([svg],{type:'image/svg+xml;charset=utf-8'}),safeName(name)+'.svg');return;}
    const canvas=await rasterSVG(svg,s);try{await saveRaster(canvas,s,safeName(name),metadata);}finally{canvas.width=canvas.height=1;}
  }
  async function exclusive(action){if(busy)throw Error('A figure export is already being prepared.');busy=true;try{return await action();}finally{busy=false;}}
  function savePlot(spec,name){return exclusive(()=>{const s=settings();return saveSVG(plot(spec,s),name,s,{kind:'spectrum',x_label:spec.xLabel,y_label:spec.yLabel,x_min:spec.xMin,x_max:spec.xMax,reverse_x:!!spec.reverseX,y_min:spec.yMin??0,y_max:spec.yMax,y2_label:spec.y2Label||null,layers:spec.series.map(v=>v.name),...spec.metadata});});}
  function saveOrbitals(job,name){return exclusive(()=>{const s=settings();return saveSVG(orbitals(job,s),name,s,{kind:'frontier_orbital_eigenvalues',source_name:job.molecule||job.name||null,method:job.method||null,basis_set:job.basis_set||null,homo_ev:job.homo_ev,lumo_ev:job.lumo_ev,alpha_homo_ev:job.alpha_homo_ev,alpha_lumo_ev:job.alpha_lumo_ev,beta_homo_ev:job.beta_homo_ev,beta_lumo_ev:job.beta_lumo_ev});});}
  async function renderMolecule(viewer,options={},preview=false){
    if(!root.PublicationMolecule)throw Error('Reload the page to load the molecular figure renderer.');
    const s=settings(),{width,height}=dimensions(s),atoms=viewer.getModel()?.selectedAtoms({});
    if(!atoms?.length)throw Error('Open a molecular geometry before exporting.');
    const value=(id,fallback)=>el(id)?.value||fallback;
    const selectedBackground=value('engine-3d-bg','#ffffff'),background=selectedBackground==='custom'?value('engine-3d-bg-color','#ffffff'):selectedBackground,labelSize=Number(value('engine-3d-label-size',8)),labelColor=value('engine-3d-label-color','#111111');
    if(!finite(labelSize)||labelSize<5||labelSize>20)throw Error('Choose a label size of 5-20 pt.');
    const drawing={style:value('engine-3d-style','ballAndStick'),atomScale:Number(value('engine-3d-atom-size',.24)),bondRadius:Number(value('engine-3d-bond-radius',.085)),bondSpacing:Number(value('engine-3d-bond-spacing',3.2)),colorMode:value('engine-3d-figure-colors','element')};
    const bold=el('engine-3d-label-bold')?.classList.contains('btn-primary')||false,placement=value('engine-3d-label-layout','outside'),badge=value('engine-3d-label-badge','transparent');
    const scene=root.PublicationMolecule.scene(atoms,viewer.getView().slice(),drawing);
    const scale=preview?Math.min(1,1100/width,800/height):1,w=Math.round(width*scale),h=Math.round(height*scale);
    const canvas=document.createElement('canvas');canvas.width=w;canvas.height=h;const ctx=canvas.getContext('2d');
    const sampleScale=w*h<=4000000&&typeof ctx.drawImage==='function'&&typeof ctx.getImageData==='function'?2:1;
    const raster=await root.PublicationMolecule.raster(scene,w*sampleScale,h*sampleScale,{background,padding:options.labels?.some(Boolean)?.13:.075,yield:()=>new Promise(resolve=>setTimeout(resolve,0))});
    let image;
    if(sampleScale===2){const high=document.createElement('canvas');high.width=raster.width;high.height=raster.height;const hc=high.getContext('2d'),full=hc.createImageData(high.width,high.height);full.data.set(raster.data);hc.putImageData(full,0,0);ctx.imageSmoothingEnabled=true;ctx.imageSmoothingQuality='high';ctx.drawImage(high,0,0,w,h);image=ctx.getImageData(0,0,w,h);high.width=high.height=1;}
    else{image=ctx.createImageData(w,h);image.data.set(raster.data);ctx.putImageData(image,0,0);}
    raster.data=null;
    if(canvas.style&&s.format==='tiff-gray')canvas.style.filter='grayscale(1)';
    // Lay out once in physical points: dpi and preview resolution cannot change composition.
    const physical=dimensions(s,false),sx=w/physical.w,sy=h/physical.h;
    const fontFamily=root.PublicationStyle?.style(s).font||'Arial, Helvetica, sans-serif';
    ctx.font=`${bold?'600':'400'} ${labelSize}px ${fontFamily}`;
    const baseBoxes=root.PublicationMolecule.layoutLabels(scene,root.PublicationMolecule.frame(scene,physical.w,physical.h,options.labels?.some(Boolean)?.13:.075),physical.w,physical.h,options.labels||[],labelSize,text=>ctx.measureText(text).width,placement);
    const labelStyle={fontSize:labelSize*sx,color:labelColor,badge,bold,background,fontFamily},panel=s.panel;
    let boxes=[];
    function redrawLabels(positions={}){
      const physicalBoxes=root.PublicationMolecule.positionLabels(baseBoxes,physical.w,physical.h,positions);
      boxes=root.PublicationMolecule.scaleLabels(physicalBoxes,sx,sy);
      ctx.putImageData(image,0,0);root.PublicationMolecule.drawLabels(ctx,boxes,labelStyle);
      if(panel){ctx.textAlign='left';ctx.textBaseline='alphabetic';ctx.font=`bold ${9*sx}px Arial`;ctx.fillStyle=labelColor;ctx.fillText(panel,8*sx,12*sy);}
      return boxes.map(b=>({...b,x:b.x/w,y:b.y/h,w:b.w/w,h:b.h/h}));
    }
    const labelBoxes=redrawLabels(options.labelPositions);
    return {canvas,settings:s,labelBoxes,redrawLabels,metadata:{kind:'molecular_geometry',...options.metadata,renderer:'analytic spheres and cylinders; depth buffered; matte diffuse lighting; adaptive supersampling',projection:'orthographic; current orientation; automatic framing',coordinates:'unchanged',source_bonds:scene.bonds.map(b=>({atom_1:b.i+1,atom_2:b.j+1,order:b.order})),drawing,background,label_size_pt:labelSize,label_color:labelColor,label_layout:placement,label_positions:labelBoxes.map(b=>({atom:b.atom+1,text:b.text,x:b.x+b.w/2,y:b.y+b.h/2,manual:!!options.labelPositions?.[b.atom]}))}};
  }

  function saveMolecule(viewer,viewport,options={}){return exclusive(async()=>{
    if(['svg','tiff-line'].includes(settings().format))throw Error('Choose PNG, RGB TIFF or grayscale TIFF for 3D geometry. Vector and 1-bit line art are available for plots.');
    const result=await renderMolecule(viewer,options);
    try{await saveRaster(result.canvas,result.settings,'molecular-geometry-publication',result.metadata);}finally{result.canvas.width=result.canvas.height=1;}
  });}
  function init(){
    root.PublicationPreview?.init();
    el('figure-format')?.addEventListener('change',e=>{if(e.target.value==='tiff-line'&&Number(el('figure-dpi').value)<1000)el('figure-dpi').value='1200';});
    el('figure-preset')?.addEventListener('change',e=>{const [w,h]=e.target.value.split(',');el('figure-width-mm').value=w;el('figure-height-mm').value=h;});
  }
  root.PublicationFigures={settings,dimensions,ticks,plot,orbitals,pngMetadata,tiff,savePlot,saveOrbitals,saveMolecule,renderMolecule,saveSVG,saveRaster,pngImage,legendLabel,setLegendLabel,init};
})(typeof window==='undefined'?globalThis:window);
