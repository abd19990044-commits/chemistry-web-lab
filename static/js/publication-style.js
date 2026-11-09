/* MIT. Print layouts use physical points and retain the supplied numerical data. */
(function(root){
  'use strict';
  const palettes={accessible:['#0072B2','#D55E00','#009E73','#CC79A7','#000000','#8C6500'],muted:['#254B70','#B95C32','#47745C','#80628A','#6A6257','#222222']};
  const fonts={arial:'Arial, Helvetica, sans-serif',helvetica:'Helvetica, Arial, sans-serif',times:'Times New Roman, Times, serif'};
  const dashes=['','5 2','2 2','6 2 1 2','1 2','8 3'];
  const finite=v=>typeof v==='number'&&Number.isFinite(v);
  const esc=v=>String(v??'').replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g,'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&apos;'}[c]));
  let serial=0;
  function style(s){
    const size=Number(s.fontSize??8),stroke=Number(s.lineWidth??1.1);
    if(!finite(size)||size<5||size>14||!finite(stroke)||stroke<.4||stroke>1.5)throw Error('Use 5-14 pt text and 0.4-1.5 pt curve widths.');
    return {size,axis:size+.5,stroke,font:fonts[s.fontFamily]||fonts.arial,palette:palettes[s.palette]||palettes.accessible,mono:s.monochrome||s.format==='tiff-line',legend:['top','bottom','right','none'].includes(s.legend)?s.legend:'bottom',frame:s.frame==='boxed'?'boxed':'open',grid:!!s.grid,peaks:!!s.peakLabels};
  }
  let measuringContext;
  function textWidth(value,size,font){
    if(measuringContext===undefined){
      try{measuringContext=root.document?.createElement('canvas').getContext('2d')||null;}catch{measuringContext=null;}
    }
    if(measuringContext){measuringContext.font=`${size}px ${font}`;return measuringContext.measureText(value).width;}
    return Array.from(value).reduce((w,c)=>w+(/[ilI1.,:'| ]/.test(c)?.32:/[MW@%]/.test(c)?.91:.62)*size,0);
  }
  function wrap(value,width,size,font){
    const rows=[];let row='';
    for(const word of String(value||'').trim().split(/\s+/)){
      if(row&&textWidth(`${row} ${word}`,size,font)>width){rows.push(row);row='';}
      if(textWidth(word,size,font)<=width){row=row?`${row} ${word}`:word;continue;}
      for(const character of word){
        if(row&&textWidth(row+character,size,font)>width){rows.push(row);row='';}
        row+=character;
      }
    }
    if(row)rows.push(row);
    return rows;
  }
  const text=(v,x,y,attrs='')=>`<text x="${x}" y="${y}" ${attrs}>${esc(v)}</text>`;
  const line=(x1,y1,x2,y2,attrs='')=>`<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" ${attrs}/>`;
  function number(v){if(Math.abs(v)<1e-12)return '0';return Math.abs(v)>=1e5||Math.abs(v)<1e-3?v.toExponential(1):String(Number(v.toPrecision(5)));}
  function tick(v,x,y,anchor,fs){
    const value=number(v),parts=value.split('e');
    return parts.length===1?text(value,x,y,`text-anchor="${anchor}"`):`<text x="${x}" y="${y}" text-anchor="${anchor}">${esc(parts[0])}×10<tspan baseline-shift="super" font-size="${fs*.75}">${Number(parts[1])}</tspan></text>`;
  }
  function start(s,title,meta){
    const {w,h}=root.PublicationFigures.dimensions(s,false),p=style(s);
    return `<svg xmlns="http://www.w3.org/2000/svg" width="${s.widthMm}mm" height="${s.heightMm}mm" viewBox="0 0 ${w} ${h}" role="img"><title>${esc(title)}</title><metadata>${esc(JSON.stringify({software:'Chemistry Web Lab',color_space:'sRGB',width_mm:s.widthMm,height_mm:s.heightMm,...meta,figure_style:{font:p.font,text_pt:p.size,curve_pt:p.stroke,palette:p.mono?'monochrome':s.palette||'accessible',legend:p.legend,frame:p.frame,grid:p.grid}}))}</metadata><rect width="${w}" height="${h}" fill="#ffffff"/><g font-family="${p.font}" font-size="${p.size}" fill="#16191c" stroke-linejoin="round" stroke-linecap="round">`;
  }
  function axisTitle(value,x,y,width,p,vertical=false){const rows=wrap(value,width,p.axis,p.font);if(rows.length>2)throw Error('Axis title needs more space. Shorten it, reduce text size or enlarge the figure.');if(vertical)x+=(vertical==='right'?-1:1)*(4+(rows.length-1)*(p.axis+2)/2);return `<g class="axis-title" transform="translate(${x} ${y})${vertical==='right'?' rotate(90)':vertical?' rotate(-90)':''}" font-size="${p.axis}">`+rows.map((v,i)=>text(v,0,(i-(rows.length-1)/2)*(p.axis+2),'text-anchor="middle"')).join('')+'</g>';}
  function peakCandidates(series,spec){
    const dips=spec.metadata?.y_mode==='transmittance'||/transmittance|%T/i.test(spec.yLabel),out=[];
    series.forEach((item,index)=>{const pts=item.points||[],candidates=[];let low=Infinity,high=-Infinity;for(const q of pts)if(finite(q.x)&&finite(q.y)&&q.x>=spec.xMin&&q.x<=spec.xMax){low=Math.min(low,q.y);high=Math.max(high,q.y);}for(let i=0;i<pts.length;i++){const q=pts[i];if(!finite(q.x)||!finite(q.y)||q.x<spec.xMin||q.x>spec.xMax)continue;
      if(item.sticks&&Math.abs(q.y-(item.baseline??0))<1e-12)continue;
      if(!item.sticks&&(dips?high-q.y:q.y-low)<(high-low)*.02)continue;
      const previous=pts[i-1],next=pts[i+1];
      if(item.sticks||(previous&&next&&finite(previous.y)&&finite(next.y)&&(dips?q.y<previous.y&&q.y<=next.y:q.y>previous.y&&q.y>=next.y)))candidates.push({x:q.x,y:q.y,index,axis:item.axis===2,strength:dips?-q.y:q.y});}
      candidates.sort((a,b)=>b.strength-a.strength);const picked=[];
      for(const q of candidates){if(picked.every(v=>Math.abs(v.x-q.x)>(spec.xMax-spec.xMin)*.005))picked.push(q);if(picked.length===3)break;}out.push(...picked);
    });return out;
  }
  function plot(spec,s){
    const P=root.PublicationFigures,{w,h}=P.dimensions(s,false),p=style(s),fs=p.size,series=spec.series||[];
    if(!series.length||!series.some(v=>(v.points||[]).some(q=>finite(q.x)&&finite(q.y))))throw Error('No finite spectrum data are available for export.');
    if(series.reduce((n,v)=>n+(v.points?.length||0),0)>500000)throw Error('Too many points for one figure. Export fewer visible layers.');
    const x0=spec.xMin,x1=spec.xMax,y0=spec.yMin??0,y1=spec.yMax,xt=P.ticks(x0,x1,w<350?4:6),yt=P.ticks(y0,y1,4),y2t=spec.y2Label?P.ticks(spec.y2Min??0,spec.y2Max,4):[];
    const titles=s.includeTitle?wrap(spec.title,w-35,fs+1,p.font):[],titleHeight=titles.length*(fs+4),legend=spec.showLegend===false?'none':p.legend;
    const tickWidth=values=>Math.max(0,...values.map(v=>textWidth(number(v).replace(/e\+?/, '×10'),fs,p.font)));
    const xEdge=Math.max(12,tickWidth(xt)/2+4);
    const left=Math.max(45,tickWidth(yt)+fs*3.9),axisRight=spec.y2Label?Math.max(48,tickWidth(y2t)+fs*3.9):xEdge;
    const legendWidth=legend==='right'?Math.min(w*.3,135):w-left-axisRight,columns=legend==='right'||w<350?1:2,cell=legendWidth/columns;
    const legendRows=[];let legendHeight=0;
    if(legend!=='none')for(let i=0;i<series.length;i+=columns){const row=series.slice(i,i+columns).map((item,j)=>({index:i+j,lines:wrap(item.displayName||item.name,cell-26,fs,p.font)}));const height=Math.max(...row.map(item=>item.lines.length))*(fs+2)+5;legendRows.push({items:row,offset:legendHeight});legendHeight+=height;}
    const top=16+titleHeight+(legend==='top'?legendHeight+5:0),bottom=fs*4.5+(legend==='bottom'?legendHeight+5:0),right=axisRight+(legend==='right'?legendWidth+10:0),pw=w-left-right,ph=h-top-bottom;
    if(pw<80||ph<60||(legend==='right'&&legendHeight>ph))throw Error('Labels need more space. Increase the figure dimensions, move the legend, or reduce visible layers.');
    const X=x=>left+(spec.reverseX?(x1-x):(x-x0))/(x1-x0)*pw,Y=(y,secondary=false)=>top+ph-(y-(secondary?(spec.y2Min??0):y0))/((secondary?spec.y2Max:y1)-(secondary?(spec.y2Min??0):y0))*ph;
    const clip='publication-plot-'+(++serial),color=i=>p.mono?'#111111':p.palette[i%p.palette.length],dash=i=>dashes[i%dashes.length];
    let svg=start(s,spec.title||'Scientific spectrum',{...spec.metadata,axes:{x:spec.xLabel,y:spec.yLabel,y2:spec.y2Label||null,x_range:[x0,x1],y_range:[y0,y1],y2_range:spec.y2Label?[spec.y2Min??0,spec.y2Max]:null,descending_x:!!spec.reverseX},source_layers:series.map(v=>v.name),display_layers:series.map(v=>v.displayName||v.name),peak_labels:p.peaks?'Strongest local extrema; numerical positions only, no chemical assignment':false});
    if(s.panel)svg+=text(s.panel,7,11,`font-size="${Math.max(8,fs)}" font-weight="bold"`);
    titles.forEach((v,i)=>{svg+=text(v,w/2,12+i*(fs+4),`font-size="${fs+1}" text-anchor="middle"`);});
    if(legend!=='none'){const lx=legend==='right'?w-legendWidth: left,ly=legend==='top'?16+titleHeight:legend==='bottom'?top+ph+fs*4.2:top+fs;
      for(const row of legendRows)for(let j=0;j<row.items.length;j++){const item=row.items[j],x=lx+j*cell,y=ly+row.offset;svg+=line(x,y-fs*.32,x+17,y-fs*.32,`stroke="${color(item.index)}" stroke-width="${p.stroke}" stroke-dasharray="${dash(item.index)}"`);item.lines.forEach((v,k)=>{svg+=text(v,x+23,y+k*(fs+2));});}}
    svg+=`<defs><clipPath id="${clip}"><rect x="${left}" y="${top}" width="${pw}" height="${ph}"/></clipPath></defs>`;
    if(p.grid){for(const y of yt)svg+=line(left,Y(y),left+pw,Y(y),'stroke="#e2e5e8" stroke-width="0.35"');}
    svg+=line(left,top,left,top+ph,'stroke="#16191c" stroke-width="0.65"')+line(left,top+ph,left+pw,top+ph,'stroke="#16191c" stroke-width="0.65"');
    if(p.frame==='boxed')svg+=line(left,top,left+pw,top,'stroke="#16191c" stroke-width="0.65"');
    if(p.frame==='boxed'||spec.y2Label)svg+=line(left+pw,top,left+pw,top+ph,'stroke="#16191c" stroke-width="0.65"');
    let tickRight=-Infinity;
    for(const x of xt.slice().sort((a,b)=>X(a)-X(b))){const px=X(x),half=textWidth(number(x),fs,p.font)/2;
      if(px-half<tickRight+5)continue;
      tickRight=px+half;
      svg+=line(px,top+ph-3,px,top+ph,'stroke="#16191c" stroke-width="0.65"')+tick(x,px,top+ph+fs+5,'middle',fs);if(p.frame==='boxed')svg+=line(px,top,px,top+3,'stroke="#16191c" stroke-width="0.65"');}
    for(const y of yt){const py=Y(y);svg+=line(left,py,left+3,py,'stroke="#16191c" stroke-width="0.65"')+tick(y,left-6,py+fs*.32,'end',fs);}
    const verticalTitleLength=ph+Math.min(top-3,fs+8)*2;
    svg+=axisTitle(spec.xLabel,left+pw/2,top+ph+fs*2.8,pw,p)+axisTitle(spec.yLabel,fs+1,top+ph/2,verticalTitleLength,p,true);
    if(spec.y2Label){for(const y of y2t){const py=Y(y,true);svg+=line(left+pw-3,py,left+pw,py,'stroke="#16191c" stroke-width="0.65"')+tick(y,left+pw+6,py+fs*.32,'start',fs);}svg+=axisTitle(spec.y2Label,w-(legend==='right'?legendWidth+10:0)-fs-1,top+ph/2,verticalTitleLength,p,'right');}
    svg+=`<g clip-path="url(#${clip})">`;
    series.forEach((item,index)=>{let path='',connected=false,single=null;const singles=[];for(const q of item.points||[]){if(!finite(q.x)||!finite(q.y)){if(single)singles.push(single);single=null;connected=false;continue;}const x=X(q.x),y=Y(q.y,item.axis===2);
      if(item.sticks){path+=`M${x.toFixed(3)},${Y(item.baseline??0,item.axis===2).toFixed(3)}L${x.toFixed(3)},${y.toFixed(3)}`;continue;}
      single=connected?null:{x,y};
      path+=`${connected?'L':'M'}${x.toFixed(3)},${y.toFixed(3)}`;connected=true;}
      if(single)singles.push(single);
      svg+=`<path d="${path}" class="data-series" data-axis="${item.axis===2?2:1}" fill="none" stroke="${color(index)}" stroke-width="${p.stroke}" stroke-dasharray="${dash(index)}"/>`;
      for(const {x,y} of singles)svg+=`<circle cx="${x}" cy="${y}" r="${p.stroke*1.3}" fill="${color(index)}"/>`;
    });svg+='</g>';
    if(p.peaks){const occupied=[],dip=spec.metadata?.y_mode==='transmittance'||/transmittance|%T/i.test(spec.yLabel);
      for(const q of peakCandidates(series,spec)){
        const x=X(q.x),y=Y(q.y,q.axis);if(y<top||y>top+ph)continue;
        const label=q.x.toFixed(spec.metadata?.spectrum==='NMR'?2:spec.metadata?.spectrum==='UV-Vis'?1:0),width=textWidth(label,fs,p.font);
        const offsets=[dip?fs+6:-6,dip?-6:fs+6],dx=width/2+8;
        const positions=[[x,y+offsets[0]],[x+dx,y+offsets[0]],[x-dx,y+offsets[0]],[x+dx,y+offsets[1]],[x-dx,y+offsets[1]]];
        const placement=positions.find(([u,v])=>u-width/2>=left+2&&u+width/2<=left+pw-2&&v>=top+fs&&v<=top+ph-3&&occupied.every(b=>Math.abs(b.x-u)>=(b.w+width)/2+4||Math.abs(b.y-v)>=fs*1.5));
        if(!placement)continue;
        const [tx,ty]=placement;occupied.push({x:tx,y:ty,w:width});
        svg+=line(x,y,tx,ty+(ty<y?2:-fs),'stroke="#555" stroke-width="0.45"')+text(label,tx,ty,'text-anchor="middle" paint-order="stroke fill" stroke="white" stroke-width="1.8"');
      }
    }
    return svg+'</g></svg>';
  }
  function orbitals(job,s){
    const P=root.PublicationFigures,{w,h}=P.dimensions(s,false),p=style(s),channels=[];
    for(const [name,homo,lumo]of [['α',job.alpha_homo_ev,job.alpha_lumo_ev],['β',job.beta_homo_ev,job.beta_lumo_ev]])if(finite(homo)&&finite(lumo))channels.push({name,homo,lumo});
    if(!channels.length&&finite(job.homo_ev)&&finite(job.lumo_ev))channels.push({name:'Frontier orbitals',homo:job.homo_ev,lumo:job.lumo_ev});
    if(!channels.length)throw Error('No complete HOMO/LUMO eigenvalue pair was reported.');
    const values=channels.flatMap(c=>[c.homo,c.lumo]),lo=Math.min(...values),hi=Math.max(...values),pad=Math.max((hi-lo)*.25,.75),min=lo-pad,max=hi+pad,left=48,top=28,bottom=43,pw=w-left-12,ph=h-top-bottom,Y=e=>top+ph-(e-min)/(max-min)*ph;
    let svg=start(s,'Frontier orbital eigenvalues',{kind:'orbital_eigenvalues',channels,method:job.method||null,basis_set:job.basis_set||null,energy_unit:'eV',note:'Orbital eigenvalue differences are not optical excitation energies.'});
    if(s.panel)svg+=text(s.panel,7,11,'font-size="8" font-weight="bold"');
    if(s.includeTitle)svg+=text('Frontier orbital eigenvalues',w/2,12,`font-size="${p.axis}" text-anchor="middle"`);
    svg+=line(left,top,left,top+ph,'stroke="#16191c" stroke-width="0.65"');
    for(const e of P.ticks(min,max,5)){const y=Y(e);svg+=line(left,y,left+3,y,'stroke="#16191c" stroke-width="0.65"')+tick(e,left-6,y+p.size*.32,'end',p.size);}
    svg+=axisTitle('Energy (eV)',p.size+2,top+ph/2,ph,p,true);
    channels.forEach((c,i)=>{const center=left+pw*(i+.5)/channels.length,bw=Math.min(70,pw/channels.length*.58),yh=Y(c.homo),yl=Y(c.lumo),color=p.mono?'#111111':p.palette[i%p.palette.length];
      svg+=line(center-bw/2,yh,center+bw/2,yh,`stroke="${color}" stroke-width="${p.stroke+ .25}"`)+line(center-bw/2,yl,center+bw/2,yl,`stroke="${color}" stroke-width="${p.stroke+ .25}" stroke-dasharray="4 2"`);
      svg+=text(`HOMO ${c.homo.toFixed(3)}`,center,yh+p.size+5,'text-anchor="middle"')+text(`LUMO ${c.lumo.toFixed(3)}`,center,yl-5,'text-anchor="middle"');
      if(Math.abs(yh-yl)>12)svg+=line(center,Math.min(yh,yl)+5,center,Math.max(yh,yl)-5,'stroke="#646b72" stroke-width="0.5" stroke-dasharray="2 2"');
      svg+=text(`Δε = ${(c.lumo-c.homo).toFixed(3)} eV`,center,h-20,'text-anchor="middle"')+text(c.name,center,h-7,'text-anchor="middle" font-weight="bold"');
    });return svg+'</g></svg>';
  }
  root.PublicationStyle={style,plot,orbitals,peakCandidates};
})(globalThis);
