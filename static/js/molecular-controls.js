/* MIT License. Molecular editing and view navigation use separate transactions. */
(function (root) {
  "use strict";
  const dot = (a, b) => a.x*b.x + a.y*b.y + a.z*b.z;
  const add = (a, b) => ({x:a.x+b.x, y:a.y+b.y, z:a.z+b.z});
  const scale = (a, k) => ({x:a.x*k, y:a.y*k, z:a.z*k});
  const normalize = q => {
    const n = Math.hypot(...q) || 1;
    return q.map(v => v/n);
  };
  function multiply(a, b) {
    return normalize([
      a[3]*b[0]+a[0]*b[3]+a[1]*b[2]-a[2]*b[1],
      a[3]*b[1]-a[0]*b[2]+a[1]*b[3]+a[2]*b[0],
      a[3]*b[2]+a[0]*b[1]-a[1]*b[0]+a[2]*b[3],
      a[3]*b[3]-a[0]*b[0]-a[1]*b[1]-a[2]*b[2]
    ]);
  }
  function rotation(axis, angle) {
    const n = Math.hypot(axis.x, axis.y, axis.z);
    if (!n) return [0,0,0,1];
    const k = Math.sin(angle/2)/n;
    return [axis.x*k, axis.y*k, axis.z*k, Math.cos(angle/2)];
  }
  function rotate(p, q) {
    const [x,y,z,w] = normalize(q);
    const tx=2*(y*p.z-z*p.y), ty=2*(z*p.x-x*p.z), tz=2*(x*p.y-y*p.x);
    return {x:p.x+w*tx+y*tz-z*ty, y:p.y+w*ty+z*tx-x*tz, z:p.z+w*tz+x*ty-y*tx};
  }
  function basis(view) {
    const q = normalize(view.slice(4,8));
    const inverse = [-q[0],-q[1],-q[2],q[3]];
    return {right:rotate({x:1,y:0,z:0},inverse), up:rotate({x:0,y:1,z:0},inverse), normal:rotate({x:0,y:0,z:1},inverse)};
  }
  // The projection is sampled at the picked atom's depth. No hard-coded Å/pixel
  // factor, viewport origin, or assumed camera orientation enters a drag.
  function screenDelta(viewer, point, dx, dy, options = {}) {
    const b = basis(viewer.getView()), origin = viewer.modelToScreen(point);
    const pr = viewer.modelToScreen(add(point,b.right)), pu = viewer.modelToScreen(add(point,b.up));
    const ax=pr.x-origin.x, ay=pr.y-origin.y, bx=pu.x-origin.x, by=pu.y-origin.y;
    const det=ax*by-ay*bx;
    if (![ax,ay,bx,by,det].every(Number.isFinite) || Math.abs(det)<1e-8) return {x:0,y:0,z:0};
    const precision = options.shiftKey ? 0.2 : 1;
    dx *= precision; dy *= precision;
    const axis = options.axis;
    if (axis && axis !== 'free') {
      const unit = {x:0,y:0,z:0}; unit[axis] = 1;
      const p = viewer.modelToScreen(add(point,unit));
      const sx=p.x-origin.x, sy=p.y-origin.y, length2=sx*sx+sy*sy;
      // An axis aimed into the screen is controlled with vertical motion.
      const amount = length2 > 4 ? (dx*sx+dy*sy)/length2 : -dy/Math.hypot(bx,by);
      return scale(unit,amount);
    }
    if (options.altKey) return scale(b.normal,-dy/Math.hypot(bx,by));
    return add(scale(b.right,(dx*by-dy*bx)/det),scale(b.up,(dy*ax-dx*ay)/det));
  }
  function centroid(atoms) {
    if (!atoms.length) return {x:0,y:0,z:0};
    return scale(atoms.reduce((s,a)=>add(s,a),{x:0,y:0,z:0}),1/atoms.length);
  }
  function applyTransform(atoms, originals, ids, delta, quaternion, center) {
    atoms.forEach(a => {
      if (!ids.has(a.id)) return;
      const p = originals.get(a.id);
      let next = add(p,delta);
      if (quaternion) next = add(center,rotate({x:p.x-center.x,y:p.y-center.y,z:p.z-center.z},quaternion));
      if ([next.x,next.y,next.z].every(Number.isFinite)) Object.assign(a,next);
    });
  }
  function createController(options) {
    const {element, getViewer, getState, pick, select, click, redraw, begin, commit, cancel, status} = options;
    const events = new AbortController();
    const listen = (target,type,fn,extra={}) => target.addEventListener(type,fn,{...extra,signal:events.signal});
    let gesture=null, frame=0, disposed=false;
    const touches = new Map();
    let pinch=null;
    const changed = () => options.viewChanged?.();
    function schedule() {
      if (!frame) frame=requestAnimationFrame(()=>{
        frame=0;
        if (disposed) return;
        redraw(true);
        changed();
      });
    }
    function finish(rollback=false) {
      const g=gesture;
      gesture=null;
      if (frame) { cancelAnimationFrame(frame); frame=0; }
      if (!g) return;
      if (g.edit && g.moved) {
        if (rollback) {
          applyTransform(getState().atoms,g.originals,g.ids,{x:0,y:0,z:0});
          cancel?.();
        } else commit?.();
        redraw(false);
      }
      try { if (element.hasPointerCapture(g.pointerId)) element.releasePointerCapture(g.pointerId); } catch (_) {}
      element.classList.remove('is-dragging');
      status?.(rollback ? 'Move cancelled · original coordinates restored' : '');
    }
    function down(e) {
      if (disposed || !getViewer() || e.button>2) return;
      e.preventDefault(); e.stopPropagation();
      element.focus({preventScroll:true});
      try { element.setPointerCapture(e.pointerId); } catch (_) {}
      if (e.pointerType==='touch') {
        touches.set(e.pointerId,{x:e.clientX,y:e.clientY});
        if (touches.size>1) { finish(true); pinch=null; return; }
      }
      const state=getState(), hit=pick(e.clientX,e.clientY);
      const tool=state.activeTool;
      const cameraPan=e.button===1 || e.button===2 || (e.shiftKey && !['move','rotate','moveAtom'].includes(tool));
      const edit=!cameraPan && hit && ['move','rotate','moveAtom'].includes(tool);
      if (hit && e.button===0) select(hit);
      const targets=edit ? state.atoms.filter(a=>tool==='moveAtom' ? a.id===hit.id : a.molId===hit.molId) : [];
      gesture={pointerId:e.pointerId, x:e.clientX,y:e.clientY, lastX:e.clientX,lastY:e.clientY,
        hit, tool, edit, moved:false, action:cameraPan?'pan':(edit?tool:'orbit'),
        ids:new Set(targets.map(a=>a.id)), originals:new Map(targets.map(a=>[a.id,{x:a.x,y:a.y,z:a.z}])),
        center:centroid(targets), point:hit?{x:hit.x,y:hit.y,z:hit.z}:null,
        view:getViewer().getView().slice(), delta:{x:0,y:0,z:0}, quaternion:[0,0,0,1], axis:state.transformAxis||'free'};
    }
    function move(e) {
      if (touches.has(e.pointerId)) touches.set(e.pointerId,{x:e.clientX,y:e.clientY});
      if (touches.size>1) {
        const [a,b]=[...touches.values()], center={x:(a.x+b.x)/2,y:(a.y+b.y)/2}, distance=Math.hypot(a.x-b.x,a.y-b.y);
        if (pinch && distance>4 && pinch.distance>4) {
          getViewer().zoom(distance/pinch.distance);
          getViewer().translate(center.x-pinch.x,center.y-pinch.y);
          changed();
        }
        pinch={...center,distance}; e.preventDefault(); return;
      }
      const g=gesture;
      if (!g || g.pointerId!==e.pointerId) {
        if (!gesture && e.pointerType!=='touch') {
          const hit=pick(e.clientX,e.clientY), tool=getState().activeTool;
          element.style.cursor=hit && ['move','moveAtom','rotate'].includes(tool)?'grab':(['bond','measure'].includes(tool)?'crosshair':(tool==='navigate'?'grab':'default'));
          options.hover?.(hit);
        }
        return;
      }
      e.preventDefault();
      if (!g.moved && Math.hypot(e.clientX-g.x,e.clientY-g.y)<4) return;
      if (!g.moved) { g.moved=true; if (g.edit) begin?.(); element.classList.add('is-dragging'); }
      const dx=e.clientX-g.lastX, dy=e.clientY-g.lastY;
      g.lastX=e.clientX; g.lastY=e.clientY;
      const viewer=getViewer();
      if (!g.edit) {
        if (g.action==='pan') viewer.translate(dx,dy);
        else {
          const turn=rotation({x:dy,y:dx,z:0},Math.hypot(dx,dy)*0.008);
          const view=viewer.getView().slice();
          view.splice(4,4,...multiply(turn,view.slice(4,8))); viewer.setView(view);
        }
        changed(); return;
      }
      const fine=e.shiftKey?0.2:1;
      if (g.action==='rotate') {
        const b=basis(g.view);
        const axis=g.axis==='free'?add(scale(b.up,dx),scale(b.right,dy)):{x:g.axis==='x'?1:0,y:g.axis==='y'?1:0,z:g.axis==='z'?1:0};
        const angle=(g.axis==='free'?Math.hypot(dx,dy):dx-dy)*0.009*fine;
        g.quaternion=multiply(rotation(axis,angle),g.quaternion);
        applyTransform(getState().atoms,g.originals,g.ids,g.delta,g.quaternion,g.center);
        status?.('Rotating active molecule · other molecules fixed · Esc cancels');
      } else {
        g.delta=add(g.delta,screenDelta(viewer,g.point,dx,dy,{shiftKey:e.shiftKey,altKey:e.altKey,axis:g.axis}));
        applyTransform(getState().atoms,g.originals,g.ids,g.delta);
        status?.(`Displacement ${Math.hypot(g.delta.x,g.delta.y,g.delta.z).toFixed(3)} Å · Esc cancels`);
      }
      schedule();
    }
    function up(e) {
      if (touches.has(e.pointerId)) touches.delete(e.pointerId);
      if (pinch) { pinch=null; finish(); return; }
      const g=gesture;
      if (!g || e.pointerId!==g.pointerId) return;
      if (!g.moved && e.button===0) {
        if (g.hit) click(g.hit);
        else options.clearSelection?.();
      }
      finish();
    }
    function wheel(e) {
      const viewer=getViewer(); if (!viewer) return;
      e.preventDefault(); e.stopPropagation();
      // Wheel always controls the camera. Geometry depth is an explicit Alt-drag.
      if (gesture?.edit) return;
      const unit=e.deltaMode===1?16:(e.deltaMode===2?element.clientHeight:1);
      const delta=Math.max(-160,Math.min(160,e.deltaY*unit));
      viewer.zoom(Math.exp(-delta*0.002)); changed();
    }
    element.style.touchAction='none';
    listen(element,'pointerdown',down,{capture:true});
    listen(element,'pointermove',move);
    listen(element,'pointerup',up);
    listen(element,'pointercancel',()=>{touches.clear();pinch=null;finish(true);});
    listen(element,'lostpointercapture',e=>{ if(gesture?.pointerId===e.pointerId) finish(true); });
    listen(element,'wheel',wheel,{passive:false,capture:true});
    listen(element,'contextmenu',e=>e.preventDefault());
    listen(element,'dblclick',e=>{e.preventDefault();finish();options.fit?.();});
    listen(window,'blur',()=>{touches.clear();pinch=null;finish(true);});
    return {finish, destroy() {finish(true);disposed=true;events.abort();touches.clear();}, isDragging:()=>Boolean(gesture?.moved)};
  }

  // Canvas painter fallback keeps real XYZ coordinates and the same interaction
  // contract on machines/browsers without a WebGL context.
  function createSoftwareViewer(element) {
    const canvas=document.createElement('canvas');
    const context=canvas.getContext('2d');
    if (!context) throw new Error('A canvas context is required for molecular rendering');
    canvas.style.cssText='display:block;width:100%;height:100%;';
    canvas.setAttribute('aria-label','Interactive three-dimensional molecular model');
    let container=element, models=[], labels=[], shapes=[], background='#090e17', alpha=1, exportScale=1, view=[0,0,0,6,0,0,0,1], orthographic=false, width=1,height=1, pan={x:0,y:0};
    const colors={H:'#edf2f7',C:'#687b91',N:'#6875ed',O:'#ef505a',S:'#f6cf44',P:'#f49a46',F:'#65d780',Cl:'#50d879',Br:'#c66b4c',I:'#a77ade',Na:'#a68be6',Fe:'#d78b56',Cu:'#c69260',Zn:'#a4b7c9'};
    const radii={H:1.2,C:1.7,N:1.55,O:1.52,S:1.8,P:1.8,F:1.47,Cl:1.75,Br:1.85,I:1.98};
    const matches=(a,sel)=>Object.entries(sel||{}).every(([k,v])=>Array.isArray(v)?v.includes(a[k]):a[k]===v);
    const all=()=>models.flatMap(m=>m.atoms);
    function project(atom) {
      const r=rotate({x:atom.x+view[0],y:atom.y+view[1],z:atom.z+view[2]},view.slice(4,8));
      const depth=view[3]-r.z, ratio=height*1.2/(orthographic?view[3]:Math.max(.25,depth));
      return {x:width/2+pan.x+r.x*ratio,y:height/2+pan.y-r.y*ratio,z:r.z,depth,ratio};
    }
    function atomRadius(a,p) {
      return a.style?.sphere ? Math.max(2,(a.style.sphere.scale||0.27)*(radii[a.elem]||1.7)*p.ratio) : Math.max(4,(a.style?.stick?.radius||0.06)*p.ratio);
    }
    function paint() {
      const dpr=Math.min(2,window.devicePixelRatio||1)*exportScale;
      context.setTransform(dpr,0,0,dpr,0,0);
      context.clearRect(0,0,width,height);if(alpha){context.fillStyle=background;context.fillRect(0,0,width,height);}
      const entries=[];
      models.forEach(model=>{
        const projected=model.atoms.map(project);
        model.atoms.forEach((a,i)=>{
          const p=projected[i];
          if (p.depth<=.25) return;
          if (a.style?.sphere || !a.bonds.length) entries.push({type:'atom',a,p,z:p.z});
          (a.bonds||[]).forEach((j,k)=>{
            if(j<=i || !projected[j] || projected[j].depth<=.25) return;
            const b=model.atoms[j], end=projected[j], style=a.style||{};
            if (!style.stick && !style.line) return;
            entries.push({type:'bond',a,b,p,end,z:(p.z+end.z)/2,order:a.bondOrder?.[k]||1});
          });
        });
      });
      entries.sort((a,b)=>a.z-b.z);
      for(const item of entries) {
        const {a,p}=item;
        if(item.type==='bond') {
          const {b,end}=item, dx=end.x-p.x,dy=end.y-p.y,length=Math.hypot(dx,dy)||1;
          const radius=a.style.stick?Math.max(1,a.style.stick.radius*(p.ratio+end.ratio)/2):1;
          const c1=a.style.stick?.color||colors[a.elem]||'#a1aab8', c2=b.style.stick?.color||colors[b.elem]||'#a1aab8';
          const gradient=context.createLinearGradient(p.x,p.y,end.x,end.y);
          gradient.addColorStop(0,c1);gradient.addColorStop(.48,c1);gradient.addColorStop(.52,c2);gradient.addColorStop(1,c2);
          context.strokeStyle=gradient;context.lineWidth=radius*2;context.lineCap='round';
          const count=Math.max(1,Math.min(3,item.order));
          for(let j=0;j<count;j++) {
            const offset=(j-(count-1)/2)*radius*2.5;
            context.beginPath();context.moveTo(p.x-dy/length*offset,p.y+dx/length*offset);
            context.lineTo(end.x-dy/length*offset,end.y+dx/length*offset);context.stroke();
          }
        } else {
          const radius=atomRadius(a,p), color=a.style.sphere?.color||colors[a.elem]||'#bd86d0';
          const gradient=context.createRadialGradient(p.x-radius*.3,p.y-radius*.35,radius*.06,p.x,p.y,radius);
          gradient.addColorStop(0,'#ffffff');gradient.addColorStop(.25,color);gradient.addColorStop(1,'#182333');
          context.beginPath();context.arc(p.x,p.y,radius,0,Math.PI*2);context.fillStyle=gradient;context.fill();
          context.strokeStyle='rgba(226,232,240,0.25)';context.lineWidth=1;context.stroke();
        }
      }
      shapes.forEach(shape=>{
        const a=project(shape.start),b=project(shape.end);if(a.depth<=.25||b.depth<=.25)return;
        context.strokeStyle=shape.color||'#f6c85f';context.fillStyle=context.strokeStyle;context.lineWidth=shape.linewidth||3;
        context.setLineDash(shape.dashed?[6,4]:[]);context.beginPath();context.moveTo(a.x,a.y);context.lineTo(b.x,b.y);context.stroke();context.setLineDash([]);
        if(shape.arrow){const angle=Math.atan2(b.y-a.y,b.x-a.x),size=12;context.beginPath();context.moveTo(b.x,b.y);context.lineTo(b.x-size*Math.cos(angle-.4),b.y-size*Math.sin(angle-.4));context.lineTo(b.x-size*Math.cos(angle+.4),b.y-size*Math.sin(angle+.4));context.closePath();context.fill();}
      });
      context.textAlign='center';context.textBaseline='middle';
      labels.forEach(label=>{
        const p=project(label.spec.position), text=label.text;
        const spec=label.spec,size=spec.fontSize||12;context.font=`${spec.bold===false?'400':'600'} ${size}px sans-serif`;
        const box=context.measureText(text).width+12;
        if(spec.showBackground!==false){context.fillStyle=spec.backgroundColor||'rgba(15,23,42,.88)';context.fillRect(p.x-box/2,p.y-size*.75,box,size*1.5);}
        context.fillStyle=spec.fontColor||'#f8fafc';context.fillText(text,p.x,p.y);
      });
      return viewer;
    }
    const viewer={
      rendererType:'canvas',
      clear(){models=[];labels=[];shapes=[];return viewer;},
      getModel:()=>models[models.length-1],
      removeAllSurfaces(){return viewer;},
      removeAllShapes(){shapes=[];return viewer;},
      addLine(spec){shapes.push({...spec});return viewer;},
      addArrow(spec){shapes.push({...spec,arrow:true});return viewer;},
      setBackgroundColor(color,opacity=1){background=typeof color==='number'?'#'+color.toString(16).padStart(6,'0'):color;alpha=opacity;return viewer;},
      pngURI(scale=2){try{exportScale=scale;viewer.resize();paint();return canvas.toDataURL('image/png');}finally{exportScale=1;viewer.resize();paint();}},
      getRenderer:()=>({domElement:canvas}),
      setContainer(el){container=el;container.appendChild(canvas);viewer.resize();return viewer;},
      resize(){const rect=container.getBoundingClientRect();width=Math.max(1,rect.width);height=Math.max(1,rect.height);const dpr=Math.min(2,window.devicePixelRatio||1)*exportScale;canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);return viewer;},
      getView:()=>view.slice(),
      setView(v){view=v.slice();return paint();},
      modelToScreen(a){const p=project(a),rect=container.getBoundingClientRect();return{x:rect.left+window.scrollX+p.x,y:rect.top+window.scrollY+p.y};},
      translate(dx,dy){pan.x+=dx;pan.y+=dy;return paint();},
      zoom(factor){view[3]=Math.max(.5,Math.min(1e5,view[3]/factor));return paint();},
      zoomTo(){const atoms=all(),c=centroid(atoms);view[0]=-c.x;view[1]=-c.y;view[2]=-c.z;view[3]=Math.max(5,...atoms.map(a=>Math.hypot(a.x-c.x,a.y-c.y,a.z-c.z)*3));pan={x:0,y:0};return paint();},
      setProjection(proj){orthographic=proj==='orthographic';return paint();},
      render:paint,
      removeAllLabels(){labels=[];return viewer;},
      addLabel(text,spec){labels.push({text,spec});return viewer;},
      setStyle(sel,spec){models.forEach(m=>m.setStyle(sel,spec,false));return viewer;},
      addStyle(sel,spec){models.forEach(m=>m.setStyle(sel,spec,true));return viewer;},
      targetedObjects(x,y){
        const sx=(x+1)*width/2,sy=(1-y)*height/2;
        return all().map(a=>({a,p:project(a)})).filter(({a,p})=>p.depth>.25 && Math.hypot(p.x-sx,p.y-sy)<atomRadius(a,p)+5)
          .sort((a,b)=>b.p.z-a.p.z).map(({a,p})=>({clickable:a,distance:p.depth}));
      },
      addModel(xyz){
        const atoms=xyz.trim().split('\n').slice(2).filter(Boolean).map((line,index)=>{
          const [elem,x,y,z]=line.trim().split(/\s+/);
          return {elem,x:Number(x),y:Number(y),z:Number(z),index,id:index+1,bonds:[],bondOrder:[],style:{}};
        });
        const model={atoms,selectedAtoms:(sel)=>atoms.filter(a=>matches(a,sel)),
          setClickable(sel,value){atoms.filter(a=>matches(a,sel)).forEach(a=>a.clickable=value);},
          setStyle(sel,spec,append){atoms.filter(a=>matches(a,sel)).forEach(a=>{if(!append)a.style={};Object.entries(spec).forEach(([key,value])=>a.style[key]={...(a.style[key]||{}),...value});});}
        };
        models.push(model);return model;
      }
    };
    viewer.setContainer(element);return viewer;
  }

  function pngBlob(viewer, viewport, scale=3) {
    const rect=viewport.getBoundingClientRect();
    if (!rect.width || !rect.height) throw Error('Open the molecular viewer before exporting.');
    const canvas=viewer.getRenderer()?.domElement;
    const pixelRatio=Math.max(1,(canvas?.width||rect.width)/rect.width);
    const factor=Math.min(scale,4096/(Math.max(rect.width,rect.height)*pixelRatio));
    const originalView=viewer.getView().slice();
    let uri;
    try {
      if (viewer.rendererType==='canvas') uri=viewer.pngURI(factor);
      else {
        viewer.setWidth(Math.max(1,Math.round(rect.width*factor)));
        viewer.setHeight(Math.max(1,Math.round(rect.height*factor)));
        viewer.render();
        uri=viewer.pngURI();
      }
    } finally {
      viewer.resize();viewer.setView(originalView);viewer.render();
    }
    if (!uri?.startsWith('data:image/png;base64,')) throw Error('The renderer returned an invalid PNG.');
    const bytes=Uint8Array.from(atob(uri.split(',')[1]),c=>c.charCodeAt(0));
    return new Blob([bytes],{type:'image/png'});
  }

  root.MolecularControls={pngBlob,createSoftwareViewer,basis,screenDelta,centroid,rotate,rotation,multiply,applyTransform,createController,dot};
})(typeof window==='undefined'?globalThis:window);
