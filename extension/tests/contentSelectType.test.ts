// @vitest-environment jsdom
import { describe, expect, it, afterEach } from "vitest";
import "../lib/content";

const agent = () => window.__navigatorAgent as unknown as {
  type: (targetId: string, text: string, clear: boolean, name?: string) => { ok: boolean; error?: string };
};

afterEach(() => {
  document.body.innerHTML = "";
});

describe("native <select> filters via type action", () => {
  it('selects the option matching the visible label and fires change', () => {
    document.body.innerHTML = `
      <select id="sort" data-navigator-id="sort-1">
        <option value="">Relevance</option>
        <option value="price_asc">Price -- Low to High</option>
        <option value="price_desc">Price -- High to Low</option>
      </select>`;
    const select = document.getElementById("sort") as HTMLSelectElement;
    let changed = 0;
    select.addEventListener("change", () => { changed++; });

    const res = agent().type("sort-1", "Price -- Low to High", true);

    expect(res.ok).toBe(true);
    expect(select.value).toBe("price_asc");
    expect(changed).toBe(1);
  });

  it('matches by option value when the label differs', () => {
    document.body.innerHTML = `
      <select id="f2" data-navigator-id="f-2">
        <option value="4plus">4★ & above</option>
        <option value="2plus">2★ & above</option>
      </select>`;
    const select = document.getElementById("f2") as HTMLSelectElement;

    const res = agent().type("f-2", "4★ & above", true);

    expect(res.ok).toBe(true);
    expect(select.value).toBe("4plus");
  });

  it('fails honestly when no option matches the requested text', () => {
    document.body.innerHTML = `
      <select id="f3" data-navigator-id="f-3">
        <option value="a">Alpha</option>
        <option value="b">Beta</option>
      </select>`;
    const select = document.getElementById("f3") as HTMLSelectElement;
    const before = select.value;

    const res = agent().type("f-3", "Gamma", true);

    expect(res.ok).toBe(false);
    expect(res.error).toContain('no option matching');
    expect(select.value).toBe(before);
  });
});
