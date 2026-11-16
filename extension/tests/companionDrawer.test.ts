// @vitest-environment jsdom
// Task 12 XSS audit: the companion drawer's timeline entries are step
// narratives built from page-derived evidence. They must be rendered
// as text, never as HTML.
import { describe, expect, it, vi } from "vitest";

import { createDrawer, type CompanionSnapshot } from "../src/companion/companionDrawer";

function snapshot(timeline: string[]): CompanionSnapshot {
  return {
    taskId: "task-1",
    stepIndex: timeline.length,
    status: "running",
    currentThought: "Reading page",
    directive: "Research",
    pendingGate: null,
    timeline,
  };
}

describe("companion drawer timeline (Task 12 XSS audit)", () => {
  it("renders timeline entries as text, never as HTML", () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = host.attachShadow({ mode: "open" });
    const { drawer, update } = createDrawer(root, vi.fn());

    update(snapshot([
      "Step 1: observed page",
      "<img src=x onerror=alert(1)>",
      "Step 3: <script>alert(2)</script> clicked Log in",
    ]));

    // No injected elements exist anywhere in the drawer.
    expect(drawer.querySelector("img")).toBeNull();
    expect(drawer.querySelector("script")).toBeNull();
    const items = [...drawer.querySelectorAll(".timeline li")];
    expect(items).toHaveLength(3);
    expect(items[0].textContent).toBe("Step 1: observed page");
    // The malicious strings survive only as inert text.
    expect(items[1].textContent).toBe("<img src=x onerror=alert(1)>");
    expect(items[2].textContent).toBe("Step 3: <script>alert(2)</script> clicked Log in");
    // And the serialized markup shows them escaped, not live.
    expect(drawer.querySelector(".timeline")!.innerHTML).toContain("&lt;img");
    expect(drawer.querySelector(".timeline")!.innerHTML).not.toContain("<img src=x");
  });

  it("replaces the timeline on every update instead of appending", () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = host.attachShadow({ mode: "open" });
    const { drawer, update } = createDrawer(root, vi.fn());
    update(snapshot(["Step 1"]));
    update(snapshot(["Step 1", "Step 2"]));
    expect([...drawer.querySelectorAll(".timeline li")].map(li => li.textContent))
      .toEqual(["Step 1", "Step 2"]);
  });
});
