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
      host.querySelector("#btnGateApproveStep")?.textContent.includes("Approve This Step Only"),
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
    assert.ok(tpl[1].includes("Approve This Step Only"), "template lost the approve-step label");
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
  assert.ok(controlCenter.includes("index === 1 || index === 2 || index === 8"), "mount is not limited to the views that need live nodes");
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

test("Editorial redesign: one warm-paper palette, zero shadows, zero gradients", () => {
  // 2026-11-18 Editorial redesign: the workspace is warm alabaster
  // paper (#faf8f5) with deep olive-charcoal (#484830) accents, forest
  // sage privacy, terracotta gate, muted crimson abort. The old
  // cyan/purple/navy AND the ink+lime palettes are both gone. Zero
  // drop shadows, zero gradients, zero blue/purple.
  const srcCss = read("src/styles/agent.css");
  for (const bad of [
    "#46d7ff", "#a06bff", "#c084fc", "#c9a9ff", "#38bdf8", "#a78bfa",
    "#55eaff", "#7ae3ff", "#0ea5e9", "#f1edff", "#2563EB",
    "70, 215, 255", "160, 107, 255", "85, 234, 255",
    "56, 189, 248", "70, 160, 220",
    "#060b1e", "#04122b", "#0a1230",
    "#d5ff70", "213, 255, 112", "#101210", "#1b1e1b",
  ]) {
    assert.ok(!srcCss.includes(bad), `agent.css still uses the old palette colour ${bad}`);
  }
  assert.ok(srcCss.includes("--bg: #faf8f5"), "agent.css does not define the alabaster background");
  assert.ok(srcCss.includes("--accent: #484830"), "agent.css does not define the olive-charcoal accent");
  assert.ok(srcCss.includes("--privacy: #2d5a3c"), "agent.css does not define the forest-sage privacy colour");
  assert.ok(srcCss.includes("--gate: #9a5b13"), "agent.css does not define the terracotta gate colour");
  assert.ok(srcCss.includes("--danger: #a62626"), "agent.css does not define the muted-crimson abort colour");
  // Zero gradients, zero drop shadows (focus rings are 0 0 0 2px).
  assert.ok(!/linear-gradient\(/.test(srcCss), "agent.css still has a linear gradient");
  assert.ok(!/radial-gradient\(/.test(srcCss), "agent.css still has a radial gradient");
  for (const m of srcCss.matchAll(/box-shadow:\s*([^;]+);/g)) {
    const v = m[1].trim();
    assert.ok(v === "none" || v.startsWith("0 0 0 2px"), `agent.css still has a drop shadow: ${v}`);
  }
  // No glassmorphism on panels.
  assert.ok(!srcCss.includes("backdrop-filter: blur(12px)"), "agent.css still blurs the header");
  assert.ok(!srcCss.includes("backdrop-filter: blur(8px)"), "agent.css still blurs a card");
});

test("Editorial redesign: motion background is gone (no aurora blobs)", () => {
  assert.ok(!agentHtml.includes('class="motion-bg"'), "agent.html still has a .motion-bg element");
  assert.ok(!agentHtml.includes("motion-bg__blob"), "agent.html still has aurora blobs");
  const srcCss = read("src/styles/agent.css");
  assert.ok(!srcCss.includes(".motion-bg"), "agent.css still has motion-bg rules");
  assert.ok(!srcCss.includes("motion-bg-drift"), "agent.css still has the aurora drift animation");
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

test("Thinking state is a warm-stone pulse bar + 'Sanitizing DOM locally…', not bouncing dots", () => {
  // 2026-11-18 Editorial redesign: the bouncing dots are gone. The
  // live row renders a subtle horizontal warm-stone pulse bar with a
  // "Sanitizing DOM locally…" label, so the user can see the agent is
  // still working without a chatty animation.
  assert.ok(agentApp.includes("thinking-pulse"), "addStepItem does not render the thinking pulse bar");
  assert.ok(agentApp.includes("Sanitizing DOM locally"), "addStepItem does not label the thinking row");
  assert.ok(agentApp.includes("agent-step-item--thinking"), "thinking row has no distinguishing class");
  assert.ok(!agentApp.includes("thinking-dots"), "addStepItem still renders the old bouncing dots");
  const srcCss = read("src/styles/agent.css");
  assert.ok(srcCss.includes("@keyframes thinking-pulse"), "agent.css has no thinking-pulse animation");
  assert.ok(srcCss.includes(".thinking-pulse"), "agent.css has no .thinking-pulse rule");
  assert.ok(
    srcCss.includes("@media (prefers-reduced-motion: reduce)") && srcCss.includes(".thinking-pulse { animation: none; }"),
    "thinking pulse does not stop for prefers-reduced-motion",
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
    fn[0].includes("if (isMessageInTimeline(cleanMessage)) return;"),
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

test("Editorial command header: 60px, serif wordmark, segmented pills, redaction pill", () => {
  // Spec: 60px sticky alabaster header with a hairline bottom border;
  // serif PrivAgent wordmark + SIH-26171 pill; segmented Provider /
  // Vault / Memory pills with live counts; forest-sage redaction pill;
  // olive New Mission pill; ghost Public Site pill.
  const srcCss = read("src/styles/agent.css");
  assert.ok(srcCss.includes("height: 60px"), "agent.css does not set the 60px command header");
  assert.ok(srcCss.includes(".privacy-telemetry-pill"), "agent.css has no redaction pill");
  assert.ok(srcCss.includes(".header-pill"), "agent.css has no segmented header pills");
  assert.ok(srcCss.includes(".brand-pill"), "agent.css has no SIH-26171 pill");
  assert.ok(agentHtml.includes('class="brand-wordmark"'), "agent.html has no serif wordmark");
  assert.ok(agentHtml.includes("SIH-26171"), "agent.html has no SIH-26171 pill");
  assert.ok(agentHtml.includes('id="providerStatus"'), "agent.html has no Provider count");
  assert.ok(agentHtml.includes('id="vaultCount"'), "agent.html has no Vault count");
  assert.ok(agentHtml.includes('id="memoryCount"'), "agent.html has no Memory count");
  assert.ok(agentHtml.includes('id="privacyTelemetryPill"'), "agent.html has no redaction pill");
  assert.ok(agentHtml.includes("On-Device Redaction"), "redaction pill lost its label");
  assert.ok(agentHtml.includes("Public Site"), "agent.html lost the Public Site pill");
});

test("Editorial nav rail: no giant P. lettermark, active view is an olive-bordered pill", () => {
  // Spec: compact nav rail, no giant P. lettermark at the top; the
  // active view gets a subtle #eae6dd pill with a #484830 left border.
  assert.ok(!agentHtml.includes("control-monogram"), "agent.html still renders the giant P. lettermark");
  const cc = read("src/controlCenter.ts");
  assert.ok(!cc.includes("control-monogram"), "controlCenter.ts still renders the giant P. lettermark");
  const srcCss = read("src/styles/control-center.css");
  assert.ok(srcCss.includes(".control-nav button.selected"), "control-center.css has no selected nav state");
  assert.ok(srcCss.includes("background:#eae6dd"), "selected nav view is not the #eae6dd pill");
  assert.ok(srcCss.includes("border-left:3px solid var(--cc-lime)"), "selected nav view has no olive left border");
});

test("Editorial step stream: connected track, status circles, mono durations, redaction pill", () => {
  const srcCss = read("src/styles/agent.css");
  assert.ok(srcCss.includes(".agent-steps::before"), "agent.css has no connected step track");
  assert.ok(srcCss.includes("agent-step-item--completed"), "agent.css has no completed-step state");
  assert.ok(srcCss.includes(".agent-step-item__duration"), "agent.css has no step duration");
  assert.ok(srcCss.includes(".redaction-pill"), "agent.css has no Local Redaction Pill");
  assert.ok(srcCss.includes("redaction-pill__tokens"), "redaction pill has no token display");
  assert.ok(agentApp.includes('agent-step-item--completed'), "addStepItem does not mark completed steps");
});

test("Editorial composer: white input box, Attach pill + chips, Send morphs to Stop", () => {
  // Spec: clean white input box (no floating shadow box); a custom
  // styled Attach file ghost pill + file chip list instead of the
  // native <input type=file>; the olive Send pill swaps to a crimson
  // Stop pill while a mission runs; micro-status bar underneath.
  assert.ok(agentHtml.includes('id="btnAttachMission"'), "agent.html has no Attach pill");
  assert.ok(agentHtml.includes('id="btnSendMission"'), "agent.html has no Send pill");
  assert.ok(agentHtml.includes('id="composerChips"'), "agent.html has no file chip list");
  assert.ok(agentHtml.includes('id="composerStatus"'), "agent.html has no composer micro-status bar");
  assert.ok(agentHtml.includes("Local redaction on"), "micro-status bar lost its redaction text");
  assert.ok(agentHtml.includes("Human gate on"), "micro-status bar lost its gate text");
  assert.ok(agentApp.includes("renderAttachmentChips"), "agentApp does not render file chips");
  assert.ok(agentApp.includes("btnSendMission.hidden = working"), "Send does not morph to Stop while working");
  const srcCss = read("src/styles/agent.css");
  assert.ok(srcCss.includes(".composer-attach"), "agent.css has no Attach pill style");
  assert.ok(srcCss.includes(".composer-send"), "agent.css has no Send pill style");
  assert.ok(srcCss.includes(".composer-chip"), "agent.css has no file chip style");
  assert.ok(srcCss.includes(".composer-status"), "agent.css has no micro-status bar style");
  // The textarea is transparent on the white card (no black panel).
  assert.ok(!srcCss.includes("rgba(16, 18, 16, 0.9)"), "agent.css still has the black textarea background");
});

test("Editorial inspector: dark titlebar + mono URL bar over a white viewport", () => {
  // Spec: a clean #1a1918 browser titlebar (window dots + monospace
  // URL bar + obstacle pill) over a #ffffff screenshot viewport, plus
  // bottom telemetry counters (DOM Scanned, 0B Leaked, Scan Latency).
  assert.ok(agentHtml.includes('class="browser-frame"'), "agent.html has no browser frame");
  assert.ok(agentHtml.includes("browser-frame__chrome"), "agent.html has no dark titlebar");
  assert.ok(agentHtml.includes("browser-frame__url"), "agent.html has no mono URL bar");
  assert.ok(agentHtml.includes('id="browserFrameUrlText"'), "agent.html has no frame URL text node");
  assert.ok(agentHtml.includes('id="telDomNodes"'), "agent.html has no DOM Scanned counter");
  assert.ok(agentHtml.includes('id="telPiiMasked"'), "agent.html has no PII Masked counter");
  assert.ok(agentHtml.includes('id="obstacleBadge"'), "agent.html has no obstacle pill");
  const srcCss = read("src/styles/agent.css");
  assert.ok(srcCss.includes(".browser-frame__chrome"), "agent.css has no titlebar style");
  assert.ok(srcCss.includes("background: var(--dark-band)"), "titlebar is not the dark editorial band");
  assert.ok(agentApp.includes("browserFrameUrlText"), "agentApp does not sync the frame URL");
});

test("Editorial human gate: terracotta card, serif title, Takeover toggle, three pills", () => {
  // Spec: warm terracotta-amber card with a Source Serif 4 title
  // "Sign-in or Payment Confirmation Required", a Takeover toggle, and
  // three pill buttons (Continue olive / Approve This Step Only ghost
  // / Abort Mission crimson).
  assert.ok(agentHtml.includes("Sign-in or Payment Confirmation Required"), "human gate lost its title");
  assert.ok(agentHtml.includes('id="takeoverToggle"'), "human gate has no Takeover toggle");
  assert.ok(agentHtml.includes("I Have Completed This / Continue"), "human gate lost the Continue pill");
  assert.ok(agentHtml.includes("Approve This Step Only"), "human gate lost the Approve This Step Only pill");
  assert.ok(agentHtml.includes("Abort Mission"), "human gate lost the Abort pill");
  const srcCss = read("src/styles/agent.css");
  assert.ok(srcCss.includes(".human-gate__title"), "agent.css has no human-gate title style");
  assert.ok(srcCss.includes(".human-gate__takeover"), "agent.css has no Takeover toggle style");
  assert.ok(srcCss.includes(".human-gate__buttons .btn--danger"), "agent.css has no crimson Abort pill");
  assert.ok(srcCss.includes("font-family: var(--font-serif)"), "human-gate title is not serif");
});

test("Editorial hero: 82vh grid, serif H1, split DOM redaction card", () => {
  // Spec: 82vh tight editorial grid; H1 "A browser agent that never
  // leaks your screen." (52px desktop / 34px mobile, -1.3px tracking);
  // right column is a crisp white Split DOM Redaction Card showing raw
  // inputs -> 8ms redaction layer -> sanitized tokens.
  const indexHtml = read("index.html");
  assert.ok(indexHtml.includes("A browser agent that never leaks your screen."), "hero H1 is not the Editorial headline");
  assert.ok(indexHtml.includes("SIH 26171"), "hero lost the SIH eyebrow");
  assert.ok(indexHtml.includes("redact-flow"), "hero has no Split DOM Redaction Card");
  assert.ok(indexHtml.includes("[CARD_REDACTED]"), "redaction card lost the sanitized card token");
  assert.ok(indexHtml.includes("[PHONE_REDACTED]"), "redaction card lost the sanitized phone token");
  assert.ok(indexHtml.includes("8ms On-Device Redaction Layer"), "redaction card lost the redaction layer band");
  assert.ok(indexHtml.includes("0 bytes leaked"), "redaction card lost the 0-bytes-leaked badge");
  assert.ok(indexHtml.includes("Launch Workspace"), "hero lost the Launch Workspace CTA");
  assert.ok(indexHtml.includes("Run Live Demo"), "hero lost the Run Live Demo CTA");
  const main = read("src/styles/main.css");
  assert.ok(main.includes("min-height: 82vh"), "hero is not 82vh");
  assert.ok(main.includes("letter-spacing: -1.3px"), "hero H1 tracking is not -1.3px");
  assert.ok(main.includes(".redact-flow"), "main.css has no redact-flow rules");
  // The old space hero (stars, space art, preview card) is gone.
  assert.ok(!indexHtml.includes("hero-space-02.png"), "index.html still references the space hero image");
  assert.ok(!indexHtml.includes('id="stars"'), "index.html still has the starfield");
  assert.ok(!indexHtml.includes("hero__preview"), "index.html still has the old preview card");
});

test("Editorial landing: tech strip, 3-step workflow tabs, console chips, dark CTA band", () => {
  const indexHtml = read("index.html");
  assert.ok(indexHtml.includes("tech-strip"), "landing has no quiet tech strip");
  assert.ok(indexHtml.includes("BUILT WITH") || indexHtml.includes("Built with"), "tech strip lost its eyebrow");
  assert.ok(indexHtml.includes("Manifest V3"), "tech strip lost Manifest V3");
  assert.ok(indexHtml.includes("OWASP Agentic-2026"), "tech strip lost OWASP Agentic-2026");
  assert.ok(indexHtml.includes("flow-scrubber"), "workflow has no 3-step scrubber");
  assert.ok(indexHtml.includes("Local On-Device Redaction"), "workflow lost the redaction step");
  assert.ok(indexHtml.includes("Supervised Browser Execution"), "workflow lost the execution step");
  assert.ok(indexHtml.includes("console__chip"), "console has no preset mission chips");
  assert.ok(indexHtml.includes("Compare yellow dresses on Amazon and Myntra"), "console lost the yellow-dress preset");
  assert.ok(indexHtml.includes("cta-band"), "landing has no dark final CTA band");
  assert.ok(indexHtml.includes("Automate the web without surrendering your data."), "CTA band lost its headline");
  assert.ok(indexHtml.includes("Local redaction on"), "CTA band lost its reassurance line");
  const main = read("src/styles/main.css");
  assert.ok(main.includes(".cta-band"), "main.css has no cta-band rules");
  assert.ok(main.includes(".flow-tab"), "main.css has no flow-tab rules");
  assert.ok(main.includes(".tech-strip"), "main.css has no tech-strip rules");
  assert.ok(main.includes(".console__chip"), "main.css has no console-chip rules");
  assert.ok(read("src/main.ts").includes("flow-tab"), "main.ts does not wire the workflow tabs");
  assert.ok(read("src/taskconsole.ts").includes("console__chip"), "taskconsole.ts does not wire the preset chips");
});

test("Editorial contrast audit: no dark-on-dark panels in any view", () => {
  // Every card, sidebar, input and view must be light (#faf8f5 /
  // #f3f0ea / #ffffff) with #181818 ink. No leftover black panels,
  // near-white text on light, or dark fallbacks.
  for (const cssFile of [
    "src/styles/agent.css", "src/styles/main.css",
    "src/styles/control-center.css", "src/styles/control-center-refresh.css",
    "src/styles/landing-refresh.css", "src/styles/taskconsole.css",
  ]) {
    const css = read(cssFile);
    for (const bad of [
      "rgba(27, 30, 27", "rgba(16, 18, 16", "rgba(0, 0, 0, 0.3",
      "rgba(0, 0, 0, 0.35", "rgba(0, 0, 0, 0.4", "rgba(0, 0, 0, 0.45",
      "#cbd5e1", "#94a3b8", "#f1f5f9", "#e2e8f0", "#f87171",
      "#fbbf24", "#fef3c7", "#34d399", "#6ee7b7",
      "#0b1230", "#061032",
    ]) {
      assert.ok(!css.includes(bad), `${cssFile} still has a dark-on-dark value: ${bad}`);
    }
  }
  // Inline styles in the workspace JS must not reintroduce them.
  for (const tsFile of ["src/agentApp.ts", "src/controlViews.ts"]) {
    const ts = read(tsFile);
    for (const bad of ["#d5ff70", "#a4ad9e", "#34d399", "#f87171", "#fbbf24", "#cbd5e1", "#6ee7b7"]) {
      assert.ok(!ts.includes(bad), `${tsFile} still has an off-palette inline colour: ${bad}`);
    }
  }
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

test("Chat-only monitoring: the Missions page (monitor form + scheduled list) is gone", () => {
  // 2026-11-18: monitoring is chat-only. The Missions view (Create a
  // monitor form + Scheduled missions list) was removed — a monitor can
  // be anything (price, page, inbox), not just a product URL, so the
  // product-only form was the wrong shape. The nav has 11 views now.
  assert.ok(!read("src/controlCenter.ts").includes("'Missions'"), "controlCenter still lists a Missions view");
  assert.ok(!read("src/controlCenter.ts").includes("◎"), "controlCenter still has the Missions nav icon");
  assert.ok(!read("src/controlViews.ts").includes("Create a monitor"), "controlViews still renders the monitor form");
  assert.ok(!read("src/controlViews.ts").includes("Scheduled missions"), "controlViews still renders the scheduled list");
  assert.ok(!read("src/controlViews.ts").includes("missionForm"), "controlViews still wires the monitor form");
  assert.ok(!read("src/controlViews.ts").includes("mission-action"), "controlViews still wires monitor Pause/Activate");
  assert.ok(!read("src/controlViews.ts").includes("mission-events"), "controlViews still wires View diffs");
  assert.ok(!read("src/controlViews.ts").includes("missionDiffs"), "controlViews still has the mission diffs container");
  // The nav indices shifted: Email Intelligence is now index 3 and
  // Activity is index 8; the mount list must match controlViews.
  const cc = read("src/controlCenter.ts");
  assert.ok(cc.includes("if(index===8){content.append(nodes!.feed)"), "Activity is not index 8 after the removal");
  assert.ok(cc.includes("if([0,3,4,5,6,7,9,10].includes(index))"), "mount list does not match the shifted indices");
  const cv = read("src/controlViews.ts");
  assert.ok(cv.includes("if (index === 3) {\n      await mountEmailIntelligence"), "Email Intelligence is not index 3 after the removal");
  assert.ok(cv.includes("if (index === 10) {"), "Settings is not index 10 after the removal");
  // The old #missions hash falls back to Overview (route() renders
  // index 0 when no view name matches), so a bookmarked link does not
  // break the control center.
  assert.ok(cc.includes("render(index<0?0:index)"), "unknown hash does not fall back to Overview");
});

test("Interactive watch card: confirmation carries a marker, chat renders Pause / Cancel / View changes", () => {
  // 2026-11-18: when the agent creates a watch in chat, the backend
  // appends a machine-readable marker to the confirmation. The
  // workspace strips it before rendering (it must never show as
  // literal text) and renders an interactive watch card with real
  // controls wired to the missions API.
  assert.ok(agentApp.includes("privagent-watch:"), "agentApp has no watch-marker handling");
  assert.ok(agentApp.includes("function stripWatchMarker"), "agentApp has no stripWatchMarker helper");
  assert.ok(agentApp.includes("WATCH_MARKER_RE"), "agentApp has no watch-marker regex");
  assert.ok(agentApp.includes("watch-card"), "agentApp renders no watch card");
  assert.ok(agentApp.includes("data-watch-pause"), "watch card has no Pause/Resume button");
  assert.ok(agentApp.includes("data-watch-cancel"), "watch card has no Cancel button");
  assert.ok(agentApp.includes("data-watch-events"), "watch card has no View changes button");
  assert.ok(agentApp.includes("function hydrateWatchCard"), "watch card never fills in the real title/status");
  assert.ok(agentApp.includes("/api/control/missions/${encodeURIComponent(missionId)}"), "watch card does not call the missions API by id");
  assert.ok(agentApp.includes('method: "PATCH"'), "watch card never pauses/resumes via PATCH");
  assert.ok(agentApp.includes('method: "DELETE"'), "watch card never cancels via DELETE");
  assert.ok(agentApp.includes("Confirm cancel?"), "Cancel is not a two-step inline confirm");
  // The marker must be stripped before renderMarkdown in the result
  // card path too, or it would show as literal text there.
  assert.ok(agentApp.includes("stripWatchMarker(rawMessage).text"), "formatResultHtml does not strip the marker");
  assert.ok(agentApp.includes("stripWatchMarker(message).text"), "renderCompletion failed path does not strip the marker");
  const css = read("src/styles/agent.css");
  assert.ok(css.includes(".watch-card"), "agent.css has no watch-card styles");
  assert.ok(css.includes(".watch-card__status"), "agent.css has no watch status pill");
  assert.ok(css.includes(".watch-card__btn--danger"), "agent.css has no crimson Cancel button");
});

test("Interactive step stream: completed steps expand to a detail panel with Copy", () => {
  // 2026-11-18: the bot's current actions were static text. A
  // completed step is now a button (chevron + text); clicking it
  // opens a detail panel with the step number, the full message, and
  // a Copy button. The transient Thinking row stays a plain status.
  assert.ok(agentApp.includes("agent-step-item__toggle"), "addStepItem does not render an expandable step toggle");
  assert.ok(agentApp.includes('aria-expanded="false"'), "step toggle has no aria-expanded state");
  assert.ok(agentApp.includes("agent-step-item__detail"), "addStepItem does not render a step detail panel");
  assert.ok(agentApp.includes("agent-step-item__copy"), "step detail has no Copy button");
  assert.ok(agentApp.includes("data-copy-step"), "Copy button has no data-copy-step hook");
  assert.ok(agentApp.includes("navigator.clipboard"), "Copy does not use the clipboard");
  const css = read("src/styles/agent.css");
  assert.ok(css.includes(".agent-step-item__toggle"), "agent.css has no step toggle styles");
  assert.ok(css.includes(".agent-step-item__chevron"), "agent.css has no step chevron");
  assert.ok(css.includes(".agent-step-item__detail"), "agent.css has no step detail styles");
  assert.ok(css.includes(".agent-step-item__copy"), "agent.css has no Copy button styles");
});

test("Chat-only memory: the 'Remember a fact' form is gone, the transparency lists stay", () => {
  // 2026-11-18: same pattern as monitoring. Facts are learned from
  // the conversation ("remember my preferred language is Python") —
  // the manual Category/Key/Value/Retention form is not how memory
  // should be entered. The lists underneath stay: they are the
  // privacy surface showing what the agent knows, with Forget/Delete.
  const cv = read("src/controlViews.ts");
  assert.ok(!cv.includes("factForm"), "controlViews still wires the fact form");
  assert.ok(!cv.includes("cc-form--memory"), "controlViews still renders the fact form");
  assert.ok(!cv.includes('name="ttlDays"'), "controlViews still renders the retention input");
  assert.ok(cv.includes("CHAT-ONLY MEMORY"), "Memory view has no chat-only pointer card");
  assert.ok(cv.includes("remember my preferred language is Python"), "pointer card does not show the chat example");
  // The transparency lists stay, with their Forget/Delete controls.
  assert.ok(cv.includes("card('Semantic facts'"), "Semantic facts list is gone");
  assert.ok(cv.includes("card('Preferences'"), "Preferences list is gone");
  assert.ok(cv.includes("card('Episodic history'"), "Episodic history is gone");
  assert.ok(cv.includes("data-forget"), "Forget button is gone");
  assert.ok(cv.includes("data-preference"), "Preference Delete button is gone");
  assert.ok(cv.includes("data-delete-chat"), "Delete chat button is gone");
  // The backend chat path must exist: the agent loop routes
  // "remember ..." to the companion before the browser task.
  const companion = read("backend/app/memory_companion.py");
  assert.ok(companion.includes("def is_remember_directive"), "no remember-directive detector");
  assert.ok(companion.includes("def handle_remember_instruction"), "no remember handler");
  assert.ok(companion.includes("semantic.remember"), "remember handler does not use the semantic store");
  const loop = read("backend/app/agentLoop.py");
  assert.ok(loop.includes("is_remember_directive(record.instruction)"), "agent loop does not route remember directives");
});
