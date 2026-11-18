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

test("UI redesign: one palette — Tactile Daylight / Studio Slate in agent.css", () => {
  // 2026-11-18 redesign (user brief): the ink (#101210) + lime
  // (#d5ff70) palette and the old cyan/purple "AI slop" are gone.
  // The workspace is now light slate with restrained semantic
  // accents: cobalt primary, emerald privacy, amber human-gate,
  // crimson abort. No gradient blobs, no neon.
  const srcCss = read("src/styles/agent.css");
  for (const bad of [
    "#d5ff70", "#101210", "#1b1e1b",
    "#46d7ff", "#a06bff", "#c084fc", "#c9a9ff", "#38bdf8", "#a78bfa",
    "#55eaff", "#7ae3ff", "#0ea5e9", "#f1edff",
    "70, 215, 255", "160, 107, 255", "85, 234, 255",
    "56, 189, 248", "70, 160, 220", "213, 255, 112",
    "#060b1e", "#04122b", "#0a1230",
  ]) {
    assert.ok(!srcCss.includes(bad), `agent.css still uses the old palette colour ${bad}`);
  }
  assert.ok(srcCss.includes("--bg: #F8F9FA"), "agent.css does not define the slate canvas background");
  assert.ok(srcCss.includes("--accent: #2563EB"), "agent.css does not define the cobalt primary accent");
  assert.ok(srcCss.includes("--privacy: #059669"), "agent.css does not define the emerald privacy accent");
  assert.ok(srcCss.includes("--gate: #D97706"), "agent.css does not define the amber human-gate accent");
  assert.ok(srcCss.includes("--danger: #DC2626"), "agent.css does not define the crimson abort accent");
  // No decorative gradients anywhere (the new system is flat + shadow).
  const gradients = [...srcCss.matchAll(/linear-gradient\([^)]*\)/g)].map((m) => m[0]);
  assert.equal(gradients.length, 0, `agent.css still has a decorative gradient: ${gradients[0] ?? ""}`);
  // Glassmorphism is intentional ONLY on the sticky command header.
  assert.ok(srcCss.includes(".command-header"), "agent.css has no command-header rules");
  assert.ok(srcCss.includes("backdrop-filter: blur(12px)"), "command header is not glassmorphic");
});

test("UI redesign: no gradient motion background; reduced motion still respected", () => {
  // The lime aurora motion-bg is gone by design (new system has no
  // gradient blobs). Reduced-motion must still disable animation.
  assert.ok(!agentHtml.includes('class="motion-bg"'), "agent.html still has the .motion-bg element");
  assert.ok(!agentHtml.includes("motion-bg__blob"), "agent.html still has aurora blobs");
  const srcCss = read("src/styles/agent.css");
  assert.ok(!srcCss.includes(".motion-bg"), "agent.css still has motion-bg rules");
  assert.ok(!srcCss.includes("motion-bg-drift"), "agent.css still has the drift animation");
  assert.ok(
    srcCss.includes("@media (prefers-reduced-motion: reduce)"),
    "agent.css has no prefers-reduced-motion rule",
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

test("workspace opens on a blank New Mission, not the latest completed chat", () => {
  // Bug 2026-11-18: opening (or refreshing) the frontend then the
  // workspace auto-selected the latest completed chat (saved id from
  // localStorage, else chats[0]). The workspace must start blank; the
  // sidebar still lists every mission and clicking one opens it.
  const mount = agentApp.indexOf("void loadChatList({ autoSelect: false });");
  assert.ok(mount >= 0, "initWorkspaceElements does not load the chat list with autoSelect: false on mount");
  // The mount call must be inside initWorkspaceElements, not just
  // anywhere (delete/close paths also pass autoSelect: false).
  const initStart = agentApp.indexOf("function initWorkspaceElements()");
  assert.ok(initStart >= 0 && initStart < mount, "the autoSelect:false loadChatList is not the mount call in initWorkspaceElements");
  // No other bare loadChatList() in the mount path may re-open a chat.
  const initEnd = agentApp.indexOf("// Session & Task State", initStart);
  const initBody = agentApp.slice(initStart, initEnd);
  assert.ok(!initBody.includes("void loadChatList();"), "initWorkspaceElements still calls loadChatList() with auto-select on mount");
});

test("sidebar close button is a Close (reopenable), not a Delete", () => {
  // Bug 2026-11-18: the × on a chat was a hard DELETE (DELETE
  // /api/chat/{id}, no confirmation), so a "closed" mission was gone
  // forever. The × is now a Close: it deselects the open mission and
  // the mission stays in the list, clickable to open again. Permanent
  // delete lives in the control center behind a confirmation dialog.
  assert.ok(!agentApp.includes("chat-delete-btn"), "agentApp still renders a delete button in the chat list");
  assert.ok(!agentApp.includes("async function deleteChat"), "agentApp still has deleteChat");
  assert.ok(agentApp.includes("chat-close-btn"), "chat list has no close button");
  assert.ok(agentApp.includes('title="Close mission"'), "close button is not labelled Close mission");
  // The × only renders on the OPEN (active) item — a mission that is
  // not open has nothing to close.
  assert.ok(
    agentApp.includes("chat.id === activeChatId"),
    "close button is not restricted to the active chat item",
  );
  assert.ok(agentApp.includes("closeChat(chat.id)"), "close button does not call closeChat");
  const fn = agentApp.match(/async function closeChat\(chatId: string\)[\s\S]*?\n\}/);
  assert.ok(fn, "no closeChat in agentApp.ts");
  assert.ok(!fn[0].includes("DELETE"), "closeChat still deletes the mission");
  assert.ok(fn[0].includes("createNewChat()"), "closeChat does not return to the blank New Mission state");
});

test("Thinking indicator is a shimmer skeleton + pulse ring, not static text", () => {
  // Redesign 2026-11-18: the bouncing-dots Thinking row is replaced
  // by a sleek shimmer skeleton bar + a pulsing ring on the status
  // node, per the Tactile Daylight spec. The live row must still be
  // visibly animated and must stop for prefers-reduced-motion.
  assert.ok(agentApp.includes("thinking-shimmer"), "addStepItem does not render the shimmer skeleton bar");
  assert.ok(agentApp.includes("agent-step-item--thinking"), "thinking row has no distinguishing class");
  const srcCss = read("src/styles/agent.css");
  assert.ok(srcCss.includes(".thinking-shimmer"), "agent.css has no thinking-shimmer rule");
  assert.ok(srcCss.includes("@keyframes thinking-shimmer"), "agent.css has no thinking-shimmer animation");
  assert.ok(srcCss.includes("@keyframes thinking-ring"), "agent.css has no thinking-ring pulse animation");
  assert.ok(
    srcCss.includes("@media (prefers-reduced-motion: reduce)") && srcCss.includes(".thinking-shimmer { animation: none; }"),
    "thinking shimmer does not stop for prefers-reduced-motion",
  );
});

test("attached file name is shown in the user message bubble", () => {
  // Bug 2026-11-18: selecting a file and sending it showed only the
  // instruction text — the user could not tell the agent received the
  // file, and the planner was never told either. The bubble now shows
  // the file name under the instruction.
  assert.ok(agentApp.includes("agent-step-item__attachment"), "user bubble has no attachment line");
  assert.ok(
    agentApp.includes("addUserMessageItem(instruction, attachedFileName)"),
    "startMission does not pass the attached file name to the bubble",
  );
  assert.ok(
    agentApp.includes("missionAttachment?.files?.[0]?.name"),
    "startMission does not read the selected file name",
  );
  const srcCss = read("src/styles/agent.css");
  assert.ok(srcCss.includes(".agent-step-item__attachment"), "agent.css has no attachment-line rules");
});

test("completed message is shown once, not repeated in the result card", () => {
  // Bug 2026-11-18: the final message appeared twice — once as a
  // timeline step ("[1] Hey there!...") and again in the "✓ Completed"
  // result card with the same text. The reply must appear once.
  // renderCompletion omits the raw message from the result card when
  // it already lives in the timeline, and addCompletionStepItem skips
  // adding a "Mission Result" item for a message already there.
  assert.ok(agentApp.includes("function isMessageInTimeline"), "no isMessageInTimeline dedup helper");
  assert.ok(agentApp.includes("function normalizeForDedup"), "no normalizeForDedup helper");
  assert.ok(
    agentApp.includes("formatResultHtml(message || \"The autonomous task completed successfully.\", showRaw)"),
    "renderCompletion does not pass the dedup flag to formatResultHtml",
  );
  assert.ok(
    agentApp.includes("const showRaw = !isMessageInTimeline(message);"),
    "renderCompletion does not compute whether the message is already in the timeline",
  );
  const fn = agentApp.match(/function addCompletionStepItem\(taskState: string, message: string\)[\s\S]*?\n\}/);
  assert.ok(fn, "no addCompletionStepItem in agentApp.ts");
  assert.ok(
    fn[0].includes("if (isMessageInTimeline(message)) return;"),
    "addCompletionStepItem does not skip a message already in the timeline",
  );
  const fmt = agentApp.match(/function formatResultHtml\(rawMessage: string, includeRawMessage = true\)[\s\S]*?\n\}/);
  assert.ok(fmt, "formatResultHtml has no includeRawMessage parameter");
  assert.ok(fmt[0].includes("includeRawMessage"), "formatResultHtml ignores includeRawMessage");
});

test("empty progress updates do not render a blank step (lone thunder icon)", () => {
  // Bug 2026-11-18: after sending a message, an empty step row
  // appeared with only the ⚡ icon and no text — a progress event with
  // an empty message still created a timeline row. addStepItem must
  // ignore empty / whitespace-only messages.
  const fn = agentApp.match(/function addStepItem\(message: string, stepNum\?: number\)[\s\S]*?\n\}/);
  assert.ok(fn, "no addStepItem in agentApp.ts");
  assert.ok(fn[0].includes("if (!displayMessage) return;"), "addStepItem does not ignore empty messages");
});

test("landing page uses the same Tactile Daylight palette as the workspace", () => {
  // Redesign 2026-11-18: the whole app is one palette now — Tactile
  // Daylight (slate canvas, cobalt accent). The old cyan/purple/navy
  // and the interim ink/lime palettes are both gone.
  for (const cssFile of ["src/styles/main.css", "src/styles/taskconsole.css"]) {
    const css = read(cssFile);
    for (const bad of [
      "#46d7ff", "#a06bff", "#6b8cff", "#030616", "#060b24", "#04122b",
      "70, 215, 255", "160, 107, 255", "122, 162, 255",
      "3, 6, 22", "4, 8, 26", "6, 11, 36", "10, 18, 48",
      "#d5ff70", "#101210", "#1b1e1b", "213, 255, 112",
    ]) {
      assert.ok(!css.includes(bad), `${cssFile} still uses an old palette colour ${bad}`);
    }
  }
  const main = read("src/styles/main.css");
  assert.ok(main.includes("--bg: #F8F9FA"), "landing page does not use the slate canvas");
  assert.ok(main.includes("--accent: #2563EB"), "landing page accent is not cobalt");
  assert.ok(main.includes("--privacy: #059669"), "landing page has no emerald privacy accent");
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

test("Floating composer dock: morphing Send/Stop, attach trigger, file chips, micro-status bar", () => {
  // Redesign 2026-11-18: the composer is a floating dock (14px,
  // focus ring) with a cobalt Send arrow that morphs into a crimson
  // Stop circle while a mission runs, a paperclip attach trigger,
  // file-chip previews, and a micro-status bar.
  assert.ok(agentHtml.includes('id="btnSendMission"'), "composer has no Send button");
  assert.ok(agentHtml.includes('id="btnAttachMission"'), "composer has no attach trigger");
  assert.ok(agentHtml.includes('id="composerChips"'), "composer has no file-chip container");
  assert.ok(agentHtml.includes('id="composerStatus"'), "composer has no micro-status bar");
  assert.ok(agentHtml.includes("Local redaction ON"), "micro-status bar does not show redaction state");
  assert.ok(agentHtml.includes("Human gate ON"), "micro-status bar does not show human-gate state");
  assert.ok(agentApp.includes("btnSendMission"), "agentApp does not wire the Send button");
  assert.ok(agentApp.includes("renderAttachmentChips"), "agentApp does not render attachment chips");
  // setButtonWorking morphs Send <-> Stop.
  assert.ok(agentApp.includes("btnSendMission.hidden = working"), "setButtonWorking does not hide Send while running");
  assert.ok(agentApp.includes("btnCancelMission.hidden = !working"), "setButtonWorking does not show Stop while running");
  const srcCss = read("src/styles/agent.css");
  assert.ok(srcCss.includes(".agent-input-card:focus-within"), "composer dock has no focus-within ring");
  assert.ok(srcCss.includes(".composer-send"), "agent.css has no Send button rules");
  assert.ok(srcCss.includes(".composer-chip"), "agent.css has no file-chip rules");
  assert.ok(srcCss.includes(".composer-status"), "agent.css has no micro-status bar rules");
  assert.ok(srcCss.includes("border-radius: 14px"), "composer dock is not 14px radius");
});

test("Command header: 52px glass, SIH-26171 pill, telemetry pill, New Mission ⌘K", () => {
  // Redesign 2026-11-18: sticky 52px glassmorphic command header.
  assert.ok(agentHtml.includes("command-header"), "agent.html has no command-header");
  assert.ok(agentHtml.includes("SIH-26171"), "header has no SIH-26171 pill");
  assert.ok(agentHtml.includes("privacy-telemetry-pill"), "header has no privacy telemetry pill");
  assert.ok(agentHtml.includes("On-Device Redaction"), "telemetry pill has no redaction label");
  assert.ok(agentHtml.includes("⌘K"), "New Mission has no ⌘K shortcut hint");
  assert.ok(agentHtml.includes('id="providerStatus"'), "Provider pill has no status count");
  assert.ok(agentHtml.includes('id="vaultCount"'), "Vault pill has no item count");
  assert.ok(agentHtml.includes('id="memoryCount"'), "Memory pill has no item count");
  const srcCss = read("src/styles/agent.css");
  assert.ok(srcCss.includes(".command-header"), "agent.css has no command-header rules");
  assert.ok(srcCss.includes("height: 52px"), "command header is not 52px");
  assert.ok(srcCss.includes("backdrop-filter: blur(12px)"), "command header is not glassmorphic");
  assert.ok(srcCss.includes(".privacy-telemetry-pill__dot"), "telemetry pill has no pulsing dot");
  assert.ok(srcCss.includes("@keyframes pulse-dot"), "agent.css has no pulse-dot animation");
});

test("Step stream: connected stepper track, status nodes, redaction pill", () => {
  // Redesign 2026-11-18: vertical 1.5px slate track, circular status
  // nodes (emerald check / cobalt ring), redaction pill, mono duration.
  const srcCss = read("src/styles/agent.css");
  assert.ok(srcCss.includes(".agent-steps::before"), "step stream has no connected track");
  assert.ok(srcCss.includes("width: 1.5px"), "step track is not 1.5px");
  assert.ok(srcCss.includes(".agent-step-item--completed"), "completed steps have no status class");
  assert.ok(srcCss.includes(".redaction-pill"), "agent.css has no redaction pill");
  assert.ok(srcCss.includes(".agent-step-item__duration"), "agent.css has no step duration style");
  assert.ok(agentApp.includes("agent-step-item--completed"), "addStepItem does not mark completed steps");
  assert.ok(agentApp.includes("agent-step-item--thinking"), "addStepItem does not mark the thinking row");
});

test("Landing redesign: redaction pipeline hero, 3-step scrubber, bento features, console preset chips", () => {
  // Redesign 2026-11-18: editorial hero with a split redaction
  // preview (raw DOM -> local layer -> sanitized tokens), a 3-step
  // workflow scrubber, a bento features grid, and 3 preset mission
  // chips in the live console.
  const landing = read("index.html");
  assert.ok(landing.includes("redact-flow"), "hero has no redaction pipeline visual");
  assert.ok(landing.includes("LOCAL REDACTION LAYER"), "hero visual has no redaction layer");
  assert.ok(landing.includes("[CARD_REDACTED]"), "hero visual shows no redacted tokens");
  assert.ok(landing.includes("0 bytes leaked"), "hero visual has no 0-bytes-leaked badge");
  assert.ok(landing.includes("Launch Control Center"), "hero CTA is not 'Launch Control Center'");
  assert.ok(landing.includes("Try Live Demo"), "hero has no 'Try Live Demo' secondary CTA");
  assert.ok(landing.includes("flow-scrubber__tab"), "workflow has no scrubber tabs");
  assert.ok(landing.includes("Natural Language Mission"), "scrubber step 1 missing");
  assert.ok(landing.includes("Local On-Device Redaction"), "scrubber step 2 missing");
  assert.ok(landing.includes("Supervised Browser Execution"), "scrubber step 3 missing");
  assert.ok(landing.includes("bento__card--wide"), "features has no bento wide card");
  assert.ok(landing.includes("Human-in-the-Loop Gates"), "bento is missing a feature card");
  assert.ok(landing.includes("console__chip"), "console has no preset mission chips");
  assert.ok(landing.includes("Compare yellow dresses on Amazon"), "console preset 1 missing");
  assert.ok(landing.includes("Extract the invoice summary to XLSX"), "console preset 2 missing");
  assert.ok(landing.includes("MX Master 3S"), "console preset 3 missing");
  const mainTs = read("src/main.ts");
  assert.ok(mainTs.includes("flow-scrubber__tab"), "main.ts does not wire the scrubber tabs");
  const consoleTs = read("src/taskconsole.ts");
  assert.ok(consoleTs.includes("console__chip"), "taskconsole.ts does not wire the preset chips");
  const mainCss = read("src/styles/main.css");
  assert.ok(mainCss.includes(".redact-flow"), "main.css has no redaction-flow rules");
  assert.ok(mainCss.includes(".flow-scrubber"), "main.css has no scrubber rules");
  assert.ok(mainCss.includes(".bento"), "main.css has no bento grid rules");
  assert.ok(mainCss.includes("--font-mono"), "main.css has no mono font token");
});

test("Control center nav: sliding active pill + light tokens", () => {
  // Redesign 2026-11-18: the 12-view nav has an animated active
  // pill that slides behind the selected view, and the control
  // center tokens are the light Tactile Daylight system.
  const cc = read("src/controlCenter.ts");
  assert.ok(cc.includes("controlNavPill"), "controlCenter.ts has no active-pill element");
  assert.ok(cc.includes("moveNavPill"), "controlCenter.ts does not position the active pill");
  assert.ok(cc.includes("cubic-bezier(0.16, 1, 0.3, 1)") || read("src/styles/control-center-refresh.css").includes("cubic-bezier(0.16, 1, 0.3, 1)"), "active pill has no spec easing");
  const refresh = read("src/styles/control-center-refresh.css");
  assert.ok(refresh.includes(".control-nav__pill"), "refresh CSS has no active-pill rules");
  assert.ok(!refresh.includes("#d5ff70"), "control-center refresh still uses lime");
  assert.ok(!refresh.includes("#101210"), "control-center refresh still uses ink");
  const ccCss = read("src/styles/control-center.css");
  assert.ok(ccCss.includes("--cc-ink:#F8F9FA"), "control-center tokens are not the light system");
  assert.ok(ccCss.includes("--cc-lime:#2563EB"), "control-center accent is not cobalt");
});

test("Inspector: browser viewport frame, URL bar, telemetry counters", () => {
  // Redesign 2026-11-18: the inspector is a real browser viewport
  // frame (macOS controls, monospace URL bar with lock), with a
  // hover inspect overlay and DOM/privacy telemetry counters.
  assert.ok(agentHtml.includes("browser-frame"), "inspector has no browser viewport frame");
  assert.ok(agentHtml.includes("browser-frame__dot"), "viewport frame has no window controls");
  assert.ok(agentHtml.includes("browserFrameUrlText"), "viewport frame has no URL bar");
  assert.ok(agentHtml.includes("inspector-viewport__overlay"), "viewport has no hover inspect overlay");
  assert.ok(agentHtml.includes('id="telDomNodes"'), "telemetry has no DOM Nodes Scanned counter");
  assert.ok(agentHtml.includes('id="telPiiMasked"'), "telemetry has no PII Masked counter");
  assert.ok(agentHtml.includes("0 bytes leaked"), "telemetry has no 0-bytes-leaked label");
  assert.ok(agentApp.includes("browserFrameUrlText"), "agentApp does not sync the frame URL bar");
  const srcCss = read("src/styles/agent.css");
  assert.ok(srcCss.includes(".browser-frame__chrome"), "agent.css has no browser chrome rules");
  assert.ok(srcCss.includes(".browser-frame__url"), "agent.css has no URL bar rules");
  assert.ok(srcCss.includes(".obstacle-badge--clear"), "agent.css has no obstacle badge rules");
});
