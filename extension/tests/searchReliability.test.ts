import { afterEach, describe, expect, it, vi } from "vitest";
import { clearExtensionSessions, executeAction } from "../src/actionExecutor";
import type { BrowserAction } from "../src/contracts";

interface PageState {
  url: string;
  title: string;
  readyState: string;
  textLength: number;
  textHash: number;
  interactiveCount: number;
  resultCount: number;
}

type Transition = "navigate" | "state" | "none";
type Failure = "none" | "evaluate";

function installSearchHarness(options: {
  transition?: Transition;
  failure?: Failure;
  withSubmitButton?: boolean;
  wrongValue?: boolean;
  hangDetach?: boolean;
  hangSettleInjection?: boolean;
} = {}) {
  const transition = options.transition ?? "navigate";
  const failure = options.failure ?? "none";
  let typedText = "";
  let attached = false;
  let detachCount = 0;

  const state: PageState = {
    url: "https://shop.local/",
    title: "Shop",
    readyState: "complete",
    textLength: 100,
    textHash: 1,
    interactiveCount: 3,
    resultCount: 0,
  };
  const tab: Record<string, unknown> = {
    id: 1, url: state.url, title: state.title, active: true, windowId: 1, status: "complete",
  };
  const candidates = [
    {
      targetId: "search-input", role: "searchbox", name: "Search", tag: "input",
      inputType: "text", search: true, cssSelector: "#search-input",
      enabled: true, visible: true, bounds: { x: 100, y: 100, width: 260, height: 36 },
    },
    ...(options.withSubmitButton ? [{
      targetId: "search-submit", role: "button", name: "Search", tag: "button",
      inputType: "submit", search: true, cssSelector: "#search-submit",
      enabled: true, visible: true, bounds: { x: 300, y: 100, width: 80, height: 36 },
    }] : []),
  ];

  const applyTransition = () => {
    if (transition === "navigate") {
      state.url = "https://shop.local/search?q=titan+watch";
      state.title = "titan watch - Shop";
      state.resultCount = 8;
      state.textHash = 42;
      tab.url = state.url;
      tab.title = state.title;
    } else if (transition === "state") {
      state.resultCount = 6;
      state.textHash = 77;
      state.textLength = 420;
    }
  };

  const executeScript = vi.fn(async (injection: any) => {
    const fn = injection.func?.toString() || "";
    const args: unknown[] = injection.args || [];
    // A document that navigates mid-injection can leave Chrome's executeScript
    // promise pending forever. Simulate that for the DOM-quiescence probe.
    if (options.hangSettleInjection && fn.includes("MutationObserver")) {
      return await new Promise(() => {});
    }
    if (fn.includes("Boolean(")) return [{ result: true }];
    if (fn.includes("__navigatorAgent?.list")) return [{ result: candidates }];
    if (fn.includes("getCoords")) {
      return [{
        result: args[0] === "search-submit"
          ? { x: 340, y: 118, width: 80, height: 36 }
          : { x: 200, y: 118, width: 260, height: 36 },
      }];
    }
    if (fn.includes("readValue")) {
      return [{ result: { ok: true, value: options.wrongValue ? "" : typedText, editable: true } }];
    }
    if (fn.includes("pageState")) return [{ result: { ...state } }];
    if (fn.includes("__navigatorAgent?.type")) {
      if (options.wrongValue) return [{ result: { ok: true, value: "" } }];
      typedText = String(args[1] ?? "");
      return [{ result: { ok: true, value: typedText } }];
    }
    if (fn.includes("document.readyState")) {
      return [{ result: { ready: true, interactiveCount: 3, isConnecting: false } }];
    }
    if (fn.includes("devicePixelRatio")) {
      return [{ result: { y: 0, w: 1200, h: 800, dpr: 1 } }];
    }
    if (fn.includes("extractText")) return [{ result: "" }];
    return [{ result: true }];
  });

  const sendCommand = vi.fn(async (_target: unknown, method: string, params: any) => {
    if (method === "Runtime.evaluate") {
      if (failure === "evaluate") throw new Error("Runtime domain failed");
      return {
        result: {
          value: {
            targetFound: true,
            targetValue: options.wrongValue ? "" : typedText,
            activeValue: options.wrongValue ? "" : typedText,
            activeEditable: !options.wrongValue,
          },
        },
      };
    }
    if (method === "Input.insertText") {
      typedText = String(params?.text ?? "");
      return {};
    }
    if (method === "Input.dispatchMouseEvent" && params?.type === "mousePressed" && params.x >= 300) {
      applyTransition();
      return {};
    }
    if (method === "Input.dispatchKeyEvent" && params?.type === "rawKeyDown" && params.key === "Enter") {
      applyTransition();
      return {};
    }
    return {};
  });

  vi.stubGlobal("chrome", {
    tabs: {
      // Empty query => no other tabs (skips new-tab polling). Focused-window
      // queries return the harness tab so session resolution does not try to
      // create a real window.
      query: vi.fn(async (q: any = {}) => (Object.keys(q).length === 0 ? [] : [{ ...tab }])),
      get: vi.fn(async () => ({ ...tab })),
      update: vi.fn(async () => ({})),
    },
    windows: { get: vi.fn(async () => ({ focused: true })), update: vi.fn(async () => ({})) },
    scripting: { executeScript },
    debugger: {
      attach: vi.fn(async () => { attached = true; }),
      detach: vi.fn(async () => {
        attached = false;
        detachCount++;
        if (options.hangDetach) await new Promise(() => {});
      }),
      sendCommand,
    },
  });

  return {
    state,
    tab,
    wasAttached: () => attached,
    detachCount: () => detachCount,
    sendCommand,
    executeScript,
  };
}

function searchTypeAction(submit: boolean, overrides: Partial<BrowserAction> = {}): BrowserAction {
  return {
    type: "type",
    risk: "write",
    verify: "search results loaded",
    timeoutMs: 2500,
    target: { targetId: "search-input", name: "Search", search: true },
    params: { text: "titan watch", submit },
    ...overrides,
  } as BrowserAction;
}

afterEach(() => {
  clearExtensionSessions();
  vi.unstubAllGlobals();
});

describe("type + submit reliability", () => {
  it("types, verifies the DOM value, submits and confirms real navigation", async () => {
    const harness = installSearchHarness({ transition: "navigate" });

    const result = await executeAction(searchTypeAction(true), "task-search-nav");

    expect(result.ok).toBe(true);
    expect(result.evidence).toContain("native typing verified");
    expect(result.evidence).toContain("navigated");
    expect(result.evidence).toContain("shop.local/search");
    expect(harness.tab.url).toContain("shop.local/search");
    expect(harness.wasAttached()).toBe(false);
    expect(harness.detachCount()).toBeGreaterThan(0);
  });

  it("accepts an SPA search-state change even when the URL does not move", async () => {
    const harness = installSearchHarness({ transition: "state" });

    const result = await executeAction(searchTypeAction(true), "task-search-state");

    expect(result.ok).toBe(true);
    expect(result.evidence).toContain("search state changed");
    expect(harness.state.resultCount).toBeGreaterThan(0);
    expect(harness.detachCount()).toBeGreaterThan(0);
  });

  it("fails honestly when submission changes neither navigation nor search state", async () => {
    const harness = installSearchHarness({ transition: "none" });

    const result = await executeAction(searchTypeAction(true), "task-search-nochange");

    expect(result.ok).toBe(false);
    expect(result.error?.code).toBe("NAVIGATION_FAILED");
    expect(result.error?.message).toContain("no navigation or search-state change");
    expect(harness.detachCount()).toBeGreaterThan(0);
  });

  it("always detaches the debugger after a CDP failure", async () => {
    const harness = installSearchHarness({ failure: "evaluate" });

    const result = await executeAction(searchTypeAction(false), "task-search-cdp-fail");

    // The in-page fallback is allowed to complete the typing, but the debugger
    // session must not stay attached.
    expect(result.ok).toBe(true);
    expect(harness.wasAttached()).toBe(false);
    expect(harness.detachCount()).toBeGreaterThan(0);
  });

  it("rejects a fallback that did not leave the requested value in the DOM", async () => {
    const harness = installSearchHarness({ wrongValue: true });

    const result = await executeAction(searchTypeAction(false), "task-search-wrong-value");

    expect(result.ok).toBe(false);
    expect(result.error?.code).toBe("STALE_TARGET");
    expect(result.error?.message).toContain("typed value not confirmed");
    expect(harness.detachCount()).toBeGreaterThan(0);
  });

  it("uses the submit button inside the same session when one exists", async () => {
    const harness = installSearchHarness({ transition: "navigate", withSubmitButton: true });

    const result = await executeAction(searchTypeAction(true), "task-search-button");

    expect(result.ok).toBe(true);
    expect(result.evidence).toContain("submitted via button");
    expect(harness.tab.url).toContain("shop.local/search");
    expect(harness.detachCount()).toBe(1);
  });

  it("does not let a stuck debugger detach swallow a completed action", async () => {
    const harness = installSearchHarness({ transition: "navigate", hangDetach: true });
    const started = Date.now();

    const result = await executeAction(searchTypeAction(false), "task-detach-hang");

    // Detach is attempted (so the session is not leaked on purpose), but a
    // pending detach must not turn a verified typing action into TIMEOUT.
    expect(result.ok).toBe(true);
    expect(result.evidence).toContain("native typing verified");
    expect(harness.detachCount()).toBeGreaterThan(0);
    expect(Date.now() - started).toBeLessThan(5_000);
  });

  it("does not wedge when a page injection never settles during navigation", async () => {
    installSearchHarness({ transition: "navigate", hangSettleInjection: true });
    const started = Date.now();

    const result = await executeAction(searchTypeAction(true), "task-settle-hang");

    // The submit really navigated; a DOM-quiescence injection that Chrome
    // never settles must not convert that success into an action TIMEOUT.
    expect(result.ok).toBe(true);
    expect(result.evidence).toContain("navigated");
    expect(result.observation?.url).toContain("shop.local/search");
    expect(Date.now() - started).toBeLessThan(8_000);
  });
});
