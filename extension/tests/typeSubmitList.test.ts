import { afterEach, describe, expect, it, vi } from "vitest";
import { clearExtensionSessions, executeAction } from "../src/actionExecutor";
import type { BrowserAction } from "../src/contracts";

/**
 * Task 11 (4b): doType's submit-control lookup must reuse the
 * candidate list already collected for target resolution instead of
 * re-collecting it. With the submit button present in the initial
 * list, the whole type+submit flow needs exactly two list()
 * collections: one to resolve the target, one for the trailing
 * post-action observation. Before the fix it needed three (the
 * submit lookup re-listed first).
 */
function installHarness() {
  let typedText = "";
  let listCalls = 0;
  const state = {
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
    {
      targetId: "search-submit", role: "button", name: "Search", tag: "button",
      inputType: "submit", search: true, cssSelector: "#search-submit",
      enabled: true, visible: true, bounds: { x: 300, y: 100, width: 80, height: 36 },
    },
  ];

  const executeScript = vi.fn(async (injection: any) => {
    const fn = injection.func?.toString() || "";
    const args: unknown[] = injection.args || [];
    if (fn.includes("__navigatorAgent?.list")) {
      listCalls += 1;
      return [{ result: candidates }];
    }
    if (fn.includes("Boolean(")) return [{ result: true }];
    if (fn.includes("getCoords")) {
      return [{
        result: args[0] === "search-submit"
          ? { x: 340, y: 118, width: 80, height: 36 }
          : { x: 200, y: 118, width: 260, height: 36 },
      }];
    }
    if (fn.includes("readValue")) return [{ result: { ok: true, value: typedText, editable: true } }];
    if (fn.includes("pageState")) return [{ result: { ...state } }];
    if (fn.includes("__navigatorAgent?.type")) {
      typedText = String(args[1] ?? "");
      return [{ result: { ok: true, value: typedText } }];
    }
    if (fn.includes("document.readyState")) {
      return [{ result: { ready: true, interactiveCount: 3, isConnecting: false } }];
    }
    if (fn.includes("devicePixelRatio")) return [{ result: { y: 0, w: 1200, h: 800, dpr: 1 } }];
    if (fn.includes("extractText")) return [{ result: "" }];
    return [{ result: true }];
  });

  const sendCommand = vi.fn(async (_target: unknown, method: string, params: any) => {
    if (method === "Runtime.evaluate") {
      return {
        result: {
          value: {
            targetFound: true,
            targetValue: typedText,
            activeValue: typedText,
            activeEditable: true,
          },
        },
      };
    }
    if (method === "Input.insertText") {
      typedText = String(params?.text ?? "");
      return {};
    }
    if (method === "Input.dispatchMouseEvent" && params?.type === "mousePressed" && params.x >= 300) {
      state.url = "https://shop.local/search?q=titan+watch";
      state.title = "titan watch - Shop";
      state.resultCount = 8;
      state.textHash = 42;
      tab.url = state.url;
      tab.title = state.title;
      return {};
    }
    return {};
  });

  vi.stubGlobal("chrome", {
    tabs: {
      query: vi.fn(async (q: any = {}) => (Object.keys(q).length === 0 ? [] : [{ ...tab }])),
      get: vi.fn(async () => ({ ...tab })),
      update: vi.fn(async () => ({})),
    },
    windows: { get: vi.fn(async () => ({ focused: true })), update: vi.fn(async () => ({})) },
    scripting: { executeScript },
    debugger: {
      attach: vi.fn(async () => {}),
      detach: vi.fn(async () => {}),
      sendCommand,
    },
  });

  return { listCalls: () => listCalls };
}

describe("doType submit-control list reuse (Task 11)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearExtensionSessions();
  });

  it("reuses the resolution list for the submit lookup (2 list calls)", async () => {
    const harness = installHarness();
    const action: BrowserAction = {
      type: "type",
      target: { targetId: "search-input", name: "Search" },
      params: { text: "titan watch", submit: true },
      risk: "write",
      verify: "search submitted",
      timeoutMs: 5000,
    };
    const result = await executeAction(action, "task-type-submit-list");
    expect(result.ok).toBe(true);
    expect(result.evidence).toContain("submitted via button");
    // 1 = target resolution, 1 = trailing post-action observation.
    // The submit-control lookup must not add a third collection.
    expect(harness.listCalls()).toBe(2);
  });
});
