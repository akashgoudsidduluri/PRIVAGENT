// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "../lib/content";

interface Agent {
  getOverlayText: () => string;
  readValue: (targetId: string, name?: string) => { ok: boolean; value?: string; error?: string };
  hitTest: (targetId: string, name?: string, x?: number, y?: number) => { ok: boolean; error?: string };
}

const agent = () => window.__navigatorAgent as unknown as Agent;

function stubElementFromPoint(el: Element | null): void {
  Object.defineProperty(document, "elementFromPoint", {
    configurable: true,
    value: () => el,
  });
}

afterEach(() => {
  vi.restoreAllMocks();
  delete (document as unknown as { elementFromPoint?: unknown }).elementFromPoint;
  document.body.innerHTML = "";
});

describe("type read-back", () => {
  it("returns the current value of a plain input", () => {
    document.body.innerHTML = `<input id="name" value="" />`;
    const input = document.getElementById("name") as HTMLInputElement;
    input.value = "2026-12-24";

    expect(agent().readValue("name", "Name")).toEqual({ ok: true, value: "2026-12-24" });
  });

  it("reads through a wrapper to its inner control", () => {
    document.body.innerHTML = `<div id="wrap"><input id="inner" /></div>`;
    const inner = document.getElementById("inner") as HTMLInputElement;
    inner.value = "hello";

    expect(agent().readValue("wrap", "Search")).toEqual({ ok: true, value: "hello" });
  });

  it("reads a contenteditable editor", () => {
    document.body.innerHTML = `<div id="editor" contenteditable="true">typed text</div>`;
    expect(agent().readValue("editor", "Message")).toEqual({ ok: true, value: "typed text" });
  });

  it("reports STALE_TARGET when the field disappeared", () => {
    document.body.innerHTML = `<div>gone</div>`;
    const res = agent().readValue("missing", "Missing");
    expect(res.ok).toBe(false);
    expect(res.error).toBe("STALE_TARGET");
  });
});

describe("click hit test", () => {
  it("accepts the target itself", () => {
    document.body.innerHTML = `<button id="go">Go</button>`;
    const btn = document.getElementById("go")!;
    stubElementFromPoint(btn);

    expect(agent().hitTest("go", "Go", 10, 10)).toEqual({ ok: true });
  });

  it("accepts a child of the target", () => {
    document.body.innerHTML = `<button id="go"><span id="label">Go</span></button>`;
    stubElementFromPoint(document.getElementById("label")!);

    expect(agent().hitTest("go", "Go", 10, 10)).toEqual({ ok: true });
  });

  it("rejects whatever replaced a removed control", () => {
    document.body.innerHTML = `<p id="state">Button removed</p>`;
    stubElementFromPoint(document.getElementById("state")!);

    const res = agent().hitTest("proceed", "Proceed to checkout", 10, 10);
    expect(res.ok).toBe(false);
    expect(res.error).toBe("STALE_TARGET");
  });

  it("rejects a point that lands on an unrelated element", () => {
    document.body.innerHTML = `<button id="go">Go</button><p id="other">Other</p>`;
    stubElementFromPoint(document.getElementById("other")!);

    expect(agent().hitTest("go", "Go", 10, 10).ok).toBe(false);
  });
});

describe("overlay text extraction", () => {
  it("returns visible tooltip text", () => {
    document.body.innerHTML = `
      <div role="tooltip">Renewal date: 15 January 2027</div>
      <div class="tooltip" hidden>hidden tooltip text</div>`;

    const text = agent().getOverlayText();
    expect(text).toContain("15 January 2027");
    expect(text).not.toContain("hidden tooltip text");
  });

  it("reads an open details panel", () => {
    document.body.innerHTML = `<details open><summary>Billing</summary>Renews 15 January 2027</details>`;
    expect(agent().getOverlayText()).toContain("Renews 15 January 2027");
  });

  it("returns an empty string when no overlay is visible", () => {
    document.body.innerHTML = `<p>plain page</p>`;
    expect(agent().getOverlayText()).toBe("");
  });
});
