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
