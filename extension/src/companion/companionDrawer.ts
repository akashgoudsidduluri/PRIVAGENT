export interface CompanionSnapshot {
  taskId: string | null;
  stepIndex: number;
  status: string;
  currentThought: string;
  directive: string;
  pendingGate: { taskId: string; message: string } | null;
  timeline: string[];
}

export const trustedCompanionEvent = (event: Event): boolean => event.isTrusted === true;

export function createDrawer(root: ShadowRoot, send: (message: object) => void) {
  const drawer = document.createElement("section");
  drawer.className = "drawer";
  drawer.hidden = true;
  drawer.setAttribute("aria-label", "PrivAgent mission controls");
  drawer.innerHTML = `<header><small>PRIVAGENT / LIVE</small><strong>Mission control</strong><span class="state">Idle</span></header>
    <p class="directive"></p><ol class="timeline" aria-live="polite"></ol>
    <div class="gate" hidden><p class="gate-message"></p><button data-action="approve">Approve &amp; continue</button><button data-action="abort">Abort</button></div>
    <div class="controls"><button data-action="pause">Pause</button><button data-action="resume" hidden>Resume</button>
    <button data-action="cancel">Cancel mission</button><button data-action="dashboard">Open dashboard ↗</button></div>`;
  root.append(drawer);
  let current: CompanionSnapshot | null = null;
  drawer.addEventListener("click", event => {
    if (!trustedCompanionEvent(event)) return;
    const button = (event.target as Element).closest<HTMLButtonElement>("button[data-action]");
    if (!button || !drawer.contains(button)) return;
    const action = button.dataset.action;
    if (action === "dashboard") { send({ type: "COMPANION_OPEN_DASHBOARD" }); return; }
    if (!current?.taskId) return;
    if ((action === "approve" || action === "abort") && current.pendingGate?.taskId === current.taskId) {
      send({ type: "COMPANION_GATE", taskId: current.taskId, decision: action === "approve" ? "approve" : "reject" });
    } else if (["pause", "resume", "cancel"].includes(action || "")) {
      send({ type: "COMPANION_CONTROL", taskId: current.taskId, action });
    }
  });
  const update = (snapshot: CompanionSnapshot) => {
    current = snapshot;
    drawer.querySelector(".state")!.textContent = snapshot.status.replaceAll("_", " ");
    drawer.querySelector(".directive")!.textContent = snapshot.directive.slice(0, 160);
    const timeline = drawer.querySelector(".timeline")!;
    timeline.replaceChildren(...snapshot.timeline.slice(-8).map(item => {
      const li = document.createElement("li"); li.textContent = item.slice(0, 180); return li;
    }));
    const gate = drawer.querySelector<HTMLElement>(".gate")!;
    gate.hidden = !snapshot.pendingGate;
    gate.querySelector(".gate-message")!.textContent = snapshot.pendingGate?.message.slice(0, 250) || "";
    drawer.querySelector<HTMLButtonElement>('[data-action="pause"]')!.hidden = snapshot.status === "paused";
    drawer.querySelector<HTMLButtonElement>('[data-action="resume"]')!.hidden = snapshot.status !== "paused";
  };
  return { drawer, update };
}
