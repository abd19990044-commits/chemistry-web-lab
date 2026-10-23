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
      dpi:Number(el('figure-dpi')?.value||600),format:el('figure-format')?.value||'png',
      monochrome:el('figure-monochrome')?.checked||false,includeTitle:el('figure-title')?.checked||false,
      panel:el('figure-panel')?.value||'',...overrides};
  }
  function dimensions(s,raster=true){
    if(!finite(s.widthMm)||!finite(s.heightMm)||s.widthMm<50||s.widthMm>190||s.heightMm<40||s.heightMm>240)throw Error('Choose a width of 50-190 mm and a height of 40-240 mm.');
    if(![300,600,1200].includes(s.dpi))throw Error('Choose 300, 600 or 1200 dpi.');
    const width=Math.round(s.widthMm*s.dpi/25.4),height=Math.round(s.heightMm*s.dpi/25.4);
    if(raster&&(width>10000||height>10000||width*height>32000000))throw Error('This size exceeds the 32 megapixel export limit. Use 600 dpi, a smaller size, or SVG for plots.');
    return {width,height,w:s.widthMm*72/25.4,h:s.heightMm*72/25.4};
  }
  function ticks(lo,hi,count=5){
    if(!finite(lo)||!finite(hi)||hi<=lo)throw Error('The axis limits must be finite and increasing.');
    const raw=(hi-lo)/count,power=10**Math.floor(Math.log10(raw)),step=([1,2,2.5,5,10].find(n=>n*power>=raw)||10)*power;
    const values=[];for(let i=Math.ceil(lo/step);i*step<=hi+step*1e-8&&values.length<30;i++)values.push(i*step);
    return values;
  }
  function number(v){
    if(v===0)return '0';
    if(Math.abs(v)<.001||Math.abs(v)>=100000)return v.toExponential(1);
    return Number(v.toPrecision(5)).toString();
  }
  function wrap(text,width,size=8){
    const limit=Math.max(8,Math.floor(width/(size*.56))),lines=[];
    let remaining=String(text||'');
    while(remaining.length>limit){let cut=remaining.lastIndexOf(' ',limit);if(cut<limit/2)cut=limit;lines.push(remaining.slice(0,cut));remaining=remaining.slice(cut).trimStart();}
    lines.push(remaining);return lines;
  }
  function text(value,x,y,extra=''){return `<text x="${x}" y="${y}" ${extra}>${esc(value)}</text>`;}
  function line(x1,y1,x2,y2,extra=''){return `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" ${extra}/>`;}
  function start(s,title,metadata){
    const {w,h}=dimensions(s,false);
    return `<svg xmlns="http://www.w3.org/2000/svg" width="${s.widthMm}mm" height="${s.heightMm}mm" viewBox="0 0 ${w} ${h}" role="img"><title>${esc(title)}</title><metadata>${esc(JSON.stringify({software:'Chemistry Web Lab',color_space:'sRGB',width_mm:s.widthMm,height_mm:s.heightMm,...metadata}))}</metadata><rect width="${w}" height="${h}" fill="#fff"/><g font-family="Arial, Helvetica, sans-serif" font-size="8" fill="#111" stroke-linejoin="round" stroke-linecap="round">`;
  }
  function plot(spec,s=settings()){
    const {w,h}=dimensions(s,false),series=spec.series||[];
    if(!series.length||!series.some(v=>(v.points||[]).some(p=>finite(p.x)&&finite(p.y))))throw Error('No finite spectrum data are available for export.');
    if(series.reduce((n,v)=>n+(v.points?.length||0),0)>500000)throw Error('Too many points for one figure. Export fewer visible layers.');
    const x0=spec.xMin,x1=spec.xMax,y0=spec.yMin??0,y1=spec.yMax;
    ticks(x0,x1);ticks(y0,y1);if(spec.y2Label)ticks(spec.y2Min??0,spec.y2Max);
    const left=52,right=spec.y2Label?52:15,plotW=w-left-right;
    const columns=w>350?2:1,cellW=(w-left-right)/columns;
    const titleLines=s.includeTitle?wrap(spec.title,w-40,9):[];
    let cursor=titleLines.length?titleLines.length*11+18:16,legend='';
    if(spec.showLegend!==false){
      for(let i=0;i<series.length;i+=columns){
        let rowHeight=0;
        for(let j=0;j<columns&&i+j<series.length;j++){
          const index=i+j,item=series[index],color=s.monochrome?'#111':palette[index%palette.length],dash=dashes[index%dashes.length];
          const lines=wrap(item.name,cellW-24),x=left+j*cellW;
          legend+=line(x,cursor-3,x+16,cursor-3,`stroke="${color}" stroke-width="1" stroke-dasharray="${dash}"`);
          lines.forEach((v,k)=>{legend+=text(v,x+21,cursor+k*10);});rowHeight=Math.max(rowHeight,lines.length*10+5);
        }
        cursor+=rowHeight;
      }
    }
    const top=cursor+8,bottom=42,plotH=h-top-bottom;
    if(plotH<65||plotW<100)throw Error('Labels need more space. Increase the figure height or width, or reduce the visible layers.');
    const mapX=x=>left+(spec.reverseX?(x1-x):(x-x0))/(x1-x0)*plotW;
    const mapY=(y,secondary=false)=>top+plotH-(y-(secondary?(spec.y2Min??0):y0))/((secondary?spec.y2Max:y1)-(secondary?(spec.y2Min??0):y0))*plotH;
    let svg=start(s,spec.title||'Scientific spectrum',spec.metadata||{});
    if(s.panel)svg+=text(s.panel,8,12,'font-weight="bold" font-size="9"');
    titleLines.forEach((v,i)=>{svg+=text(v,w/2,12+i*11,'text-anchor="middle" font-size="9"');});
    svg+=legend+`<defs><clipPath id="plot-area"><rect x="${left}" y="${top}" width="${plotW}" height="${plotH}"/></clipPath></defs>`;
    svg+=`<rect x="${left}" y="${top}" width="${plotW}" height="${plotH}" fill="none" stroke="#111" stroke-width="0.6"/>`;
    for(const x of ticks(x0,x1,w<350?4:6)){const px=mapX(x);svg+=line(px,top+plotH,px,top+plotH+3,'stroke="#111" stroke-width="0.6"')+text(number(x),px,top+plotH+13,'text-anchor="middle"');}
    for(const y of ticks(y0,y1,4)){const py=mapY(y);svg+=line(left-3,py,left,py,'stroke="#111" stroke-width="0.6"')+text(number(y),left-6,py+2.5,'text-anchor="end"');}
    svg+=text(spec.xLabel,left+plotW/2,h-10,'text-anchor="middle" font-size="9"');
    svg+=text(spec.yLabel,0,0,`transform="translate(12 ${top+plotH/2}) rotate(-90)" text-anchor="middle" font-size="9"`);
    if(spec.y2Label){
      for(const y of ticks(spec.y2Min??0,spec.y2Max,4)){const py=mapY(y,true);svg+=line(w-right,py,w-right+3,py,'stroke="#111" stroke-width="0.6"')+text(number(y),w-right+6,py+2.5);}
      svg+=text(spec.y2Label,0,0,`transform="translate(${w-10} ${top+plotH/2}) rotate(-90)" text-anchor="middle" font-size="9"`);
    }
    svg+='<g clip-path="url(#plot-area)">';
    series.forEach((item,index)=>{
      const color=s.monochrome?'#111':palette[index%palette.length],dash=dashes[index%dashes.length];let path='',connected=false;
      for(const p of item.points){
        if(!finite(p.x)||!finite(p.y)){connected=false;continue;}
        const x=mapX(p.x),y=mapY(p.y,item.axis===2);
        if(item.sticks){path+=`M${x.toFixed(3)},${mapY(item.baseline??0,item.axis===2).toFixed(3)}L${x.toFixed(3)},${y.toFixed(3)}`;}
        else{path+=`${connected?'L':'M'}${x.toFixed(3)},${y.toFixed(3)}`;connected=true;}
      }
      svg+=`<path d="${path}" fill="none" stroke="${color}" stroke-width="0.9" ${item.sticks?'':`stroke-dasharray="${dash}"`}/>`;
    });
    return svg+'</g></g></svg>';
  }
  function orbitals(job,s=settings()){
    const {w,h}=dimensions(s,false),channels=[];
    for(const [name,homo,lumo]of [['α',job.alpha_homo_ev,job.alpha_lumo_ev],['β',job.beta_homo_ev,job.beta_lumo_ev]])if(finite(homo)&&finite(lumo))channels.push({name,homo,lumo});
    if(!channels.length&&finite(job.homo_ev)&&finite(job.lumo_ev))channels.push({name:'Frontier orbitals',homo:job.homo_ev,lumo:job.lumo_ev});
    if(!channels.length)throw Error('No complete HOMO/LUMO eigenvalue pair was reported.');
    const values=channels.flatMap(c=>[c.homo,c.lumo]),lo=Math.min(...values),hi=Math.max(...values),pad=Math.max((hi-lo)*.2,.5),min=lo-pad,max=hi+pad;
    const left=48,top=28,bottom=34,pw=w-left-15,ph=h-top-bottom,mapY=e=>top+ph-(e-min)/(max-min)*ph;
    let svg=start(s,'Frontier orbital eigenvalues',{kind:'orbital_eigenvalues',channels,method:job.method||null,basis_set:job.basis_set||null,energy_unit:'eV',note:'Orbital eigenvalue differences are not optical excitation energies.'});
    if(s.panel)svg+=text(s.panel,8,12,'font-weight="bold" font-size="9"');
    if(s.includeTitle)svg+=text('Frontier orbital eigenvalues',w/2,12,'text-anchor="middle" font-size="9"');
    svg+=line(left,top,left,top+ph,'stroke="#111" stroke-width="0.6"');
    for(const e of ticks(min,max,5)){const y=mapY(e);svg+=line(left-3,y,left,y,'stroke="#111" stroke-width="0.6"')+text(number(e),left-6,y+2.5,'text-anchor="end"');}
    svg+=text('Energy (eV)',0,0,`transform="translate(12 ${top+ph/2}) rotate(-90)" text-anchor="middle" font-size="9"`);
    channels.forEach((c,i)=>{
      const center=left+pw*(i+.5)/channels.length,bw=Math.min(75,pw/channels.length*.65),yh=mapY(c.homo),yl=mapY(c.lumo),color=s.monochrome?'#111':palette[i%palette.length];
      svg+=line(center-bw/2,yh,center+bw/2,yh,`stroke="${color}" stroke-width="1.5"`)+line(center-bw/2,yl,center+bw/2,yl,`stroke="${color}" stroke-width="1.5" stroke-dasharray="4 2"`);
      svg+=text(`HOMO ${c.homo.toFixed(3)}`,center,yh+12,'text-anchor="middle"')+text(`LUMO ${c.lumo.toFixed(3)}`,center,yl-5,'text-anchor="middle"');
      const gap=c.lumo-c.homo;
      svg+=text(`Δε = ${gap.toFixed(3)} eV`,center,h-18,'text-anchor="middle"')+text(c.name,center,h-5,'text-anchor="middle" font-weight="bold"');
    });
    return svg+'</g></svg>';
  }
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
  function tiff(rgba,width,height,dpi,metadata={}){
    if(!Number.isInteger(width)||!Number.isInteger(height)||width<1||height<1||width*height>32000000||rgba.length!==width*height*4)throw Error('Invalid TIFF raster dimensions.');
    const ascii=JSON.stringify({color_space:'sRGB',...metadata}).replace(/[\u007f-\uffff]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));
    const description=new TextEncoder().encode(ascii+'\0'),software=new TextEncoder().encode('Chemistry Web Lab\0'),align=n=>(n+1)&~1;
    const entries=15,ifdEnd=8+2+entries*12+4,bits=ifdEnd,xres=bits+6,yres=xres+8,desc=yres+8,soft=align(desc+description.length),pixels=align(soft+software.length);
    const out=new Uint8Array(pixels+width*height*3),v=new DataView(out.buffer);
    out[0]=out[1]=73;v.setUint16(2,42,true);v.setUint32(4,8,true);v.setUint16(8,entries,true);
    const tags=[[256,4,1,width],[257,4,1,height],[258,3,3,bits],[259,3,1,1],[262,3,1,2],[270,2,description.length,desc],[273,4,1,pixels],[277,3,1,3],[278,4,1,height],[279,4,1,width*height*3],[282,5,1,xres],[283,5,1,yres],[284,3,1,1],[296,3,1,2],[305,2,software.length,soft]];
    tags.forEach(([tag,type,count,value],i)=>{const p=10+i*12;v.setUint16(p,tag,true);v.setUint16(p+2,type,true);v.setUint32(p+4,count,true);if(type===3&&count===1)v.setUint16(p+8,value,true);else v.setUint32(p+8,value,true);});
    for(let i=0;i<3;i++)v.setUint16(bits+i*2,8,true);
    for(const p of [xres,yres]){v.setUint32(p,dpi,true);v.setUint32(p+4,1,true);}
    out.set(description,desc);out.set(software,soft);
    for(let i=0,j=pixels;i<rgba.length;i+=4){const alpha=rgba[i+3]/255;for(let c=0;c<3;c++)out[j++]=Math.round(rgba[i+c]*alpha+255*(1-alpha));}
    return out;
  }
  async function imageOf(blob){
    const url=URL.createObjectURL(blob);try{return await new Promise((resolve,reject)=>{const img=new Image();img.onload=()=>resolve(img);img.onerror=()=>reject(Error('Could not render the figure.'));img.src=url;});}finally{URL.revokeObjectURL(url);}
  }
  function download(blob,name){const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  async function saveRaster(canvas,s,name,metadata){
    const meta={...metadata,dpi:s.dpi,width_mm:s.widthMm,height_mm:s.heightMm,pixel_width:canvas.width,pixel_height:canvas.height,color_space:'sRGB'};
    if(s.format==='tiff')download(new Blob([tiff(canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data,canvas.width,canvas.height,s.dpi,meta)],{type:'image/tiff'}),name+'.tiff');
    else{const blob=await new Promise((resolve,reject)=>canvas.toBlob(b=>b?resolve(b):reject(Error('PNG encoding failed.')),'image/png'));download(new Blob([pngMetadata(new Uint8Array(await blob.arrayBuffer()),s.dpi,meta)],{type:'image/png'}),name+'.png');}
  }
  function safeName(value){return String(value||'figure').replace(/[^\p{L}\p{N}._-]+/gu,'_').slice(0,100);}
  async function saveSVG(svg,name,s,metadata){
    if(s.format==='svg'){download(new Blob([svg],{type:'image/svg+xml;charset=utf-8'}),safeName(name)+'.svg');return;}
    const {width,height}=dimensions(s),canvas=document.createElement('canvas');canvas.width=width;canvas.height=height;
    const ctx=canvas.getContext('2d');ctx.fillStyle='#fff';ctx.fillRect(0,0,width,height);
    ctx.drawImage(await imageOf(new Blob([svg],{type:'image/svg+xml;charset=utf-8'})),0,0,width,height);
    await saveRaster(canvas,s,safeName(name),metadata);
    canvas.width=canvas.height=1;
  }
  async function exclusive(action){if(busy)throw Error('A figure export is already being prepared.');busy=true;try{return await action();}finally{busy=false;}}
  function savePlot(spec,name){return exclusive(()=>{const s=settings();return saveSVG(plot(spec,s),name,s,{kind:'spectrum',x_label:spec.xLabel,y_label:spec.yLabel,x_min:spec.xMin,x_max:spec.xMax,reverse_x:!!spec.reverseX,y_min:spec.yMin??0,y_max:spec.yMax,y2_label:spec.y2Label||null,layers:spec.series.map(v=>v.name),...spec.metadata});});}
  function saveOrbitals(job,name){return exclusive(()=>{const s=settings();return saveSVG(orbitals(job,s),name,s,{kind:'frontier_orbital_eigenvalues',job});});}
  async function renderMolecule(viewer,options={},preview=false){
    if(!root.PublicationMolecule)throw Error('Reload the page to load the molecular figure renderer.');
    const s=settings(),{width,height}=dimensions(s),atoms=viewer.getModel()?.selectedAtoms({});
    if(!atoms?.length)throw Error('Open a molecular geometry before exporting.');
    const value=(id,fallback)=>el(id)?.value||fallback;
    const selectedBackground=value('engine-3d-bg','#ffffff'),background=selectedBackground==='custom'?value('engine-3d-bg-color','#ffffff'):selectedBackground,labelSize=Number(value('engine-3d-label-size',8)),labelColor=value('engine-3d-label-color','#111111');
    if(!finite(labelSize)||labelSize<5||labelSize>20)throw Error('Choose a label size of 5-20 pt.');
    const drawing={style:value('engine-3d-style','ballAndStick'),atomScale:Number(value('engine-3d-atom-size',.24)),bondRadius:Number(value('engine-3d-bond-radius',.085)),bondSpacing:Number(value('engine-3d-bond-spacing',3.2))};
    const scene=root.PublicationMolecule.scene(atoms,viewer.getView().slice(),drawing);
    const scale=preview?Math.min(1,1100/width,800/height):1,w=Math.round(width*scale),h=Math.round(height*scale);
    const raster=await root.PublicationMolecule.raster(scene,w,h,{background,padding:options.labels?.some(Boolean)?.13:.075,yield:()=>new Promise(resolve=>setTimeout(resolve,0))});
    const canvas=document.createElement('canvas');canvas.width=w;canvas.height=h;const ctx=canvas.getContext('2d');
    const image=ctx.createImageData(w,h);image.data.set(raster.data);ctx.putImageData(image,0,0);
    const fontSize=labelSize*s.dpi/72*scale,bold=el('engine-3d-label-bold')?.classList.contains('btn-primary')||false;
    ctx.font=`${bold?'600':'400'} ${fontSize}px Arial, Helvetica, sans-serif`;
    const boxes=root.PublicationMolecule.layoutLabels(scene,raster.frame,w,h,options.labels||[],fontSize,text=>ctx.measureText(text).width,value('engine-3d-label-layout','outside'));
    root.PublicationMolecule.drawLabels(ctx,boxes,{fontSize,color:labelColor,badge:value('engine-3d-label-badge','transparent'),bold,background});
    if(s.panel){ctx.textAlign='left';ctx.textBaseline='alphabetic';ctx.font=`bold ${9*s.dpi/72*scale}px Arial`;ctx.fillStyle=labelColor;ctx.fillText(s.panel,8*s.dpi/72*scale,12*s.dpi/72*scale);}
    return {canvas,settings:s,metadata:{kind:'molecular_geometry',...options.metadata,renderer:'analytic spheres and cylinders; depth buffered; diffuse lighting',projection:'orthographic; current orientation; automatic framing',coordinates:'unchanged',source_bonds:scene.bonds.map(b=>({atom_1:b.i+1,atom_2:b.j+1,order:b.order})),drawing,background,label_size_pt:labelSize,label_color:labelColor,label_layout:value('engine-3d-label-layout','outside')}};
  }
  function saveMolecule(viewer,viewport,options={}){return exclusive(async()=>{
    if(settings().format==='svg')throw Error('Choose PNG or TIFF for 3D geometry. SVG is available for plots.');
    const result=await renderMolecule(viewer,options);
    try{await saveRaster(result.canvas,result.settings,'molecular-geometry-publication',result.metadata);}finally{result.canvas.width=result.canvas.height=1;}
  });}
  function init(){
    el('figure-preset')?.addEventListener('change',e=>{const [w,h]=e.target.value.split(',');el('figure-width-mm').value=w;el('figure-height-mm').value=h;});
  }
  root.PublicationFigures={settings,dimensions,ticks,plot,orbitals,pngMetadata,tiff,savePlot,saveOrbitals,saveMolecule,renderMolecule,init};
})(typeof window==='undefined'?globalThis:window);
