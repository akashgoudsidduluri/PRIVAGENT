// @vitest-environment jsdom
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

describe("search control discovery", () => {
  it("discovers an Amazon-style search input without a placeholder", () => {
    document.body.innerHTML = `
      <form id="nav-search-bar-form" role="search" action="/s">
        <input id="twotabsearchtextbox" name="field-keywords" type="text"
               class="nav-input nav-progressive-attribute" autocomplete="off">
        <input type="submit" value="Go" aria-label="Go">
      </form>`;
    makeVisible();

    const found = list().find(e => e.targetId === "twotabsearchtextbox");
    expect(found).toBeDefined();
    expect(found?.search).toBe(true);
    expect(found?.role).toBe("searchbox");
    expect(found?.name.toLowerCase()).toContain("search");
    expect(found?.name.toLowerCase()).toContain("field-keywords");

    const input = document.getElementById("twotabsearchtextbox") as HTMLInputElement;
    // Original identity/attributes must survive observation.
    expect(input.id).toBe("twotabsearchtextbox");
    expect(input.name).toBe("field-keywords");
    expect(input.type).toBe("text");
    expect(input.getAttribute("placeholder")).toBeNull();
    expect(input.getAttribute("data-navigator-id")).toBe("twotabsearchtextbox");
  });

  it("keeps Flipkart's accessible search label intact", () => {
    document.body.innerHTML = `
      <input id="fk-search" class="Pke_EE" type="text" name="q"
             title="Search for Products, Brands and More" autocomplete="off">`;
    makeVisible();

    const found = list().find(e => e.targetId === "fk-search");
    expect(found).toBeDefined();
    expect(found?.name).toBe("Search for Products, Brands and More");
    expect(found?.search).toBe(true);
    expect(found?.role).toBe("searchbox");
    expect(found?.inputType).toBe("text");
  });

  it("resolves aria-labelledby before falling back to attributes", () => {
    document.body.innerHTML = `
      <span id="search-label">Search our catalogue</span>
      <input id="catalog" name="catalog-query" type="text" aria-labelledby="search-label">`;
    makeVisible();

    const found = list().find(e => e.targetId === "catalog");
    expect(found?.name).toBe("Search our catalogue");
    expect(found?.search).toBe(true);
  });

  it("resolves a native associated <label for>", () => {
    document.body.innerHTML = `
      <label for="site-search">Find an article</label>
      <input id="site-search" name="term" type="text">`;
    makeVisible();

    const found = list().find(e => e.targetId === "site-search");
    expect(found?.name).toBe("Find an article");
    expect(found?.search).toBe(true);
  });

  it("classifies a generic type=search input with no name or placeholder", () => {
    document.body.innerHTML = `<input id="plain-search" type="search">`;
    makeVisible();

    const found = list().find(e => e.targetId === "plain-search");
    expect(found?.search).toBe(true);
    expect(found?.role).toBe("searchbox");
    expect(found?.name).toBe("Search");
  });
});
