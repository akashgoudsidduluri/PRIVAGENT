import type { ElementTarget } from "./contracts";
import { stripIconGlyphs } from "./semantic";

// Task 13: the icon-glyph strip is imported from ./semantic now —
// the local copy (TARGET_GLYPH_RANGES / targetGlyphRe /
// stripIconGlyphsForTarget) is gone. It existed only because the old
// multi-input vite config split semantic.ts into a shared chunk that
// content.js imported at the top level, and the IIFE wrapper around
// content.js cannot contain a top-level import (the Moodle-fix
// landmine, which broke the build twice). The per-entry vite config
// inlines every import into its own entry, so the cross-entry import
// is safe; tests/viteSelfContainedEntries.test.ts proves it, and
// tests/semanticPerception.test.ts pins the strip itself. The ranges
// must stay identical to _ICON_GLYPH_RE in backend/app/grounding.py.

export interface Candidate {
  targetId: string;
  /** Task 7: element reference from the observation's accessibility tree. */
  elementRef?: string;
  role: string;
  name: string;
  cssSelector: string;
  enabled: boolean;
  visible: boolean;
  bounds?: { x: number; y: number; width: number; height: number };
  href?: string;
  tag?: string;
  inputType?: string;
  /** Explicit observer classification; used for safe search-only recovery. */
  search?: boolean;
}

export interface TargetResolutionOptions {
  /**
   * Set by the executor only after it explicitly classified the intended
   * action as a search. Without this, a stale target is never redirected to a
   * search box.
   */
  searchIntent?: boolean;
  /**
   * Typing must keep the same identity. Fuzzy name/coordinate matching may
   * never silently redirect input into a different field.
   */
  strictIdentity?: boolean;
}

const norm = (s: string | undefined) => (s ?? "").trim().toLowerCase();
const cleanId = (s: string | undefined) => (s ?? "").replace(/^#/, "").trim();

/** Details of the last ambiguity failure, for an honest error message.
 * Reset on every findTarget call; set only when a name-only target
 * matches several elements exactly and no ref disambiguated it. */
export interface TargetAmbiguity {
  name: string;
  count: number;
}
let lastAmbiguity: TargetAmbiguity | null = null;
export function getLastTargetAmbiguity(): TargetAmbiguity | null {
  return lastAmbiguity;
}
const SEARCH_WORD_RE = /\b(search|query|keywords?|find)\b/i;
const NON_TEXT_INPUT_TYPES = new Set([
  "button", "submit", "reset", "checkbox", "radio", "file", "image", "range", "color", "hidden",
]);

function tagOf(candidate: Candidate): string {
  return norm(candidate.tag);
}

function roleOf(candidate: Candidate): string {
  return norm(candidate.role);
}

function inputTypeOf(candidate: Candidate): string {
  return norm(candidate.inputType);
}

function isTextEntry(candidate: Candidate): boolean {
  const tag = tagOf(candidate);
  const role = roleOf(candidate);
  if (tag === "input") {
    // Submit/button/checkbox inputs are controls, never text-entry targets.
    const type = inputTypeOf(candidate);
    if (NON_TEXT_INPUT_TYPES.has(type)) return false;
    return true;
  }
  return tag === "textarea"
    || role === "textbox"
    || role === "searchbox"
    || role === "combobox"
    || role === "input";
}

function isButtonLike(candidate: Candidate): boolean {
  const tag = tagOf(candidate);
  const role = roleOf(candidate);
  const type = inputTypeOf(candidate);
  return tag === "button"
    || role === "button"
    || role === "submit"
    || (tag === "input" && (type === "submit" || type === "button" || type === "reset" || type === "image"));
}

/** True only for a search text-entry control. */
export function isSearchField(candidate: Candidate): boolean {
  if (!isTextEntry(candidate)) return false;
  if (candidate.search === true) return true;
  if (roleOf(candidate) === "searchbox") return true;
  if (inputTypeOf(candidate) === "search") return true;
  const name = norm(candidate.name);
  return SEARCH_WORD_RE.test(name) || name === "q";
}

/** True for a search field or a search submit control. */
export function isSearchControl(candidate: Candidate): boolean {
  if (isSearchField(candidate)) return true;
  if (!isButtonLike(candidate)) return false;
  return candidate.search === true || SEARCH_WORD_RE.test(norm(candidate.name));
}

/**
 * Explicit search-intent classification. `target.search` is the strongest
 * signal; otherwise only a search role/name or a known search-field id counts.
 * A candidate passed here is the element the planner actually saw.
 */
export function classifySearchTarget(
  target: ElementTarget,
  candidate?: Candidate | null
): boolean {
  if (target.search === true) return true;
  if (candidate && isSearchControl(candidate)) return true;
  if (candidate && roleOf(candidate) === "searchbox") return true;
  if (norm(target.role) === "searchbox") return true;
  if (SEARCH_WORD_RE.test(norm(target.name))) return true;
  const tid = cleanId(target.targetId).toLowerCase();
  // Generic search-field ids only (Task 8): no per-retailer ids.
  return /^(q|query|search|searchbox|searchinput|keyword|keywords)$/.test(tid);
}

/** Resolve an action target against the current observation's element list.
 * Priority: CSS selector → author targetId → accessible name (strict for
 * typing) → explicit search-only recovery → synthetic targetId → coordinates.
 * `interaction` filters unusable candidates (disabled). */
export function findTarget(
  target: ElementTarget,
  candidates: Candidate[],
  interaction: "click" | "type" | "none" = "none",
  options: TargetResolutionOptions = {}
): Candidate | null {
  const tid = cleanId(target.targetId || (target as any).id || (target as any).target_id);
  // Review (Moodle fix): the wanted name must be glyph-stripped like
  // the candidate names are. A glyph in the target name otherwise
  // defeats the exact match AND the ambiguity guard below, and the
  // reverse-substring fallback then returns the FIRST duplicate.
  const wantedName = norm(stripIconGlyphs(target.name ?? ""));
  const stem = (s: string) => norm(s).replace(/s$/, "");
  const stemWanted = stem(wantedName);
  const strict = options.strictIdentity === true || interaction === "type";
  const searchIntent = options.searchIntent === true || classifySearchTarget(target);
  lastAmbiguity = null;

  const usable = (c: Candidate, kind: "click" | "type" | "none") => {
    if (kind !== "none" && !c.enabled) return false;
    // A type action must land in an editable control. Some sites expose
    // their search input and search-icon button under the same accessible
    // name; the button sorts first, so without this guard typing resolves
    // to it.
    if (kind === "type" && (isButtonLike(c) || tagOf(c) === "a" || roleOf(c) === "link")) return false;
    return true;
  };

  // 0. Task 7: element reference. Refs are scoped to one observation,
  //    so a ref match is the strongest identity — but only when the
  //    live element still agrees on name/role. A re-render can shift
  //    indices, so a ref that now points at a different control must
  //    NOT be trusted: fall through to name matching instead.
  const wantedRef = typeof target.elementRef === "string" ? target.elementRef.trim() : "";
  if (/^e\d+$/.test(wantedRef)) {
    const byRef = candidates.find(c => c.elementRef === wantedRef);
    if (byRef) {
      // Trust the ref only when every identity hint the caller supplied
      // still agrees. A name that moved means the ref now points at a
      // different control after a re-render — fall through to name
      // matching, which fails honestly when nothing matches.
      const nameAgrees = !wantedName || norm(byRef.name) === wantedName;
      const roleAgrees = !target.role || norm(byRef.role) === norm(target.role);
      if (nameAgrees && roleAgrees && usable(byRef, interaction)) return byRef;
      // Ref points at a different element now: do not trust it, and do
      // not silently click the wrong control — fall through to name
      // matching, which fails honestly when nothing matches.
    }
  }

  // 0b. A caller-supplied CSS selector is a stable identity when it is a real
  //    selector, not a synthetic session registry marker.
  const wantedCss = norm(target.cssSelector);
  if (wantedCss && !wantedCss.includes("data-navigator-id") && !wantedCss.includes("nav-el-")) {
    const byCss = candidates.find(c => norm(c.cssSelector) === wantedCss);
    if (byCss) return usable(byCss, interaction) ? byCss : null;
  }

  // 1. If an author ID exists (not synthetic nav-el-*), match directly.
  if (tid && !tid.startsWith("nav-el-")) {
    const byId = candidates.find(c => cleanId(c.targetId) === tid || c.targetId === target.targetId);
    if (byId) {
      return usable(byId, interaction) ? byId : null;
    }
  }

  // 2. Match by accessible name / label if provided.
  if (wantedName) {
    // Moodle fix: an exact name shared by several usable elements is
    // ambiguous — guessing the first would click an arbitrary one
    // (e.g. "I-SEM" eight times on a Moodle course list, or a course
    // title on both its image link and its title link). Fail honestly
    // instead; the caller reports the count and the planner re-plans
    // quoting the element ref. A supplied role narrows the count, but
    // only when it matches exactly one of the duplicates: a role that
    // matches NONE of them is not a disambiguator, and falling through
    // to the role-blind matches below would click the first duplicate.
    // The reported count is the name-only count, matching the
    // backend validator's error. A ref (step 0) already bypassed this
    // branch.
    const nameMatches = candidates.filter(c => {
      if (!usable(c, interaction)) return false;
      return norm(c.name) === wantedName;
    });
    if (nameMatches.length > 1) {
      const roleMatches = target.role
        ? nameMatches.filter(c => norm(c.role) === norm(target.role))
        : nameMatches;
      if (roleMatches.length === 1) return roleMatches[0];
      lastAmbiguity = { name: wantedName, count: nameMatches.length };
      return null;
    }
    if (nameMatches.length === 1) return nameMatches[0];

    // 2a. Exact match or stem match (group vs groups)
    const exact = candidates.find(c => (norm(c.name) === wantedName || (stemWanted.length >= 4 && stem(c.name) === stemWanted)) && usable(c, interaction));
    if (exact) return exact;

    // Typing may not fall through to fuzzy substring/token matches: those can
    // bind "Email" to "Email or phone" or worse, a nearby search field.
    if (!strict) {
      // 2b. Candidate name contains wanted name (e.g. "Realme 15 Pro Emerald Green" contains "realme 15 pro")
      const substring = candidates.find(c => {
        if (!usable(c, interaction)) return false;
        const cn = norm(c.name);
        return cn.length > 2 && (cn.includes(wantedName) || (stemWanted.length >= 4 && stem(cn).includes(stemWanted)));
      });
      if (substring) return substring;

      // 2c. Wanted name contains candidate name (e.g. wanted "send message" contains candidate "send", "first group" contains "groups")
      const reverseSub = candidates.find(c => {
        if (!usable(c, interaction)) return false;
        const cn = norm(c.name);
        return cn.length >= 3 && (wantedName.includes(cn) || (cn.length >= 4 && stemWanted.includes(stem(cn))));
      });
      if (reverseSub) return reverseSub;

      // 2d. Multi-token match (all words in wanted are present in candidate)
      const wantedTokens = wantedName.split(/\s+/).filter(w => w.length > 1);
      if (wantedTokens.length > 1) {
        const tokenMatch = candidates.find(c => {
          if (!usable(c, interaction)) return false;
          const cn = norm(c.name);
          return wantedTokens.every(tok => cn.includes(tok));
        });
        if (tokenMatch) return tokenMatch;

        // 2e. Token overlap matching: candidate name contains majority (>= 60%) of wanted tokens
        // or candidate's tokens are all contained within wantedName
        let bestCandidate: Candidate | null = null;
        let highestScore = 0;
        for (const c of candidates) {
          if (!usable(c, interaction)) continue;
          const cn = norm(c.name);
          const cTokens = cn.split(/\s+/).filter(w => w.length > 1);
          if (cTokens.length === 0) continue;
          const matched = wantedTokens.filter(tok => cn.includes(tok)).length;
          const score = matched / wantedTokens.length;
          const candSubset = cTokens.length >= 2 && cTokens.every(tok => wantedTokens.some(wt => wt.includes(tok)));
          if ((score >= 0.6 && score > highestScore) || candSubset) {
            highestScore = score;
            bestCandidate = c;
          }
        }
        if (bestCandidate) return bestCandidate;
      }
    }
  }

  // 3. Search-specific stale recovery. This branch is reachable only when the
  //    executor explicitly classified the intended action as a search.
  if (searchIntent) {
    if (interaction === "type") {
      const fields = candidates.filter(c => usable(c, interaction) && isSearchField(c));
      // Exactly one search field is safe to re-resolve; zero or multiple is
      // ambiguous and must fail instead of guessing.
      if (fields.length === 1) return fields[0];
    } else if (interaction === "click") {
      const controls = candidates.filter(c => usable(c, interaction) && isSearchControl(c));
      const buttons = controls.filter(isButtonLike);
      if (buttons.length === 1) return buttons[0];
      if (controls.length === 1) return controls[0];
    }
  }

  // 4. Fallback to synthetic targetId (nav-el-*).
  if (tid) {
    const byId = candidates.find(c => cleanId(c.targetId) === tid || c.targetId === target.targetId);
    if (byId && usable(byId, interaction)) {
      if (!wantedName) {
        return byId;
      }
      if (!strict) {
        const cn = norm(byId.name);
        const wantedTokens = wantedName.split(/\s+/).filter(w => w.length > 1);
        const sharesToken = wantedTokens.some(tok => cn.includes(tok));
        const sharesRole = target.role ? byId.role === target.role : true;
        if (sharesToken || (cn.length === 0 && sharesRole) || (!tid.startsWith("nav-el-"))) {
          return byId;
        }
      }
    }
  }

  // 5. Fallback to bounding box coordinates. A stale identity-bearing typing
  //    target may not be redirected by coordinates — the old rectangle can now
  //    contain a different field.
  if (target.coords && !(interaction === "type" && (tid || wantedName))) {
    const { x, y } = target.coords;
    const byCoords = candidates.find(c => {
      if (!c.bounds || !usable(c, interaction)) return false;
      const b = c.bounds;
      return x >= b.x && x <= b.x + b.width && y >= b.y && y <= b.y + b.height;
    });
    if (byCoords) return byCoords;
  }
  return null;
}
