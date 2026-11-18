import "./styles/taskconsole.css";
import { linkify } from "./sanitize";
import { apiFetch, wsUrl } from "./auth";

const API = "http://localhost:8000";

const input = document.getElementById("taskInput") as HTMLTextAreaElement | null;
const startBtn = document.getElementById("taskStart") as HTMLButtonElement | null;
const cancelBtn = document.getElementById("taskCancel") as HTMLButtonElement | null;
const stateEl = document.getElementById("consoleState") as HTMLElement | null;
const stepsEl = document.getElementById("taskSteps") as HTMLOListElement | null;
const summaryEl = document.getElementById("taskSummary") as HTMLElement | null;
const confirmModal = document.getElementById("confirmModal") as HTMLElement | null;
const confirmMsg = document.getElementById("confirmMsg") as HTMLElement | null;
const confirmAction = document.getElementById("confirmAction") as HTMLElement | null;
const approveBtn = document.getElementById("confirmApprove") as HTMLButtonElement | null;
const rejectBtn = document.getElementById("confirmReject") as HTMLButtonElement | null;

if (confirmModal) confirmModal.hidden = true;
if (cancelBtn) cancelBtn.hidden = true;
if (summaryEl) summaryEl.hidden = true;

let currentTaskId: string | null = null;
let confirmResolve: ((decision: boolean) => void) | null = null;
let cancelRequested = false;

function setState(text: string): void {
  if (stateEl) stateEl.textContent = text;
}

function formatSummary(text: string): string {
  // Escape + validated http(s) linkification lives in sanitize.ts.
  return linkify(text);
}

function actionIcon(msg: string): string {
  const m = msg.toLowerCase();
  if (m.includes("navigate") || m.includes("opened") || m.includes("http")) return "🧭";
  if (m.includes("click")) return "🖱️";
  if (m.includes("type") || m.includes("enter") || m.includes("submit") || m.includes("searched")) return "⌨️";
  if (m.includes("scroll")) return "📜";
  if (m.includes("zoom")) return "🔍";
  if (m.includes("wait")) return "⏳";
  if (m.includes("sign-in") || m.includes("login") || m.includes("sensitive") || m.includes("credential")) return "🔐";
  return "⚡";
}

function renderTaskState(taskState: string, message: string): void {
  // Single source of truth: the backend task state. The UI never invents "complete".
  setState(taskState);
  if (["completed", "failed", "cancelled"].includes(taskState)) {
    // A terminal task can no longer be approved; never leave a stale modal.
    if (confirmModal && !confirmModal.hidden) closeConfirm(false);
  }
  if (taskState === "completed" && summaryEl) {
    summaryEl.innerHTML = `<strong>✓ Completed</strong> · ${formatSummary(message || "Task completed.")}`;
    summaryEl.hidden = false;
  } else if (taskState === "failed" && summaryEl) {
    summaryEl.innerHTML = `<strong>✗ Blocked / Failed</strong> · ${formatSummary(message || "Task stopped.")}`;
    summaryEl.hidden = false;
  } else if (taskState === "awaiting_takeover" && summaryEl) {
    // Task 4: takeover is not a confirmation — the confirm modal posts
    // to /confirm and must never be opened here. The operator completes
    // the step in the browser tab and resumes from the agent workspace.
    summaryEl.innerHTML = `<strong>🧑‍💻 Takeover — you're in control</strong> · ${formatSummary(message || "Complete the login / payment step in your browser tab, then resume from the agent workspace.")}`;
    summaryEl.hidden = false;
  } else if (summaryEl) {
    summaryEl.hidden = true;
  }
}

function addStep(message: string): void {
  if (!stepsEl || !message) return;
  const lastLi = stepsEl.querySelector("li:last-child");
  if (lastLi && lastLi.textContent?.trim().includes(message.trim())) {
    return;
  }
  const li = document.createElement("li");
  li.innerHTML = `<span style="opacity: 0.9; margin-right: 4px;">${actionIcon(message)}</span> ${formatSummary(message)}`;
  stepsEl.appendChild(li);
}

function openConfirm(message: string, action: unknown): Promise<boolean> {
  return new Promise(resolve => {
    if (!confirmModal) { resolve(false); return; }
    confirmResolve?.(false);
    confirmResolve = resolve;
    if (confirmMsg) confirmMsg.textContent = message;
    if (confirmAction) confirmAction.textContent = JSON.stringify(action, null, 2);
    confirmModal.hidden = false;
  });
}

function closeConfirm(decision: boolean): void {
  if (confirmModal) confirmModal.hidden = true;
  confirmResolve?.(decision);
  confirmResolve = null;
}

approveBtn?.addEventListener("click", () => {
  const id = currentTaskId;
  closeConfirm(true);
  if (id) apiFetch(`${API}/api/task/${id}/confirm`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision: "approve" }) });
});
rejectBtn?.addEventListener("click", () => {
  const id = currentTaskId;
  closeConfirm(false);
  if (id) apiFetch(`${API}/api/task/${id}/confirm`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision: "reject" }) });
});

input?.addEventListener("input", () => {
  if (startBtn) startBtn.disabled = !input.value.trim();
});

// Preset mission chips: fill the console input with a real task and
// enable Start, so the demo is one tap away.
document.querySelectorAll<HTMLButtonElement>(".console__chip").forEach(chip => {
  chip.addEventListener("click", () => {
    const preset = chip.dataset.preset;
    if (!input || !preset) return;
    input.value = preset;
    input.dispatchEvent(new Event("input"));
    input.focus();
  });
});

input?.addEventListener("keydown", (e: KeyboardEvent) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    if (startBtn && !startBtn.disabled && input.value.trim()) {
      startBtn.click();
    }
  }
});

startBtn?.addEventListener("click", async () => {
  if (!input || !startBtn || startBtn.disabled || !input.value.trim()) return;
  startBtn.disabled = true;
  closeConfirm(false);
  currentTaskId = null;
  cancelRequested = false;
  if (stepsEl) stepsEl.innerHTML = "";
  if (summaryEl) summaryEl.hidden = true;
  setState("planning");
  try {
    const res = await apiFetch(`${API}/api/task`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instruction: input.value.trim() }),
    });
    if (!res.ok) throw new Error(`backend ${res.status}`);
    const data = (await res.json()) as { taskId: string };
    currentTaskId = data.taskId;
    if (cancelBtn) cancelBtn.hidden = false;

    // Cancel clicked while the task POST was in flight: honour it now that
    // the real task id exists instead of silently losing the request.
    if (cancelRequested) {
      try {
        await apiFetch(`${API}/api/task/${currentTaskId}/cancel`, { method: "POST" });
      } catch { /* offline */ }
      setState("cancelled");
      if (cancelBtn) cancelBtn.hidden = true;
      startBtn.disabled = false;
      return;
    }

    try {
      const stRes = await apiFetch(`${API}/api/task/${currentTaskId}`);
      if (stRes.ok) {
        const t = (await stRes.json()) as { taskState: string; message: string };
        if (t.taskState && t.taskState !== "planning") {
          renderTaskState(t.taskState, t.message);
          if (["completed", "failed", "cancelled"].includes(t.taskState)) {
            startBtn.disabled = false;
            if (cancelBtn) cancelBtn.hidden = true;
          }
        }
      }
    } catch { /* initial poll ignore */ }
  } catch (e) {
    setState(`backend offline — start uvicorn on :8000 (${e})`);
    startBtn.disabled = false;
    cancelRequested = false;
  }
});

cancelBtn?.addEventListener("click", async () => {
  const id = currentTaskId;
  cancelRequested = true;
  closeConfirm(false);
  if (cancelBtn) cancelBtn.hidden = true;
  if (id) {
    try {
      await apiFetch(`${API}/api/task/${id}/cancel`, { method: "POST" });
    } catch {
      // Offline: keep the UI consistent with the user's intent.
    }
  }
  setState("cancelled");
  if (startBtn) startBtn.disabled = false;
});

function connectEvents(): void {
  const ws = new WebSocket(wsUrl(`ws://localhost:8000/ws/client`));
  ws.onopen = async () => {
    if (currentTaskId) {
      try {
        const res = await apiFetch(`${API}/api/task/${currentTaskId}`);
        if (res.ok) {
          const t = (await res.json()) as { taskState: string; message: string };
          if (["completed", "failed", "cancelled"].includes(t.taskState)) {
            renderTaskState(t.taskState, t.message);
            if (startBtn) startBtn.disabled = !input?.value.trim();
            if (cancelBtn) cancelBtn.hidden = true;
          }
        }
      } catch { /* offline */ }
    }
  };
  ws.onmessage = ev => {
    try {
      const env = JSON.parse(ev.data) as {
        type: string;
        taskId: string;
        payload: {
          taskState: string;
          stepsDone: number;
          message: string;
          intent?: string;
          isBrowserTask?: boolean;
          sessionId?: string | null;
        };
      };
      if (env.type !== "PROGRESS" || env.taskId !== currentTaskId) return;
      const p = env.payload;
      const modeStr = p.intent === "NON_BROWSER" || p.isBrowserTask === false
        ? " [Conversational]"
        : (p.sessionId ? ` [Browser Session ${p.sessionId.slice(0, 8)}]` : "");
      // Raw state drives logic (terminal checks, summary branches); the
      // suffix must never contaminate state comparisons — it is display-only.
      renderTaskState(p.taskState, p.message);
      if (stateEl && modeStr) {
        setState(`${p.taskState}${modeStr}`);
      }
      addStep(`[${p.stepsDone}] ${p.message}`);
      if (p.taskState === "awaiting_confirmation") {
        apiFetch(`${API}/api/task/${env.taskId}`)
          .then(r => r.json())
          .then(t => openConfirm(t.pending_confirmation?.message ?? "Confirm this action", t.pending_confirmation?.action ?? {}));
      }
      if (p.taskState === "awaiting_takeover") {
        // Task 4: show the takeover reason in the summary; the resume /
        // cancel buttons live in the agent workspace, not in this console.
        apiFetch(`${API}/api/task/${env.taskId}/takeover`)
          .then(r => r.json())
          .then(t => {
            const reason = (t.takeover as { reason?: string } | null)?.reason;
            if (reason && summaryEl) {
              summaryEl.innerHTML = `<strong>🧑‍💻 Takeover — you're in control</strong> · ${formatSummary(reason)}`;
              summaryEl.hidden = false;
            }
          })
          .catch(() => { /* offline — renderTaskState already showed the state */ });
      }
      if (["completed", "failed", "cancelled"].includes(p.taskState)) {
        startBtn && (startBtn.disabled = false);
        if (cancelBtn) cancelBtn.hidden = true;
      }
    } catch { /* non-JSON frame — ignore */ }
  };
  ws.onclose = () => setTimeout(connectEvents, 3000);
}

if (startBtn && stateEl) connectEvents();
