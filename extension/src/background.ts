import { Relay } from "./relay";
import { executeAction } from "./actionExecutor";
import type { ActionResult } from "./contracts";

const BACKEND_WS = "ws://localhost:8000/ws/extension";
const RECONNECT_MS = 2000;

const relay = new Relay();
let socket: WebSocket | null = null;
let isConnecting = false;

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
    relay.send({ type: "RESULT", taskId, payload: result });
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
  }
});

connect();
