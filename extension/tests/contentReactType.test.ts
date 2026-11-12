// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import "../lib/content";

const agent = () => window.__navigatorAgent as unknown as {
  type: (targetId: string, text: string, clear: boolean, name?: string) => { ok: boolean; error?: string; value?: string };
  readValue: (targetId: string, name?: string) => { ok: boolean; value?: string };
};

afterEach(() => {
  document.body.innerHTML = "";
});

describe("React-controlled input fallback typing", () => {
  it("fires input/change and keeps controlled state in sync with the DOM value", () => {
    document.body.innerHTML = `
      <input id="react-search" type="text" name="q" aria-label="Search products">`;
    const input = document.getElementById("react-search") as HTMLInputElement;

    // Minimal controlled-component model: application state follows the
    // input event, exactly as React's onChange handler would.
    let appState = "";
    const events: string[] = [];
    input.addEventListener("input", () => {
      events.push("input");
      appState = input.value;
    });
    input.addEventListener("change", () => {
      events.push("change");
      appState = input.value;
    });

    const result = agent().type("react-search", "titan watch", true, "Search products");

    expect(result.ok).toBe(true);
    expect(events).toContain("input");
    expect(events).toContain("change");
    expect(input.value).toBe("titan watch");
    expect(appState).toBe("titan watch");
    expect(result.value).toBe("titan watch");
  });

  it("appends without clearing and reports the real DOM value", () => {
    document.body.innerHTML = `<input id="react-append" type="text" aria-label="Search">`;
    const input = document.getElementById("react-append") as HTMLInputElement;
    input.value = "blue ";

    const result = agent().type("react-append", "shoes", false, "Search");

    expect(result.ok).toBe(true);
    expect(input.value).toBe("blue shoes");
    expect(result.value).toBe("blue shoes");
    expect(agent().readValue("react-append").value).toBe("blue shoes");
  });
});
