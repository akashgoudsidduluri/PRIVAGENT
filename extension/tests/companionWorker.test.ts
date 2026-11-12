import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => { vi.unstubAllGlobals(); vi.resetModules(); });

describe("companion service worker hydration", () => {
  it("restores an authorized child tab after navigation and rejects other tabs", async () => {
    const sockets: Array<{ url: string; onmessage?: (event: { data: string }) => Promise<void>; addEventListener: ReturnType<typeof vi.fn> }> = [];
    class Socket {
      static OPEN = 1; static CONNECTING = 0;
      readyState = 1;
      onmessage?: (event: { data: string }) => Promise<void>;
      onclose?: () => void;
      onerror?: () => void;
      addEventListener = vi.fn();
      constructor(public url: string) { sockets.push(this); }
    }
    const committed = vi.fn(); const completed = vi.fn(); const runtimeListener = vi.fn();
    const executeScript = vi.fn(async () => []); const sendMessage = vi.fn(async () => ({}));
    const prior = { taskId: "task-1", stepIndex: 2, status: "running", currentThought: "Reading page",
      directive: "Research", pendingGate: null, timeline: ["Step 1", "Step 2"] };
    vi.stubGlobal("WebSocket", Socket);
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ pending_confirmation: { message: "Approve action?" } }) })));
    vi.stubGlobal("chrome", {
      runtime: { id: "extension-id", onMessage: { addListener: runtimeListener } },
      alarms: { create: vi.fn(), onAlarm: { addListener: vi.fn() } },
      storage: { session: { get: vi.fn(async () => ({ companion: { state: prior, primary: 9, children: [10] } })), set: vi.fn(async () => {}) } },
      tabs: { get: vi.fn(async () => ({ url: "https://example.org/article" })), sendMessage, create: vi.fn(async () => ({})) },
      scripting: { executeScript },
      webNavigation: { onCommitted: { addListener: committed }, onCompleted: { addListener: completed } },
    });
    await import("../src/background");
    await Promise.resolve();
    expect(sockets.map(s => s.url)).toContain("ws://localhost:8000/ws/client");
    const listener = runtimeListener.mock.calls[0][0] as (message: any, sender: any, reply: (value: any) => void) => void;
    const allowedReply = vi.fn();
    listener({ type: "COMPANION_SYNC" }, { id: "extension-id", tab: { id: 10 } }, allowedReply);
    await Promise.resolve();
    expect(allowedReply).toHaveBeenCalledWith(prior);
    const deniedReply = vi.fn();
    listener({ type: "COMPANION_SYNC" }, { id: "extension-id", tab: { id: 11 } }, deniedReply);
    await Promise.resolve();
    expect(deniedReply).toHaveBeenCalledWith(null);
    const onCompleted = completed.mock.calls[0][0] as (details: any) => void;
    onCompleted({ tabId: 10, frameId: 0 });
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(executeScript).toHaveBeenCalledWith({ target: { tabId: 10 }, files: ["companion.js"] });
    expect(sendMessage).toHaveBeenCalledWith(10, { type: "COMPANION_STATE", state: prior });
    const progress = sockets.find(s => s.url.endsWith("/ws/client"))!;
    await progress.onmessage!({ data: JSON.stringify({ type: "PROGRESS", taskId: "task-1", payload: {
      taskState: "awaiting_confirmation", stepsDone: 3, message: "Approval needed", instruction: "Research",
      primaryTabId: 9, childTabIds: [10], isBrowserTask: true } }) });
    expect(sendMessage).toHaveBeenCalledWith(10, expect.objectContaining({ type: "COMPANION_STATE",
      state: expect.objectContaining({ pendingGate: { taskId: "task-1", message: "Approve action?" } }) }));
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    listener({ type: "COMPANION_GATE", taskId: "task-1", decision: "approve" }, { id: "extension-id", tab: { id: 11 } }, vi.fn());
    await Promise.resolve();
    expect(fetchMock).toHaveBeenCalledTimes(1); // pending-gate read only
    listener({ type: "COMPANION_GATE", taskId: "task-1", decision: "approve" }, { id: "extension-id", tab: { id: 10 } }, vi.fn());
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(fetchMock).toHaveBeenCalledWith("http://localhost:8000/api/task/task-1/confirm", expect.objectContaining({ method: "POST" }));
  });
});
