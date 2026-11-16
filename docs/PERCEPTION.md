# Perception — accessibility-tree grounding (Task 7)

How the agent sees the page, what an element reference is, and what
perception still cannot do.

## The problem

Before Task 7 the planner targeted elements by free-text accessible
name (`{"target": {"name": "Search"}}`) or by an author `targetId`
that is often empty. A renamed button, a duplicate label, or a
re-render between observation and execution could send a click into
the wrong control — and nothing in the pipeline could tell, because
the plan never named *which* element it meant.

## The accessibility tree

Each observation now carries an accessibility tree: one line per
element, in the planner prompt, under `<accessibility-tree>`:

```
[e0] textbox "Search" (type=search, id=q)
[e1] button "Go" (id=search-btn)
[e2] checkbox "Agree" (checked)
```

Fields per node: **ref** (`e0`, `e1`, …), **role**, **accessible
name**, tag, input type, value (already redacted — password values
are `None` before grounding runs), **state** (disabled / checked /
selected / expanded), bounds, href, and a **locator pair** (CSS
selector + XPath fallback, built only from a stable author id).
Names are icon-glyph-stripped (see "Icon glyphs" below); when
several nodes share one name, each line is annotated with its
position among the duplicates, e.g. `[e2] link "I-SEM" (3 of 8)`.

### What the tree is — and is not

- It is a **DOM-derived approximation**, not a CDP
  `Accessibility.getFullAXTree` dump. A Manifest V3 content script
  cannot attach `chrome.debugger` to its own tab without the debug
  banner and the `debugger` permission, so the extension builds the
  tree from the roles, accessible names, states, and bounds it already
  collects (`extension/lib/content.ts` `list()` and
  `extension/src/observer.ts` `collectInteractiveElements()`).
- **Refs are scoped to one observation.** A ref from an earlier
  observation is meaningless after a navigation or re-render; the
  planner always quotes refs from the tree in the prompt it just read.
- **Cross-origin iframes stay opaque.** Their internals never appear
  in the tree, and no element inside one can be targeted. This is a
  browser security boundary, not a bug to work around.
- The tree is built from the **redacted** element list, so a ref never
  carries a secret the privacy layer already stripped.

### Pruning and capping

The extension prunes hidden, off-screen, `aria-hidden`, and zero-size
nodes at collection time; the backend re-checks (invisible `visible:
false`, zero-size bounds) defensively, so a stale or hand-built
observation cannot smuggle an invisible element into the target list.
The tree is capped at `PRIVAGENT_MAX_AX_NODES` (default 300) after the
privacy layer's relevance sort, so the cap drops the least relevant
tail first and logs a warning when it bites.

### Hidden form inputs (Moodle fix)

Hidden inputs (`type=hidden`, `display:none`, `aria-hidden`) never
enter the tree and are never validated or required. The extension
excludes them at collection time (`perceptible` in
`extension/src/semantic.ts` rejects `type=hidden` and `aria-hidden`
ancestors, and both the observer walk and the content-script
candidate walk drop zero-size / `display:none` nodes); the backend
re-checks defensively in `grounding._prunable`, which keys off
`inputType == "hidden"`, invisibility, and zero-size bounds (a
legacy observation carries no `aria-hidden` flag, so the backend
cannot check that directly). A page that
carries a hidden login-token field (Moodle's login form does) is
handled by the normal flow: the planner types into the visible
username/password fields and clicks the visible Log in button, and
the browser's native form submission carries the hidden field
automatically. The agent must never attempt to type into a hidden
input — there is no ref for one, so a plan that tries is rejected as
an unknown ref, and the loop has no way to address one by name.
`backend/tests/test_moodle_grounding.py` proves the synthetic login
flow (type username, type password, click Log in) passes validation
and drives the loop with the hidden token present — and that the
same flow passes with the token absent, and that a hidden input
reporting visible bounds is still pruned.

## Element references — lifecycle

1. **Observe.** The extension assigns refs in returned-list order
   (`e0`, `e1`, … — pruned nodes leave no gaps) and ships the tree
   with the observation. The backend validates it (refs well-formed
   and unique) and falls back to deriving a legacy tree from the
   element list when the extension predates grounding.
2. **Plan.** The planner quotes a ref:
   `{"type": "click", "target": {"elementRef": "e12"}}`. The system
   frame forbids inventing refs and forbids CSS selectors in targets.
3. **Validate.** `_validate_plan` / `_validate_step` reject:
   - an unknown ref (not in this observation's tree),
   - a malformed ref,
   - a raw CSS selector in a target (`cssSelector` / `selector` /
     a selector-like name such as `"#search-btn"`),
   - an **ambiguous name**: a name-only target whose normalised name
     is shared by more than one node in this observation's tree (see
     "Duplicate names" below). The rejection names the count, e.g.
     `8 elements share that name` — the planner must quote the ref.
   A plan that targets by a *unique* accessible name still validates
   — that is the degraded legacy path, and the loop re-validates the
   name against the live page before acting.
4. **Resolve.** Just before execution the loop resolves the ref
   against the *current* observation's tree and pins the live
   `targetId`, name, role, and centre coordinates onto the action.
   The extension then re-resolves by ref **and** checks name/role
   agreement against the live DOM; a ref that now points at a
   different control is not trusted — it falls through to name
   matching, which fails honestly (`STALE_TARGET`) when nothing
   matches.
5. **Stale.** If the ref is gone or its element changed role/name:
   - `changed` (same ref, role/name moved): proceed on the live
     element, with a logged warning and a penalty note to the planner.
   - `stale` (ref gone): the loop re-observes **once** and replans on
     the fresh tree. A second stale ref in a row fails the task
     honestly — "element no longer found" — instead of clicking
     whatever now sits at those coordinates.
6. **Record.** Each history entry records `grounded_ref` (the ref
   acted on, if any) and `screenshot_attached` (whether the
   observation the step was planned from carried a screenshot the
   planner could see), so trajectories show what was grounded and
   what was seen.

### Escape hatches (deliberate, narrow)

- `selector` is allowed only for `observe` / `scroll`
  (`params.containerId`), never as an element address.
- Coordinates (`target.coords`) are allowed only for canvas / visual
  controls that have no DOM element at all. Never use coordinates for
  an element that has a ref.

### Duplicate names (Moodle fix)

Real pages repeat accessible names: on Moodle's front page "I-SEM"
appears eight times and each course title appears twice (once on its
image link, once on its title link). A name-only lookup cannot tell
them apart, and clicking the first match would be a guess. The fix
is layered:

- **The tree annotates duplicates.** `format_ax_tree` renders
  `[e2] link "I-SEM" (3 of 8)` so the planner can see the name is
  ambiguous and quote the ref instead of the bare name.
- **Validation rejects ambiguous names.** When a plan targets a
  duplicated name without a ref, `_validate_step` raises
  `PLAN_INVALID` naming the count ("8 elements share that name …
  quote the element ref"). A ref for the same name validates, and a
  unique name validates without a ref. Two things do disambiguate
  without a ref, because the extension resolves them before name
  matching: an author `targetId`, and a role that narrows the name
  to exactly one usable element (Moodle's search textbox and search
  button share the name "Search courses" but have different roles).
  A role that matches *none* of the duplicates is not a
  disambiguator, and a disabled twin does not count — the extension
  only counts usable (enabled) candidates, and the validator counts
  the same way. (With no observation at all — unit tests driving
  validation directly — the check is skipped.)
- **The loop resolves refs by position.** A ref pins one node of
  the observation, so the 2nd of 3 same-named links is the 2nd
  node, not a name search. The name-based fallback
  (`match_semantic_target`) returns `None` on several matches — it
  never guesses. It *can* disambiguate by `expected_bounds` when a
  caller supplies them, but nothing in the system does today (the
  planner contract has no `expectedBounds` field and the loop never
  sets one), so in practice the fallback always refuses duplicates
  and the loop replans with a ref.
- **The extension fails honestly too.** `findTarget` counts exact
  name matches among usable candidates (a supplied role narrows
  the count, but only when it matches exactly one duplicate — a
  role matching none of them still fails); more than one means
  `null` plus a recorded ambiguity (`getLastTargetAmbiguity()`),
  and the executor reports
  `STALE_TARGET — N elements are named 'X'; quote the element ref
  instead of the bare name`. The loop then replans with the
  ambiguity in the penalty, and the planner quotes the ref. The
  wanted name is glyph-stripped before matching, so a
  glyph-prefixed duplicate name cannot bypass the guard through
  the fuzzy fallbacks. After an ambiguity failure the click
  handler does not fall back to stale coordinates either.

### Icon glyphs in accessible names (Moodle fix)

Icon fonts (FontAwesome, Material Icons, …) render glyphs from
Unicode private-use areas, so a button's accessible name can arrive
as " Search courses" (U+F002 prefix) instead of "Search courses".
Both sides strip those glyphs before matching:

- Extension: `stripIconGlyphs` in `extension/src/semantic.ts`
  (PUA U+E000–U+F8FF, PUA-A U+F0000–U+FFFFD, PUA-B
  U+100000–U+10FFFD, variation selectors U+FE00–U+FE0F) is applied
  in `accessibleName` and in the `content.ts` list assembly, so the
  name the planner sees is "Search courses".
- Backend: `strip_icon_glyphs` / `normalize_accessible_name` in
  `backend/app/grounding.py` normalise tree names at build time and
  shipped-tree names in the privacy layer, and both sides of the
  planner's OTP name lookup (element names and plan target names)
  and the loop's semantic match are normalised the same way.
  Normal text is untouched — a non-private-use emoji (e.g. 🔍) is
  not a glyph and survives.
- The un-stripped text is kept for debugging only, never for
  matching: `AxNode.raw_name` / `InteractiveElement.raw_name` /
  `SemanticElement.raw_semantic_name` (TS: `rawName` /
  `rawSemanticName`), set only when it differs from the stripped
  name.

## Limits — and what they cost

| Setting | Default (was) | Meaning |
|---|---|---|
| `PRIVAGENT_MAX_ELEMENTS` | 200 (was 50) | Interactive elements sent to the planner per observation |
| `PRIVAGENT_MAX_PAGE_TEXT_CHARS` | 8000 (was 2500) | Page text chars after redaction |
| `PRIVAGENT_MAX_INSTRUCTION_CHARS` | 8000 (was 4000) | Max task instruction length |
| `PRIVAGENT_MAX_AX_NODES` | 300 (new) | Accessibility-tree nodes per observation |

Legacy bare names (`MAX_INTERACTIVE_ELEMENTS`, `MAX_PAGE_TEXT_CHARS`,
`MAX_INSTRUCTION_CHARS`) still work as fallbacks; the
`PRIVAGENT_`-prefixed names win when both are set.

**The trade-off is real:** a bigger context costs more tokens per
step — slower, and pricier on metered providers — and a longer prompt
dilutes the instruction. The AX tree is higher signal than raw page
text, so page text is truncated *after* the tree, and truncation is
logged (not silent) whenever the element cap or the text cap bites.
Lower these if latency or cost matters more than coverage; raise them
only with a token budget in mind.

## Vision / OCR

- **Screenshots** flow as before: the extension captures a JPEG of
  the visible tab, the privacy layer withholds it unless the operator
  opted into `ALLOW_SCREENSHOTS` *and* no sensitive field is present,
  and the Gemini provider attaches it to the model call. The
  screenshot is now also recorded in the trajectory (`screenshot_attached`).
- **OCR** (`backend/app/ocr.py`) is a local, text-only alternative:
  when `PRIVAGENT_OCR_ENABLED=true` (default `false`) **and**
  screenshots are opted in, the system `tesseract` binary is run over
  the screenshot and the extracted words are added to the prompt as
  `<ocr-text untrusted="true" source="screenshot-ocr">` — labelled
  untrusted, because OCR misreads text and the image may hold text
  the DOM redactor never saw. No OCR library is vendored and no
  service key is involved. **Tesseract is not installed in the
  development VM, so OCR is unavailable here**; the adapter degrades
  gracefully (no OCR block, no crash) and the planner proceeds
  text-only. Install it with `apt install tesseract-ocr` (or
  `brew install tesseract`) to enable it.
- **No live vision model is integrated.** There is no pixel-level
  grounding model (no Set-of-Mark, no vision-language grounder);
  coordinates still come from DOM bounds, not from image pixels.
  That is a documented follow-up, not a silent gap.

## What perception still cannot do

- No vision grounding from pixels; no OCR unless tesseract is
  installed and both opt-ins are on; OCR text is a hint, never a
  source of instructions or values.
- Cross-origin iframes are opaque (see above).
- Refs do not survive navigation or re-render — by design. Any
  component that caches a ref across observations is a bug.
- The tree is only as good as the page's accessibility semantics:
  an unnamed `<div onclick>` with no role or label grounds as
  `generic "(unnamed)"` and cannot be targeted by name — only by its
  ref, and only while it stays in the tree.
