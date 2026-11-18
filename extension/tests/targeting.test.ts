import { describe, expect, it } from "vitest";
import { findTarget, getLastTargetAmbiguity, isSearchField, type Candidate } from "../src/targeting";

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

  it("never binds a type action to a same-named search button", () => {
    // Flipkart exposes the search icon button and the search input under the
    // same accessible name; document order puts the button first.
    const list: Candidate[] = [
      { targetId: "nav-el-1", role: "button", name: "Search for Products, Brands and More", tag: "button", search: true, cssSelector: "#icon", enabled: true, visible: true },
      { targetId: "nav-el-2", role: "searchbox", name: "Search for Products, Brands and More", tag: "input", inputType: "text", search: true, cssSelector: "#input", enabled: true, visible: true },
    ];
    const t = findTarget(
      { targetId: "nav-el-2", name: "Search for Products, Brands and More", search: true },
      list,
      "type",
    );
    expect(t?.targetId).toBe("nav-el-2");
    expect(t?.tag).toBe("input");
  });

  it("fails closed instead of typing into a button when no editable target matches", () => {
    const list: Candidate[] = [
      { targetId: "nav-el-1", role: "button", name: "Search for Products, Brands and More", tag: "button", search: true, cssSelector: "#icon", enabled: true, visible: true },
    ];
    expect(findTarget({ targetId: "nav-el-2", name: "Search for Products, Brands and More", search: true }, list, "type")).toBeNull();
  });

  it("never treats a search submit input as a text-entry field", () => {
    const submit: Candidate = {
      targetId: "nav-search-submit-button", role: "input", name: "Search", tag: "input",
      inputType: "submit", search: true, cssSelector: "#submit", enabled: true, visible: true,
    };
    const textbox: Candidate = {
      targetId: "twotabsearchtextbox", role: "searchbox", name: "Search Amazon.in", tag: "input",
      inputType: "text", search: true, cssSelector: "#q", enabled: true, visible: true,
    };
    expect(isSearchField(submit)).toBe(false);
    expect(isSearchField(textbox)).toBe(true);
    const t = findTarget({ targetId: "gone", name: "Search" }, [submit, textbox], "type");
    expect(t?.targetId).toBe("twotabsearchtextbox");
  });
});

describe("element-ref resolution (Task 7)", () => {
  const candidates: Candidate[] = [
    { targetId: "b1", elementRef: "e0", role: "button", name: "Search", cssSelector: "#b1", enabled: true, visible: true },
    { targetId: "i1", elementRef: "e1", role: "textbox", name: "Query", cssSelector: "#qfield", enabled: true, visible: true },
    { targetId: "b2", elementRef: "e2", role: "button", name: "Go", cssSelector: "#b2", enabled: true, visible: true },
  ];

  it("resolves by elementRef alone", () => {
    const t = findTarget({ targetId: "", elementRef: "e1" }, candidates, "type");
    expect(t?.targetId).toBe("i1");
  });

  it("ref wins over a stale targetId", () => {
    // After a re-render the author id may be gone; the ref still pins it.
    const t = findTarget({ targetId: "gone", elementRef: "e2" }, candidates, "click");
    expect(t?.targetId).toBe("b2");
  });

  it("ref pointing at a different element is not trusted", () => {
    // The ref says e0 but the planner also names "Go": the live element
    // at e0 is "Search", so the ref must not be trusted — fall through
    // to name matching, which finds the real "Go" button.
    const t = findTarget({ targetId: "", elementRef: "e0", name: "Go" }, candidates, "click");
    expect(t?.targetId).toBe("b2");
  });

  it("unknown ref falls through to name matching, then fails honestly", () => {
    expect(findTarget({ targetId: "", elementRef: "e99", name: "Go" }, candidates, "click")?.targetId).toBe("b2");
    expect(findTarget({ targetId: "", elementRef: "e99", name: "Nope" }, candidates, "click")).toBeNull();
    expect(findTarget({ targetId: "", elementRef: "e99" }, candidates, "click")).toBeNull();
  });

  it("malformed ref is ignored, not trusted", () => {
    const t = findTarget({ targetId: "", elementRef: "ref-7", name: "Query" }, candidates, "type");
    expect(t?.targetId).toBe("i1");
  });

  it("disabled ref target is rejected for clicks", () => {
    const disabled = candidates.map(c => c.elementRef === "e0" ? { ...c, enabled: false } : c);
    expect(findTarget({ targetId: "", elementRef: "e0" }, disabled, "click")).toBeNull();
  });
});

describe("duplicate-name ambiguity (Moodle fix)", () => {
  // Moodle's course list: "I-SEM" eight times, each course title twice.
  const duplicates: Candidate[] = [
    { targetId: "c1", elementRef: "e0", role: "link", name: "I-SEM", cssSelector: "#c1", enabled: true, visible: true },
    { targetId: "c2", elementRef: "e1", role: "link", name: "I-SEM", cssSelector: "#c2", enabled: true, visible: true },
    { targetId: "c3", elementRef: "e2", role: "link", name: "I-SEM", cssSelector: "#c3", enabled: true, visible: true },
  ];

  it("refuses a name-only target shared by several elements", () => {
    const t = findTarget({ targetId: "gone", name: "I-SEM" }, duplicates, "click");
    expect(t).toBeNull();
    expect(getLastTargetAmbiguity()).toEqual({ name: "i-sem", count: 3 });
  });

  it("resolves the right duplicate by element ref", () => {
    // The 2nd of 3 same-named links: the ref pins it by position.
    const t = findTarget({ targetId: "", elementRef: "e1" }, duplicates, "click");
    expect(t?.targetId).toBe("c2");
    expect(getLastTargetAmbiguity()).toBeNull();
  });

  it("resolves a duplicate when the role narrows it to one", () => {
    const pair: Candidate[] = [
      { targetId: "sb", role: "button", name: "Search", cssSelector: "#sb", enabled: true, visible: true },
      { targetId: "sf", role: "searchbox", name: "Search", tag: "input", inputType: "text", cssSelector: "#sf", enabled: true, visible: true },
    ];
    // Without a role the name is ambiguous.
    expect(findTarget({ targetId: "gone", name: "Search" }, pair, "click")).toBeNull();
    expect(getLastTargetAmbiguity()).toEqual({ name: "search", count: 2 });
    // With a role it is not: exactly one searchbox is named "Search".
    const t = findTarget({ targetId: "gone", name: "Search", role: "searchbox" }, pair, "click");
    expect(t?.targetId).toBe("sf");
    expect(getLastTargetAmbiguity()).toBeNull();
  });

  it("clears the ambiguity record on a successful resolution", () => {
    findTarget({ targetId: "gone", name: "I-SEM" }, duplicates, "click");
    expect(getLastTargetAmbiguity()).not.toBeNull();
    findTarget({ targetId: "c1", name: "I-SEM" }, duplicates, "click");
    expect(getLastTargetAmbiguity()).toBeNull();
  });

  it("a glyph-prefixed duplicate name is still ambiguous, never the first match (review)", () => {
    // The planner can quote a name with an icon glyph (e.g. from raw page
    // text). The glyph must not let the ambiguity guard be bypassed via
    // the reverse-substring fallback, which would click the first
    // duplicate. U+F002 built from its code point.
    const glyph = String.fromCodePoint(0xf002);
    const t = findTarget({ targetId: "gone", name: `${glyph} I-SEM` }, duplicates, "click");
    expect(t).toBeNull();
    expect(getLastTargetAmbiguity()).toEqual({ name: "i-sem", count: 3 });
  });

  it("a glyph-prefixed name still honours the element ref (review)", () => {
    // Ref agreement must compare glyph-stripped names, or the ref is
    // distrusted and the fallbacks return the FIRST duplicate.
    const glyph = String.fromCodePoint(0xf002);
    const t = findTarget({ targetId: "", elementRef: "e1", name: `${glyph} I-SEM` }, duplicates, "click");
    expect(t?.targetId).toBe("c2");
    expect(getLastTargetAmbiguity()).toBeNull();
  });

  it("a glyph-prefixed unique name resolves to the right element (review)", () => {
    const glyph = String.fromCodePoint(0xf002);
    const btn: Candidate[] = [
      { targetId: "sb", elementRef: "e0", role: "button", name: "Search courses", cssSelector: "#sb", enabled: true, visible: true },
    ];
    const t = findTarget({ targetId: "gone", name: `${glyph} Search courses` }, btn, "click");
    expect(t?.targetId).toBe("sb");
  });

  // Task 13: the local glyph-strip copy is gone — targeting.ts imports
  // stripIconGlyphs from ./semantic (the per-entry vite config makes
  // that safe; tests/viteSelfContainedEntries.test.ts proves the build
  // survives the cross-entry import). The strip itself is pinned in
  // tests/semanticPerception.test.ts, and the findTarget test above
  // exercises it through targeting's own path (a glyph-prefixed
  // wanted name resolves the plain-named candidate).
});

describe("safe stale-target recovery after SPA rerender", () => {
  const searchField: Candidate = {
    targetId: "nav-el-42",
    role: "searchbox",
    name: "Search our catalogue",
    tag: "input",
    inputType: "text",
    search: true,
    cssSelector: "[data-navigator-id=\"nav-el-42\"]",
    enabled: true,
    visible: true,
  };
  const emailField: Candidate = {
    targetId: "email",
    role: "textbox",
    name: "Email address",
    tag: "input",
    inputType: "email",
    cssSelector: "#email",
    enabled: true,
    visible: true,
  };

  it("re-resolves a stale synthetic id using the accessible name", () => {
    const t = findTarget(
      { targetId: "nav-el-5", name: "Search our catalogue" },
      [searchField],
      "type",
    );
    expect(t?.targetId).toBe("nav-el-42");
  });

  it("recovers the unique search field only when the action is explicitly search", () => {
    // The name alone does not classify this as a search, so the explicit
    // executor flag is the only thing that permits recovery.
    const target = { targetId: "nav-el-5", name: "Go" };
    const recovered = findTarget(target, [searchField], "type", { searchIntent: true });
    expect(recovered?.targetId).toBe("nav-el-42");

    const refused = findTarget(target, [searchField], "type");
    expect(refused).toBeNull();
  });

  it("does not redirect a stale email/password field into a search box", () => {
    const searchOnly = [searchField];
    expect(findTarget({ targetId: "nav-el-8", name: "Email" }, searchOnly, "type")).toBeNull();
    expect(findTarget({ targetId: "nav-el-8", name: "Password" }, searchOnly, "type")).toBeNull();
    expect(findTarget({ targetId: "nav-el-8", name: "Email", search: false }, searchOnly, "type", { searchIntent: false })).toBeNull();
  });

  it("does not replace an arbitrary stale button target with a search input", () => {
    const t = findTarget(
      { targetId: "nav-el-3", name: "Sign in" },
      [searchField],
      "click",
      { searchIntent: false },
    );
    expect(t).toBeNull();
  });

  it("refuses ambiguous search recovery instead of guessing", () => {
    const second: Candidate = { ...searchField, targetId: "nav-el-43", name: "Search the site" };
    expect(
      findTarget({ targetId: "gone", name: "Search", search: true }, [searchField, second], "type", { searchIntent: true }),
    ).toBeNull();
  });

  it("can recover a unique search submit button for a search click", () => {
    const searchButton: Candidate = {
      targetId: "search-submit",
      role: "button",
      name: "Search",
      tag: "button",
      search: true,
      cssSelector: "#search-submit",
      enabled: true,
      visible: true,
    };
    const t = findTarget(
      { targetId: "gone", name: "Search", search: true },
      [searchField, searchButton],
      "click",
      { searchIntent: true },
    );
    expect(t?.targetId).toBe("search-submit");
  });

  it("keeps coordinate fallback for clicks but not for stale typing identities", () => {
    const field: Candidate = {
      ...emailField,
      bounds: { x: 10, y: 10, width: 200, height: 30 },
    };
    const target = { targetId: "gone", name: "Email", coords: { x: 20, y: 20 } };
    expect(findTarget(target, [field], "click")).not.toBeNull();
    expect(findTarget(target, [field], "type")).toBeNull();
  });
});
