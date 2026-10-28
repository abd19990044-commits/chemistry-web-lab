/* Preserve chemical graphs while adding editable Ketcher KET annotations. */
(function(root){
  'use strict';
  function decorateReaction(input,options={}){
    const ket=typeof input==='string'?JSON.parse(input):structuredClone(input);
    const nodes=ket.root?.nodes;
    if(!Array.isArray(nodes))throw Error('Invalid editable reaction drawing.');
    const arrow=nodes.find(n=>n.type==='arrow'&&Array.isArray(n.data?.pos)&&n.data.pos.length===2);
    if(!arrow)throw Error('No reaction arrow was found in the imported RXN.');
    const [a,b]=arrow.data.pos;
    if(![a.x,a.y,b.x,b.y].every(Number.isFinite))throw Error('Invalid reaction arrow coordinates.');
    arrow.data.mode=options.arrow_style==='equilibrium'?'equilibrium-filled-half-bow':options.arrow_style==='reversible'?'both-ends-filled-triangle':'filled-triangle';
    const cx=(a.x+b.x)/2,cy=(a.y+b.y)/2;
    function addText(value,above){
      const text=String(value||'').trim().slice(0,240);if(!text)return;
      const lines=[];let line='';
      // Bound caption width rather than running text through adjacent species.
      for(const word of text.split(/\s+/)){
        if(line&&line.length+word.length+1>32){lines.push(line);line='';}
        if(word.length>32){if(line){lines.push(line);line='';}for(let i=0;i<word.length;i+=32)lines.push(word.slice(i,i+32));}
        else line+=(line?' ':'')+word;
      }
      if(line)lines.push(line);
      const width=Math.max(.4,...lines.map(l=>l.length*.19)),height=lines.length*.5;
      nodes.push({type:'text',boundingBox:{x:cx-width/2,y:above?cy+1.1+height:cy-1.1,width,height},paragraphs:lines.map(text=>({parts:[{text,font:{size:15}}]}))});
    }
    addText(options.arrow_top,true);addText(options.arrow_bottom,false);
    return ket;
  }
  root.ChemicalDrawing={decorateReaction};
})(globalThis);
