import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cdpKeySequence,
  clearExtensionSessions,
  executeAction,
  handleTabRemoved,
  isTakeoverActive,
  setTakeoverActive,
  extensionSessions,
  getOrCreateActiveTab,
  initSession,
  isBlockedUrl,
  isConsoleUrl,
  isDrivableTabUrl,
  isPageSettled,
  observe,
  resolveSessionTab,
  withAgent,
} from "../src/actionExecutor";
import type { BrowserAction } from "../src/contracts";

describe("action executor URL guards", () => {
  it("blocks privileged and scriptable schemes", () => {
    for (const url of [
      "chrome://extensions",
      "chrome-extension://abc/popup.html",
      "chrome-search://local-ntp",
      "devtools://devtools/bundled/inspector.html",
      "view-source:https://example.com",
      "edge://settings",
      "about:blank",
      "file:///C:/Windows/win.ini",
      "data:text/html,<script>alert(1)</script>",
      "blob:https://example.com/uuid",
      "javascript:alert(1)",
      "vbscript:msgbox(1)",
      "filesystem:https://example.com/temporary/x",
      "mailto:a@b.com",
      "",
    ]) {
      expect(isBlockedUrl(url), url).toBe(true);
    }
  });

  it("allows ordinary http(s) pages", () => {
    expect(isBlockedUrl("https://example.com/a?b=1")).toBe(false);
    expect(isBlockedUrl("http://shop.local/checkout")).toBe(false);
  });

  it("recognizes the local console so the agent never drives itself", () => {
    expect(isConsoleUrl("http://localhost:5173/agent.html")).toBe(true);
    expect(isConsoleUrl("http://127.0.0.1:4173/")).toBe(true);
    expect(isConsoleUrl("http://localhost:8000/docs")).toBe(false);
    expect(isConsoleUrl("https://localhost:5173/")).toBe(true);
    expect(isConsoleUrl("https://example.com/")).toBe(false);
    expect(isConsoleUrl("not a url")).toBe(false);
  });
});

describe("native CDP key sequences", () => {
  it("pairs a rawKeyDown with a keyUp for Enter", () => {
    const events = cdpKeySequence("enter");

    expect(events).toHaveLength(2);
    expect(events[0]).toMatchObject({
      type: "rawKeyDown", key: "Enter", code: "Enter",
      windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13, modifiers: 0,
    });
    expect(events[1]).toMatchObject({
      type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13,
    });
  });

  it("uses the control modifier for select-all on Windows/Linux", () => {
    const events = cdpKeySequence("selectAll", false);

    expect(events[0]).toMatchObject({
      type: "rawKeyDown", key: "a", code: "KeyA",
      windowsVirtualKeyCode: 65, modifiers: 2,
    });
    expect(events[1]).toMatchObject({ type: "keyUp", modifiers: 2 });
  });

  it("uses the meta modifier for select-all on macOS", () => {
    expect(cdpKeySequence("selectAll", true)[0]).toMatchObject({ modifiers: 4 });
  });

  it("clears with Backspace through the native pipeline", () => {
    const events = cdpKeySequence("backspace");

    expect(events[0]).toMatchObject({
      type: "rawKeyDown", key: "Backspace", code: "Backspace",
      windowsVirtualKeyCode: 8, modifiers: 0,
    });
    expect(events[1]).toMatchObject({ type: "keyUp", key: "Backspace" });
  });
});

describe("page settlement", () => {
  it("accepts an interactive page immediately", () => {
    expect(isPageSettled({ ready: true, interactiveCount: 3, isConnecting: false })).toBe(true);
  });
  it("keeps waiting while the document is loading or WhatsApp is connecting", () => {
    expect(isPageSettled({ ready: false, interactiveCount: 9, isConnecting: false })).toBe(false);
    expect(isPageSettled({ ready: true, interactiveCount: 9, isConnecting: true })).toBe(false);
  });

  it("exits a low-element page after two stable samples", () => {
    const one = { ready: true, interactiveCount: 1, isConnecting: false };
    expect(isPageSettled(one, 1, 1)).toBe(false);
    expect(isPageSettled(one, 1, 2)).toBe(true);
  });

  it("does not make an empty page wait for the old 3.5s SPA timer", () => {
    const empty = { ready: true, interactiveCount: 0, isConnecting: false };
    expect(isPageSettled(empty, 0, 4)).toBe(false);
    expect(isPageSettled(empty, 0, 5)).toBe(true);
  });
});

type TabLike = { id: number; url: string; lastAccessed?: number };

function installChrome(tabs: TabLike[]) {
  const create = vi.fn(async (props: chrome.tabs.CreateProperties) => ({ id: 99, url: props.url }));
  const query = vi.fn(async (q: chrome.tabs.QueryInfo) => (q.active ? tabs.slice(0, 1) : tabs));
  vi.stubGlobal("chrome", { tabs: { query, create } });
  return { create, query };
}

afterEach(() => {
  clearExtensionSessions();
  vi.unstubAllGlobals();
});

describe("drivable tab URLs", () => {
  it("accepts a fresh about:blank tab as a starting point", () => {
    expect(isDrivableTabUrl("about:blank")).toBe(true);
    expect(isDrivableTabUrl("")).toBe(true);
    expect(isDrivableTabUrl("https://web.whatsapp.com/")).toBe(true);
  });

  it("still refuses privileged pages, the console, and scriptable schemes", () => {
    for (const url of [
      "chrome://newtab",
      "chrome-extension://abc/popup.html",
      "devtools://devtools/bundled/inspector.html",
      "http://localhost:5173/agent.html",
      "data:text/html,<script>alert(1)</script>",
      "javascript:alert(1)",
    ]) {
      expect(isDrivableTabUrl(url), url).toBe(false);
    }
  });

  it("keeps about:blank invalid as a navigation target", () => {
    expect(isBlockedUrl("about:blank")).toBe(true);
  });
});

describe("tab lifecycle", () => {
  it("creates a tab when the browser only has privileged pages", async () => {
    const { create } = installChrome([
      { id: 1, url: "chrome://newtab" },
      { id: 2, url: "http://localhost:5173/agent.html" },
      { id: 3, url: "chrome-extension://abc/popup.html" },
    ]);

    await expect(getOrCreateActiveTab()).resolves.toBe(99);
    expect(create).toHaveBeenCalledWith({ url: "about:blank", active: true });
  });

  it("creates a tab when no tab exists at all", async () => {
    const { create } = installChrome([]);
    await expect(getOrCreateActiveTab()).resolves.toBe(99);
    expect(create).toHaveBeenCalledWith({ url: "about:blank", active: true });
  });

  it("reuses an existing about:blank tab instead of creating one per action", async () => {
    const { create } = installChrome([{ id: 7, url: "about:blank" }]);
    await expect(getOrCreateActiveTab()).resolves.toBe(7);
    expect(create).not.toHaveBeenCalled();
  });

  it("prefers the active ordinary tab", async () => {
    const { create } = installChrome([
      { id: 5, url: "https://example.com/" },
      { id: 6, url: "https://other.example/" },
    ]);
    await expect(getOrCreateActiveTab()).resolves.toBe(5);
    expect(create).not.toHaveBeenCalled();
  });
});

describe("observation on unscriptable tabs", () => {
  it("returns a metadata-only observation for about:blank", async () => {
    const executeScript = vi.fn();
    vi.stubGlobal("chrome", {
      tabs: {
        get: vi.fn(async (id: number) => ({ id, url: "about:blank", title: "", windowId: 1 })),
        update: vi.fn(async () => ({})),
      },
      scripting: { executeScript },
    });

    const obs = await observe(7);

    expect(obs.tabId).toBe(7);
    expect(obs.url).toBe("about:blank");
    expect(obs.interactive).toEqual([]);
    // No injection attempt at all: Chrome would reject it and kill the mission.
    expect(executeScript).not.toHaveBeenCalled();
  });

  it("degrades to metadata when injection is refused on a scriptable URL", async () => {
    vi.stubGlobal("chrome", {
      tabs: {
        get: vi.fn(async (id: number) => ({ id, url: "https://example.com/", title: "Example", windowId: 1 })),
        update: vi.fn(async () => ({})),
      },
      scripting: {
        executeScript: vi.fn(async () => {
          throw new Error('Cannot access contents of url "https://example.com/".');
        }),
      },
    });

    const obs = await observe(9);

    expect(obs.url).toBe("https://example.com/");
    expect(obs.title).toBe("Example");
    expect(obs.interactive).toEqual([]);
  });

  it("observe still succeeds when no tab exists, using a fresh blank tab", async () => {
    vi.stubGlobal("chrome", {
      tabs: {
        query: vi.fn(async (q: chrome.tabs.QueryInfo) => (q.active ? [] : [])),
        create: vi.fn(async () => ({ id: 99, url: "about:blank" })),
        get: vi.fn(async (id: number) => ({ id, url: "about:blank", title: "", windowId: 1 })),
        update: vi.fn(async () => ({})),
      },
      scripting: { executeScript: vi.fn() },
    });

    const action = {
      type: "observe", risk: "read", verify: "fresh observation", timeoutMs: 5000,
    } as BrowserAction;

    const result = await executeAction(action, "task-1");

    expect(result.ok).toBe(true);
    expect(result.observation?.url).toBe("about:blank");
    expect(result.observation?.interactive).toEqual([]);
  });
});

describe("withAgent argument serialization regression tests", () => {
  it("serializes all arguments when typing with undefined clear and name", async () => {
    let capturedArgs: unknown[] | undefined;
    const executeScript = vi.fn(async (injection: { args?: unknown[] }) => {
      if (injection.args) {
        capturedArgs = injection.args;
        for (let i = 0; i < injection.args.length; i++) {
          if (injection.args[i] === undefined) {
            throw new Error(`Error at parameter 'injection': Error at property 'args': Error at index [${i}]: Value is unserializable.`);
          }
        }
      }
      return [{ result: { ok: true } }];
    });

    vi.stubGlobal("chrome", {
      scripting: { executeScript },
    });

    const agent = await withAgent(1);
    const result = await agent.type("input-1", "hello", undefined, undefined);

    expect(result.ok).toBe(true);
    expect(capturedArgs).toBeDefined();
    expect(capturedArgs).toEqual(["input-1", "hello", true, ""]);
    expect(capturedArgs?.every(arg => arg !== undefined)).toBe(true);
  });

  it("preserves clear=false without converting to undefined or true", async () => {
    let capturedArgs: unknown[] | undefined;
    const executeScript = vi.fn(async (injection: { args?: unknown[] }) => {
      if (injection.args) {
        capturedArgs = injection.args;
      }
      return [{ result: { ok: true } }];
    });

    vi.stubGlobal("chrome", {
      scripting: { executeScript },
    });

    const agent = await withAgent(1);
    await agent.type("input-1", "hello", false, "test-name");

    expect(capturedArgs).toEqual(["input-1", "hello", false, "test-name"]);
  });

  it("ensures click, getCoords, clickPoint, submit, and scroll never pass undefined arguments", async () => {
    const executedInjections: Array<{ args?: unknown[] }> = [];
    const executeScript = vi.fn(async (injection: { args?: unknown[] }) => {
      executedInjections.push(injection);
      if (injection.args) {
        for (let i = 0; i < injection.args.length; i++) {
          if (injection.args[i] === undefined) {
            throw new Error(`Error at parameter 'injection': Error at property 'args': Error at index [${i}]: Value is unserializable.`);
          }
        }
      }
      return [{ result: { ok: true, x: 10, y: 10, width: 20, height: 20 } }];
    });

    vi.stubGlobal("chrome", {
      scripting: { executeScript },
    });

    const agent = await withAgent(1);
    await agent.getCoords("el-1", undefined);
    await agent.clickPoint("el-1", undefined);
    await agent.click("el-1", undefined);
    await agent.submit("el-1", undefined);
    await agent.scroll("down", undefined);

    for (const inj of executedInjections) {
      if (inj.args) {
        expect(inj.args.every(a => a !== undefined)).toBe(true);
      }
    }
  });

  it("executeAction type fallback succeeds when CDP typing is unavailable", async () => {
    let capturedArgs: unknown[] | undefined;
    const executeScript = vi.fn(async (injection: { args?: unknown[]; files?: string[]; func?: Function }) => {
      if (injection.args) {
        for (let i = 0; i < injection.args.length; i++) {
          if (injection.args[i] === undefined) {
            throw new Error(`Error at parameter 'injection': Error at property 'args': Error at index [${i}]: Value is unserializable.`);
          }
        }
      }
      if (injection.files) {
        return [{ result: true }];
      }
      if (injection.args && injection.args.length === 4) {
        capturedArgs = injection.args;
        return [{ result: { ok: true } }];
      }
      const fnStr = injection.func?.toString() || "";
      if (fnStr.includes("Boolean")) {
        return [{ result: true }];
      }
      if (fnStr.includes("list")) {
        return [{
          result: [
            { targetId: "input-1", role: "textbox", name: "Search", tag: "input", enabled: true, visible: true },
          ],
        }];
      }
      if (fnStr.includes("getCoords")) {
        return [{ result: null }];
      }
      if (fnStr.includes("document.readyState")) {
        return [{ result: { ready: true, interactiveCount: 1, isConnecting: false } }];
      }
      if (fnStr.includes("devicePixelRatio")) {
        return [{ result: { y: 0, w: 1000, h: 800, dpr: 1 } }];
      }
      if (fnStr.includes("readValue")) {
        return [{ result: { ok: true, value: "test search", editable: true } }];
      }
      return [{ result: { ok: true } }];
    });

    vi.stubGlobal("chrome", {
      tabs: {
        query: vi.fn(async () => [{ id: 1, url: "https://example.com/search", active: true }]),
        get: vi.fn(async (id: number) => ({ id, url: "https://example.com/search", title: "Search", windowId: 1 })),
        update: vi.fn(async () => ({})),
        captureVisibleTab: vi.fn(async () => "data:image/jpeg;base64,mock"),
      },
      scripting: { executeScript },
      debugger: {
        attach: vi.fn(async () => { throw new Error("CDP attach failed"); }),
        detach: vi.fn(async () => ({})),
        sendCommand: vi.fn(async () => { throw new Error("CDP unavailable"); }),
      },
    });

    const action: BrowserAction = {
      type: "type",
      target: { targetId: "input-1", name: "Search" },
      params: { text: "test search", submit: false },
      risk: "write",
      verify: "typed",
      timeoutMs: 5000,
    };

    const result = await executeAction(action, "task-type-1");
    expect(result.ok).toBe(true);
    expect(capturedArgs).toBeDefined();
    expect(capturedArgs).toEqual(["input-1", "test search", true, "Search"]);
    expect(capturedArgs?.every(arg => arg !== undefined)).toBe(true);
  });

  it("clickCoords fails closed on missing or invalid coordinates without defaulting to (0,0)", async () => {
    const executeScript = vi.fn();
    vi.stubGlobal("chrome", {
      scripting: { executeScript },
    });

    const agent = await withAgent(1);
    executeScript.mockClear();

    // Missing / undefined coordinates
    const r1 = await agent.clickCoords(undefined as unknown as number, 100);
    expect(r1).toEqual({ ok: false, error: "invalid coordinates" });

    const r2 = await agent.clickCoords(100, undefined as unknown as number);
    expect(r2).toEqual({ ok: false, error: "invalid coordinates" });

    // NaN / Infinity coordinates
    const r3 = await agent.clickCoords(Number.NaN, 100);
    expect(r3).toEqual({ ok: false, error: "invalid coordinates" });

    const r4 = await agent.clickCoords(100, Number.POSITIVE_INFINITY);
    expect(r4).toEqual({ ok: false, error: "invalid coordinates" });

    // None of these should have triggered an executeScript click at (0, 0)
    expect(executeScript).not.toHaveBeenCalled();

    // Valid coordinates execute properly
    executeScript.mockResolvedValueOnce([{ result: { ok: true, targetId: "target-1" } }]);
    const r5 = await agent.clickCoords(150, 250);
    expect(r5.ok).toBe(true);
    expect(executeScript).toHaveBeenCalledWith(
      expect.objectContaining({
        args: [150, 250],
      })
    );
  });

  it("detects newly opened tab during click and returns new tab observation", async () => {
    let activeTabId = 1;
    const tabs = [
      { id: 1, url: "https://www.amazon.com/s?k=laptop", title: "Amazon Search", active: true, windowId: 10 },
    ];
    const newTab = { id: 2, url: "https://www.amazon.com/dp/B0EXAMPLE", title: "Laptop Product", active: false, windowId: 10, openerTabId: 1 };

    const executeScript = vi.fn(async (injection: any) => {
      // Simulate click opening a new tab in background
      if (injection.func?.toString().includes("clickPoint")) {
        return [{ result: { x: 50, y: 50 } }];
      }
      if (injection.func?.toString().includes("click")) {
        tabs.push(newTab);
        return [{ result: { ok: true } }];
      }
      if (injection.func?.toString().includes("list")) {
        return [{
          result: [{
            targetId: "nav-el-1",
            role: "link",
            name: "Acer Laptop",
            tag: "a",
            cssSelector: "#link1",
            enabled: true,
            visible: true,
            bounds: { x: 10, y: 10, width: 100, height: 30 },
          }],
        }];
      }
      if (injection.func?.toString().includes("extractText")) {
        return [{ result: "### Acer Laptop\n\n16GB RAM, 512GB SSD" }];
      }
      return [{ result: true }];
    });

    vi.stubGlobal("chrome", {
      debugger: {
        attach: vi.fn().mockRejectedValue(new Error("CDP unavailable in test")),
        detach: vi.fn(),
      },
      tabs: {
        query: vi.fn(async (q: any) => {
          if (q.active && q.currentWindow) return tabs.filter(t => t.id === activeTabId);
          return [...tabs];
        }),
        get: vi.fn(async (id: number) => {
          const found = tabs.find(t => t.id === id);
          if (!found) throw new Error("Tab not found");
          return found;
        }),
        update: vi.fn(async (id: number, props: any) => {
          if (props.active) {
            activeTabId = id;
            tabs.forEach(t => (t.active = t.id === id));
          }
          return {};
        }),
      },
      scripting: { executeScript },
    });

    const action: BrowserAction = {
      type: "click",
      risk: "write",
      verify: "navigated to product page",
      timeoutMs: 1000,
      target: { targetId: "nav-el-1", name: "Acer Laptop" },
    };

    const res = await executeAction(action, "task-click-newtab");
    expect(res.ok).toBe(true);
    expect(res.evidence).toContain("opened new tab 2");
    expect(res.observation?.tabId).toBe(2);
    expect(res.observation?.pageText).toContain("Acer Laptop");
    expect(activeTabId).toBe(2);
  });

  it("fails with NAVIGATION_FAILED when expected navigation fails to occur", async () => {
    const tabs = [
      { id: 1, url: "https://en.wikipedia.org/w/index.php?search=neural", title: "Search", active: true, windowId: 10 },
    ];

    const executeScript = vi.fn(async (injection: any) => {
      if (injection.func?.toString().includes("list")) {
        return [{
          result: [{
            targetId: "nav-el-1",
            role: "link",
            name: "Neural network",
            tag: "a",
            cssSelector: "#link1",
            enabled: true,
            visible: true,
            bounds: { x: 10, y: 10, width: 100, height: 30 },
          }],
        }];
      }
      if (injection.func?.toString().includes("clickPoint")) {
        return [{ result: null }];
      }
      if (injection.func?.toString().includes("click")) {
        // In-page click succeeded, but page URL did NOT change
        return [{ result: { ok: true } }];
      }
      return [{ result: true }];
    });

    vi.stubGlobal("chrome", {
      debugger: {
        attach: vi.fn().mockRejectedValue(new Error("CDP unavailable")),
        detach: vi.fn(),
      },
      tabs: {
        query: vi.fn(async () => [...tabs]),
        get: vi.fn(async (id: number) => tabs[0]),
        update: vi.fn(async () => ({})),
      },
      scripting: { executeScript },
    });

    const action: BrowserAction = {
      type: "click",
      risk: "write",
      verify: "navigated to article page",
      timeoutMs: 500,
      target: { targetId: "nav-el-1", name: "Neural network" },
    };

    const res = await executeAction(action, "task-click-nav-fail");
    expect(res.ok).toBe(false);
    expect(res.error?.code).toBe("NAVIGATION_FAILED");
    expect(res.error?.message).toContain("expected navigation did not occur");
  });
});

describe("browser session management and tab identity pinning", () => {
  it("initSession creates a fresh dedicated tab by default", async () => {
    const create = vi.fn(async () => ({ id: 55, url: "about:blank" }));
    vi.stubGlobal("chrome", { tabs: { create } });

    const res = await initSession({ sessionId: "sess-1" });
    expect(res.ok).toBe(true);
    expect(res.primaryTabId).toBe(55);
    expect(extensionSessions.get("sess-1")?.primaryTabId).toBe(55);
    expect(create).toHaveBeenCalledWith({ url: "about:blank", active: true });
  });

  it("initSession resolves matching existing user tab when requested", async () => {
    const tabs = [
      { id: 10, url: "https://www.amazon.in/dp/B000", title: "Product" },
      { id: 20, url: "https://www.google.com", title: "Google" },
    ];
    const query = vi.fn(async () => [...tabs]);
    vi.stubGlobal("chrome", { tabs: { query } });

    const res = await initSession({
      sessionId: "sess-2",
      useExistingTab: true,
      tabHint: "amazon",
    });
    expect(res.ok).toBe(true);
    expect(res.primaryTabId).toBe(10);
    expect(extensionSessions.get("sess-2")?.primaryTabId).toBe(10);
  });

  it("initSession fails closed with AMBIGUOUS_TAB when multiple tabs match hint", async () => {
    const tabs = [
      { id: 10, url: "https://www.amazon.in/dp/B000", title: "Product 1" },
      { id: 11, url: "https://www.amazon.in/dp/B001", title: "Product 2" },
    ];
    const query = vi.fn(async () => [...tabs]);
    vi.stubGlobal("chrome", { tabs: { query } });

    const res = await initSession({
      sessionId: "sess-3",
      useExistingTab: true,
      tabHint: "amazon",
    });
    expect(res.ok).toBe(false);
    expect(res.error?.code).toBe("AMBIGUOUS_TAB");
  });

  it("initSession fails closed with TAB_UNAVAILABLE when no tabs match hint", async () => {
    const tabs = [{ id: 20, url: "https://www.google.com", title: "Google" }];
    const query = vi.fn(async () => [...tabs]);
    vi.stubGlobal("chrome", { tabs: { query } });

    const res = await initSession({
      sessionId: "sess-4",
      useExistingTab: true,
      tabHint: "wikipedia",
    });
    expect(res.ok).toBe(false);
    expect(res.error?.code).toBe("TAB_UNAVAILABLE");
  });

  it("resolveSessionTab rejects unauthorized foreign tab with TAB_UNAUTHORIZED", async () => {
    const create = vi.fn(async () => ({ id: 50, url: "about:blank" }));
    vi.stubGlobal("chrome", { tabs: { create, get: vi.fn(async (id: number) => ({ id, url: "https://example.com" })) } });

    await initSession({ sessionId: "sess-5" });

    const action: BrowserAction = {
      type: "navigate",
      risk: "write",
      verify: "url changed",
      timeoutMs: 1000,
      sessionId: "sess-5",
      tabId: 999, // Unauthorized tab!
      params: { url: "https://example.com" },
    };

    const res = await resolveSessionTab(action);
    expect("ok" in res && res.ok).toBe(false);
    expect("error" in res && res.error?.code).toBe("TAB_UNAUTHORIZED");
  });

  it("resolveSessionTab reports TAB_UNAVAILABLE when pinned tab is closed", async () => {
    const create = vi.fn(async () => ({ id: 60, url: "about:blank" }));
    const get = vi.fn(async () => null); // Tab closed!
    vi.stubGlobal("chrome", { tabs: { create, get } });

    await initSession({ sessionId: "sess-6" });

    const action: BrowserAction = {
      type: "navigate",
      risk: "write",
      verify: "url changed",
      timeoutMs: 1000,
      sessionId: "sess-6",
      params: { url: "https://example.com" },
    };

    const res = await resolveSessionTab(action);
    expect("ok" in res && res.ok).toBe(false);
    expect("error" in res && res.error?.code).toBe("TAB_UNAVAILABLE");
  });

  it("resolveSessionTab permits registered child tabs", async () => {
    const create = vi.fn(async () => ({ id: 70, url: "about:blank" }));
    const get = vi.fn(async (id: number) => ({ id, url: "https://example.com" }));
    vi.stubGlobal("chrome", { tabs: { create, get } });

    await initSession({ sessionId: "sess-7" });
    const sess = extensionSessions.get("sess-7");
    sess?.childTabIds.add(75); // Register child tab

    const action: BrowserAction = {
      type: "navigate",
      risk: "write",
      verify: "url changed",
      timeoutMs: 1000,
      sessionId: "sess-7",
      tabId: 75,
      params: { url: "https://example.com/item" },
    };

    const res = await resolveSessionTab(action);
    expect("tabId" in res && res.tabId).toBe(75);
  });
});

describe("takeover mode (Task 4)", () => {
  afterEach(() => {
    setTakeoverActive(false);
  });

  it("refuses every action while takeover is active", async () => {
    setTakeoverActive(true);
    expect(isTakeoverActive()).toBe(true);
    const result = await executeAction(
      { type: "observe", risk: "read", verify: "fresh observation", timeoutMs: 5000 } as BrowserAction,
      "task-takeover",
    );
    expect(result.ok).toBe(false);
    expect(result.error?.code).toBe("TAKEOVER_ACTIVE");
    expect(result.error?.recoverable).toBe(true);
  });

  it("observe returns an empty observation while takeover is active", async () => {
    const executeScript = vi.fn(async () => {
      throw new Error("must not be called during takeover");
    });
    vi.stubGlobal("chrome", {
      tabs: {
        get: vi.fn(async (id: number) => ({ id, url: "https://example.com/login", title: "Sign in", windowId: 1 })),
        update: vi.fn(async () => ({})),
      },
      scripting: { executeScript },
    });
    setTakeoverActive(true);
    const obs = await observe(11);
    expect(obs.url).toBe("https://example.com/login");
    expect(obs.interactive).toEqual([]);
    expect(obs.pageText).toBeUndefined();
    expect(obs.screenshot).toBeUndefined();
    expect(executeScript).not.toHaveBeenCalled();
  });

  it("restores normal execution after TAKEOVER_END", async () => {
    setTakeoverActive(true);
    setTakeoverActive(false);
    expect(isTakeoverActive()).toBe(false);
    vi.stubGlobal("chrome", {
      tabs: {
        query: vi.fn(async () => []),
        create: vi.fn(async () => ({ id: 99, url: "about:blank" })),
        get: vi.fn(async (id: number) => ({ id, url: "about:blank", title: "", windowId: 1 })),
        update: vi.fn(async () => ({})),
      },
      scripting: { executeScript: vi.fn() },
    });
    const result = await executeAction(
      { type: "observe", risk: "read", verify: "fresh observation", timeoutMs: 5000 } as BrowserAction,
      "task-takeover",
    );
    expect(result.ok).toBe(true);
  });
});

describe("switch_tab (Task 9)", () => {
  function installSwitchChrome() {
    const update = vi.fn(async () => ({}));
    const executeScript = vi.fn();
    vi.stubGlobal("chrome", {
      tabs: {
        create: vi.fn(async () => ({ id: 50, url: "about:blank" })),
        get: vi.fn(async (id: number) => ({ id, url: "about:blank", title: "", windowId: 1, status: "complete" })),
        update,
        query: vi.fn(async () => []),
      },
      scripting: { executeScript },
    });
    return { update, executeScript };
  }

  it("activates a child tab and moves the session's active marker", async () => {
    const { update, executeScript } = installSwitchChrome();
    await initSession({ sessionId: "sess-sw-1" });
    extensionSessions.get("sess-sw-1")?.childTabIds.add(51);

    const result = await executeAction(
      { type: "switch_tab", risk: "read", verify: "switched", timeoutMs: 200, sessionId: "sess-sw-1", params: { tabId: 51 } } as BrowserAction,
      "task-sw-1",
    );

    expect(result.ok).toBe(true);
    expect(update).toHaveBeenCalledWith(51, { active: true });
    expect(extensionSessions.get("sess-sw-1")?.activeTabId).toBe(51);
    // The observation that comes back belongs to the switched-to tab.
    expect(result.observation?.tabId).toBe(51);
    // about:blank is observed metadata-only; no injection is attempted.
    expect(executeScript).not.toHaveBeenCalled();
  });

  it("refuses a tab outside the session with TAB_UNAUTHORIZED", async () => {
    const { update } = installSwitchChrome();
    await initSession({ sessionId: "sess-sw-2" });

    const result = await executeAction(
      { type: "switch_tab", risk: "read", verify: "switched", timeoutMs: 200, sessionId: "sess-sw-2", params: { tabId: 999 } } as BrowserAction,
      "task-sw-2",
    );

    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error?.code).toBe("TAB_UNAUTHORIZED");
    expect(update).not.toHaveBeenCalled();
  });

  it("switches without a session (no authorization scope to check)", async () => {
    const { update } = installSwitchChrome();

    const result = await executeAction(
      { type: "switch_tab", risk: "read", verify: "switched", timeoutMs: 200, params: { tabId: 7 } } as BrowserAction,
      "task-sw-3",
    );

    expect(result.ok).toBe(true);
    expect(update).toHaveBeenCalledWith(7, { active: true });
    expect(result.observation?.tabId).toBe(7);
  });

  it("select_tab behaves identically to switch_tab", async () => {
    const { update } = installSwitchChrome();
    await initSession({ sessionId: "sess-sw-4" });
    extensionSessions.get("sess-sw-4")?.childTabIds.add(52);

    const result = await executeAction(
      { type: "select_tab", risk: "read", verify: "switched", timeoutMs: 200, sessionId: "sess-sw-4", params: { tabId: 52 } } as BrowserAction,
      "task-sw-4",
    );

    expect(result.ok).toBe(true);
    expect(update).toHaveBeenCalledWith(52, { active: true });
    expect(extensionSessions.get("sess-sw-4")?.activeTabId).toBe(52);
  });
});

describe("type read-back errors never embed field values", () => {
  it("a mismatched read-back reports the failure without the typed text or the DOM value", async () => {
    const executeScript = vi.fn(async (injection: { args?: unknown[]; files?: string[]; func?: Function }) => {
      if (injection.files) return [{ result: true }];
      if (injection.args && injection.args.length === 4) {
        return [{ result: { ok: true } }];
      }
      const fnStr = injection.func?.toString() || "";
      if (fnStr.includes("Boolean")) return [{ result: true }];
      if (fnStr.includes("list")) {
        return [{
          result: [
            { targetId: "input-1", role: "textbox", name: "Password", tag: "input", enabled: true, visible: true },
          ],
        }];
      }
      if (fnStr.includes("getCoords")) return [{ result: null }];
      if (fnStr.includes("document.readyState")) {
        return [{ result: { ready: true, interactiveCount: 1, isConnecting: false } }];
      }
      if (fnStr.includes("devicePixelRatio")) {
        return [{ result: { y: 0, w: 1000, h: 800, dpr: 1 } }];
      }
      if (fnStr.includes("readValue")) {
        return [{ result: { ok: true, value: "wrong-value-in-dom", editable: true } }];
      }
      return [{ result: { ok: true } }];
    });

    vi.stubGlobal("chrome", {
      tabs: {
        query: vi.fn(async () => [{ id: 1, url: "https://example.com/login", active: true }]),
        get: vi.fn(async (id: number) => ({ id, url: "https://example.com/login", title: "Login", windowId: 1 })),
        update: vi.fn(async () => ({})),
        captureVisibleTab: vi.fn(async () => "data:image/jpeg;base64,mock"),
      },
      scripting: { executeScript },
      debugger: {
        attach: vi.fn(async () => { throw new Error("CDP attach failed"); }),
        detach: vi.fn(async () => ({})),
        sendCommand: vi.fn(async () => { throw new Error("CDP unavailable"); }),
      },
    });

    const action: BrowserAction = {
      type: "type",
      target: { targetId: "input-1", name: "Password" },
      params: { text: "hunter2-secret", submit: false },
      risk: "write",
      verify: "typed",
      timeoutMs: 5000,
    };

    const result = await executeAction(action, "task-type-secret");
    expect(result.ok).toBe(false);
    expect(result.error?.code).toBe("STALE_TARGET");
    expect(result.error?.message ?? "").not.toContain("hunter2-secret");
    expect(result.error?.message ?? "").not.toContain("wrong-value-in-dom");
  });
});

describe("in-page type fallback verifies its own read-back", () => {
  it("a fallback search submit with no observable outcome fails closed instead of claiming a state change", async () => {
    const executeScript = vi.fn(async (injection: { args?: unknown[]; files?: string[]; func?: Function }) => {
      if (injection.args) {
        for (let i = 0; i < injection.args.length; i++) {
          if (injection.args[i] === undefined) {
            throw new Error(`Error at parameter 'injection': Error at index [${i}]: Value is unserializable.`);
          }
        }
      }
      if (injection.files) return [{ result: true }];
      if (injection.args && injection.args.length === 4) {
        return [{ result: { ok: true } }];
      }
      const fnStr = injection.func?.toString() || "";
      if (fnStr.includes("Boolean")) return [{ result: true }];
      if (fnStr.includes("list")) {
        return [{
          result: [
            { targetId: "input-1", role: "textbox", name: "Search", tag: "input", enabled: true, visible: true },
          ],
        }];
      }
      if (fnStr.includes("getCoords")) return [{ result: null }];
      if (fnStr.includes("document.readyState")) {
        return [{ result: { ready: true, interactiveCount: 1, isConnecting: false } }];
      }
      if (fnStr.includes("devicePixelRatio")) {
        return [{ result: { y: 0, w: 1000, h: 800, dpr: 1 } }];
      }
      if (fnStr.includes("readValue")) {
        return [{ result: { ok: true, value: "test search", editable: true } }];
      }
      return [{ result: { ok: true } }];
    });

    vi.stubGlobal("chrome", {
      tabs: {
        query: vi.fn(async () => [{ id: 1, url: "https://example.com/search", active: true }]),
        get: vi.fn(async (id: number) => ({ id, url: "https://example.com/search", title: "Search", windowId: 1 })),
        update: vi.fn(async () => ({})),
        captureVisibleTab: vi.fn(async () => "data:image/jpeg;base64,mock"),
      },
      scripting: { executeScript },
      debugger: {
        attach: vi.fn(async () => { throw new Error("CDP attach failed"); }),
        detach: vi.fn(async () => ({})),
        sendCommand: vi.fn(async () => { throw new Error("CDP unavailable"); }),
      },
    });

    const action: BrowserAction = {
      type: "type",
      target: { targetId: "input-1", name: "Search" },
      params: { text: "test search", submit: true },
      risk: "write",
      verify: "submitted search",
      timeoutMs: 1000,
    };

    const result = await executeAction(action, "task-type-search");
    expect(result.ok).toBe(false);
    expect(result.error?.code).toBe("NAVIGATION_FAILED");
  });
});

describe("doClick duplicate-tab fallback", () => {
  function clickHarness(linkTargetValue: string, href: string, pageUrl: string, linkName: string) {
    let activeTabId = 1;
    const tabs: Array<{ id: number; url: string; title: string; active: boolean; windowId: number; openerTabId?: number }> = [
      { id: 1, url: pageUrl, title: "Page", active: true, windowId: 10 },
    ];
    const create = vi.fn(async (props: { url: string }) => {
      const tab = { id: 2, url: props.url, title: "Created", active: true, windowId: 10, openerTabId: 1 };
      tabs.push(tab);
      return tab;
    });
    const executeScript = vi.fn(async (injection: { args?: unknown[]; files?: string[]; func?: Function }) => {
      if (injection.func?.toString().includes("linkTarget")) {
        return [{ result: linkTargetValue }];
      }
      if (injection.func?.toString().includes("clickPoint")) {
        return [{ result: { x: 50, y: 50 } }];
      }
      if (injection.func?.toString().includes("click")) {
        return [{ result: { ok: true } }];
      }
      if (injection.func?.toString().includes("list")) {
        return [{
          result: [{
            targetId: "nav-el-1", role: "link", name: linkName, tag: "a", href,
            enabled: true, visible: true, bounds: { x: 10, y: 10, width: 100, height: 30 },
          }],
        }];
      }
      if (injection.func?.toString().includes("getCoords")) return [{ result: null }];
      if (injection.func?.toString().includes("document.readyState")) {
        return [{ result: { ready: true, interactiveCount: 1, isConnecting: false } }];
      }
      if (injection.func?.toString().includes("devicePixelRatio")) {
        return [{ result: { y: 0, w: 1000, h: 800, dpr: 1 } }];
      }
      if (injection.func?.toString().includes("extractText")) {
        return [{ result: "### Page\n\nSome text" }];
      }
      return [{ result: true }];
    });
    vi.stubGlobal("chrome", {
      debugger: {
        attach: vi.fn().mockRejectedValue(new Error("CDP unavailable in test")),
        detach: vi.fn(),
      },
      tabs: {
        query: vi.fn(async (q: { active?: boolean; currentWindow?: boolean }) => {
          if (q.active && q.currentWindow) return tabs.filter(t => t.id === activeTabId);
          return [...tabs];
        }),
        get: vi.fn(async (id: number) => {
          const found = tabs.find(t => t.id === id);
          if (!found) throw new Error("Tab not found");
          return found;
        }),
        update: vi.fn(async (id: number, props: { active?: boolean }) => {
          if (props.active) {
            activeTabId = id;
            tabs.forEach(t => { t.active = t.id === id; });
          }
          return {};
        }),
        create,
        captureVisibleTab: vi.fn(async () => "data:image/jpeg;base64,mock"),
      },
      scripting: { executeScript },
    });
    return { create, tabs };
  }

  it("clicking a same-page anchor never opens a duplicate tab", async () => {
    const { create } = clickHarness("", "https://example.com/product#reviews", "https://example.com/product", "Reviews");
    const action: BrowserAction = {
      type: "click", risk: "write", verify: "clicked Reviews", timeoutMs: 1000,
      target: { targetId: "nav-el-1", name: "Reviews" },
    };
    const res = await executeAction(action, "task-click-anchor");
    expect(res.ok).toBe(true);
    expect(create).not.toHaveBeenCalled();
    expect(res.evidence ?? "").toContain("no navigation detected");
  });

  it("a blocked _blank link to another page is still rescued with an explicit tab", async () => {
    const { create } = clickHarness("_blank", "https://example.com/product", "https://example.com/search", "Product");
    const action: BrowserAction = {
      type: "click", risk: "write", verify: "clicked product", timeoutMs: 1000,
      target: { targetId: "nav-el-1", name: "Product" },
    };
    const res = await executeAction(action, "task-click-blank");
    expect(res.ok).toBe(true);
    expect(create).toHaveBeenCalledTimes(1);
    expect(create).toHaveBeenCalledWith(expect.objectContaining({ url: "https://example.com/product" }));
    expect(res.evidence ?? "").toContain("opened new tab 2");
  });
});

describe("doType protects a target inside a modal", () => {
  it("passes the resolved target id to the modal dismiss check", async () => {
    let capturedDismissArgs: unknown[] | undefined;
    const executeScript = vi.fn(async (injection: { args?: unknown[]; files?: string[]; func?: Function }) => {
      const fnStr = injection.func?.toString() || "";
      if (fnStr.includes("dismissModalInPage")) {
        capturedDismissArgs = injection.args;
        return [{ result: false }];
      }
      if (injection.files) return [{ result: true }];
      if (injection.args && injection.args.length === 4) {
        return [{ result: { ok: true } }];
      }
      if (fnStr.includes("Boolean")) return [{ result: true }];
      if (fnStr.includes("list")) {
        return [{
          result: [
            { targetId: "input-1", role: "textbox", name: "Search", tag: "input", enabled: true, visible: true },
          ],
        }];
      }
      if (fnStr.includes("getCoords")) return [{ result: null }];
      if (fnStr.includes("document.readyState")) {
        return [{ result: { ready: true, interactiveCount: 1, isConnecting: false } }];
      }
      if (fnStr.includes("devicePixelRatio")) {
        return [{ result: { y: 0, w: 1000, h: 800, dpr: 1 } }];
      }
      if (fnStr.includes("readValue")) {
        return [{ result: { ok: true, value: "test search", editable: true } }];
      }
      return [{ result: { ok: true } }];
    });

    vi.stubGlobal("chrome", {
      tabs: {
        query: vi.fn(async () => [{ id: 1, url: "https://example.com/search", active: true }]),
        get: vi.fn(async (id: number) => ({ id, url: "https://example.com/search", title: "Search", windowId: 1 })),
        update: vi.fn(async () => ({})),
        captureVisibleTab: vi.fn(async () => "data:image/jpeg;base64,mock"),
      },
      scripting: { executeScript },
      debugger: {
        attach: vi.fn(async () => { throw new Error("CDP attach failed"); }),
        detach: vi.fn(async () => ({})),
        sendCommand: vi.fn(async () => { throw new Error("CDP unavailable"); }),
      },
    });

    const action: BrowserAction = {
      type: "type",
      target: { targetId: "input-1", name: "Search" },
      params: { text: "test search", submit: false },
      risk: "write",
      verify: "typed",
      timeoutMs: 5000,
    };

    const result = await executeAction(action, "task-type-modal");
    expect(result.ok).toBe(true);
    expect(capturedDismissArgs).toEqual(["input-1"]);
  });
});

describe("settleAfterAction ignores unrelated new tabs", () => {
  it("does not adopt an ad pop-under with no opener link to the clicked tab", async () => {
    let activeTabId = 1;
    const tabs = [
      { id: 1, url: "https://www.amazon.com/s?k=laptop", title: "Amazon Search", active: true, windowId: 10 },
    ];
    // An ad pop-under opened in the same window while the click ran —
    // no openerTabId: nothing links it to the clicked tab.
    const adTab = { id: 2, url: "https://ads.example.com/promo", title: "Promo", active: false, windowId: 10 };

    const executeScript = vi.fn(async (injection: any) => {
      if (injection.func?.toString().includes("clickPoint")) {
        return [{ result: { x: 50, y: 50 } }];
      }
      if (injection.func?.toString().includes("click")) {
        tabs.push(adTab);
        return [{ result: { ok: true } }];
      }
      if (injection.func?.toString().includes("list")) {
        return [{
          result: [{
            targetId: "nav-el-1", role: "link", name: "Acer Laptop", tag: "a",
            cssSelector: "#link1", enabled: true, visible: true,
            bounds: { x: 10, y: 10, width: 100, height: 30 },
          }],
        }];
      }
      if (injection.func?.toString().includes("extractText")) {
        return [{ result: "### Acer Laptop\n\n16GB RAM, 512GB SSD" }];
      }
      return [{ result: true }];
    });

    const update = vi.fn(async (id: number, props: any) => {
      if (props.active) {
        activeTabId = id;
        tabs.forEach(t => (t.active = t.id === id));
      }
      return {};
    });

    vi.stubGlobal("chrome", {
      debugger: {
        attach: vi.fn().mockRejectedValue(new Error("CDP unavailable in test")),
        detach: vi.fn(),
      },
      tabs: {
        query: vi.fn(async (q: any) => {
          if (q.active && q.currentWindow) return tabs.filter(t => t.id === activeTabId);
          return [...tabs];
        }),
        get: vi.fn(async (id: number) => {
          const found = tabs.find(t => t.id === id);
          if (!found) throw new Error("Tab not found");
          return found;
        }),
        update,
      },
      scripting: { executeScript },
    });

    const action: BrowserAction = {
      type: "click",
      risk: "write",
      verify: "clicked Acer Laptop",
      timeoutMs: 1000,
      target: { targetId: "nav-el-1", name: "Acer Laptop" },
    };

    const res = await executeAction(action, "task-click-adtabs");
    expect(res.ok).toBe(true);
    expect(res.observation?.tabId).toBe(1);
    expect(activeTabId).toBe(1);
    expect(update).not.toHaveBeenCalledWith(2, expect.objectContaining({ active: true }));
    expect(res.evidence ?? "").not.toContain("opened new tab 2");
  });
});

describe("deadline abort stops the in-flight action", () => {
  it("an already-aborted type action performs no side effect and reports TIMEOUT", async () => {
    const typeCalls: unknown[][] = [];
    const executeScript = vi.fn(async (injection: { args?: unknown[]; files?: string[]; func?: Function }) => {
      const fnStr = injection.func?.toString() || "";
      if (injection.files) return [{ result: true }];
      if (injection.args && injection.args.length === 4) {
        typeCalls.push(injection.args);
        return [{ result: { ok: true } }];
      }
      if (fnStr.includes("Boolean")) return [{ result: true }];
      if (fnStr.includes("list")) {
        return [{
          result: [
            { targetId: "input-1", role: "textbox", name: "Search", tag: "input", enabled: true, visible: true },
          ],
        }];
      }
      if (fnStr.includes("getCoords")) return [{ result: null }];
      if (fnStr.includes("document.readyState")) {
        return [{ result: { ready: true, interactiveCount: 1, isConnecting: false } }];
      }
      if (fnStr.includes("devicePixelRatio")) {
        return [{ result: { y: 0, w: 1000, h: 800, dpr: 1 } }];
      }
      if (fnStr.includes("readValue")) {
        return [{ result: { ok: true, value: "test search", editable: true } }];
      }
      return [{ result: { ok: true } }];
    });

    const update = vi.fn(async () => ({}));
    vi.stubGlobal("chrome", {
      tabs: {
        query: vi.fn(async () => [{ id: 1, url: "https://example.com/search", active: true }]),
        get: vi.fn(async (id: number) => ({ id, url: "https://example.com/search", title: "Search", windowId: 1 })),
        update,
        captureVisibleTab: vi.fn(async () => "data:image/jpeg;base64,mock"),
      },
      scripting: { executeScript },
      debugger: {
        attach: vi.fn(async () => { throw new Error("CDP attach failed"); }),
        detach: vi.fn(async () => ({})),
        sendCommand: vi.fn(async () => { throw new Error("CDP unavailable"); }),
      },
    });

    const controller = new AbortController();
    controller.abort();
    const action: BrowserAction = {
      type: "type",
      target: { targetId: "input-1", name: "Search" },
      params: { text: "test search", submit: false },
      risk: "write",
      verify: "typed",
      timeoutMs: 5000,
    };

    const result = await executeAction(action, "task-abort", undefined, controller.signal);
    expect(result.ok).toBe(false);
    expect(result.error?.code).toBe("TIMEOUT");
    expect(typeCalls).toEqual([]);
    expect(update).not.toHaveBeenCalled();
  });
});

describe("session tab drift", () => {
  afterEach(() => {
    clearExtensionSessions();
    vi.unstubAllGlobals();
  });

  function seedSession() {
    extensionSessions.set("s1", {
      sessionId: "s1",
      primaryTabId: 1,
      activeTabId: 1,
      childTabIds: new Set([2, 3]),
      origin: "new_task",
      createdAt: 0,
      lastActivityAt: 0,
    });
  }

  it("resume fails honestly when the primary tab is closed", async () => {
    seedSession();
    vi.stubGlobal("chrome", {
      tabs: {
        query: vi.fn(async () => []),
        get: vi.fn(async () => { throw new Error("Tab not found"); }),
        update: vi.fn(async () => ({})),
      },
      scripting: { executeScript: vi.fn(async () => [{ result: true }]) },
    });
    const res = await executeAction(
      { type: "init_session", sessionId: "s1" } as unknown as BrowserAction,
      "task-resume-dead",
    );
    expect(res.ok).toBe(false);
    expect(res.error?.code).toBe("TAB_UNAVAILABLE");
    expect(res.error?.message).toContain("closed");
    expect(extensionSessions.has("s1")).toBe(false);
  });

  it("select_tab does not record the switch when activation fails", async () => {
    seedSession();
    const update = vi.fn(async () => { throw new Error("tab closed"); });
    vi.stubGlobal("chrome", {
      tabs: {
        query: vi.fn(async () => [{ id: 1, url: "https://example.com", active: true }]),
        get: vi.fn(async (id: number) => ({ id, url: "https://example.com", title: "Page", windowId: 1 })),
        update,
      },
      scripting: { executeScript: vi.fn(async () => [{ result: true }]) },
    });
    const res = await executeAction(
      { type: "select_tab", sessionId: "s1", params: { tabId: 2 }, risk: "read", verify: "tab 2 active", timeoutMs: 1000 } as unknown as BrowserAction,
      "task-select-dead",
    );
    expect(res.ok).toBe(false);
    expect(update).toHaveBeenCalledWith(2, { active: true });
    expect(extensionSessions.get("s1")?.activeTabId).toBe(1);
  });

  it("closing the primary promotes the earliest surviving child", () => {
    seedSession();
    extensionSessions.get("s1")!.activeTabId = 2;
    handleTabRemoved(1);
    const sess = extensionSessions.get("s1")!;
    expect(sess.primaryTabId).toBe(2);
    expect(sess.activeTabId).toBe(2);
    expect([...sess.childTabIds]).toEqual([3]);
  });

  it("closing the active child falls back to the primary", () => {
    seedSession();
    extensionSessions.get("s1")!.activeTabId = 2;
    handleTabRemoved(2);
    const sess = extensionSessions.get("s1")!;
    expect(sess.primaryTabId).toBe(1);
    expect(sess.activeTabId).toBe(1);
    expect([...sess.childTabIds]).toEqual([3]);
  });

  it("closing the last tab drops the session", () => {
    extensionSessions.set("s1", {
      sessionId: "s1",
      primaryTabId: 1,
      activeTabId: 1,
      childTabIds: new Set(),
      origin: "new_task",
      createdAt: 0,
      lastActivityAt: 0,
    });
    handleTabRemoved(1);
    expect(extensionSessions.has("s1")).toBe(false);
  });
});
