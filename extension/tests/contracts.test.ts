import { describe, expect, it } from "vitest";
import {
  ACTION_TYPES,
  RISK_CLASSES,
  TYPE_ACTION_RISK_FLOOR,
  validateAction,
  type BrowserAction,
} from "../src/contracts";

describe("contracts", () => {
  it("exposes the canonical action type list", () => {
    expect(ACTION_TYPES).toEqual([
      "navigate",
      "click",
      "type",
      "press",
      "scroll",
      "zoom",
      "wait",
      "observe",
      "select_tab",
      "new_tab",
      "close_tab",
      "select_option",
      "set_date",
      "upload_file",
    ]);
  });

  it("exposes the canonical risk classes", () => {
    expect(RISK_CLASSES).toEqual(["read", "write", "sensitive"]);
  });

  it("rejects an action type outside the whitelist", () => {
    const fake = { type: "run_js", risk: "read", timeoutMs: 1000, verify: "url" };
    const result = validateAction(fake as unknown as BrowserAction);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("run_js");
  });

  it("rejects risk below the action's floor (typing is write)", () => {
    const action = {
      type: "type",
      risk: "read",
      timeoutMs: 1000,
      verify: "field filled",
    } as BrowserAction;
    const result = validateAction(action);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("risk");
  });

  it("accepts a valid action", () => {
    const action: BrowserAction = {
      type: "click",
      risk: "write",
      timeoutMs: 5000,
      verify: "page changed",
      target: { targetId: "t1" },
    };
    expect(validateAction(action).ok).toBe(true);
  });

  it("requires verify criteria on every evaluated action", () => {
    const action = { type: "observe", risk: "read", timeoutMs: 1000 } as unknown as BrowserAction;
    const result = validateAction(action);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("verify");
  });

  it("type floor is write", () => {
    expect(TYPE_ACTION_RISK_FLOOR.type).toBe("write");
  });

  it("rejects navigation to non-http(s) schemes", () => {
    for (const url of [
      "javascript:alert(1)",
      "data:text/html,<script>alert(1)</script>",
      "file:///C:/Windows/win.ini",
      "https://user:pass@example.com/",
    ]) {
      const action = {
        type: "navigate", risk: "write", timeoutMs: 1000, verify: "url",
        params: { url },
      } as unknown as BrowserAction;
      const result = validateAction(action);
      expect(result.ok).toBe(false);
      if (!result.ok) expect(result.error).toContain("unsafe");
    }
  });

  it("requires per-action parameters", () => {
    const cases: unknown[] = [
      { type: "click", risk: "write", timeoutMs: 1000, verify: "v" },
      { type: "type", risk: "write", timeoutMs: 1000, verify: "v", target: { targetId: "t" } },
      { type: "type", risk: "write", timeoutMs: 1000, verify: "v", target: { targetId: "t" }, params: {} },
      { type: "press", risk: "write", timeoutMs: 1000, verify: "v", params: {} },
      { type: "scroll", risk: "read", timeoutMs: 1000, verify: "v", params: { direction: "sideways" } },
      { type: "zoom", risk: "read", timeoutMs: 1000, verify: "v", params: { factor: 9 } },
      { type: "select_tab", risk: "read", timeoutMs: 1000, verify: "v", params: { tabId: -1 } },
      { type: "new_tab", risk: "write", timeoutMs: 1000, verify: "v", params: { url: "about:blank" } },
    ];
    for (const action of cases) {
      expect(validateAction(action).ok).toBe(false);
    }
  });

  it("clamps oversized timeouts to the 60s bound", () => {
    const action = {
      type: "observe", risk: "read", timeoutMs: 999_999, verify: "v",
    } as unknown as BrowserAction;
    expect(validateAction(action).ok).toBe(true);
    expect((action as { timeoutMs: number }).timeoutMs).toBe(60_000);
  });

  it("rejects non-finite coordinate targets", () => {
    const action = {
      type: "click", risk: "write", timeoutMs: 1000, verify: "v",
      target: { targetId: "", coords: { x: "left", y: 2 } },
    } as unknown as BrowserAction;
    expect(validateAction(action).ok).toBe(false);
  });

  it("accepts finite coordinate targets", () => {
    const action = {
      type: "click", risk: "write", timeoutMs: 1000, verify: "v",
      target: { targetId: "", coords: { x: 10, y: 20 } },
    } as unknown as BrowserAction;
    expect(validateAction(action).ok).toBe(true);
  });
});

/**
 * Mirrors `ActionParams.model_dump(by_alias=True)` in the Python backend:
 * every unset optional field is an explicit JSON null. These are the exact
 * payloads the extension receives over the WebSocket in production.
 */
const PYTHON_NULL_PARAMS = {
  url: null,
  direction: null,
  text: null,
  clear: null,
  submit: null,
  key: null,
  containerId: null,
  tabId: null,
  factor: null,
};

const base = (type: string, risk: string) => ({
  type,
  risk,
  verify: "verification",
  timeoutMs: 5000,
});

describe("validateAction with Python null optionals", () => {
  it("accepts a navigate url whose direction and unused params are null", () => {
    const action = {
      ...base("navigate", "write"),
      target: null,
      params: { ...PYTHON_NULL_PARAMS, url: "https://www.flipkart.com/" },
    } as unknown as BrowserAction;

    const result = validateAction(action);

    expect(result.ok).toBe(true);
    // Normalization removes nulls before the executor sees them.
    expect((action as unknown as { params: Record<string, unknown> }).params.direction).toBeUndefined();
    expect((action as unknown as { params: Record<string, unknown> }).params.factor).toBeUndefined();
  });

  it("never stringifies a null navigate direction as 'null'", () => {
    const action = {
      ...base("navigate", "write"),
      params: { ...PYTHON_NULL_PARAMS, url: "https://example.com" },
    } as unknown as BrowserAction;

    const result = validateAction(action);

    expect(result.ok).toBe(true);
    if (!result.ok) expect(result.error).not.toContain("null");
  });

  it("accepts zoom direction=in with factor=null", () => {
    const action = {
      ...base("zoom", "read"),
      params: { ...PYTHON_NULL_PARAMS, direction: "in" },
    } as unknown as BrowserAction;
    expect(validateAction(action).ok).toBe(true);
  });

  it("accepts zoom factor with direction=null", () => {
    const action = {
      ...base("zoom", "read"),
      params: { ...PYTHON_NULL_PARAMS, factor: 1.5 },
    } as unknown as BrowserAction;
    expect(validateAction(action).ok).toBe(true);
  });

  it("accepts scroll with containerId=null", () => {
    const action = {
      ...base("scroll", "read"),
      params: { ...PYTHON_NULL_PARAMS, direction: "down" },
    } as unknown as BrowserAction;
    expect(validateAction(action).ok).toBe(true);
  });

  it("accepts tab actions whose other params are null", () => {
    const cases = [
      { type: "select_tab", risk: "read", tabId: 4 },
      { type: "close_tab", risk: "write", tabId: 4 },
    ];
    for (const { type, risk, tabId } of cases) {
      const action = {
        ...base(type, risk),
        params: { ...PYTHON_NULL_PARAMS, tabId },
      } as unknown as BrowserAction;
      expect(validateAction(action).ok, type).toBe(true);
    }
  });

  it("accepts a click target with null optional fields", () => {
    const action = {
      ...base("click", "write"),
      target: { targetId: "t1", name: null, cssSelector: null, coords: null },
    } as unknown as BrowserAction;
    const result = validateAction(action);
    expect(result.ok).toBe(true);
    expect((action as unknown as { target: Record<string, unknown> }).target.name).toBeUndefined();
    expect((action as unknown as { target: Record<string, unknown> }).target.coords).toBeUndefined();
  });

  it("accepts type with null optional params and target fields", () => {
    const action = {
      ...base("type", "write"),
      target: { targetId: "q", name: null, cssSelector: null, coords: null },
      params: { ...PYTHON_NULL_PARAMS, text: "best titan watch under 8000" },
    } as unknown as BrowserAction;
    expect(validateAction(action).ok).toBe(true);
  });

  it("accepts press, new_tab, observe, and wait payloads with null optionals", () => {
    const actions = [
      { ...base("press", "write"), params: { ...PYTHON_NULL_PARAMS, key: "Enter" } },
      { ...base("new_tab", "write"), params: { ...PYTHON_NULL_PARAMS, url: "https://example.com" } },
      { ...base("observe", "read"), target: null, params: null },
      { ...base("wait", "read"), target: null, params: null },
    ] as unknown as BrowserAction[];

    for (const action of actions) {
      expect(validateAction(action).ok, action.type).toBe(true);
    }
  });

  it("still rejects null where a value is required", () => {
    const invalid = [
      // navigate needs at least url or direction
      { ...base("navigate", "write"), params: { ...PYTHON_NULL_PARAMS } },
      // type needs text
      { ...base("type", "write"), target: { targetId: "q" }, params: { ...PYTHON_NULL_PARAMS } },
      // press needs key
      { ...base("press", "write"), params: { ...PYTHON_NULL_PARAMS } },
      // scroll needs direction
      { ...base("scroll", "read"), params: { ...PYTHON_NULL_PARAMS } },
      // zoom needs direction or factor
      { ...base("zoom", "read"), params: { ...PYTHON_NULL_PARAMS } },
      // tab actions need a numeric tabId
      { ...base("select_tab", "read"), params: { ...PYTHON_NULL_PARAMS } },
      { ...base("close_tab", "read"), params: { ...PYTHON_NULL_PARAMS } },
      // new_tab needs a safe url
      { ...base("new_tab", "write"), params: { ...PYTHON_NULL_PARAMS } },
      // click needs targetId, name, or coords
      {
        ...base("click", "write"),
        target: { targetId: null, name: null, cssSelector: null, coords: null },
      },
    ] as unknown as BrowserAction[];

    for (const action of invalid) {
      const result = validateAction(action);
      expect(result.ok, `${action.type} should be rejected`).toBe(false);
    }
  });
});
