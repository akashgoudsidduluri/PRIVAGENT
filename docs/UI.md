# UI — plan preview, Privacy Center, timeline, responsive layout (Task 12)

## What was added

### Plan preview (confirmation gate)
Before a plan executes, the workspace shows the operator the planned steps
in plain language — action type, target name/role, risk — inside the
confirmation banner (`agent.html` `#planPreview`, rendered by
`src/agentApp.ts:showHumanGate`). The preview is built from the plan's
**structured fields only** (`backend/app/plan_preview.py`): navigate steps
show the host only (never path/query), a type into a secret-named target
is masked (`•••••• (hidden — sensitive field)` — password, passcode,
PIN, OTP, token, and related names; REVIEW: the Task 12 code only
masked names containing "password", so a literal typed into a
"Passcode"/"PIN" field was shown in full — fixed, see below), vault placeholders
(`{{CREDENTIAL:…}}`) are shown as the placeholder, plain typed text is
truncated to 120 chars, and planner free text (`why`), evidence and URLs
are never included. Malformed steps degrade to `"unknown step"`.

Controls: **Approve** (run the whole plan), **Approve this step only**
(`approve_step`), **Reject**. Approve-step-by-step reuses the existing
confirmation gate — it is not a second gate. It does not weaken any
gate: after an approved step the loop re-observes and re-plans, so the
preview updates when the plan changes. The backend still enforces its
rules per step — validation (`PLAN_INVALID`) rejects before the gate,
takeover pauses observation, and the payment amount cap is a hard
block. One precision: an allow-list denial or isolation-checker flag
already goes to the operator as an explicit one-off override decision
(Task 3 design, audited) — approval there is the operator's informed
choice, not a silent bypass; `approve_step` adds no new override. See
`backend/app/agentLoop.py:_request_confirmation` and
`backend/app/schemas.py:ConfirmInput`.

### Privacy Center (control center, view 10)
One panel for the Task 10 privacy features (`src/controlViews.ts`,
index 10, data from `GET /api/control/privacy`):
- **Planner status** — active provider/model, local vs cloud, LOCAL_ONLY
  and zero-retention flags with their current values. These are backend
  env vars; the UI labels them read-only and names the variables
  (`PRIVAGENT_PLANNER_LOCAL_ONLY`, `PRIVAGENT_ZERO_RETENTION`).
- **Audit log viewer** — `GET /api/audit` (Task 1 token auth) with event
  filter (all 11 event types), limit 25/50/100, refresh button, chain
  status tag (`✓ Chain intact` / `⚠ Chain broken at entry N`), entries
  rendered as escaped text.
- **Tracker blocklist** — the 24 DNR hosts grouped by category
  (`backend/app/tracker_hosts.py`, the backend mirror of
  `extension/rules/trackers.json`; a parity test keeps them in sync),
  with a plain-language explanation.
- **DPIA summary** — short summary + link to `docs/DPIA.md`.

### Task timeline
`src/timeline.ts` joins Task 11 `LoopTimings` steps with checkpoint
history by step number (`GET /api/task/{id}` `timings` + `steps`):
each row shows step, action type, status
(`verified` / `completed_unverified` / `failed` / `executed`), duration,
and flags (`batched`, `cache hit`, `checker skipped`). Rendered with
`textContent` only (`agent.html` `#taskTimelineCard`).

### Responsive layout
A `@media (max-width: 480px)` layer in `src/styles/agent.css` and
`src/styles/control-center-refresh.css` (on top of the existing
850px/600px breakpoints): header and nav rail stay usable, gate buttons
and action buttons go full-width with `min-height: 44px`, the plan
preview and timeline rows reflow (timeline becomes a two-line grid),
cards get tighter padding, tracker host lists wrap. No CSS framework.

## XSS audit (Task 12)

Every `innerHTML` / `outerHTML` / `document.write` /
`insertAdjacentHTML` sink in `src/`, `extension/src/`, `agent.html` and
`index.html` was audited. Results (measured 2026-11-16):

| Category | Count |
|---|---|
| `innerHTML` assignment sites (total) | 66 |
| — empty-string clears (`innerHTML = ""`) | 9 |
| — non-clear sites, proven safe by the guard test | 57 |
| `outerHTML` / `document.write` / `insertAdjacentHTML` | 0 |
| Real vulnerabilities found and fixed | 3 (4 code sites) |

Fixed:
1. `extension/src/companion/companionDrawer.ts` — the timeline rendered
   page-derived step narratives with
   `timeline.innerHTML = snapshot.timeline.map(step => …)`. Now built
   with `textContent` (`replaceChildren()` + `li.textContent = step`).
2. `src/agentApp.ts` — a `sessionId` slice was interpolated unescaped
   into innerHTML (a sibling site escaped it; this one did not). Now
   `escapeHtml(...)`.
3. `src/agentApp.ts` — the backend `file_format` was interpolated raw
   into a class attribute and `artifact_id` raw into hrefs. Format is
   now whitelisted (`pdf/docx/pptx/xlsx/csv/html/md/txt/png/jpg/jpeg`,
   else `"file"`); artifact ids go through `encodeURIComponent`.

The remaining 57 non-clear sites are safe by construction and proven by
the guard test, not by inspection alone: every interpolated value passes
through `escapeHtml` / `safe` / `linkify` / `renderMarkdown` /
`formatResultHtml` / `formatSummary`, a trusted builder (`tag` /
`button` / `artifactUrl` / `actionIcon`), or is a compile-time constant
/ number / formatted date. `card()` / `empty()` do **not** escape their
arguments — their callers must (and do, via `safe()`).

Guard test: `backend/tests/test_no_innerhtml.py` — a syntactic analyser
that scans `src/` + `extension/src/` (+ `agent.html`, `index.html`) for
`.innerHTML =` / `+=` assignments and proves every interpolated value
safe; it also asserts zero `outerHTML` / `document.write` /
`insertAdjacentHTML`.

REVIEW (Task 12 audit): the guard as committed had two holes, so its
"proven safe" claim did not hold at the time:
1. Template literals were scanned starting in "code" mode, so no
   `${...}` inside a backtick template was ever inspected — the most
   common interpolation style in this codebase. Reverting the drawer
   fix to the original template-literal line (timeline innerHTML built
   from snapshot.timeline.map with a li template per step)
   did **not** fail the guard, despite the Task 12 claim that it was
   negative-tested.
2. `_is_literal` used a first-char/last-char check, so a concatenation
   like `'<p>' + x + '</p>'` was treated as one string literal and the
   unescaped value waved through.
Both are fixed (template bodies are scanned in template mode; a literal
must be one escape-aware string; nested interpolations are no longer
double-counted outside their escaper call). The fixed guard newly
surfaced 9 sites — all verified false positives by reading each one
(a `.filter(...).length` count, `Math.round(...)`, locale date
formatting, a `.map()` index in arithmetic) — and the guard now has
narrow sound rules for those shapes plus 5 regression tests pinning
the two holes. Reverting the drawer fix now fails the guard with
`unescaped identifier: step` (verified in the review).

## Verification (and its limits)

- **Headless Chrome layout check — NOT completed.** The VM's managed
  Chromium (`/opt/meta-chromium/chrome`, Muse Browser 157) refuses to
  start standalone (it needs a Hatch WebGL domain-policy file + SHA-256
  pair; a valid file was constructed and the binary then starts), but
  then blocks **every** navigation to a non-public address with
  `net::ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS` — loopback
  (`127.0.0.1`), the VM's private IP (`198.19.0.2`), argv navigation,
  CDP `Page.navigate`, and browser-level `/json/new` target creation
  all fail, and `--disable-features=LocalNetworkAccessChecks` /
  `--disable-web-security` / a `LocalNetworkAccessAllowedForUrls`
  managed-policy allowlist do not lift it (the policy files under
  `/etc/opt/chrome/policies/managed/` were temporarily extended for the
  test and restored byte-for-byte afterwards). The browser is meant to
  be driven through the runtime's broker, not launched from a shell.
  The CDP driver is saved at
  `~/workspace/privagent-analysis/task12-layout-check.mjs` so a
  browser-task-capable agent can run the same 390px/820px measurement
  (overflow scan, tap-target heights, hostile-plan XSS check,
  screenshots) against the built frontend.
- **What was verified instead:** (a) rule-presence tests
  (`tests/task12_ui.test.mjs`, 10 tests) pin the 480px rules in source
  CSS; (b) a DOM smoke test
  (`~/workspace/privagent-analysis/task12-verify-dom.mjs`, 25 checks,
  all passing) parses the **built** `dist/agent.html`, asserts every
  new node exists, replays the app's `textContent` render path with a
  hostile plan step (stays inert text), and asserts the built CSS
  bundle carries the 480px block and the new classes; (c) the built
  agent bundle contains `btnGateApproveStep`, `approve_step`, and the
  Privacy Center view. This proves the rules and DOM ship in the build;
  it does **not** prove the visual layout at 390px — that still needs
  a real browser run.
