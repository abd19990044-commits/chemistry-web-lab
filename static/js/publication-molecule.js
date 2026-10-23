/* MIT. Deterministic orthographic molecular figures from the source graph.
 * Coordinates and bond orders are read-only. No server, WebGL or inferred bonds.
 */
(function(root){
  'use strict';
  const colors={H:'#eeeeee',C:'#727780',N:'#3056c9',O:'#db302a',F:'#54b544',Cl:'#39a04a',Br:'#9c452a',I:'#744aab',S:'#e8bf32',P:'#e98328',B:'#d88b82',Si:'#bf926c',Fe:'#bc672c',Na:'#9871c2',Cu:'#b77b43',Zn:'#899bad'};
  const radii={H:1.2,C:1.7,N:1.55,O:1.52,F:1.47,Cl:1.75,Br:1.85,I:1.98,S:1.8,P:1.8};
  const rgb=c=>/^#[0-9a-f]{6}$/i.test(c)?[1,3,5].map(i=>parseInt(c.slice(i,i+2),16)):[255,255,255];
  function rotate(p,q){
    let [x,y,z,w]=q,n=Math.hypot(x,y,z,w)||1;x/=n;y/=n;z/=n;w/=n;
    const tx=2*(y*p.z-z*p.y),ty=2*(z*p.x-x*p.z),tz=2*(x*p.y-y*p.x);
    return {x:p.x+w*tx+y*tz-z*ty,y:p.y+w*ty+z*tx-x*tz,z:p.z+w*tz+x*ty-y*tx};
  }
  function scene(atoms,view,options={}){
    if(!atoms.length||atoms.length>2000||atoms.some(a=>![a.x,a.y,a.z].every(Number.isFinite)))throw Error('Use a finite geometry with 1-2000 atoms.');
    const o={atomScale:.24,bondRadius:.085,bondSpacing:3.2,style:'ballAndStick',...options};
    if(!(o.atomScale>=.12&&o.atomScale<=1&&o.bondRadius>=.025&&o.bondRadius<=.3&&o.bondSpacing>=2.4&&o.bondSpacing<=5))throw Error('Invalid molecular drawing sizes.');
    const points=atoms.map((a,i)=>({...rotate(a,view.slice(4,8)),elem:a.elem,id:a.id??i+1,r:(radii[a.elem]||1.7)*(o.style==='sphere'?.85:o.atomScale),color:colors[a.elem]||'#bb79ad'}));
    const primitives=[],bonds=[];
    atoms.forEach((a,i)=>(a.bonds||[]).forEach((j,k)=>{
      if(j<=i||!points[j])return;
      const order=a.bondOrder?.[k]??1;
      if(![1,1.5,2,3].includes(order))throw Error('Unsupported source bond order.');
      bonds.push({i,j,order});
      if(o.style==='sphere')return;
      const a1=points[i],a2=points[j],dx=a2.x-a1.x,dy=a2.y-a1.y,d=Math.hypot(dx,dy),nx=d>1e-8?-dy/d:1,ny=d>1e-8?dx/d:0;
      const count=order===1.5?2:order,r=o.style==='wireframe'?.027:o.bondRadius;
      for(let lane=0;lane<count;lane++){
        const offset=(lane-(count-1)/2)*r*o.bondSpacing;
        const start={x:a1.x+nx*offset,y:a1.y+ny*offset,z:a1.z},end={x:a2.x+nx*offset,y:a2.y+ny*offset,z:a2.z};
        // Aromatic input remains aromatic: one solid lane and one dashed lane.
        const parts=order===1.5&&lane===1?[[.12,.27],[.42,.57],[.72,.87]]:[[0,1]];
        for(const [lo,hi] of parts){const point=t=>({x:start.x+(end.x-start.x)*t,y:start.y+(end.y-start.y)*t,z:start.z+(end.z-start.z)*t});primitives.push({type:'cylinder',a:point(lo),b:point(hi),r,c1:rgb(a1.color),c2:rgb(a2.color),lo,hi,bond:[i,j],order});}
      }
    }));
    points.forEach((p,i)=>{if(o.style==='wireframe'&&atoms[i].bonds?.length)return;const r=o.style==='stick'?Math.max(o.bondRadius,.12):p.r;primitives.push({type:'sphere',...p,r,color:rgb(p.color),atom:i});});
    const bounds={minX:Infinity,maxX:-Infinity,minY:Infinity,maxY:-Infinity};
    for(const p of points){bounds.minX=Math.min(bounds.minX,p.x-p.r);bounds.maxX=Math.max(bounds.maxX,p.x+p.r);bounds.minY=Math.min(bounds.minY,p.y-p.r);bounds.maxY=Math.max(bounds.maxY,p.y+p.r);}
    return {points,primitives,bonds,bounds,options:o};
  }
  function frame(s,width,height,padding=.07){
    const b=s.bounds,scale=Math.min(width*(1-2*padding)/Math.max(.1,b.maxX-b.minX),height*(1-2*padding)/Math.max(.1,b.maxY-b.minY));
    return {scale,x:width/2-(b.minX+b.maxX)*scale/2,y:height/2+(b.minY+b.maxY)*scale/2};
  }
  // Analytic cylinder intersection for a parallel ray (x,y,z), looking along -Z.
  function cylinderHit(p,x,y){
    const vx=p.b.x-p.a.x,vy=p.b.y-p.a.y,vz=p.b.z-p.a.z,len=Math.hypot(vx,vy,vz);if(len<1e-10)return null;
    const dx=vx/len,dy=vy/len,dz=vz/len,ox=x-p.a.x,oy=y-p.a.y,oz=-p.a.z,od=ox*dx+oy*dy+oz*dz;
    const a=1-dz*dz,b=2*(oz-od*dz),c=ox*ox+oy*oy+oz*oz-od*od-p.r*p.r;
    let best=null;
    if(a>1e-10){const disc=b*b-4*a*c;if(disc>=0){const root=Math.sqrt(disc);for(const z of [(-b+root)/(2*a),(-b-root)/(2*a)]){const h=od+z*dz;if(h>=0&&h<=len&&(!best||z>best.z))best={z,nx:(ox-dx*h)/p.r,ny:(oy-dy*h)/p.r,nz:(z-p.a.z-dz*h)/p.r,t:h/len};}}}
    if(Math.abs(dz)>1e-10)for(const h of [0,len]){const z=(h-od)/dz,rx=ox-dx*h,ry=oy-dy*h,rz=z-p.a.z-dz*h;if(rx*rx+ry*ry+rz*rz<=p.r*p.r&&(!best||z>best.z)){const sign=h===0?-1:1;best={z,nx:sign*dx,ny:sign*dy,nz:sign*dz,t:h/len};}}
    return best;
  }
  async function raster(s,width,height,options={}){
    if(!Number.isInteger(width)||!Number.isInteger(height)||width<1||height<1||width*height>32000000)throw Error('Molecular raster exceeds the 32 megapixel limit.');
    const f=frame(s,width,height,options.padding??.075),data=new Uint8ClampedArray(width*height*4),depth=new Float32Array(width*height);depth.fill(-Infinity);
    const bg=rgb(options.background||'#ffffff'),transparent=options.background==='transparent';
    for(let i=0;i<data.length;i+=4){data[i]=bg[0];data[i+1]=bg[1];data[i+2]=bg[2];data[i+3]=transparent?0:255;}
    let work=0;
    const primitives=[...s.primitives].sort((a,b)=>(a.type==='sphere'?a.z:(a.a.z+a.b.z)/2)-(b.type==='sphere'?b.z:(b.a.z+b.b.z)/2));
    for(let n=0;n<primitives.length;n++){
      const p=primitives[n],sphere=p.type==='sphere';
      const ax=sphere?p.x:p.a.x,ay=sphere?p.y:p.a.y,bx=sphere?p.x:p.b.x,by=sphere?p.y:p.b.y;
      const x0=Math.max(0,Math.floor(f.x+(Math.min(ax,bx)-p.r)*f.scale-1)),x1=Math.min(width-1,Math.ceil(f.x+(Math.max(ax,bx)+p.r)*f.scale+1));
      const y0=Math.max(0,Math.floor(f.y-(Math.max(ay,by)+p.r)*f.scale-1)),y1=Math.min(height-1,Math.ceil(f.y-(Math.min(ay,by)-p.r)*f.scale+1));
      work+=(x1-x0+1)*(y1-y0+1);if(work>180000000)throw Error('This view is too complex at the selected size. Reduce the print size or resolution.');
      for(let py=y0;py<=y1;py++)for(let px=x0;px<=x1;px++){
        const x=(px+.5-f.x)/f.scale,y=(f.y-py-.5)/f.scale;let hit,color,coverage=1;
        if(sphere){const dx=x-p.x,dy=y-p.y,rr=p.r*p.r-dx*dx-dy*dy;if(rr<0)continue;const dz=Math.sqrt(rr);hit={z:p.z+dz,nx:dx/p.r,ny:dy/p.r,nz:dz/p.r};color=p.color;coverage=Math.min(1,(p.r-Math.hypot(dx,dy))*f.scale+.5);}
        else{hit=cylinderHit(p,x,y);if(!hit)continue;color=(p.lo+(p.hi-p.lo)*hit.t)<.5?p.c1:p.c2;}
        const index=py*width+px;if(hit.z<=depth[index])continue;depth[index]=hit.z;
        // Broad diffuse lighting, restrained highlight and a soft rim. No chrome.
        const diffuse=Math.max(0,-.38*hit.nx+.48*hit.ny+.79*hit.nz),shade=.46+.54*diffuse;
        const shine=5*Math.pow(Math.max(0,-.20*hit.nx+.25*hit.ny+.947*hit.nz),24),offset=index*4;
        const oldAlpha=data[offset+3]/255,newAlpha=coverage+oldAlpha*(1-coverage);
        for(let k=0;k<3;k++){const value=Math.min(255,color[k]*shade+shine);data[offset+k]=Math.round((value*coverage+data[offset+k]*oldAlpha*(1-coverage))/newAlpha);}
        data[offset+3]=Math.round(255*newAlpha);
      }
      if(options.yield&&n%6===5)await options.yield();
    }
    return {data,width,height,frame:f};
  }
  function layoutLabels(s,f,width,height,labels,fontSize,measure,placement='outside'){
    const boxes=[],margin=fontSize*.8;
    for(let i=0;i<s.points.length;i++){
      if(!labels[i])continue;
      const p=s.points[i],x=f.x+p.x*f.scale,y=f.y-p.y*f.scale,r=p.r*f.scale,w=measure(labels[i])+fontSize*.6,h=fontSize*1.3;
      const candidates=[];
      if(placement==='center')candidates.push({x:x-w/2,y:y-h/2});
      for(const distance of [1,1.7,2.5,3.5,5])for(let k=0;k<16;k++){const a=-Math.PI/2+k*Math.PI/8;candidates.push({x:x+Math.cos(a)*(r+fontSize*.65)*distance-w/2,y:y+Math.sin(a)*(r+fontSize*.65)*distance-h/2});}
      let best=null,bestCost=Infinity;
      for(const c of candidates){
        c.x=Math.max(margin,Math.min(width-margin-w,c.x));c.y=Math.max(margin,Math.min(height-margin-h,c.y));
        let cost=Math.hypot(c.x+w/2-x,c.y+h/2-y)*.01;
        for(const b of boxes){const overlap=Math.max(0,Math.min(c.x+w,b.x+b.w)-Math.max(c.x,b.x))*Math.max(0,Math.min(c.y+h,b.y+b.h)-Math.max(c.y,b.y));cost+=overlap*20;}
        if(placement!=='center')for(const a of s.points){const sx=f.x+a.x*f.scale,sy=f.y-a.y*f.scale,nearX=Math.max(c.x,Math.min(c.x+w,sx)),nearY=Math.max(c.y,Math.min(c.y+h,sy));cost+=Math.max(0,a.r*f.scale+fontSize*.2-Math.hypot(sx-nearX,sy-nearY))*fontSize*8;}
        if(cost<bestCost){bestCost=cost;best=c;}
      }
      boxes.push({...best,w,h,text:labels[i],anchor:{x,y,r},atom:i});
    }
    return boxes;
  }
  function drawLabels(ctx,boxes,options){
    const {fontSize,color='#111111',badge='transparent',bold=false,background='#ffffff'}=options;
    ctx.font=`${bold?'600':'400'} ${fontSize}px Arial, Helvetica, sans-serif`;ctx.textAlign='center';ctx.textBaseline='middle';
    for(const b of boxes){const cx=b.x+b.w/2,cy=b.y+b.h/2,dx=cx-b.anchor.x,dy=cy-b.anchor.y,d=Math.hypot(dx,dy);
      if(d>b.anchor.r+fontSize){ctx.strokeStyle=color;ctx.globalAlpha=.55;ctx.lineWidth=Math.max(1,fontSize*.045);ctx.beginPath();ctx.moveTo(b.anchor.x+dx/d*b.anchor.r,b.anchor.y+dy/d*b.anchor.r);ctx.lineTo(cx,cy);ctx.stroke();ctx.globalAlpha=1;}
      if(badge!=='transparent'){ctx.fillStyle=badge==='dark'?'#17202b':'#ffffff';ctx.fillRect(b.x,b.y,b.w,b.h);}
      if(badge==='transparent'){ctx.strokeStyle=background==='transparent'?'#ffffff':background;ctx.lineWidth=fontSize*.12;ctx.lineJoin='round';ctx.strokeText(b.text,cx,cy);}
      ctx.fillStyle=color;ctx.fillText(b.text,cx,cy);
    }
  }
  root.PublicationMolecule={scene,frame,raster,layoutLabels,drawLabels,cylinderHit};
})(typeof window==='undefined'?globalThis:window);
