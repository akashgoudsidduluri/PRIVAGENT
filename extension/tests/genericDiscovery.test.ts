// @vitest-environment jsdom
// Task 8 — Generalisation: element discovery must work on a synthetic page
// the agent has never seen, by role + accessible name + text. No fixture
// here uses a per-site id, class, or data-testid.
import { afterEach, describe, expect, it } from "vitest";
import "../lib/content";

interface Listed {
  targetId: string;
  role: string;
  name: string;
  tag: string;
  inputType?: string;
  search?: boolean;
}

const list = () => (window.__navigatorAgent as unknown as { list: () => Listed[] }).list();

function rect(left: number, top: number, width: number, height: number): DOMRect {
  return {
    left, top, width, height, x: left, y: top,
    right: left + width, bottom: top + height,
    toJSON: () => ({}),
  } as DOMRect;
}

function makeVisible(): void {
  document.querySelectorAll("*").forEach(el => {
    (el as HTMLElement).getBoundingClientRect = () => rect(0, 0, 240, 32);
  });
}

afterEach(() => {
  document.body.innerHTML = "";
});

describe("generic discovery (Task 8)", () => {
  it("finds a search box by role and accessible name, with no site id", () => {
    document.body.innerHTML = `
      <form role="search" action="/search">
        <input type="search" aria-label="Search products" name="q">
        <button type="submit">Go</button>
      </form>`;
    makeVisible();

    const found = list().find(e => e.role === "searchbox");
    expect(found).toBeDefined();
    expect(found?.search).toBe(true);
    expect(found?.name).toBe("Search products");
  });

  it("finds an unlabelled search input by type alone", () => {
    document.body.innerHTML = `<input type="search" name="query">`;
    makeVisible();

    const found = list().find(e => e.role === "searchbox");
    expect(found).toBeDefined();
    expect(found?.search).toBe(true);
    expect(found?.name.toLowerCase()).toContain("search");
  });

  it("detects an ad-skip control by aria-label, not by player classes", () => {
    document.body.innerHTML = `
      <div class="video-player">
        <button aria-label="Skip Ad">Skip Ad</button>
      </div>`;
    makeVisible();

    const found = list().find(e => e.role === "ad-skip");
    expect(found).toBeDefined();
    expect(found?.name).toContain("Skip Ad");
  });

  it("detects a dialog close control by aria-label", () => {
    document.body.innerHTML = `
      <div role="dialog" aria-modal="true">
        <p>Subscribe to our newsletter</p>
        <button aria-label="Close">✕</button>
      </div>`;
    makeVisible();

    const found = list().find(e => e.role === "close-button");
    expect(found).toBeDefined();
    expect(found?.name).toBe("Close");
  });

  it("lists a cookie-banner dismiss button by accessible name", () => {
    document.body.innerHTML = `
      <div role="region" aria-label="Cookie consent">
        <p>We use cookies.</p>
        <button aria-label="Dismiss">Dismiss</button>
      </div>`;
    makeVisible();

    const found = list().find(e => e.name === "Dismiss");
    expect(found).toBeDefined();
    // A dismiss control is a close control by accessible name.
    expect(found?.role).toBe("close-button");
  });

  it("names a chat row by its title text, found by role", () => {
    document.body.innerHTML = `
      <div role="list">
        <div role="listitem"><span title="Team chat">Team chat</span></div>
        <div role="listitem"><span title="Family group">Family group</span></div>
      </div>`;
    makeVisible();

    const rows = list().filter(e => e.role === "listitem");
    expect(rows.map(r => r.name)).toContain("Team chat");
    expect(rows.map(r => r.name)).toContain("Family group");
  });

  it("names a Groups filter button by its text on any site", () => {
    document.body.innerHTML = `
      <div role="toolbar">
        <button>All</button>
        <button>Groups</button>
      </div>`;
    makeVisible();

    const found = list().find(e => e.name === "Groups");
    expect(found).toBeDefined();
    expect(found?.role).toBe("button");
  });

  it("groups a product price by currency text, not by retailer classes", () => {
    document.body.innerHTML = `
      <article>
        <a href="/dp/B123">Wireless Headphones</a>
        <span>₹24,999</span>
      </article>`;
    makeVisible();

    const found = list().find(e => e.targetId && e.name.includes("Wireless Headphones"));
    expect(found).toBeDefined();
    expect(found?.name).toContain("₹24,999");
  });
});
