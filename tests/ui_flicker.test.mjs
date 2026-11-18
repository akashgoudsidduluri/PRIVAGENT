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
  "btnStartMission",
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
  "btnStartMission",
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
    assert.equal(
      host.querySelector("#btnStartMission")?.textContent.trim().includes("Start Mission"),
      true,
      "cloned Start Mission button lost its label",
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
    'btnStartMission?.addEventListener("click"',
    'btnGateApproveStep?.addEventListener("click"',
    'document.querySelectorAll(".preset-chip")',
    'decision: "approve_step"',
  ]) {
    const at = agentApp.indexOf(wiring);
    assert.ok(at > initAt, `${wiring} is not wired inside initWorkspaceElements`);
  }
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
