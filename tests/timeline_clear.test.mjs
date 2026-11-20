import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const root = new URL("..", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const read = (p) => readFileSync(join(root, p), "utf8");
const agentApp = read("src/agentApp.ts");
const controlViews = read("src/controlViews.ts");
const controlCenter = read("src/controlCenter.ts");

// ---------------------------------------------------------------------------
// Fix 1 — + New Mission clears the TASK TIMELINE panel.
// Live bug: step rows from a previous chat stayed visible on a fresh
// mission screen because createNewChat() reset the feed, result card
// and artifacts but never touched the timeline card.
// ---------------------------------------------------------------------------

test("createNewChat clears the task timeline panel", () => {
  const start = agentApp.indexOf("function createNewChat()");
  assert.ok(start >= 0, "createNewChat missing");
  const body = agentApp.slice(start, agentApp.indexOf("\n}\n", start));
  assert.ok(body.includes("clearTaskTimeline()"), "createNewChat must call clearTaskTimeline()");
});

test("selectChat clears the previous chat timeline before loading history", () => {
  const start = agentApp.indexOf("async function selectChat(");
  assert.ok(start >= 0, "selectChat missing");
  const head = agentApp.slice(start, start + 2000);
  assert.ok(head.includes("clearTaskTimeline()"), "selectChat must clear the timeline up front");
});

test("clearTaskTimeline empties the list and hides the card", () => {
  const start = agentApp.indexOf("function clearTaskTimeline()");
  assert.ok(start >= 0, "clearTaskTimeline missing");
  const body = agentApp.slice(start, agentApp.indexOf("\n}\n", start));
  assert.ok(body.includes("taskTimelineList") && body.includes("replaceChildren()"));
  assert.ok(body.includes("taskTimelineCard") && body.includes("hidden = true"));
});

// ---------------------------------------------------------------------------
// Fix 2 — single /ws/client socket and deduplicated Control Center refreshes.
// Live bug: opening/configuring Connectors spawned multiple /ws/client
// sockets (backend: `socket.send() raised exception`) and repeated
// bursts of GET /api/connectors, /api/llm/config, /api/email/missions,
// /api/mcp/* from overlapping view mounts.
// ---------------------------------------------------------------------------

test("connectWebSocket never opens a second socket while one is open or connecting", () => {
  const start = agentApp.indexOf("function connectWebSocket()");
  assert.ok(start >= 0, "connectWebSocket missing");
  const body = agentApp.slice(start, agentApp.indexOf("// Initial Load", start));
  assert.ok(body.includes("activeSocket"), "must track the active socket");
  assert.ok(body.includes("WebSocket.OPEN") && body.includes("WebSocket.CONNECTING"));
  assert.ok(body.includes("wsReconnectTimer"), "reconnect must be single-scheduled");
});

test("Control Center coalesces concurrent renders of the same view", () => {
  assert.ok(controlCenter.includes("renderInFlight"), "render guard missing");
  assert.ok(controlCenter.includes("pendingRenderIndex"), "pending render index missing");
});

test("Connectors reads are deduplicated and cached across re-mounts", () => {
  assert.ok(controlViews.includes("getConnectors("), "getConnectors helper missing");
  assert.ok(controlViews.includes("connectorsInFlight"), "in-flight dedup missing");
  // Exactly one raw read remains: inside getConnectors itself. Every
  // other call site must go through the dedup helper.
  const rawReads = (controlViews.match(/request\('\/api\/connectors'\)/g) || []).length;
  assert.equal(rawReads, 1, `expected 1 raw /api/connectors read (inside getConnectors), found ${rawReads}`);
});
