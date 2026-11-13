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

export const COMPANION_THOUGHTS = [
  "Standing by! Tell me what to find, compare, or watch next.",
  "Tracking prices and deals across stores. Let me know what you want to check!",
  "Continuous monitoring is active on your dashboard (http://localhost:5173).",
  "Ready to research, browse, or keep watch on your inbox in the background.",
  "Surfing securely with fail-closed privacy. No credentials ever leave your browser.",
  "Here to help! Compare prices, summarize this page, or dispatch emails directly.",
  "Hey buddy! Ready whenever you are — toss me any search, price check, or research task.",
];

export function getRandomCompanionThought(): string {
  return COMPANION_THOUGHTS[Math.floor(Math.random() * COMPANION_THOUGHTS.length)];
}

// Backward-compatible alias
export const getRandomBanter = getRandomCompanionThought;

export function createDrawer(root: ShadowRoot, send: (message: object) => void) {
  const drawer = document.createElement("section");
  drawer.className = "drawer";
  drawer.hidden = true;
  drawer.setAttribute("aria-label", "PrivAgent mission controls");
  drawer.innerHTML = `
    <header>
      <div class="companion-brand">
        <span class="bot-badge">🤖</span>
        <div>
          <small>PRIVAGENT / AI COMPANION</small>
          <strong>Your Web Co-Pilot</strong>
        </div>
      </div>
      <span class="state">Idle</span>
    </header>

    <!-- Bot Speech & Co-Pilot Quick Launch -->
    <div class="bot-personality-card">
      <div class="bot-speech-bubble" id="drawerBanterText">"Standing by! Ask me to search, compare prices, or keep watch in the background."</div>
      <div class="bot-quick-chips">
        <button class="chip-btn" data-chip="summarize">🔍 Summarize Page</button>
        <button class="chip-btn" data-chip="prices">🛒 Compare Prices</button>
        <button class="chip-btn" data-chip="monitor">🎯 Monitor Price Drop</button>
        <button class="chip-btn" data-chip="inbox">📬 Inbox &amp; Calendar</button>
      </div>
      <form class="bot-mini-chat" id="botChatForm">
        <input type="text" class="bot-chat-input" placeholder="Ask PrivAgent anything or give a task..." maxlength="180" />
        <button type="submit" class="bot-chat-send">Send ↗</button>
      </form>
    </div>

    <div class="mission-status-section">
      <p class="directive"></p>
      <ol class="timeline" aria-live="polite"></ol>
    </div>

    <div class="gate" hidden>
      <p class="gate-message"></p>
      <button data-action="approve">Approve &amp; continue</button>
      <button data-action="abort">Abort</button>
    </div>

    <div class="controls">
      <button data-action="pause">Pause</button>
      <button data-action="resume" hidden>Resume</button>
      <button data-action="cancel">Cancel mission</button>
      <button data-action="dashboard">Open dashboard ↗</button>
    </div>
  `;
  root.append(drawer);

  let current: CompanionSnapshot | null = null;
  const banterTextEl = drawer.querySelector<HTMLElement>("#drawerBanterText")!;
  const chatForm = drawer.querySelector<HTMLFormElement>("#botChatForm")!;
  const chatInput = drawer.querySelector<HTMLInputElement>(".bot-chat-input")!;

  // Handle Quick Chips
  drawer.querySelectorAll<HTMLButtonElement>(".chip-btn").forEach(btn => {
    btn.addEventListener("click", event => {
      if (!trustedCompanionEvent(event)) return;
      const chip = btn.dataset.chip;
      if (chip === "summarize") {
        banterTextEl.textContent = `"Analyzing this webpage right now..."`;
        send({ type: "COMPANION_DIRECTIVE", directive: `Summarize the current webpage: ${window.location.href}` });
      } else if (chip === "prices") {
        banterTextEl.textContent = `"Searching and comparing prices across Amazon, Flipkart, and Croma... 🛍️"`;
        send({ type: "COMPANION_DIRECTIVE", directive: `Find and compare prices for products on ${window.location.hostname}` });
      } else if (chip === "monitor") {
        banterTextEl.textContent = `"Setting up continuous price tracking on your dashboard at localhost:5173... 🎯"`;
        send({ type: "COMPANION_DIRECTIVE", directive: `Continuously monitor this product price and notify me if it drops` });
      } else if (chip === "inbox") {
        banterTextEl.textContent = `"Checking your connected mail account and calendar... 📬"`;
        send({ type: "COMPANION_DIRECTIVE", directive: "Show me today's email and calendar summary" });
      }
      const bubble = root.querySelector<HTMLElement>(".bubble");
      if (bubble) {
        bubble.textContent = banterTextEl.textContent.replace(/^"|"$/g, "");
        bubble.hidden = false;
      }
    });
  });

  // Handle Mini Chat Form
  chatForm.addEventListener("submit", event => {
    event.preventDefault();
    const query = chatInput.value.trim();
    if (!query) return;
    chatInput.value = "";

    const qLower = query.toLowerCase();
    if (qLower.includes("hi") || qLower.includes("hello") || qLower.includes("hey")) {
      banterTextEl.textContent = `"Hey buddy! Long time no see! What can I help you explore or check today?"`;
    } else if (qLower.includes("monitor") || qLower.includes("watch")) {
      banterTextEl.textContent = `"Setting up continuous monitoring on your dashboard at localhost:5173..."`;
      send({ type: "COMPANION_DIRECTIVE", directive: query });
    } else if (qLower.includes("who are you") || qLower.includes("what can you do")) {
      banterTextEl.textContent = `"I'm PrivAgent — your privacy-preserving AI web co-pilot! I browse sites, compare store prices, monitor inboxes, and keep watch on localhost:5173."`;
    } else {
      banterTextEl.textContent = `"On it! Let's handle: '${query.slice(0, 50)}...' Opening dashboard to track progress!"`;
      send({ type: "COMPANION_DIRECTIVE", directive: query });
    }

    const bubble = root.querySelector<HTMLElement>(".bubble");
    if (bubble) {
      bubble.textContent = banterTextEl.textContent.replace(/^"|"$/g, "");
      bubble.hidden = false;
    }
  });

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
    timeline.innerHTML = snapshot.timeline.map(step => `<li>${step}</li>`).join("");
    const gate = drawer.querySelector(".gate") as HTMLElement;
    const gateMsg = drawer.querySelector(".gate-message") as HTMLElement;
    const isPaused = snapshot.status === "paused";
    const pauseBtn = drawer.querySelector("[data-action=pause]") as HTMLElement | null;
    const resumeBtn = drawer.querySelector("[data-action=resume]") as HTMLElement | null;
    if (pauseBtn) pauseBtn.hidden = isPaused;
    if (resumeBtn) resumeBtn.hidden = !isPaused;
    if (snapshot.pendingGate && snapshot.pendingGate.taskId === snapshot.taskId) {
      gate.hidden = false;
      gateMsg.textContent = snapshot.pendingGate.message;
    } else {
      gate.hidden = true;
    }
  };

  return { drawer, update };
}
