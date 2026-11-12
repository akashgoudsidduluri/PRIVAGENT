import type { SemanticElement } from './contracts';

const CONTROL_SELECTOR = 'button,input,textarea,select,a[href],[role],[tabindex],[contenteditable="true"]';
const clip = (s: string | null | undefined, limit=120) => (s || '').replace(/\s+/g,' ').trim().slice(0,limit);

/** Accessible-name precedence for controls, including open shadow roots. */
export function accessibleName(el: Element): string {
 const root = el.getRootNode() as Document | ShadowRoot;
 const ids = (el.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean);
 const referenced = ids.map(id=>root.getElementById(id)?.textContent||'').join(' ');
 const labels = Array.from((el as HTMLInputElement).labels||[]).map(l=>l.textContent||'').join(' ');
 const wrapping = el.closest('label')?.textContent;
 return clip(referenced)||clip(el.getAttribute('aria-label'))||clip(labels)||clip(wrapping)||
        clip(el.getAttribute('placeholder'))||clip(el.getAttribute('title'))||clip(el.textContent);
}

/** Same-origin frames and open shadow roots only. Cross-origin frames are opaque. */
export function queryAllWithShadow(selector: string, root: Document | ShadowRoot = document): Element[] {
 const out = Array.from(root.querySelectorAll(selector));
 for(const el of root.querySelectorAll('*')) {
   if(el.shadowRoot) out.push(...queryAllWithShadow(selector,el.shadowRoot));
   if(el instanceof HTMLIFrameElement) {
     try { if(el.contentDocument) out.push(...queryAllWithShadow(selector,el.contentDocument)); } catch { /* cross-origin */ }
   }
 }
 return out;
}

export function perceptible(el: Element): boolean {
 let node: Element | null = el;
 while(node) {
   const s=getComputedStyle(node);
   if(s.display==='none'||s.visibility==='hidden'||Number(s.opacity)===0||s.fontSize==='0px'||Number.parseInt(s.zIndex,10)<=-999||node.getAttribute('aria-hidden')==='true'||(node as HTMLInputElement).type==='hidden')return false;
   const root: Node=node.getRootNode();
   node=node.parentElement||(root instanceof ShadowRoot?root.host:null);
 }
 return true;
}

/** Map a nested frame's viewport coordinates into the top document. */
export function viewportBounds(el: Element): {x:number;y:number;width:number;height:number}|null {
 const r=el.getBoundingClientRect();let x=r.left,y=r.top;
 let win=el.ownerDocument.defaultView;
 try { while(win && win!==win.top) {const frame=win.frameElement; if(!frame)return null;const fr=frame.getBoundingClientRect();x+=fr.left;y+=fr.top;win=frame.ownerDocument.defaultView;} }catch{return null;}
 return {x:Math.round(x),y:Math.round(y),width:Math.round(r.width),height:Math.round(r.height)};
}

export function isOccluded(el: Element): boolean {
 const r=el.getBoundingClientRect();
 if(r.width<=0||r.height<=0)return true;
 const doc=el.ownerDocument;
 const x=Math.max(0,Math.min(doc.defaultView?.innerWidth||0,r.left+r.width/2));
 const y=Math.max(0,Math.min(doc.defaultView?.innerHeight||0,r.top+r.height/2));
 if(!doc.elementFromPoint || !x || !y)return false; // jsdom / no hit-test
 const hit=doc.elementFromPoint(x,y);
 if(hit && hit!==el && !el.contains(hit) && !hit.contains(el))return true;
 try {const win=doc.defaultView; const frame=win && win!==win.top?win.frameElement:null;if(frame)return isOccluded(frame);}catch{return true;}
 return false;
}

export function semanticRole(el: Element): string {
 const role=el.getAttribute('role');if(role)return role;
 switch(el.tagName.toLowerCase()) {
 case 'a':return el.hasAttribute('href')?'link':'generic';
 case 'button':return 'button';case 'select':return 'combobox';case 'textarea':return 'textbox';
 case 'input':{const type=(el as HTMLInputElement).type;return ({search:'searchbox',checkbox:'checkbox',radio:'radio',file:'button',button:'button',submit:'button'} as Record<string,string>)[type]||'textbox';}
 default:return el.hasAttribute('contenteditable')?'textbox':'generic';
 }
}
export function controlType(el: Element): SemanticElement['controlType'] {
 const role=semanticRole(el);const type=(el as HTMLInputElement).type;
 if(type==='file')return 'file_uploader';if(type==='date'||type==='datetime-local'||el.getAttribute('aria-haspopup')==='dialog'&&/date|calendar/i.test(accessibleName(el)))return 'calendar_picker';
 if(role==='switch')return 'toggle_switch';if(role==='checkbox')return 'checkbox';if(role==='radio')return 'radio';if(role==='dialog')return 'modal';
 if(role==='combobox'&&el.tagName!=='SELECT')return 'custom_dropdown';if(role==='searchbox')return 'search_input';return 'generic';
}
export function contextText(el: Element): string {
 const b=el.getBoundingClientRect();const parent=el.parentElement;
 if(!parent)return '';
 return clip(Array.from(parent.children).filter(n=>n!==el&&perceptible(n)&&Math.abs(n.getBoundingClientRect().top-b.top)<=50).map(n=>n.textContent||'').join(' '),160);
}
export function semanticElement(el: Element, elementId: string): SemanticElement | null {
 const bounds=viewportBounds(el);if(!bounds)return null;
 const visible=perceptible(el)&&bounds.width>0&&bounds.height>0;
 const style=getComputedStyle(el);
 return {elementId,role:semanticRole(el),semanticName:accessibleName(el),controlType:controlType(el),
  interactability:{bounds,visible,opacity:Number(style.opacity),pointerEvents:style.pointerEvents,occluded:visible&&isOccluded(el)},contextText:contextText(el)};
}

/** Detached/replaced React nodes are re-resolved only when the match is unique. */
export function checkTargetFreshness(targetId:string, expectedBounds?:{x:number;y:number;width:number;height:number}, semanticHint?:string, root:Document|ShadowRoot=document):Element|null {
 const candidates=queryAllWithShadow(CONTROL_SELECTOR,root).filter(el=>perceptible(el));
 const same=(el:Element)=>!semanticHint||accessibleName(el).toLowerCase()===semanticHint.toLowerCase();
 const live=candidates.find(el=>(el.getAttribute('data-navigator-id')===targetId||el.id===targetId)&&same(el));
 if(live)return live;
 if(!semanticHint)return null;
 const matches=candidates.filter(el=>same(el));
 if(matches.length===1)return matches[0];
 if(!expectedBounds)return null;
 const distance=(el:Element)=>{const b=viewportBounds(el);return b?Math.hypot(b.x-expectedBounds.x,b.y-expectedBounds.y):Infinity;};
 const nearby=matches.filter(el=>distance(el)<=48).sort((a,b)=>distance(a)-distance(b));
 return nearby.length===1||nearby.length>1&&distance(nearby[1])-distance(nearby[0])>24?nearby[0]:null;
}
