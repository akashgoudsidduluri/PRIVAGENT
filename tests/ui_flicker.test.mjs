import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { join } from "node:path";
import { createRequire } from "node:module";

const root = new URL("..", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const read = (p) => readFileSync(join(root, p), "utf8");

// ---------------------------------------------------------------------------
// UI flicker fix: the old workspace must not be painted HTML on first paint.
// agent.html keeps the workspace inert inside <template id="workspaceTemplate">
// and controlCenter mounts it (once) only when the Agent Workspace,
// Browser Inspector, or Activity view needs its live nodes. These tests run
// against the BUILT page (dist/agent.html) — run `npm run build` first;
// without a build the file is absent and the tests fail loudly on purpose.
// The first-paint checks use jsdom when it resolves (the extension's
// bundled copy, same as ~/workspace/privagent-analysis/task12-verify-dom.mjs)
// and fall back to template-stripping string checks otherwise. jsdom parses
// <template> content into template.content, not the document — exactly the
// inertness the fix relies on — so both paths assert the same thing.
// No live browser exists in this VM; this proves the build/DOM level, not
// a watched paint.
// ---------------------------------------------------------------------------

const DIST_HTML = join(root, "dist/agent.html");
if (!existsSync(DIST_HTML)) {
  throw new Error(
    "dist/agent.html is missing — run `npm run build` before `npm test` " +
      "(the flicker tests assert against the built page).",
  );
}

const distHtml = read("dist/agent.html");
const agentHtml = read("agent.html");
const agentApp = read("src/agentApp.ts");
const controlCenter = read("src/controlCenter.ts");

const WORKSPACE_IDS = [
  "agentWorkspace",
  "chatSidebar",
  "chatHistoryList",
  "missionInput",
  "btnCancelMission",
  "agentStepsList",
  "planPreview",
  "planPreviewList",
  "btnGateApproveStep",
  "taskTimelineCard",
  "taskTimelineList",
];

// Missions-view nodes the template clone must produce on instantiation.
const MISSIONS_IDS = [
  "missionInput",
  "btnCancelMission",
  "agentStepsList",
  "planPreview",
  "taskTimelineList",
];

let JSDOM = null;
try {
  JSDOM = createRequire(import.meta.url)(
    join(root, "extension/node_modules/jsdom"),
  ).JSDOM;
} catch {
  // Fallback path below (template-stripping string checks).
}

function stripTemplates(html) {
  return html.replace(/<template[\s\S]*?<\/template>/g, "");
}

test("first paint is the control-center shell, not the old workspace (jsdom)", { skip: !JSDOM }, () => {
  const doc = new JSDOM(distHtml).window.document;
  assert.ok(doc.getElementById("controlNav"), "first paint is missing #controlNav");
  assert.ok(doc.getElementById("controlPage"), "first paint is missing #controlPage");
  for (const id of WORKSPACE_IDS) {
    assert.equal(
      doc.getElementById(id),
      null,
      `first-paint DOM must not contain #${id} (it lives in the template)`,
    );
  }
  assert.equal(
    doc.querySelector(".workspace-heading"),
    null,
    "first-paint DOM must not contain the old workspace heading",
  );
  assert.ok(
    !doc.body.textContent.includes("MISSION DIRECTIVE"),
    "first-paint DOM must not contain the mission input label",
  );
});

test("first paint has no painted old-workspace nodes (string fallback)", { skip: !!JSDOM }, () => {
  const firstPaint = stripTemplates(distHtml);
  assert.ok(firstPaint.includes('id="controlNav"'), "first paint is missing #controlNav");
  assert.ok(firstPaint.includes('id="controlPage"'), "first paint is missing #controlPage");
  for (const id of WORKSPACE_IDS) {
    assert.ok(
      !firstPaint.includes(`id="${id}"`),
      `first-paint HTML must not contain id="${id}" outside the template`,
    );
  }
  assert.ok(!firstPaint.includes("MISSION DIRECTIVE"), "first paint must not contain the mission input label");
  assert.ok(!firstPaint.includes("workspace-heading"), "first paint must not contain the workspace heading");
});

test("the workspace survives, inert, inside the template", () => {
  assert.ok(agentHtml.includes('<template id="workspaceTemplate">'), "agent.html is missing #workspaceTemplate");
  const tpl = agentHtml.match(/<template id="workspaceTemplate">([\s\S]*?)<\/template>/);
  assert.ok(tpl, "workspace template block not found in agent.html");
  for (const id of WORKSPACE_IDS) {
    assert.ok(tpl[1].includes(`id="${id}"`), `template is missing #${id}`);
  }
  assert.ok(tpl[1].includes("MISSION DIRECTIVE"), "template lost the mission input label");
});

test("Missions view still instantiates: template clone produces its nodes", () => {
  if (JSDOM) {
    const dom = new JSDOM(distHtml);
    const doc = dom.window.document;
    const tpl = doc.getElementById("workspaceTemplate");
    assert.ok(tpl, "built page is missing #workspaceTemplate");
    const host = doc.createElement("div");
    host.append(tpl.content.cloneNode(true));
    doc.body.append(host);
    for (const id of MISSIONS_IDS) {
      assert.ok(host.querySelector(`#${id}`), `template clone is missing #${id}`);
    }
    // No Start button: Enter submits the mission, and Cancel is a
    // circular button to the right of the input. Both must be present
    // in the clone, and the old Start button must be gone.
    assert.equal(
      host.querySelector("#btnStartMission"),
      null,
      "template clone still carries a Start Mission button",
    );
    assert.ok(
      host.querySelector("#btnCancelMission")?.classList.contains("btn-cancel-circle"),
      "cloned Cancel button is not the circular button right of the input",
    );
    assert.equal(
      host.querySelector("#btnGateApproveStep")?.textContent.includes("Approve this step only"),
      true,
      "cloned approve-this-step-only control is missing",
    );
  } else {
    // String fallback: the template payload carries the nodes.
    const tpl = agentHtml.match(/<template id="workspaceTemplate">([\s\S]*?)<\/template>/);
    assert.ok(tpl, "workspace template block not found in agent.html");
    for (const id of MISSIONS_IDS) {
      assert.ok(tpl[1].includes(`id="${id}"`), `template payload is missing #${id}`);
    }
    assert.ok(tpl[1].includes("Approve this step only"), "template lost the approve-step label");
  }
});

test("workspace refs resolve lazily in agentApp, listeners wire on mount", () => {
  assert.ok(agentApp.includes("export function ensureWorkspaceMounted"), "no ensureWorkspaceMounted export in agentApp.ts");
  assert.ok(agentApp.includes("tpl.content.cloneNode(true)"), "template is not cloned on mount");
  assert.ok(agentApp.includes("function initWorkspaceElements"), "no initWorkspaceElements in agentApp.ts");
  assert.ok(!agentApp.includes('const missionInput = document.getElementById'), "missionInput is still resolved at module top level");
  assert.ok(agentApp.includes("let missionInput: HTMLTextAreaElement | null = null;"), "missionInput is not a lazy let");
  const initAt = agentApp.indexOf("function initWorkspaceElements");
  for (const wiring of [
    // Enter submits the mission: the keydown handler calls startMission(),
    // and there is no Start button to click any more.
    'void startMission?.();',
    'btnGateApproveStep?.addEventListener("click"',
    'decision: "approve_step"',
  ]) {
    const at = agentApp.indexOf(wiring);
    assert.ok(at > initAt, `${wiring} is not wired inside initWorkspaceElements`);
  }
  assert.ok(!agentApp.includes("btnStartMission"), "agentApp still references the removed Start button");
  // The static header stays live from load.
  assert.ok(agentApp.includes('const btnToggleSidebarHeader = document.getElementById("btnToggleSidebarHeader")'), "header toggle lost its eager wiring");
  assert.ok(agentApp.includes('const wsDot = document.getElementById("wsDot")'), "connection dot lost its eager wiring");
});

test("controlCenter mounts the workspace on demand, never at load", () => {
  assert.ok(controlCenter.includes("ensureWorkspaceMounted"), "controlCenter never mounts the workspace");
  assert.ok(controlCenter.includes("ensureLiveNodes"), "controlCenter does not cache the live nodes once");
  assert.ok(!controlCenter.includes("querySelector<HTMLElement>('#agentWorkspace')!"), "controlCenter still captures #agentWorkspace at module top level");
  assert.ok(controlCenter.includes("index === 1 || index === 2 || index === 9"), "mount is not limited to the views that need live nodes");
});

test("agent.html loads no landing stylesheet and agent.css exactly once", () => {
  assert.ok(!agentHtml.includes("main.css"), "agent.html still loads the landing stylesheet main.css");
  assert.ok(!agentHtml.includes('link rel="stylesheet" href="/src/styles/agent.css"'), "agent.html still has a duplicate agent.css <link>");
  assert.equal(
    (agentApp.match(/import "\.\/styles\/agent\.css";/g) || []).length,
    1,
    "agent.css must be imported exactly once (agentApp.ts)",
  );
  const links = [...distHtml.matchAll(/<link rel="stylesheet"[^>]*href="([^"]+)"[^>]*>/g)].map((m) => m[1]);
  assert.equal(links.length, 1, `built agent.html must load exactly one stylesheet, found: ${links.join(", ")}`);
  assert.ok(links[0].includes("agent-") && links[0].endsWith(".css"), `built agent.html stylesheet is not the agent bundle: ${links[0]}`);
  assert.ok(!distHtml.includes("main.css"), "built agent.html still references main.css");
});

test("UI redesign: one palette — no cyan/purple/blue accents in agent.css", () => {
  // 2026-11-18 redesign: the old cyan (#46d7ff) / purple (#a06bff) /
  // navy "AI slop" palette is gone. The workspace is ink + electric
  // lime only. Semantic colours stay: format badges (red/blue/amber/
  // green), store-card variants, and status red/amber/green.
  const srcCss = read("src/styles/agent.css");
  for (const bad of [
    "#46d7ff", "#a06bff", "#c084fc", "#c9a9ff", "#38bdf8", "#a78bfa",
    "#55eaff", "#7ae3ff", "#0ea5e9", "#f1edff",
    "70, 215, 255", "160, 107, 255", "85, 234, 255",
    "56, 189, 248", "70, 160, 220",
    "#060b1e", "#04122b", "#0a1230",
  ]) {
    assert.ok(!srcCss.includes(bad), `agent.css still uses the old palette colour ${bad}`);
  }
  assert.ok(srcCss.includes("--accent: #d5ff70"), "agent.css does not define the lime accent");
  assert.ok(srcCss.includes("--bg: #101210"), "agent.css does not define the ink background");
  // No garish gradients on cards/buttons (the motion-bg aurora and the
  // amber human-gate banner are the only gradients allowed).
  const gradients = [...srcCss.matchAll(/linear-gradient\([^)]*\)/g)].map((m) => m[0]);
  for (const g of gradients) {
    assert.ok(
      g.includes("245, 158, 11") || g.includes("217, 119, 6"),
      `agent.css still has a decorative gradient: ${g}`,
    );
  }
  // No glassmorphism on panels (the modal overlay blur is functional).
  assert.ok(!srcCss.includes("backdrop-filter: blur(12px)"), "agent.css still blurs the header");
  assert.ok(!srcCss.includes("backdrop-filter: blur(8px)"), "agent.css still blurs a card");
});

test("UI redesign: motion background exists and respects reduced motion", () => {
  assert.ok(agentHtml.includes('class="motion-bg"'), "agent.html has no .motion-bg element");
  assert.ok(agentHtml.includes("motion-bg__blob--a"), "agent.html is missing the first aurora blob");
  assert.ok(agentHtml.includes("motion-bg__blob--b"), "agent.html is missing the second aurora blob");
  const srcCss = read("src/styles/agent.css");
  assert.ok(srcCss.includes(".motion-bg__blob"), "agent.css has no motion-bg rules");
  assert.ok(srcCss.includes("@keyframes motion-bg-drift"), "agent.css has no drift animation");
  assert.ok(
    srcCss.includes("@media (prefers-reduced-motion: reduce)") && srcCss.includes(".motion-bg__blob { animation: none; }"),
    "motion background does not stop for prefers-reduced-motion",
  );
});

test("UI redesign: clutter is gone (heading, pills, sentinel copy, badges)", () => {
  assert.ok(!agentHtml.includes("workspace-heading"), "agent.html still has the marketing heading");
  assert.ok(!agentHtml.includes("Intent into"), "agent.html still has the 'Intent into action' heading");
  assert.ok(!agentHtml.includes("YOUR PRIVATE OPERATIONS LAYER"), "agent.html still has the operations-layer eyebrow");
  assert.ok(!agentHtml.includes("agent-pill"), "agent.html still has header pills");
  assert.ok(!agentHtml.includes("Human-governed autonomy"), "agent.html still has the autonomy pill");
  assert.ok(!agentHtml.includes("Local-first runtime"), "agent.html still has the runtime pill");
  assert.ok(!agentHtml.includes("live-dot"), "agent.html still has the EVIDENCE dot");
  assert.ok(!agentHtml.includes("shield-badge"), "agent.html still has the POLICY badge");
  assert.ok(!agentHtml.includes("sentinel-list"), "agent.html still has the Sentinel long copy");
  assert.ok(!agentHtml.includes("Local Vault Redaction"), "agent.html still has the Sentinel redaction paragraph");
  assert.ok(!agentHtml.includes("Local-First Perception"), "agent.html still has the Sentinel perception paragraph");
  assert.ok(!agentHtml.includes("Human-in-the-Loop Gate"), "agent.html still has the Sentinel gate paragraph");
  // The Sentinel is one status line now.
  assert.ok(agentHtml.includes("sentinel-status"), "agent.html lost the compact Sentinel status line");
  assert.ok(agentHtml.includes("Local redaction on"), "Sentinel status line lost its text");
  // The subtext has no decorative default; it is a live status line.
  assert.ok(!agentHtml.includes("Automated browsing with fail-closed privacy."), "agent.html still has the decorative subtext");
  assert.ok(!agentApp.includes('missionSubtext.textContent = "Automated browsing'), "clearStalledNotice still writes the decorative default");
  const srcCss = read("src/styles/agent.css");
  assert.ok(srcCss.includes(".agent-subtext:empty"), "agent.css does not hide the empty subtext");
});

test("new chat clears the previous chat's artifacts (artifact leak fix)", () => {
  // Bug 2026-11-18: createNewChat() cleared the steps, result card and
  // inspector but never touched the artifacts card, so the previous
  // chat's deliverables stayed visible in a fresh chat. A new chat has
  // no artifacts until loadArtifacts() runs for a selected chat.
  const start = agentApp.indexOf("function createNewChat()");
  assert.ok(start >= 0, "createNewChat not found in agentApp.ts");
  const end = agentApp.indexOf("// UI Events — static header", start);
  assert.ok(end > start, "createNewChat end marker not found");
  const body = agentApp.slice(start, end);
  assert.ok(body.includes("artifactsCard.hidden = true"), "createNewChat does not hide the artifacts card");
  assert.ok(body.includes('artifactsList.innerHTML = ""'), "createNewChat does not clear the artifacts list");
  assert.ok(body.includes('artifactsCount.textContent = "0 artifacts"'), "createNewChat does not reset the artifacts count");
});

test("Quick Missions presets are removed from the workspace", () => {
  // Removed 2026-11-18: the preset chips (WhatsApp / YouTube / yellow
  // dress / Realme specs) were demo clutter in the mission composer.
  // The composer is now just the input + circular Cancel, nothing else.
  assert.ok(!agentHtml.includes("Quick Missions"), "agent.html still renders the Quick Missions label");
  assert.ok(!agentHtml.includes("preset-chip"), "agent.html still has preset-chip buttons");
  assert.ok(!agentHtml.includes("agent-presets"), "agent.html still has the presets container");
  assert.ok(!agentApp.includes('querySelectorAll(".preset-chip")'), "agentApp still wires preset chips");
  const srcCss = read("src/styles/agent.css");
  assert.ok(!srcCss.includes(".preset-chip"), "agent.css still carries preset-chip rules");
  assert.ok(!srcCss.includes(".agent-presets"), "agent.css still carries presets rules");
});

test("Google Fonts load async and never block first paint", () => {
  // A plain <link rel="stylesheet" href="https://fonts.googleapis.com/...">
  // is render-blocking on a third-party fetch. Fonts must arrive via
  // preload + onload rel-swap instead (GLM review 2026-11-17).
  for (const [label, html] of [["source", agentHtml], ["built", distHtml]]) {
    assert.ok(!/<link[^>]*rel="stylesheet"[^>]*fonts\.googleapis/.test(html), `${label} agent.html still has a render-blocking Google Fonts stylesheet`);
    assert.ok(!/<link[^>]*fonts\.googleapis[^>]*rel="stylesheet"/.test(html), `${label} agent.html still has a render-blocking Google Fonts stylesheet (reversed attrs)`);
  }
  assert.ok(agentHtml.includes('rel="preload" as="style"'), "fonts preload link missing from agent.html");
  assert.ok(agentHtml.includes("this.rel='stylesheet'"), "fonts preload link does not swap to stylesheet on load");
});

// ---------------------------------------------------------------------------
// Built bundles.
// ---------------------------------------------------------------------------

const assets = readdirSync(join(root, "dist/assets"));
const agentJs = assets.find((f) => f.startsWith("agent-") && f.endsWith(".js"));
const agentCss = assets.find((f) => f.startsWith("agent-") && f.endsWith(".css"));
assert.ok(agentJs, "no built agent JS bundle in dist/assets");
assert.ok(agentCss, "no built agent CSS bundle in dist/assets");
const js = read(`dist/assets/${agentJs}`);
const css = read(`dist/assets/${agentCss}`);

test("built agent JS carries the mount code, not painted workspace markup", () => {
  assert.ok(js.includes("workspaceTemplate"), "built JS has no template mount code");
  assert.ok(js.includes("replaceWith") && js.includes("cloneNode"), "built JS is missing the template clone step");
  for (const s of ['id="agentWorkspace"', 'id="missionInput"', "MISSION DIRECTIVE", "YOUR PRIVATE OPERATIONS LAYER"]) {
    assert.ok(!js.includes(s), `built agent JS still carries painted workspace markup: ${s}`);
  }
  assert.ok(js.includes("approve_step"), "built JS lost the approve-step decision");
});

test("built agent CSS has the moved shell rules and no landing styles", () => {
  assert.ok(css.includes(".agent-body .btn"), "built CSS lost the moved base button rules");
  assert.ok(css.includes(".agent-body .btn--sm"), "built CSS lost .btn--sm");
  assert.ok(css.includes(".plan-preview"), "built CSS lost the plan preview rules");
  assert.ok(css.includes(".control-nav"), "built CSS lost the control nav rules");
  // Base resets main.css used to provide for this page (box-sizing, list
  // reset), scoped to .agent-body — without them the dialogs and layout
  // would silently regress when main.css stopped loading.
  const flat = css.replace(/\s+/g, "");
  assert.ok(
    /\.agent-body\*,\.agent-body\*::?before,\.agent-body\*::?after\{box-sizing:border-box\}/.test(flat),
    "built CSS lost the scoped box-sizing reset",
  );
  assert.ok(
    /\.agent-bodyul,\.agent-bodyol\{margin:0;padding:0;list-style:none\}/.test(flat),
    "built CSS lost the scoped list reset",
  );
  for (const s of [".hero__title", ".console__panel", "100deg"]) {
    assert.ok(!css.includes(s), `built agent CSS still carries landing styles: ${s}`);
  }
});
