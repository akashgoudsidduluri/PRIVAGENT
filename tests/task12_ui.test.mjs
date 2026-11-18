import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { formatPlanStep, planPreviewTitle } from "../src/planPreview.ts";
import { buildTimelineRows, formatDuration, timelineSummary } from "../src/timeline.ts";

const root = new URL("..", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const read = (p) => readFileSync(join(root, p), "utf8");

// ---------------------------------------------------------------------------
// Plan preview formatting (Task 12): plain text only, never HTML.
// ---------------------------------------------------------------------------

test("formatPlanStep composes type, risk, target and detail as plain text", () => {
  const line = formatPlanStep({
    index: 0, type: "type", risk: "write",
    target: { name: "Username", role: "textbox", elementRef: "e0" },
    detail: 'type "student"',
  });
  assert.equal(line, '1. Type [write] — Username (textbox): type "student"');
});

test("formatPlanStep never wraps output in markup, even for hostile names", () => {
  const line = formatPlanStep({
    index: 1, type: "click", risk: "write",
    target: { name: "<img src=x onerror=alert(1)>", role: "button", elementRef: "e2" },
    detail: 'click "<img src=x onerror=alert(1)>"',
  });
  // The hostile string is carried as text — the renderer (textContent)
  // is what keeps it inert; the formatter must not add any tags of its own.
  assert.ok(!line.includes("<li>") && !line.includes("<strong>"));
  assert.ok(line.includes("<img src=x onerror=alert(1)>"));
  assert.ok(line.startsWith("2. Click [write] — "));
});

test("formatPlanStep omits the risk badge for read-only steps", () => {
  const line = formatPlanStep({
    index: 0, type: "observe", risk: "read",
    target: { name: null, role: null, elementRef: null },
    detail: "look at the page",
  });
  assert.equal(line, "1. Observe: look at the page");
});

test("planPreviewTitle counts steps", () => {
  assert.equal(planPreviewTitle([]), "Plan preview");
  assert.equal(planPreviewTitle(null), "Plan preview");
  assert.equal(planPreviewTitle([{ index: 0 }]), "Plan preview — 1 step");
  assert.equal(planPreviewTitle([{ index: 0 }, { index: 1 }]), "Plan preview — 2 steps");
});

// ---------------------------------------------------------------------------
// Task timeline rows (Task 12): timings + history joined by step number.
// ---------------------------------------------------------------------------

test("buildTimelineRows joins status from history and flags from timings", () => {
  const rows = buildTimelineRows(
    [
      { step: 1, action: "type", batched: false, cache: "miss", checker_skipped: false, total_ms: 320, overhead_ms: 300 },
      { step: 2, action: "type", batched: true, cache: "n/a", checker_skipped: true, total_ms: 140, overhead_ms: 120 },
      { step: 3, action: "click", batched: false, cache: "n/a", checker_skipped: false, total_ms: 900, overhead_ms: 850 },
    ],
    [
      { stepNum: 1, message: "Step 1", status: "verified", result_ok: true },
      { stepNum: 2, message: "Step 2", status: "completed_unverified", result_ok: true },
      { stepNum: 3, message: "Step 3", status: "failed", result_ok: false },
    ],
  );
  assert.equal(rows.length, 3);
  assert.equal(rows[0].status, "verified");
  assert.equal(rows[1].status, "completed_unverified");
  assert.equal(rows[1].batched, true);
  assert.equal(rows[1].checkerSkipped, true);
  assert.equal(rows[2].status, "failed");
  assert.equal(rows[2].durationMs, 900);
});

test("buildTimelineRows defaults a missing history step to executed", () => {
  const rows = buildTimelineRows(
    [{ step: 1, action: "navigate", batched: false, cache: "miss", checker_skipped: false, total_ms: 500, overhead_ms: 480 }],
    [],
  );
  assert.equal(rows[0].status, "executed");
  assert.equal(buildTimelineRows(null, null).length, 0);
});

test("formatDuration and timelineSummary", () => {
  assert.equal(formatDuration(340), "340ms");
  assert.equal(formatDuration(1250), "1.3s");
  assert.equal(formatDuration(-5), "—");
  const rows = buildTimelineRows(
    [
      { step: 1, action: "type", batched: true, cache: "hit", checker_skipped: false, total_ms: 100, overhead_ms: 90 },
      { step: 2, action: "click", batched: false, cache: "miss", checker_skipped: false, total_ms: 200, overhead_ms: 180 },
    ],
    [],
  );
  assert.equal(timelineSummary(rows, 270), "2 steps · 1 batched · 1 cache hit · 270ms overhead");
  assert.equal(timelineSummary([], null), "No steps yet");
});

// ---------------------------------------------------------------------------
// Wiring: the workspace actually renders the preview, the step-only
// control, and the timeline (static checks over the sources).
// ---------------------------------------------------------------------------

test("workspace has the plan preview block and approve-step control", () => {
  const html = read("agent.html");
  for (const id of ["planPreview", "planPreviewTitle", "planPreviewList", "btnGateApproveStep", "taskTimelineCard", "taskTimelineList", "timelineSummary"]) {
    assert.ok(html.includes(`id="${id}"`), `agent.html is missing #${id}`);
  }
  const app = read("src/agentApp.ts");
  assert.ok(app.includes('decision: "approve_step"'), "no approve_step POST in agentApp.ts");
  assert.ok(app.includes("showHumanGate(msg, t.pending_confirmation?.plan"), "preview not fed from pending_confirmation");
  assert.ok(app.includes("renderTaskTimeline(t)"), "timeline not rendered from task poll");
  assert.ok(app.includes("planPreviewList.replaceChildren()"), "preview not built with textContent-safe DOM calls");
});

test("Privacy Center fetches the audit log and lists tracker hosts", () => {
  const views = read("src/controlViews.ts");
  assert.ok(views.includes("/api/audit?limit="), "no audit fetch in Privacy Center");
  assert.ok(views.includes("auditEventFilter"), "no audit event filter");
  assert.ok(views.includes("trackerHosts"), "no tracker blocklist rendering");
  assert.ok(views.includes("dpia"), "no DPIA summary");
  assert.ok(views.includes("read-only here"), "planner flags not labelled read-only");
});

// ---------------------------------------------------------------------------
// Responsive layout (Task 12): the 480px rules exist in the stylesheets.
// Layout is verified for real in headless Chromium (see docs/UI.md);
// these tests pin the rules so a later edit cannot silently drop them.
// ---------------------------------------------------------------------------

test("480px rules reflow the workspace without horizontal scroll", () => {
  const agentCss = read("src/styles/agent.css");
  const refreshCss = read("src/styles/control-center-refresh.css");
  assert.ok(agentCss.includes("@media (max-width: 480px)"), "no 480px block in agent.css");
  assert.ok(refreshCss.includes("@media(max-width:480px)"), "no 480px block in control-center-refresh.css");
  assert.ok(agentCss.includes(".human-gate__buttons") && agentCss.includes("flex-direction: column"), "gate buttons do not stack");
  assert.ok(agentCss.includes(".task-timeline__row") && agentCss.includes("grid-template-areas"), "timeline rows do not stack");
  assert.ok(agentCss.includes("min-height: 44px"), "no 44px tap targets");
  assert.ok(agentCss.includes(".plan-preview__list"), "no plan preview styles");
  assert.ok(refreshCss.includes(".cc-audit-controls"), "no audit controls layout");
  assert.ok(refreshCss.includes("overflow-wrap:anywhere"), "tracker hosts can overflow");
});

// ---------------------------------------------------------------------------
// Pinned composer: the mission input must stay at the bottom of the
// timeline column while the feed scrolls, so the operator never has to
// scroll back up to type the next mission (ChatGPT/Gemini-style).
// ---------------------------------------------------------------------------

test("mission composer is pinned to the bottom of the timeline", () => {
  const agentCss = read("src/styles/agent.css");
  const card = agentCss.match(/\.agent-input-card\s*\{([\s\S]*?)\}/);
  assert.ok(card, "no .agent-input-card rule in agent.css");
  const rules = card[1];
  assert.ok(/position:\s*sticky/.test(rules), "composer is not sticky");
  assert.ok(/bottom:\s*\d+px/.test(rules), "composer has no bottom offset");
  const order = rules.match(/order:\s*(\d+)/);
  assert.ok(order && Number(order[1]) > 0, "composer does not sort after the feed cards");
  assert.ok(/z-index:\s*\d+/.test(rules), "composer has no z-index (feed would paint over it)");
  // The timeline is a flex column, so order actually moves the card.
  const timeline = agentCss.match(/\.agent-timeline\s*\{([\s\S]*?)\}/);
  assert.ok(
    timeline && /display:\s*flex/.test(timeline[1]) && /flex-direction:\s*column/.test(timeline[1]),
    "timeline is not a flex column, so order cannot move the composer",
  );
  assert.ok(read("agent.html").includes('id="missionInput"'), "composer textarea missing from agent.html");
});

// ---------------------------------------------------------------------------
// Frontend flaw-hunt finding 2: the pinned composer hides whatever
// scrollIntoView just revealed — every step/completion scrolls to the
// viewport bottom edge, under the ~290px sticky input card. The
// document needs scroll-padding-bottom so targets stop above it.
// ---------------------------------------------------------------------------

test("scrollIntoView targets stop above the pinned composer", () => {
  const agentCss = read("src/styles/agent.css");
  const htmlRule = agentCss.match(/html\s*\{([\s\S]*?)\}/);
  assert.ok(htmlRule, "no html scroll-padding rule in agent.css");
  const bottom = htmlRule[1].match(/scroll-padding-bottom:\s*(\d+)px/);
  assert.ok(bottom, "html rule has no scroll-padding-bottom");
  assert.ok(
    Number(bottom[1]) >= 320,
    `scroll-padding-bottom is ${bottom[1]}px — below the ~290px composer, so scrolled-to steps would hide under it`,
  );
  assert.ok(/scroll-padding-top:\s*\d+px/.test(htmlRule[1]), "sticky header has no scroll-padding-top");
  // Small phones stack the composer's actions and grow its chips, so
  // the compensation must grow there too.
  const phone = agentCss.match(/@media\s*\(max-width:\s*480px\)\s*\{[\s\S]*?html\s*\{([\s\S]*?)\}/);
  assert.ok(phone, "no small-phone scroll-padding override in agent.css");
  const phoneBottom = phone[1].match(/scroll-padding-bottom:\s*(\d+)px/);
  assert.ok(
    phoneBottom && Number(phoneBottom[1]) >= 400,
    "small-phone scroll-padding-bottom is below the ~360px composer",
  );
});

// ---------------------------------------------------------------------------
// Frontend flaw-hunt finding 3: the header "New Mission" button was a
// visible no-op from any Control Center view — it only called
// createNewChat(), whose nodes are null until the workspace template
// mounts. It must mount, navigate to the workspace view, then start
// the new chat.
// ---------------------------------------------------------------------------

test("header New Mission mounts and navigates before starting a new chat", () => {
  const src = read("src/agentApp.ts");
  const handler = src.match(/btnNewSession\?\.addEventListener\("click", \(\) => \{([\s\S]*?)\}\);/);
  assert.ok(handler, "no btnNewSession click handler in agentApp.ts");
  const body = handler[1];
  assert.ok(
    body.includes("ensureWorkspaceMounted()"),
    "handler never mounts the workspace template — a no-op from Control Center views",
  );
  assert.ok(body.includes("agent-workspace"), "handler never navigates to the workspace view");
  // The comment mentions createNewChat() first, so compare the
  // statements (with semicolons), not bare names.
  assert.ok(
    body.indexOf("ensureWorkspaceMounted();") < body.indexOf("createNewChat();"),
    "new chat starts before the workspace is mounted",
  );
});

// ---------------------------------------------------------------------------
// Visual Perception Frame: a screenshot-less step or a chat switch must
// clear the frame, never leave the previous step's/chat's image up.
// ---------------------------------------------------------------------------

test("updateVisualFrame clears the image when the step has no screenshot", () => {
  const src = read("src/agentApp.ts");
  const fn = src.match(/function updateVisualFrame\([\s\S]*?\n\}/);
  assert.ok(fn, "no updateVisualFrame in agentApp.ts");
  const body = fn[0];
  assert.ok(
    body.includes('inspectorScreenshot.removeAttribute("src")'),
    "no-screenshot path never removes the stale src",
  );
  assert.ok(
    body.includes("inspectorScreenshot.hidden = true"),
    "no-screenshot path never hides the image",
  );
  assert.ok(
    body.includes("inspectorScreenshotPlaceholder.hidden = false"),
    "no-screenshot path never restores the placeholder",
  );
});

test("task polling and restore always refresh the frame, even with no screenshot", () => {
  const src = read("src/agentApp.ts");
  assert.doesNotMatch(src, /if \(t\.screenshot\) \{\s*\n\s*updateVisualFrame/);
  assert.doesNotMatch(src, /if \(act\.screenshot\) \{\s*\n\s*updateVisualFrame/);
  assert.ok(
    (src.match(/updateVisualFrame\(t\.screenshot \?\? null,/g) || []).length >= 2,
    "poll/restore paths must call updateVisualFrame unconditionally",
  );
  assert.ok(
    src.includes("updateVisualFrame(act.screenshot ?? null,"),
    "reconnect path must call updateVisualFrame unconditionally",
  );
});

test("selectChat clears the visual frame before loading the new chat", () => {
  const src = read("src/agentApp.ts");
  const fn = src.match(/async function selectChat\(chatId: string\)[\s\S]*?\n\}/);
  assert.ok(fn, "no selectChat in agentApp.ts");
  assert.ok(
    fn[0].includes('updateVisualFrame(null, "", "ready")'),
    "selectChat never clears the frame — the previous chat's screenshot stays up",
  );
});

// ---------------------------------------------------------------------------
// Chat switching: closing the active chat must land on the blank New
// Mission state (not re-open another one), and the closed mission must
// stay in the list so it can be opened again. The sidebar × is a Close,
// not a Delete — permanent delete lives in the control center behind a
// confirmation dialog, so there is no deleteChat in agentApp.ts.
// ---------------------------------------------------------------------------

test("closeChat reloads the list without auto-selecting another chat and without deleting it", () => {
  const src = read("src/agentApp.ts");
  assert.ok(!src.includes("async function deleteChat"), "agentApp.ts still has a deleteChat — the sidebar × must be a Close, not a Delete");
  assert.ok(!src.includes("chat-delete-btn"), "agentApp.ts still renders a delete button in the chat list");
  const fn = src.match(/async function closeChat\(chatId: string\)[\s\S]*?\n\}/);
  assert.ok(fn, "no closeChat in agentApp.ts");
  assert.ok(
    fn[0].includes("loadChatList({ autoSelect: false })"),
    "closeChat reloads with auto-select on — the blank state is instantly replaced by another mission",
  );
  assert.ok(
    !fn[0].includes("DELETE"),
    "closeChat still calls the DELETE endpoint — closing must not delete the mission",
  );
  assert.ok(
    fn[0].includes("createNewChat()"),
    "closeChat does not return to the blank New Mission state",
  );
  assert.ok(
    src.includes("options?.autoSelect ?? true"),
    "loadChatList ignores the autoSelect option",
  );
});

test("selectChat ignores a detail fetch superseded by a newer selection", () => {
  const src = read("src/agentApp.ts");
  assert.ok(src.includes("let selectChatGeneration = 0;"), "no selectChat generation counter");
  const fn = src.match(/async function selectChat\(chatId: string\)[\s\S]*?\n\}/);
  assert.ok(fn, "no selectChat in agentApp.ts");
  const body = fn[0];
  assert.ok(body.includes("const gen = ++selectChatGeneration;"), "selectChat never captures its generation");
  assert.ok(
    body.includes("if (gen !== selectChatGeneration) return;"),
    "selectChat renders the detail without checking whether a newer selection superseded it",
  );
  assert.ok(
    body.includes("loadArtifacts(null, chatId, () => gen === selectChatGeneration)"),
    "selectChat loads artifacts without a currency guard",
  );
});
