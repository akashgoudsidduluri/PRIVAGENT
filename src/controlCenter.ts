import './styles/control-center.css';
import './styles/control-center-refresh.css';
import { mountControlView, mountInspector, mountActivity } from './controlViews';
const views = ['Overview','Agent Workspace','Browser Inspector','Missions','Email Intelligence','Connectors','MCP Tools','Memory','Artifacts','Activity','Privacy Center','Settings'];
const icons = ['◈','↗','▣','◎','✉','⌘','◇','▤','▧','≋','⬡','⚙'];
const nav = document.querySelector<HTMLElement>('#controlNav')!;
const page = document.querySelector<HTMLElement>('#controlPage')!;
const workspace = document.querySelector<HTMLElement>('#agentWorkspace')!;
const originalTimeline = document.querySelector<HTMLElement>('.agent-timeline')!;
const inspector = document.querySelector<HTMLElement>('.agent-sidebar')!;
const feed = document.querySelector<HTMLElement>('.agent-feed-card')!;
nav.innerHTML = `<a class="control-monogram" href="#overview" aria-label="PrivAgent overview">P<span>•</span></a><div class="control-nav-label">WORKSPACE</div>` + views.map((v,i)=>`<button data-view="${i}" title="${v}"><span>${icons[i]}</span><b>${v}</b>${i===1?'<small>LIVE</small>':''}</button>`).join('') + '<div class="nav-foot"><span class="orbit-dot"></span>LOCAL BY DESIGN</div>';
let generation = 0;
let inspectorRefresh: number | undefined;
async function render(index: number) {
 const version = ++generation;
 if (inspectorRefresh !== undefined) { window.clearInterval(inspectorRefresh); inspectorRefresh = undefined; }
 // Move existing live nodes, never clone listeners or approval controls.
 originalTimeline.append(feed); workspace.append(inspector);
 document.body.dataset.controlView = String(index);
 nav.querySelectorAll('button').forEach((b,i)=>{b.classList.toggle('selected', i===index); b.setAttribute('aria-current',i===index?'page':'false');});
 workspace.hidden = index!==1; page.hidden=index===1;
 if(index===1) return;
 page.innerHTML=`<header class="cc-heading"><div><span class="eyebrow">PRIVAGENT / CONTROL CENTER</span><h1>${views[index]}<span> .</span></h1><p>${['Your autonomy, at a glance.','','A closer look at what your agent can actually see.','Long-running intent. Clear, accountable signals.','Continuous inbox scan, safe cleanup, and neglected email detection.','Your tools, connected on your terms.','Explicit capabilities. Explicit permissions.','Useful context, with a deliberate footprint.','From evidence to something you can use.','Every action leaves a trace.','Privacy is a process. Inspect the policy.','Tune the intelligence behind your missions.'][index]}</p></div><button class="cc-button" id="ccLaunch">↗ New mission</button></header><div id="ccContent" aria-live="polite"></div>`;
 document.querySelector('#ccLaunch')!.addEventListener('click',()=>{location.hash='agent-workspace';});
 const content=document.querySelector<HTMLElement>('#ccContent')!;
 if(index===2){content.innerHTML='<div class="inspector-stage"><div class="cc-inspector-data"></div></div><div class="cc-verification-data"></div>'; content.firstElementChild!.prepend(inspector); await mountInspector(content,()=>version===generation); if(version===generation)inspectorRefresh=window.setInterval(()=>mountInspector(content,()=>version===generation),4000); return;}
 if(index===9){content.append(feed); const audit=document.createElement('div'); audit.className='cc-activity-audit'; content.append(audit); await mountActivity(content,()=>version===generation); return;}
 if([0,3,4,5,6,7,8,10,11].includes(index)){await mountControlView(index,content,()=>version===generation);if(index===10&&version===generation)inspectorRefresh=window.setInterval(()=>mountControlView(index,content,()=>version===generation),5000);return;}

}
nav.querySelectorAll<HTMLButtonElement>('button').forEach((b,i)=>b.onclick=()=>{location.hash=views[i].toLowerCase().replaceAll(' ','-');});
function route(){const index=views.findIndex(v=>v.toLowerCase().replaceAll(' ','-')===location.hash.slice(1));render(index<0?0:index);}
window.addEventListener('hashchange',route);route();
