import { Relay } from "./relay";
import { executeAction, extensionSessions, isDrivableTabUrl } from "./actionExecutor";
import type { ActionResult } from "./contracts";
import { authorizedCompanionTab, emptyCompanionState, nextCompanionState } from "./companion/companionState";
import type { CompanionSnapshot } from "./companion/companionDrawer";

const BACKEND_WS = "ws://localhost:8000/ws/extension";
const RECONNECT_MS = 2000;

const relay = new Relay();
let socket: WebSocket | null = null;
let isConnecting = false;
let companionState: CompanionSnapshot = emptyCompanionState();
let primaryTabId: number | null = null;
let childTabIds: number[] = [];
const restored = chrome.storage.session.get("companion").then(value => {
  const prior = value.companion as { state?: CompanionSnapshot; primary?: number; children?: number[] } | undefined;
  if (prior?.state?.taskId) {
    companionState = prior.state;
    primaryTabId = prior.primary ?? null;
    childTabIds = prior.children ?? [];
  }
}).catch(() => {});

function persistCompanion(): void {
  chrome.storage.session.set({ companion: { state: companionState, primary: primaryTabId, children: childTabIds } }).catch(() => {});
}

function broadcastCompanion(): void {
  for (const tabId of [primaryTabId, ...childTabIds]) {
    if (typeof tabId === "number") chrome.tabs.sendMessage(tabId, { type: "COMPANION_STATE", state: companionState }).catch(() => {});
  }
}

let progressSocket: WebSocket | null = null;
function connectProgress(): void {
  if (progressSocket && progressSocket.readyState <= WebSocket.OPEN) return;
  const ws = new WebSocket("ws://localhost:8000/ws/client");
  progressSocket = ws;
  ws.onmessage = async event => {
    let data: any;
    try { data = JSON.parse(event.data); } catch { return; }
    if (data?.type !== "PROGRESS" || typeof data.taskId !== "string") return;
    await restored;
    const packet = data.payload;
    if (!packet || packet.isBrowserTask === false) return;
    if (typeof packet.primaryTabId !== "number" && data.taskId !== companionState.taskId) return;
    // Each new task takes ownership only when the backend supplies a mission tab.
    if (typeof packet.primaryTabId === "number") primaryTabId = packet.primaryTabId;
    if (Array.isArray(packet.childTabIds)) childTabIds = packet.childTabIds.filter((id: unknown) => typeof id === "number");
    companionState = nextCompanionState(companionState, { ...packet, taskId: data.taskId });
    if (packet.taskState === "awaiting_confirmation") {
      try {
        const response = await fetch(`http://localhost:8000/api/task/${encodeURIComponent(data.taskId)}`);
        if (response.ok) {
          const detail = await response.json();
          companionState.pendingGate = detail.pending_confirmation
            ? { taskId: data.taskId, message: String(detail.pending_confirmation.message || "Approval required") } : null;
        }
      } catch { /* Gate stays unavailable until backend reconnects. */ }
    }
    persistCompanion(); broadcastCompanion();
  };
  ws.onclose = () => { if (progressSocket === ws) progressSocket = null; setTimeout(connectProgress, RECONNECT_MS); };
  ws.onerror = () => { try { ws.close(); } catch {} };
}

function connect() {
  if (isConnecting) return;
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  isConnecting = true;
  try {
    const ws = new WebSocket(BACKEND_WS);
    socket = ws;
    ws.addEventListener("open", () => {
      console.log("[navigator] relay connected to backend");
      isConnecting = false;
    });
    ws.addEventListener("close", () => {
      relay.detach(ws);
      if (socket === ws) socket = null;
      isConnecting = false;
      setTimeout(connect, RECONNECT_MS);
    });
    ws.addEventListener("error", () => {
      try { ws.close(); } catch { /* ignore */ }
    });
    relay.attach(ws);
  } catch {
    socket = null;
    isConnecting = false;
    setTimeout(connect, RECONNECT_MS);
  }
}

function timeoutError(ms: number): ActionResult {
  return {
    ok: false,
    error: { code: "TIMEOUT", message: `action exceeded ${ms}ms`, recoverable: true },
  };
}

/** Run one action under a hard deadline so a stuck tab cannot block the
 * relay (and the backend's 45s slot) forever. */
async function withDeadline(promise: Promise<ActionResult>, ms: number): Promise<ActionResult> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<ActionResult>(resolve => {
    timer = setTimeout(() => resolve(timeoutError(ms)), ms);
  });
  try {
    return await Promise.race([promise, deadline]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

async function handleMessage(taskId: string, raw: unknown): Promise<void> {
  const action = raw as { type?: string; timeoutMs?: number } | null;
  if (action?.type === "reload_extension") {
    relay.send({ type: "RESULT", taskId, payload: { ok: true, evidence: "reloading extension" } });
    setTimeout(() => {
      try {
        chrome.runtime?.reload?.();
      } catch {}
    }, 50);
    return;
  }
  const actionMs = typeof action?.timeoutMs === "number" && action.timeoutMs > 0
    ? Math.min(action.timeoutMs, 60_000)
    : 10_000;
  try {
    const result = await withDeadline(
      executeAction(action as Parameters<typeof executeAction>[0], taskId),
      actionMs + 5_000,
    );
    if (action?.type === "init_session" && typeof (result as any)?.primaryTabId === "number") {
      primaryTabId = (result as any).primaryTabId;
      persistCompanion();
    }
    relay.send({ type: "RESULT", taskId, payload: result });
    // Completion state is driven solely by backend PROGRESS, after verification.
  } catch (e) {
    relay.send({
      type: "RESULT",
      taskId,
      payload: { ok: false, error: { code: "UNKNOWN", message: String(e), recoverable: true } },
    });
  }
}

// Serialize actions: two EXECUTE_ACTION frames must never interleave clicks
// in the same tab.
let queue: Promise<void> = Promise.resolve();
relay.onMessage(m => {
  queue = queue.then(() => handleMessage(m.taskId, m.payload)).catch(() => { /* handled */ });
});

// Alarm heartbeat keeps service worker connected to the backend
chrome.alarms?.create("keepAlive", { periodInMinutes: 0.25 });
chrome.alarms?.onAlarm.addListener(alarm => {
  if (alarm.name === "keepAlive") {
    connect();
    connectProgress();
  }
});

connect();
connectProgress();

function isAuthorizedCompanionTab(tabId: number | undefined): boolean {
  if (typeof tabId !== "number") return false;
  if (authorizedCompanionTab(tabId, primaryTabId, childTabIds)) return true;
  for (const sess of extensionSessions.values()) {
    if (sess.primaryTabId === tabId || sess.childTabIds.has(tabId)) return true;
  }
  return false;
}

async function reinjectCompanion(tabId: number): Promise<void> {
  await restored;
  if (!isAuthorizedCompanionTab(tabId)) return;
  try {
    const tab = await chrome.tabs.get(tabId);
    if (!isDrivableTabUrl(tab.url || "") || !/^https?:\/\//.test(tab.url || "")) return;
    await chrome.scripting.executeScript({ target: { tabId }, files: ["companion.js"] });
    await chrome.tabs.sendMessage(tabId, { type: "COMPANION_STATE", state: companionState }).catch(() => {});
  } catch { /* Restricted or loading page; onCompleted will retry. */ }
}

chrome.webNavigation.onCommitted.addListener(details => {
  if (details.frameId === 0) void reinjectCompanion(details.tabId);
});
chrome.webNavigation.onCompleted.addListener(details => {
  if (details.frameId === 0) void reinjectCompanion(details.tabId);
});

// The page cannot choose URLs or invoke mission approvals through this channel.
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id || !sender.tab) return;
  if (message.type === "COMPANION_SYNC") {
    void restored.then(() => reply(isAuthorizedCompanionTab(sender.tab?.id) ? companionState : null));
    return true;
  }
  void restored.then(() => {
    if (!isAuthorizedCompanionTab(sender.tab?.id)) return;
  if (message.type === "COMPANION_OPEN_DASHBOARD") {
    chrome.tabs.create({url: "http://localhost:5173/agent.html#agent-workspace"});
    return;
  }
  if (message.taskId !== companionState.taskId || !message.taskId) return;
  if (message.type === "COMPANION_GATE") {
    if (companionState.pendingGate?.taskId !== message.taskId || !["approve", "reject"].includes(message.decision)) return;
    void fetch(`http://localhost:8000/api/task/${encodeURIComponent(message.taskId)}/confirm`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision: message.decision }),
    }).then(response => { if (response.ok) { companionState.pendingGate = null; persistCompanion(); broadcastCompanion(); } });
  }
  if (message.type === "COMPANION_CONTROL" && ["pause", "resume", "cancel"].includes(message.action)) {
    void fetch(`http://localhost:8000/api/task/${encodeURIComponent(message.taskId)}/${message.action}`, { method: "POST" });
  }
  });
});
