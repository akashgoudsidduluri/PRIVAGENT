import './styles/control-center.css';
import './styles/control-center-refresh.css';
import { mountControlView, mountInspector, mountActivity } from './controlViews';
import { ensureWorkspaceMounted } from './agentApp';
const views = ['Overview','Agent Workspace','Browser Inspector','Missions','Email Intelligence','Connectors','MCP Tools','Memory','Artifacts','Activity','Privacy Center','Settings'];
const icons = ['◈','↗','▣','◎','✉','⌘','◇','▤','▧','≋','⬡','⚙'];
const nav = document.querySelector<HTMLElement>('#controlNav')!;
const page = document.querySelector<HTMLElement>('#controlPage')!;
// The workspace markup lives inert inside <template id="workspaceTemplate">
// (UI flicker fix): the first paint is the control center only —
// #agentWorkspace is absent from the initial DOM. Its live nodes are
// captured once, on first mount, and the cached refs stay valid wherever
// the nodes are moved afterwards (same as the old module-top-level
// captures, which ran when the markup was painted statically).
interface WorkspaceNodes { workspace: HTMLElement; originalTimeline: HTMLElement; inspector: HTMLElement; feed: HTMLElement; }
let liveNodes: WorkspaceNodes | null = null;
function ensureLiveNodes(): WorkspaceNodes | null {
 if (liveNodes) return liveNodes;
 const workspace = ensureWorkspaceMounted();
 if (!workspace) return null;
 const originalTimeline = workspace.querySelector<HTMLElement>('.agent-timeline');
 const inspector = workspace.querySelector<HTMLElement>('.agent-sidebar');
 const feed = workspace.querySelector<HTMLElement>('.agent-feed-card');
 if (!originalTimeline || !inspector || !feed) return null;
 liveNodes = { workspace, originalTimeline, inspector, feed };
 return liveNodes;
}
nav.innerHTML = `<a class="control-monogram" href="#overview" aria-label="PrivAgent overview">P<span>•</span></a><div class="control-nav-label">WORKSPACE</div><span class="control-nav__pill" id="controlNavPill" aria-hidden="true"></span>` + views.map((v,i)=>`<button data-view="${i}" title="${v}"><span>${icons[i]}</span><b>${v}</b>${i===1?'<small>LIVE</small>':''}</button>`).join('') + '<div class="nav-foot"><span class="orbit-dot"></span>LOCAL BY DESIGN</div>';
const navPill = nav.querySelector<HTMLElement>('#controlNavPill');
// Animated active-pill indicator: a cobalt-tinted pill that slides
// behind the selected view button (transform transition). Positioned
// on every render from the selected button's offset within the nav.
function moveNavPill(index: number): void {
  if (!navPill) return;
  const buttons = nav.querySelectorAll<HTMLButtonElement>('button');
  const btn = buttons[index];
  if (!btn) return;
  const navRect = nav.getBoundingClientRect();
  const btnRect = btn.getBoundingClientRect();
  navPill.style.height = `${btnRect.height}px`;
  navPill.style.transform = `translateY(${btnRect.top - navRect.top + nav.scrollTop}px)`;
}
let generation = 0;
let inspectorRefresh: number | undefined;
async function render(index: number) {
 const version = ++generation;
 if (inspectorRefresh !== undefined) { window.clearInterval(inspectorRefresh); inspectorRefresh = undefined; }
 // Only the views that move live workspace nodes mount the template:
 // 1 (Agent Workspace), 2 (Browser Inspector), 9 (Activity). Every other
 // view leaves the template untouched, so a fresh load never paints —
 // or even mounts — the old workspace.
 const needsWorkspace = index === 1 || index === 2 || index === 9;
 const nodes = needsWorkspace ? ensureLiveNodes() : liveNodes;
 if (nodes) {
  // Move existing live nodes home, never clone listeners or approval controls.
  nodes.originalTimeline.append(nodes.feed); nodes.workspace.append(nodes.inspector);
  nodes.workspace.hidden = index !== 1;
 }
 document.body.dataset.controlView = String(index);
 nav.querySelectorAll('button').forEach((b,i)=>{b.classList.toggle('selected', i===index); b.setAttribute('aria-current',i===index?'page':'false');});
 moveNavPill(index);
 page.hidden=index===1;
 if(index===1) return;
 page.innerHTML=`<header class="cc-heading"><div><span class="eyebrow">PRIVAGENT / CONTROL CENTER</span><h1>${views[index]}<span> .</span></h1><p>${['Your autonomy, at a glance.','','A closer look at what your agent can actually see.','Long-running intent. Clear, accountable signals.','Continuous inbox scan, safe cleanup, and neglected email detection.','Your tools, connected on your terms.','Explicit capabilities. Explicit permissions.','Useful context, with a deliberate footprint.','From evidence to something you can use.','Every action leaves a trace.','Privacy is a process. Inspect the policy.','Tune the intelligence behind your missions.'][index]}</p></div><button class="cc-button" id="ccLaunch">↗ New mission</button></header><div id="ccContent" aria-live="polite"></div>`;
 document.querySelector('#ccLaunch')!.addEventListener('click',()=>{location.hash='agent-workspace';});
 const content=document.querySelector<HTMLElement>('#ccContent')!;
 if(index===2){content.innerHTML='<div class="inspector-stage"><div class="cc-inspector-data"></div></div><div class="cc-verification-data"></div>'; content.firstElementChild!.prepend(nodes!.inspector); await mountInspector(content,()=>version===generation); if(version===generation)inspectorRefresh=window.setInterval(()=>mountInspector(content,()=>version===generation),4000); return;}
 if(index===9){content.append(nodes!.feed); const audit=document.createElement('div'); audit.className='cc-activity-audit'; content.append(audit); await mountActivity(content,()=>version===generation); return;}
 if([0,3,4,5,6,7,8,10,11].includes(index)){await mountControlView(index,content,()=>version===generation);if(index===10&&version===generation)inspectorRefresh=window.setInterval(()=>mountControlView(index,content,()=>version===generation),5000);return;}

}
nav.querySelectorAll<HTMLButtonElement>('button').forEach((b,i)=>b.onclick=()=>{location.hash=views[i].toLowerCase().replaceAll(' ','-');});
function route(){const index=views.findIndex(v=>v.toLowerCase().replaceAll(' ','-')===location.hash.slice(1));render(index<0?0:index);}
window.addEventListener('hashchange',route);route();
