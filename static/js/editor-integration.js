/* Local editor integration: PubChem 2D, real functional groups, PNG and Word. */
(function(root){
  'use strict';
  const group={ket_version:'2.0.0',header:{moleculeName:'COOH'},root:{nodes:[{$ref:'mol0'}]},mol0:{type:'molecule',atoms:[{label:'C',location:[0,0,0]},{label:'O',location:[.75,1.299,0]},{label:'O',location:[.75,-1.299,0]}],bonds:[{type:2,atoms:[0,1]},{type:1,atoms:[0,2]}],sgroups:[{type:'SUP',name:'COOH',atoms:[0,1,2],expanded:false,attachmentPoints:[{attachmentAtom:0,attachmentId:'1'}]}]}};
  const groupText=JSON.stringify(group),cache=new Map(),pending=new Map();
  function installGroups(storage){
    const key='ketcher-tmpls',raw=storage.getItem(key),templates=raw?JSON.parse(raw):[];
    if(!Array.isArray(templates))throw Error('The editor template library is not a valid list.');
    const exists=templates.some(t=>{try{return JSON.parse(t.struct).header?.moleculeName==='COOH';}catch(_){return false;}});
    if(!exists){templates.push({struct:groupText,props:{group:'User Templates',orcaBuiltIn:'carboxyl-v1'}});storage.setItem(key,JSON.stringify(templates));}
    return !exists;
  }
  const groups={COOH:'C(=O)O',CO2H:'C(=O)O',CH2OH:'CO',CHO:'C=O',CN:'C#N',NH2:'N',OH:'O',SH:'S',NO2:'[N+](=O)[O-]',Me:'C',CH3:'C',Et:'CC',C2H5:'CC',Ph:'c1ccccc1',C6H5:'c1ccccc1',OMe:'OC',OCH3:'OC',NMe2:'N(C)C',NHMe:'NC',iPr:'C(C)C',tBu:'C(C)(C)C',Ac:'C(=O)C',OAc:'OC(=O)C',Boc:'C(=O)OC(C)(C)C',COOMe:'C(=O)OC',COOCH3:'C(=O)OC',COOEt:'C(=O)OCC',CF3:'C(F)(F)F',COCl:'C(=O)Cl',SO3H:'S(=O)(=O)O',SO2NH2:'S(=O)(=O)N',CH2CH2OH:'CCO','COO-':'C(=O)[O-]'};
  const elements=new Set('H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og'.split(' '));
  function labelText(value){const label=String(value||'').replace(/[\r\n\x00-\x1f]/g,' ').trim();if(!label||label.length>80)throw Error('Enter a label of 1 to 80 characters.');return label;}
  function definition(label,smiles=''){
    label=labelText(label);smiles=String(smiles||'').trim();if(smiles.length>4096)throw Error('Custom group SMILES limit: 4096 characters.');
    const key=Object.keys(groups).find(key=>key.toLowerCase()===label.toLowerCase());
    const atom=elements.has(label)?(['B','C','N','O','P','S','F','Cl','Br','I'].includes(label)?label:'['+label+']'):'';
    return {label,smiles:smiles||atom||(key?groups[key]:''),defined:!!(smiles||atom||key)};
  }
  function makeGroup(label,input,defined=true){
    label=labelText(label);
    const ket=defined?(typeof input==='string'?JSON.parse(input):structuredClone(input)):{ket_version:'2.0.0',root:{nodes:[{$ref:'mol0'}]},mol0:{type:'molecule',atoms:[{label:'A',location:[0,0,0]}],bonds:[]}};
    const refs=ket.root?.nodes?.filter(n=>n.$ref&&ket[n.$ref]?.type==='molecule');
    if(refs?.length!==1||ket.root.nodes.some(n=>n.type==='arrow'))throw Error('Define a group with one connected molecular structure.');
    const mol=ket[refs[0].$ref],atoms=mol.atoms||[],bonds=mol.bonds||[];
    if(!atoms.length||atoms.length>100)throw Error('A custom group must contain 1 to 100 atoms.');
    if(defined&&atoms.some(a=>['A','*','R','R#'].includes(a.label)||a.type==='atom-list'||a.type==='rg-label'))throw Error('Use concrete atoms in group SMILES; the first atom is the attachment atom.');
    const connected=new Set([0]);let changed=true;while(changed){changed=false;for(const b of bonds)if(connected.has(b.atoms[0])!==connected.has(b.atoms[1])){connected.add(b.atoms[0]);connected.add(b.atoms[1]);changed=true;}}
    if(connected.size!==atoms.length)throw Error('Use one connected group.');
    ket.header={...(ket.header||{}),moleculeName:label};
    mol.sgroups=[...(mol.sgroups||[]),{type:'SUP',name:label,atoms:atoms.map((_,i)=>i),expanded:false,attachmentPoints:[{attachmentAtom:0,attachmentId:'1'}],...(defined?{}:{class:'ORCA_UNDEFINED'})}];
    return ket;
  }
  function registerGroup(storage,ket,defined){
    const key='ketcher-tmpls',raw=storage.getItem(key),templates=raw?JSON.parse(raw):[];
    if(!Array.isArray(templates))throw Error('Invalid template library.');
    const name=ket.header.moleculeName;
    // Preserve user templates. Register new names for the native keyboard
    // lookup on the next editor load; insertion below does not need a reload.
    if(!templates.some(t=>{try{return JSON.parse(t.struct).header?.moleculeName===name;}catch(_){return false;}})){
      templates.push({struct:JSON.stringify(ket),props:{group:'User Templates',orcaDefined:defined}});
      const text=JSON.stringify(templates);if(text.length>2*1024*1024)throw Error('Template library storage limit reached.');storage.setItem(key,text);
    }
  }
  function hasUndefined(input){const ket=typeof input==='string'?JSON.parse(input):input;return Object.values(ket).some(node=>node&&typeof node==='object'&&node.sgroups?.some(g=>g.class==='ORCA_UNDEFINED'));}
  async function pubchem(query,{signal}={}){
    query=String(query||'').trim();if(!query||query.length>256)throw Error('Enter a compound name or PubChem CID (up to 256 characters).');
    const namespace=/^\d+$/.test(query)?'cid':'name',key=namespace+':'+query.toLowerCase(),old=cache.get(key);
    if(old&&Date.now()-old.at<900000)return {...old.value};
    if(pending.has(key))return {...await pending.get(key)};
    const task=(async()=>{
      const response=await fetch('https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/'+namespace+'/'+encodeURIComponent(query)+'/SDF?record_type=2d',{signal:signal?AbortSignal.any([signal,AbortSignal.timeout(20000)]):AbortSignal.timeout(20000)});
      if(!response.ok)throw Error(response.status===404?'PubChem could not find this compound. Try its exact name or CID.':response.status===429?'PubChem is busy. Wait briefly and retry.':'PubChem request failed (HTTP '+response.status+').');
      const text=await response.text();if(text.length>5*1024*1024)throw Error('PubChem structure exceeds the 5 MB editor limit.');
      const records=text.split(/^\$\$\$\$\s*$/m).filter(record=>record.trim());
      if(records.length!==1)throw Error('PubChem returned multiple structures. Enter a specific CID.');
      if(!/M  END/.test(text)||!/(?:V2000|V3000)/.test(text))throw Error('PubChem did not return a valid molecular structure.');
      const cid=text.match(/>[^\n]*<PUBCHEM_COMPOUND_CID>[^\n]*\n\s*(\d+)/)?.[1]||(/^[0-9]+$/.test(query)?query:'');
      const value={structure:text,title:query,cid,source:'PubChem 2D'};cache.set(key,{at:Date.now(),value});while(cache.size>20)cache.delete(cache.keys().next().value);return value;
    })();pending.set(key,task);
    try{return {...await task};}finally{if(pending.get(key)===task)pending.delete(key);}
  }
  function maxImageBudget(memory=Number(root.navigator?.deviceMemory||0)){
    if(memory>0&&memory<=2)return {longEdge:6000,pixels:16000000};
    if(memory>0&&memory<=4)return {longEdge:8192,pixels:32000000};
    if(memory>=8)return {longEdge:12000,pixels:64000000};
    return {longEdge:10000,pixels:48000000};
  }
  function imageSize(width,height,longEdge=12000){
    if(![width,height].every(v=>Number.isFinite(v)&&v>0))throw Error('Invalid drawing dimensions.');
    if(![2048,4096,6000,12000].includes(longEdge))throw Error('Choose a supported image quality.');
    const budget=longEdge===12000?maxImageBudget():{longEdge,pixels:24000000};
    let scale=budget.longEdge/Math.max(width,height);scale=Math.min(scale,Math.sqrt(budget.pixels/(width*height)));
    return {width:Math.max(1,Math.floor(width*scale)),height:Math.max(1,Math.floor(height*scale))};
  }
  function escapeHTML(text){return String(text).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
  function wordHTML(dataURL,width,height,title='Chemical drawing'){
    const displayWidth=Math.min(640,Math.round(480*width/height)),displayHeight=Math.round(displayWidth*height/width);
    return '<div><img src="'+dataURL+'" width="'+displayWidth+'" height="'+displayHeight+'" alt="'+escapeHTML(title)+'" /></div>';
  }
  async function rasterize(svg,{longEdge=12000,transparent=false,dpi=600,title='Chemical drawing'}={}){
    // Native Ketcher returns a Blob from its iframe, with a different prototype.
    if(!svg||typeof svg.arrayBuffer!=='function'||!Number.isFinite(svg.size)||svg.size<1||svg.size>10*1024*1024)throw Error('SVG drawing exceeds the image export limit.');
    const bytesSVG=await svg.arrayBuffer();if(bytesSVG.byteLength>10*1024*1024)throw Error('SVG drawing exceeds the image export limit.');
    const url=URL.createObjectURL(new Blob([bytesSVG],{type:'image/svg+xml'}));let image;
    try{image=await new Promise((resolve,reject)=>{const img=new Image();img.onload=()=>resolve(img);img.onerror=()=>reject(Error('Could not render the drawing image.'));img.src=url;});}
    finally{URL.revokeObjectURL(url);}
    const requested=imageSize(image.naturalWidth,image.naturalHeight,longEdge);let size,raw,lastError;
    // Some browsers impose a lower canvas limit than their device memory hints.
    for(const pixelLimit of [Infinity,16000000,4000000]){
      if(pixelLimit!==Infinity&&requested.width*requested.height<=pixelLimit)continue;
      const scale=Math.min(1,Math.sqrt(pixelLimit/(requested.width*requested.height))),canvas=document.createElement('canvas');
      size={width:Math.max(1,Math.floor(requested.width*scale)),height:Math.max(1,Math.floor(requested.height*scale))};canvas.width=size.width;canvas.height=size.height;
      try{
        const context=canvas.getContext('2d');if(!context)throw Error('Canvas export is unavailable.');
        if(!transparent){context.fillStyle='#fff';context.fillRect(0,0,size.width,size.height);}context.drawImage(image,0,0,size.width,size.height);
        raw=await new Promise((resolve,reject)=>canvas.toBlob(blob=>blob?resolve(blob):reject(Error('PNG encoding failed.')),'image/png'));break;
      }catch(error){lastError=error;}finally{canvas.width=canvas.height=1;}
    }
    if(!raw)throw lastError||Error('PNG encoding failed. Choose a smaller image size.');
    const bytes=root.PublicationFigures.pngMetadata(new Uint8Array(await raw.arrayBuffer()),dpi,{kind:'editable_2d_drawing',title,pixel_width:size.width,pixel_height:size.height,background:transparent?'transparent':'white',coordinates:'2D artwork; no 3D coordinates changed'});
    return {blob:new Blob([bytes],{type:'image/png'}),...size,dpi};
  }
  function dataURL(blob){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(Error('Could not prepare the clipboard image.'));reader.readAsDataURL(blob);});}
  function copyForWord(figurePromise,title='Chemical drawing'){
    if(!root.ClipboardItem||!navigator.clipboard?.write)throw Error('Image clipboard is unavailable. Save PNG and insert it into Word.');
    // Start write in the click handler. Promise-backed representations preserve
    // user activation while the local SVG/PNG is prepared (including Safari).
    const html=figurePromise.then(async figure=>new Blob([wordHTML(await dataURL(figure.blob),figure.width,figure.height,title)],{type:'text/html'}));
    const png=figurePromise.then(f=>f.blob);png.catch(()=>{});html.catch(()=>{});
    const item=new ClipboardItem({'image/png':png,'text/html':html,'text/plain':new Blob([title],{type:'text/plain'})});
    return navigator.clipboard.write([item]);
  }
  root.EditorIntegration={group:()=>structuredClone(group),groupText,installGroups,definition,makeGroup,registerGroup,hasUndefined,pubchem,maxImageBudget,imageSize,rasterize,wordHTML,copyForWord};
})(globalThis);
