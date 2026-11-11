import { describe, expect, it } from "vitest";
import { findTarget, type Candidate } from "../src/targeting";

/**
 * Since true targetIds are DOM-session-scoped, the executor resolves a
 * BrowserAction target (targetId + name + cssSelector + coords) against the
 * elements the observer collected — the same source the planner saw.
 */
describe("target resolution", () => {
  const candidates: Candidate[] = [
    { targetId: "b1", role: "button", name: "Search", cssSelector: "#b1", enabled: true, visible: true },
    { targetId: "i1", role: "textbox", name: "Query", cssSelector: "#qfield", enabled: true, visible: true },
    { targetId: "b2", role: "button", name: "Search all", cssSelector: "#b2", enabled: false, visible: true },
  ];

  it("resolves by targetId first", () => {
    const t = findTarget({ targetId: "i1" }, candidates);
    expect(t?.cssSelector).toBe("#qfield");
  });

  it("falls back to normalized accessible name", () => {
    const t = findTarget({ targetId: "gone", name: "search all" }, candidates);
    expect(t?.targetId).toBe("b2");
  });

  it("rejects disabled targets for clicks", () => {
    const t = findTarget({ targetId: "b2", name: "Search all" }, candidates, "click");
    expect(t).toBeNull();
  });

  it("returns null when nothing matches", () => {
    expect(findTarget({ targetId: "zz", name: "does not exist" }, candidates)).toBeNull();
  });

  it("resolves targetId even with leading #", () => {
    const t = findTarget({ targetId: "#i1" }, candidates);
    expect(t?.cssSelector).toBe("#qfield");
  });

  it("resolves by substring in long product titles", () => {
    const longCandidates: Candidate[] = [
      { targetId: "p1", role: "link", name: "Realme 15 Pro 5G (8GB RAM, 256GB Storage) Emerald Green", cssSelector: "#p1", enabled: true, visible: true },
    ];
    const t = findTarget({ targetId: "unknown", name: "realme 15 pro" }, longCandidates);
    expect(t?.targetId).toBe("p1");
  });

  it("resolves by token overlap across title", () => {
    const longCandidates: Candidate[] = [
      { targetId: "p2", role: "link", name: "New Realme Mobile Phone 15 Pro Plus Edition", cssSelector: "#p2", enabled: true, visible: true },
    ];
    const t = findTarget({ targetId: "unknown", name: "realme 15 pro" }, longCandidates);
    expect(t?.targetId).toBe("p2");
  });

  it("does not fall back to synthetic nav-el-* when wantedName fails to match", () => {
    const list: Candidate[] = [
      { targetId: "nav-el-5", role: "button", name: "Different Item", cssSelector: "#btn", enabled: true, visible: true },
    ];
    // Specified name doesn't match, so it must not bind to nav-el-5
    const t = findTarget({ targetId: "nav-el-5", name: "Wanted Product" }, list);
    expect(t).toBeNull();
  });

  it("accepts synthetic nav-el-* when no wantedName is specified", () => {
    const list: Candidate[] = [
      { targetId: "nav-el-5", role: "button", name: "Different Item", cssSelector: "#btn", enabled: true, visible: true },
    ];
    const t = findTarget({ targetId: "nav-el-5" }, list);
    expect(t?.targetId).toBe("nav-el-5");
  });

  it("matches target with token overlap when descriptive words vary", () => {
    const list: Candidate[] = [
      { targetId: "el1", role: "link", name: "Realme 15 Pro (Emerald Green, 256GB)", cssSelector: "#el1", enabled: true, visible: true },
    ];
    const t = findTarget({ targetId: "nav-el-12", name: "Realme 15 Pro 5G Emerald Green 8GB 256GB Phone" }, list);
    expect(t?.targetId).toBe("el1");
  });

  it("accepts synthetic nav-el-* when element shares token affinity with wantedName", () => {
    const list: Candidate[] = [
      { targetId: "nav-el-7", role: "link", name: "Realme 15 Pro Smartphone", cssSelector: "#p7", enabled: true, visible: true },
    ];
    const t = findTarget({ targetId: "nav-el-7", name: "Realme 15 Pro Edition" }, list);
    expect(t?.targetId).toBe("nav-el-7");
  });
});
