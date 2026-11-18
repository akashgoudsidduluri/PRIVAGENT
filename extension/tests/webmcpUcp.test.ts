import { afterEach, describe, expect, it, vi } from "vitest";
import { ACTION_TYPES, TYPE_ACTION_RISK_FLOOR, validateAction, type BrowserAction } from "../src/contracts";
import { clearExtensionSessions, executeAction, observe } from "../src/actionExecutor";

afterEach(() => {
  vi.unstubAllGlobals();
  clearExtensionSessions();
});

describe("Task 14 — WebMCP / UCP contracts", () => {
  it("lists the standards actions with their risk floors", () => {
    expect(ACTION_TYPES).toContain("webmcp_tool");
    expect(ACTION_TYPES).toContain("ucp_checkout");
    // A page tool can change site state (its self-declared
    // read-only-ness is untrusted), so it floors at write; a
    // checkout moves money, so it floors at sensitive.
    expect(TYPE_ACTION_RISK_FLOOR.webmcp_tool).toBe("write");
    expect(TYPE_ACTION_RISK_FLOOR.ucp_checkout).toBe("sensitive");
  });

  it("validates the new actions like any other step", () => {
    const tool: BrowserAction = {
      type: "webmcp_tool", risk: "write", timeoutMs: 5000,
      verify: "tool called", params: { tool: "search_products", arguments: { query: "shoes" } },
    };
    expect(validateAction(tool).ok).toBe(true);

    const checkout: BrowserAction = {
      type: "ucp_checkout", risk: "sensitive", timeoutMs: 5000,
      verify: "checkout done",
      params: { merchant: "shop.example.com", amount: "1299.00", currency: "INR" },
    };
    expect(validateAction(checkout).ok).toBe(true);

    // A checkout at write risk is below its floor.
    const cheap: BrowserAction = {
      type: "ucp_checkout", risk: "write", timeoutMs: 5000,
      verify: "checkout done",
      params: { merchant: "shop.example.com", amount: "1.00", currency: "INR" },
    };
    expect(validateAction(cheap).ok).toBe(false);
  });
});

describe("Task 14 — ucp_checkout is never executed by the extension", () => {
  it("rejects a ucp_checkout step outright instead of driving a checkout", async () => {
    vi.stubGlobal("chrome", {
      tabs: {
        query: vi.fn(async () => []),
        create: vi.fn(async () => ({ id: 99, url: "about:blank" })),
        get: vi.fn(async (id: number) => ({ id, url: "about:blank", title: "", windowId: 1 })),
        update: vi.fn(async () => ({})),
      },
      scripting: { executeScript: vi.fn() },
    });

    const action = {
      type: "ucp_checkout", risk: "sensitive", verify: "checkout done",
      timeoutMs: 5000,
      params: { merchant: "shop.example.com", amount: "1299.00", currency: "INR" },
    } as BrowserAction;

    const result = await executeAction(action, "task-ucp-1");
    expect(result.ok).toBe(false);
    expect(result.error?.message).toMatch(/backend/);
  });
});

describe("Task 14 — WebMCP tool call through the extension", () => {
  it("calls the page's modelContext tool and reports its result as evidence", async () => {
    const callTool = vi.fn(async (name: string, args: Record<string, unknown>) => {
      expect(name).toBe("search_products");
      expect(args).toEqual({ query: "shoes" });
      return { results: ["red shoes", "blue shoes"] };
    });
    // The injected function reads document.modelContext in the tab.
    // The mock runs it in this context with stubbed globals, which is
    // exactly what the isolated world gives it: a WebIDL document.
    vi.stubGlobal("document", {
      modelContext: { callTool },
      querySelector: () => null,
    });
    vi.stubGlobal("navigator", {});
    vi.stubGlobal("location", { href: "https://shop.example.com/", origin: "https://shop.example.com" });

    const executeScript = vi.fn(async (injection: { func?: Function; args?: unknown[] }) => {
      const src = String(injection.func ?? "");
      if (src.includes("callTool")) {
        // The tool-call function: invoke it with its args.
        const out = await (injection.func as (...a: unknown[]) => Promise<unknown>)(
          ...(injection.args ?? []),
        );
        return [{ result: out }];
      }
      // Everything else (agent list, meta, settle, observe internals)
      // is refused: observe degrades to metadata, settleDom gives up.
      throw new Error("injection refused in test");
    });

    vi.stubGlobal("chrome", {
      tabs: {
        query: vi.fn(async () => [
          { id: 5, url: "https://shop.example.com/", title: "Shop", windowId: 1, active: true },
        ]),
        get: vi.fn(async (id: number) => ({ id, url: "https://shop.example.com/", title: "Shop", windowId: 1, active: true })),
        update: vi.fn(async () => ({})),
      },
      scripting: { executeScript },
    });

    const action = {
      type: "webmcp_tool", risk: "write", verify: "tool called",
      timeoutMs: 5000, sessionId: undefined,
      params: { tool: "search_products", arguments: { query: "shoes" } },
    } as BrowserAction;

    const result = await executeAction(action, "task-webmcp-1");
    expect(callTool).toHaveBeenCalledTimes(1);
    expect(result.ok).toBe(true);
    expect(result.evidence ?? "").toContain("search_products");
    expect(result.evidence ?? "").toContain("red shoes");
  });

  it("fails honestly (recoverable) when the page exposes no modelContext", async () => {
    vi.stubGlobal("document", { modelContext: undefined, querySelector: () => null });
    vi.stubGlobal("navigator", {});
    vi.stubGlobal("location", { href: "https://shop.example.com/", origin: "https://shop.example.com" });

    const executeScript = vi.fn(async (injection: { func?: Function; args?: unknown[] }) => {
      const src = String(injection.func ?? "");
      if (src.includes("callTool")) {
        const out = await (injection.func as (...a: unknown[]) => Promise<unknown>)(
          ...(injection.args ?? []),
        );
        return [{ result: out }];
      }
      throw new Error("injection refused in test");
    });

    vi.stubGlobal("chrome", {
      tabs: {
        query: vi.fn(async () => [
          { id: 5, url: "https://shop.example.com/", title: "Shop", windowId: 1, active: true },
        ]),
        get: vi.fn(async (id: number) => ({ id, url: "https://shop.example.com/", title: "Shop", windowId: 1, active: true })),
        update: vi.fn(async () => ({})),
      },
      scripting: { executeScript },
    });

    const action = {
      type: "webmcp_tool", risk: "write", verify: "tool called",
      timeoutMs: 5000,
      params: { tool: "search_products", arguments: { query: "shoes" } },
    } as BrowserAction;

    const result = await executeAction(action, "task-webmcp-2");
    expect(result.ok).toBe(false);
    expect(result.error?.code).toBe("WEBMCP_UNAVAILABLE");
    expect(result.error?.recoverable).toBe(true);
  });
});

describe("Task 14 — observation carries page-declared standards", () => {
  it("collects webmcpTools and ucpProfileUrl from the tab", async () => {
    const tools = [{
      name: "search_products",
      description: "Search the catalogue.",
      inputSchema: { type: "object", properties: { query: { type: "string" } } },
    }];
    vi.stubGlobal("document", {
      modelContext: { tools },
      querySelector: (sel: string) =>
        sel.includes("ucp")
          ? { href: "https://shop.example.com/.well-known/ucp" }
          : null,
    });
    vi.stubGlobal("navigator", {});
    vi.stubGlobal("location", { href: "https://shop.example.com/", origin: "https://shop.example.com" });

    const executeScript = vi.fn(async (injection: { func?: Function; args?: unknown[] }) => {
      const src = String(injection.func ?? "");
      if (src.includes("modelContext")) {
        const out = await (injection.func as (...a: unknown[]) => Promise<unknown>)(
          ...(injection.args ?? []),
        );
        return [{ result: out }];
      }
      // Agent list / meta / text extraction: return empty shapes.
      if (src.includes("scrollY")) return [{ result: { y: 0, w: 1280, h: 800, dpr: 1 } }];
      return [{ result: [] }];
    });

    vi.stubGlobal("chrome", {
      tabs: {
        get: vi.fn(async (id: number) => ({ id, url: "https://shop.example.com/", title: "Shop", windowId: 1, active: true })),
        query: vi.fn(async () => [
          { id: 5, url: "https://shop.example.com/", title: "Shop", windowId: 1, active: true },
        ]),
        update: vi.fn(async () => ({})),
      },
      scripting: { executeScript },
    });

    const obs = await observe(5);
    expect(obs.webmcpTools).toEqual(tools);
    expect(obs.ucpProfileUrl).toBe("https://shop.example.com/.well-known/ucp");
  });

  it("omits the standards fields when the page declares nothing", async () => {
    vi.stubGlobal("document", { modelContext: undefined, querySelector: () => null });
    vi.stubGlobal("navigator", {});
    vi.stubGlobal("location", { href: "https://example.com/", origin: "https://example.com" });

    const executeScript = vi.fn(async (injection: { func?: Function; args?: unknown[] }) => {
      const src = String(injection.func ?? "");
      if (src.includes("modelContext")) {
        const out = await (injection.func as (...a: unknown[]) => Promise<unknown>)(
          ...(injection.args ?? []),
        );
        return [{ result: out }];
      }
      if (src.includes("scrollY")) return [{ result: { y: 0, w: 1280, h: 800, dpr: 1 } }];
      return [{ result: [] }];
    });

    vi.stubGlobal("chrome", {
      tabs: {
        get: vi.fn(async (id: number) => ({ id, url: "https://example.com/", title: "Example", windowId: 1, active: true })),
        query: vi.fn(async () => [
          { id: 5, url: "https://example.com/", title: "Example", windowId: 1, active: true },
        ]),
        update: vi.fn(async () => ({})),
      },
      scripting: { executeScript },
    });

    const obs = await observe(5);
    expect(obs.webmcpTools).toBeUndefined();
    expect(obs.ucpProfileUrl).toBeUndefined();
  });

  it("collects ogImage from the page's og:image meta tag, resolving relative URLs", async () => {
    vi.stubGlobal("document", {
      modelContext: undefined,
      querySelector: (sel: string) =>
        sel.includes("og:image") ? { content: "/images/fit3.jpg" } : null,
    });
    vi.stubGlobal("navigator", {});
    vi.stubGlobal("location", { href: "https://www.flipkart.com/product/p/itm1", origin: "https://www.flipkart.com" });

    const executeScript = vi.fn(async (injection: { func?: Function; args?: unknown[] }) => {
      const src = String(injection.func ?? "");
      if (src.includes("modelContext")) {
        const out = await (injection.func as (...a: unknown[]) => Promise<unknown>)(
          ...(injection.args ?? []),
        );
        return [{ result: out }];
      }
      if (src.includes("scrollY")) return [{ result: { y: 0, w: 1280, h: 800, dpr: 1 } }];
      return [{ result: [] }];
    });

    vi.stubGlobal("chrome", {
      tabs: {
        get: vi.fn(async (id: number) => ({ id, url: "https://www.flipkart.com/product/p/itm1", title: "Fit3", windowId: 1, active: true })),
        query: vi.fn(async () => [
          { id: 5, url: "https://www.flipkart.com/product/p/itm1", title: "Fit3", windowId: 1, active: true },
        ]),
        update: vi.fn(async () => ({})),
      },
      scripting: { executeScript },
    });

    const obs = await observe(5);
    expect(obs.ogImage).toBe("https://www.flipkart.com/images/fit3.jpg");
  });

  it("omits ogImage when the page's og:image is not a credential-free http(s) URL", async () => {
    vi.stubGlobal("document", {
      modelContext: undefined,
      querySelector: (sel: string) =>
        sel.includes("og:image") ? { content: "javascript:alert(1)" } : null,
    });
    vi.stubGlobal("navigator", {});
    vi.stubGlobal("location", { href: "https://example.com/", origin: "https://example.com" });

    const executeScript = vi.fn(async (injection: { func?: Function; args?: unknown[] }) => {
      const src = String(injection.func ?? "");
      if (src.includes("modelContext")) {
        const out = await (injection.func as (...a: unknown[]) => Promise<unknown>)(
          ...(injection.args ?? []),
        );
        return [{ result: out }];
      }
      if (src.includes("scrollY")) return [{ result: { y: 0, w: 1280, h: 800, dpr: 1 } }];
      return [{ result: [] }];
    });

    vi.stubGlobal("chrome", {
      tabs: {
        get: vi.fn(async (id: number) => ({ id, url: "https://example.com/", title: "Example", windowId: 1, active: true })),
        query: vi.fn(async () => [
          { id: 5, url: "https://example.com/", title: "Example", windowId: 1, active: true },
        ]),
        update: vi.fn(async () => ({})),
      },
      scripting: { executeScript },
    });

    const obs = await observe(5);
    expect(obs.ogImage).toBeUndefined();
  });

  it("passes over-long names/descriptions through over-long, so the backend drops them", async () => {
    // REVIEW (Task 14 audit): the backend drops (never truncates) a
    // name over 64 chars or a description over 2000 chars. The
    // extension used to slice exactly at those limits, silently
    // truncating a 5,000-char description into a valid 2,000-char one
    // and defeating the drop. The slices are now one past the limits,
    // so an over-long value still arrives over-long.
    const tools = [{
      name: "n".repeat(100),
      description: "d".repeat(5000),
      inputSchema: { type: "object" },
    }];
    vi.stubGlobal("document", {
      modelContext: { tools },
      querySelector: () => null,
    });
    vi.stubGlobal("navigator", {});
    vi.stubGlobal("location", { href: "https://shop.example.com/", origin: "https://shop.example.com" });

    const executeScript = vi.fn(async (injection: { func?: Function; args?: unknown[] }) => {
      const src = String(injection.func ?? "");
      if (src.includes("modelContext")) {
        const out = await (injection.func as (...a: unknown[]) => Promise<unknown>)(
          ...(injection.args ?? []),
        );
        return [{ result: out }];
      }
      if (src.includes("scrollY")) return [{ result: { y: 0, w: 1280, h: 800, dpr: 1 } }];
      return [{ result: [] }];
    });

    vi.stubGlobal("chrome", {
      tabs: {
        get: vi.fn(async (id: number) => ({ id, url: "https://shop.example.com/", title: "Shop", windowId: 1, active: true })),
        query: vi.fn(async () => [
          { id: 5, url: "https://shop.example.com/", title: "Shop", windowId: 1, active: true },
        ]),
        update: vi.fn(async () => ({})),
      },
      scripting: { executeScript },
    });

    const obs = await observe(5);
    expect(obs.webmcpTools).toHaveLength(1);
    expect(obs.webmcpTools![0].name).toHaveLength(65);
    expect(obs.webmcpTools![0].description).toHaveLength(2001);
  });
});
