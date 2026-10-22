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
  root.MolecularControls={basis,screenDelta,centroid,rotate,rotation,multiply,applyTransform,createController,dot};
})(typeof window==='undefined'?globalThis:window);
