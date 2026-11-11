import { readFileSync } from "node:fs";

const agentAppTs = readFileSync(new URL("../src/agentApp.ts", import.meta.url), "utf-8");
const taskconsoleTs = readFileSync(new URL("../src/taskconsole.ts", import.meta.url), "utf-8");

const tests = [];
function test(name, fn) {
  try {
    fn();
    tests.push(`  PASS ${name}`);
  } catch (e) {
    tests.push(`  FAIL ${name} — ${e.message}`);
    process.exitCode = 1;
  }
}

// 1. Enter and Shift+Enter Behavior
test("agentApp handles Enter key to submit mission without Shift", () => {
  if (!agentAppTs.includes('e.key === "Enter" && !e.shiftKey')) {
    throw new Error("missing Enter keydown check without shiftKey");
  }
  if (!agentAppTs.includes("e.preventDefault()")) {
    throw new Error("missing e.preventDefault() on Enter submission");
  }
});

test("agentApp preserves newlines on Shift+Enter", () => {
  // If e.shiftKey is true, preventDefault must NOT be called on Enter
  const match = agentAppTs.match(/missionInput\?\.addEventListener\("keydown",\s*\(e[^)]*\)\s*=>\s*\{([\s\S]*?)\}\);/);
  if (!match) throw new Error("missing keydown listener on missionInput");
  const body = match[1];
  if (!body.includes("!e.shiftKey")) {
    throw new Error("Shift+Enter not excluded from submission");
  }
});

test("taskconsole handles Enter key to submit without Shift", () => {
  if (!taskconsoleTs.includes('e.key === "Enter" && !e.shiftKey')) {
    throw new Error("missing Enter keydown check in taskconsole.ts");
  }
});

// 2. Prevent Accidental Duplicate Submissions
test("agentApp guards against duplicate submissions while working", () => {
  if (!agentAppTs.includes("if (isWorking")) {
    throw new Error("missing isWorking guard on task submission");
  }
});

// 3. WebSocket Reconnection and Task State Synchronization
test("agentApp synchronizes active task on WebSocket onopen", () => {
  const wsMatch = agentAppTs.match(/ws\.onopen\s*=\s*\(\)\s*=>\s*\{([\s\S]*?)\};/);
  if (!wsMatch) throw new Error("missing ws.onopen in agentApp.ts");
  const onopenBody = wsMatch[1];
  if (!onopenBody.includes("syncActiveTask")) {
    throw new Error("ws.onopen does not synchronize active task state");
  }
  if (!onopenBody.includes("currentTaskId")) {
    throw new Error("ws.onopen does not check currentTaskId");
  }
});

test("taskconsole synchronizes active task on WebSocket onopen", () => {
  const wsMatch = taskconsoleTs.match(/ws\.onopen\s*=\s*async\s*\(\)\s*=>\s*\{([\s\S]*?)\};/);
  if (!wsMatch) throw new Error("missing ws.onopen in taskconsole.ts");
  const onopenBody = wsMatch[1];
  if (!onopenBody.includes("/api/task/")) {
    throw new Error("taskconsole ws.onopen does not poll /api/task/");
  }
});

// 4. Recovery After Missed Terminal Event
test("agentApp includes authoritative polling for active task", () => {
  if (!agentAppTs.includes("function startFallbackPolling")) {
    throw new Error("missing startFallbackPolling helper");
  }
  if (!agentAppTs.includes("function syncActiveTask")) {
    throw new Error("missing syncActiveTask helper");
  }
  if (!agentAppTs.includes("/api/task/")) {
    throw new Error("missing /api/task/ polling endpoint call");
  }
});

test("syncActiveTask unlocks UI on terminal state or 404 missing task", () => {
  if (!agentAppTs.includes("renderCompletion")) {
    throw new Error("missing renderCompletion call on terminal sync");
  }
  if (!agentAppTs.includes("res.status === 404")) {
    throw new Error("missing 404 missing task recovery handling");
  }
});

// 5. Stalled Task Notice & Cancellation Recovery
test("agentApp displays stalled progress notice when task hangs", () => {
  if (!agentAppTs.includes("showStalledNotice")) {
    throw new Error("missing showStalledNotice function");
  }
  if (!agentAppTs.includes("clearStalledNotice")) {
    throw new Error("missing clearStalledNotice function");
  }
});

test("cancellation halts polling and resets working state", () => {
  const cancelIdx = agentAppTs.indexOf('btnCancelMission?.addEventListener("click"');
  if (cancelIdx === -1) throw new Error("missing btnCancelMission click listener");
  const cancelSection = agentAppTs.slice(cancelIdx, cancelIdx + 500);
  if (!cancelSection.includes("stopFallbackPolling()")) {
    throw new Error("cancel does not stop fallback polling");
  }
  if (!cancelSection.includes("setButtonWorking(false)")) {
    throw new Error("cancel does not unlock the submit button");
  }
});

// 6. Mission Summary price tag and chat reopening persistence
test("formatResultHtml requires digits after currency symbol to prevent blue rs, badge", () => {
  if (!agentAppTs.includes("\\d[\\d,]*(?:\\.\\d+)?")) {
    throw new Error("price regex in formatResultHtml does not enforce leading digit");
  }
  if (!agentAppTs.includes("/\\d/.test(price)")) {
    throw new Error("price-tag rendering does not verify presence of actual digits");
  }
});

test("selectChat renders real failed/blocked status and never forces completed", () => {
  if (!agentAppTs.includes("ev.includes(\"failed\")") && !agentAppTs.includes("content.startsWith(\"error:\")")) {
    throw new Error("selectChat does not check evidence for failed state");
  }
  if (agentAppTs.includes("renderCompletion(\"completed\", lastAgentMsg, false)")) {
    throw new Error("selectChat still hardcodes completed in renderCompletion");
  }
});

console.log("frontend recovery & task dispatch tests\n" + tests.join("\n"));
