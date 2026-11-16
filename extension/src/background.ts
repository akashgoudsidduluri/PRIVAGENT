import { Relay } from "./relay";
import { executeAction, extensionSessions, isDrivableTabUrl, setTakeoverActive } from "./actionExecutor";
import type { ActionResult } from "./contracts";
import { authorizedCompanionTab, emptyCompanionState, nextCompanionState } from "./companion/companionState";
import type { CompanionSnapshot } from "./companion/companionDrawer";

const BACKEND_WS = "ws://localhost:8000/ws/extension";
const TOKEN_STORAGE_KEY = "privagentLocalToken";
// Task 3: operator-attested isolated-profile flag. Chrome does not expose
// the profile directory to extensions, so this is set by the operator
// after launching Chrome with --user-data-dir=<agent profile dir>:
//   chrome.storage.local.set({ privagentIsolatedProfile: true })
// The backend reports it (unverified) at /api/isolation/status. Until
// it is set, the extension reports isolatedProfile: false.
const ISOLATED_PROFILE_STORAGE_KEY = "privagentIsolatedProfile";
const RECONNECT_MS = 2000;

// Local operator token (Task 1). The backend requires a bearer token on
// every /api/* route and accepts ?token= on its websocket endpoints.
// The token is stored in chrome.storage.local under TOKEN_STORAGE_KEY.
// Manual first-run step: open the extension service worker console and run:
//   chrome.storage.local.set({ privagentLocalToken: "<token from backend/data/.local_token>" })
// (A token-entry UI in the extension is follow-up work.) Until a token
// is stored, the extension connects without one and the backend accepts
// the websocket with a warning log; REST calls without a token get 401.
async function getBackendToken(): Promise<string | null> {
  try {
    const value = await chrome.storage.local.get(TOKEN_STORAGE_KEY);
    const token = value?.[TOKEN_STORAGE_KEY];
    return typeof token === "string" && token ? token : null;
  } catch {
    return null;
  }
}

function withTokenQuery(url: string, token: string | null): string {
  if (!token) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}

async function getIsolatedProfileFlag(): Promise<boolean> {
  try {
    const value = await chrome.storage.local.get(ISOLATED_PROFILE_STORAGE_KEY);
    return value?.[ISOLATED_PROFILE_STORAGE_KEY] === true;
  } catch {
    return false;
  }
}

// Task 3: wipe agent-profile state once per service-worker lifetime when
// running in the isolated profile, so a previous mission's cookies /
// cache / storage cannot leak into the next one. Best-effort and
// guarded: the browsingData API may be unavailable in some contexts.
// This is wipe-on-start (not wipe-on-close — a service worker has no
// reliable "browser closed" signal), and it only runs when the operator
// has attested the isolated profile.
let profileWiped = false;
async function wipeAgentProfileStateOnce(): Promise<void> {
  if (profileWiped) return;
  profileWiped = true;
  try {
    const api = (chrome as unknown as { browsingData?: { remove: (options: unknown, data: unknown) => Promise<void> } }).browsingData;
    if (!api) return;
    await api.remove(
      { since: 0 },
      { cookies: true, cache: true, localStorage: true, indexedDB: true, serviceWorkers: true },
    );
  } catch {
    // Hygiene is best-effort; a failed wipe must not block the relay.
  }
}

async function authHeaders(): Promise<Record<string, string>> {
  const token = await getBackendToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

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
async function connectProgress(): Promise<void> {
  if (progressSocket && progressSocket.readyState <= WebSocket.OPEN) return;
  const token = await getBackendToken();
  const ws = new WebSocket(withTokenQuery("ws://localhost:8000/ws/client", token));
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
        const response = await fetch(`http://localhost:8000/api/task/${encodeURIComponent(data.taskId)}`, { headers: await authHeaders() });
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

async function connect(): Promise<void> {
  if (isConnecting) return;
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  isConnecting = true;
  try {
    const token = await getBackendToken();
    const ws = new WebSocket(withTokenQuery(BACKEND_WS, token));
    socket = ws;
    ws.addEventListener("open", () => {
      console.log("[navigator] relay connected to backend");
      isConnecting = false;
      // Task 3: self-report the (operator-attested) isolated-profile
      // flag so the backend can show it at /api/isolation/status.
      void (async () => {
        const isolatedProfile = await getIsolatedProfileFlag();
        if (isolatedProfile) void wipeAgentProfileStateOnce();
        try {
          ws.send(JSON.stringify({
            type: "HELLO",
            profile: { isolatedProfile, reportedAt: new Date().toISOString() },
          }));
        } catch { /* HELLO is informational; the relay works without it. */ }
      })();
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

async function handleMessage(taskId: string, raw: unknown, allowedDomains?: string[]): Promise<void> {
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
      executeAction(action as Parameters<typeof executeAction>[0], taskId, allowedDomains),
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
  // Task 4: takeover notifications are not actions — they flip the
  // extension's observe/screenshot flag and never enter the queue.
  if (m.type === "TAKEOVER_START") {
    setTakeoverActive(true);
    return;
  }
  if (m.type === "TAKEOVER_END") {
    setTakeoverActive(false);
    return;
  }
  // The TAKEOVER variant's discriminant is itself a union
  // ("TAKEOVER_START" | "TAKEOVER_END"), so the two equality checks above
  // do not narrow `m` away from it for the type checker. This guard does:
  // after it, `m` is the EXECUTE_ACTION variant.
  if (m.type !== "EXECUTE_ACTION") return;
  // Copy the fields into consts so the narrowing survives into the queued
  // closure — TypeScript resets parameter narrowing inside closures.
  const { taskId, payload, allowedDomains } = m;
  queue = queue.then(() => handleMessage(taskId, payload, allowedDomains)).catch(() => { /* handled */ });
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
    void authHeaders().then(headers => fetch(`http://localhost:8000/api/task/${encodeURIComponent(message.taskId)}/confirm`, {
      method: "POST", headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify({ decision: message.decision }),
    }).then(response => { if (response.ok) { companionState.pendingGate = null; persistCompanion(); broadcastCompanion(); } }));
  }
  if (message.type === "COMPANION_CONTROL" && ["pause", "resume", "cancel"].includes(message.action)) {
    void authHeaders().then(headers => fetch(`http://localhost:8000/api/task/${encodeURIComponent(message.taskId)}/${message.action}`, { method: "POST", headers }));
  }
  });
});
