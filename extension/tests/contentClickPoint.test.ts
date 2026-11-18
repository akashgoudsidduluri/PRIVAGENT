// @vitest-environment jsdom
import { describe, expect, it, afterEach, vi } from "vitest";
import "../lib/content";

interface Point { x: number; y: number; width: number; height: number }

const agent = () => window.__navigatorAgent as unknown as {
  getClickPoint: (targetId: string, name?: string) => Point | null;
};

function rect(left: number, top: number, width: number, height: number): DOMRect {
  return {
    left, top, width, height, x: left, y: top,
    right: left + width, bottom: top + height,
    toJSON: () => ({}),
  } as DOMRect;
}

function setRect(el: Element, r: DOMRect): void {
  (el as HTMLElement).getBoundingClientRect = () => r;
}

afterEach(() => {
  document.body.innerHTML = "";
});

describe("chat-row click point promotion", () => {
  it("clicks the row centre when the planner matched an inner label", () => {
    document.body.innerHTML = `
      <div role="listitem" data-navigator-id="row-1" id="row">
        <span data-navigator-id="label-1">Mummy</span>
        <span data-navigator-id="label-2">last seen recently</span>
      </div>`;
    const row = document.getElementById("row")!;
    const label = document.querySelector('[data-navigator-id="label-1"]')!;
    setRect(row, rect(100, 200, 400, 60));
    setRect(label, rect(120, 210, 80, 20));

    expect(agent().getClickPoint("label-1", "Mummy")).toEqual({
      x: 300, y: 230, width: 400, height: 60,
    });
  });

  it("also promotes div[tabindex=\"-1\"] WhatsApp chat rows", () => {
    document.body.innerHTML = `
      <div tabindex="-1" data-navigator-id="row-2">
        <span data-navigator-id="label-3">Akash</span>
      </div>`;
    const row = document.querySelector('[data-navigator-id="row-2"]')!;
    const label = document.querySelector('[data-navigator-id="label-3"]')!;
    setRect(row, rect(40, 100, 320, 64));
    setRect(label, rect(60, 110, 60, 18));

    const point = agent().getClickPoint("label-3", "Akash");
    expect(point).toEqual({ x: 200, y: 132, width: 320, height: 64 });
  });

  it("keeps real controls at their own centre", () => {
    document.body.innerHTML = `
      <div role="listitem" data-navigator-id="row-3">
        <button data-navigator-id="btn-1" id="btn">Sign in</button>
      </div>`;
    const row = document.querySelector('[data-navigator-id="row-3"]')!;
    const btn = document.getElementById("btn")!;
    setRect(row, rect(10, 10, 300, 50));
    setRect(btn, rect(50, 20, 100, 30));

    expect(agent().getClickPoint("btn-1", "Sign in")).toEqual({
      x: 100, y: 35, width: 100, height: 30,
    });
  });

  it("never promotes a full-height scroll container", () => {
    document.body.innerHTML = `
      <div tabindex="-1" data-navigator-id="pane-1">
        <span data-navigator-id="label-4">Mummy</span>
      </div>`;
    const pane = document.querySelector('[data-navigator-id="pane-1"]')!;
    const label = document.querySelector('[data-navigator-id="label-4"]')!;
    // > max(240px, 50% of the 768px jsdom viewport)
    setRect(pane, rect(0, 0, 400, 900));
    setRect(label, rect(120, 600, 80, 20));

    expect(agent().getClickPoint("label-4", "Mummy")).toEqual({
      x: 160, y: 610, width: 80, height: 20,
    });
  });

  it("returns null for an unknown target", () => {
    document.body.innerHTML = `<div>plain page</div>`;
    expect(agent().getClickPoint("missing", "Nothing here")).toBeNull();
  });
});

describe("clicking never fires a cancel-search side effect", () => {
  it("clicking a chat-named target does not click the page's cancel-search button", () => {
    // jsdom's PointerEvent rejects { view: window } outright, so the
    // constructors are stubbed with plain Events for this test; the
    // native .click() calls (the actual side effect under test) are
    // real and observable.
    class FakePointerEvent extends Event {
      constructor(type: string, init?: PointerEventInit) { super(type, init); }
    }
    vi.stubGlobal("PointerEvent", FakePointerEvent);
    vi.stubGlobal("MouseEvent", FakePointerEvent);
    let cancelled = false;
    document.body.innerHTML = `
      <button aria-label="Cancel search">✕</button>
      <button id="chat-btn">Chat with support</button>`;
    document.querySelector('button[aria-label="Cancel search"]')!
      .addEventListener("click", () => { cancelled = true; });
    (window.__navigatorAgent as unknown as { list: () => unknown[] }).list();
    const click = (window.__navigatorAgent as unknown as {
      click: (targetId: string, name?: string) => { ok: boolean; error?: string };
    }).click;
    const result = click("chat-btn", "Chat with support");
    expect(result.ok).toBe(true);
    expect(cancelled).toBe(false);
  });
});

describe("linkTarget reports the anchor's target", () => {
  it("returns _blank only for a _blank anchor", () => {
    document.body.innerHTML = `
      <a id="blank-link" href="https://example.com/p" target="_blank">Product</a>
      <a id="same-link" href="https://example.com/p">Same tab</a>
      <a id="anchor-link" href="#reviews">Reviews</a>`;
    (window.__navigatorAgent as unknown as { list: () => unknown[] }).list();
    const linkTarget = (window.__navigatorAgent as unknown as {
      linkTarget: (targetId: string, name?: string) => string;
    }).linkTarget;
    expect(linkTarget("blank-link", "Product")).toBe("_blank");
    expect(linkTarget("same-link", "Same tab")).toBe("");
    expect(linkTarget("anchor-link", "Reviews")).toBe("");
    expect(linkTarget("missing", "Nope")).toBe("");
  });
});
