import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const root = new URL("..", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const app = readFileSync(join(root, "src/agentApp.ts"), "utf8");

// ---------------------------------------------------------------------------
// Price-watch alerts: the backend persists the alert into the mission
// conversation and broadcasts MISSION_EVENT_TRIGGERED with the
// conversation id. The workspace must refresh the matching active chat
// from the server — never append the payload as a second message (the
// persisted row is the single source of truth, so a reload cannot
// duplicate it).
// ---------------------------------------------------------------------------

const segment = app.slice(
  app.indexOf('if (env.type === "MISSION_EVENT_TRIGGERED")'),
  app.indexOf('if (env.type !== "PROGRESS") return;'),
);

test("WebSocket handler handles MISSION_EVENT_TRIGGERED before the PROGRESS early return", () => {
  assert.ok(app.includes("MISSION_EVENT_TRIGGERED"), "no MISSION_EVENT_TRIGGERED branch in agentApp.ts");
  assert.ok(
    app.indexOf("MISSION_EVENT_TRIGGERED") < app.indexOf('if (env.type !== "PROGRESS") return;'),
    "MISSION_EVENT_TRIGGERED is checked only after the PROGRESS early return drops it",
  );
});

test("a mission event refreshes only the matching active chat", () => {
  assert.ok(segment.length > 0, "no MISSION_EVENT_TRIGGERED branch before the PROGRESS early return");
  assert.ok(segment.includes("env.conversationId"), "branch never reads the event conversationId");
  assert.ok(segment.includes("convId === activeChatId"), "branch refreshes chats that did not trigger the event");
  assert.ok(segment.includes("selectChat(activeChatId)"), "branch does not re-render the active chat");
});

test("the refresh re-renders from the server instead of appending the payload", () => {
  assert.ok(segment.length > 0, "no MISSION_EVENT_TRIGGERED branch before the PROGRESS early return");
  assert.ok(!segment.includes("addStepItem("), "branch appends a step item from the payload (duplicates the persisted message)");
  assert.ok(!segment.includes("addCompletionStepItem("), "branch appends a completion item from the payload (duplicates the persisted message)");
  assert.ok(segment.includes("return;"), "branch falls through to PROGRESS handling");
});

test("a mission event does not refresh while a task is in flight in that chat", () => {
  assert.ok(segment.length > 0, "no MISSION_EVENT_TRIGGERED branch before the PROGRESS early return");
  assert.ok(segment.includes("isWorking"), "branch refreshes even while a task is running (wipes the draft, the gate and the timeline)");
  assert.ok(segment.includes("currentTaskId"), "branch does not check for an in-flight task");
});

test("the WebSocket envelope type carries the conversation id", () => {
  assert.ok(app.includes("conversationId?: string | null;"), "envelope type has no conversationId field");
});
