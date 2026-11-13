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

test("agentApp appends each turn's result instead of overwriting the previous one", () => {
  const start = agentAppTs.indexOf("function addCompletionStepItem");
  const end = agentAppTs.indexOf("async function syncActiveTask");
  if (start === -1 || end === -1) throw new Error("missing addCompletionStepItem helper");
  const body = agentAppTs.slice(start, end);
  if (body.includes("existingResult")) {
    throw new Error("completion still overwrites an earlier turn's result in place");
  }
  if (!body.includes("stepsList.appendChild(li)")) {
    throw new Error("completion does not append a new timeline item");
  }
});

test("agentApp removes transient Thinking rows when a turn terminates", () => {
  if (!agentAppTs.includes('data-transient')) {
    throw new Error("Thinking status rows are not marked transient");
  }
  const start = agentAppTs.indexOf("function renderCompletion");
  const end = agentAppTs.indexOf("function showHumanGate");
  if (start === -1 || end === -1) throw new Error("missing renderCompletion helper");
  const body = agentAppTs.slice(start, end);
  if (!body.includes('li[data-transient="true"]')) {
    throw new Error("terminal rendering does not clear stale Thinking rows");
  }
});

// 7. Rehydration & Refresh Resilience
test("selectChat restores intermediate steps into timeline on reload", () => {
  if (!agentAppTs.includes("msg.steps.forEach")) {
    throw new Error("selectChat does not iterate msg.steps to rehydrate intermediate steps");
  }
  if (!agentAppTs.includes("addStepItem(st.message, st.stepNum)")) {
    throw new Error("selectChat does not add intermediate steps to timeline");
  }
});

test("selectChat updates executed step counter to reflect total steps", () => {
  if (!agentAppTs.includes("detail.total_steps") && !agentAppTs.includes("calculatedSteps")) {
    throw new Error("selectChat does not calculate executed step count");
  }
  if (!agentAppTs.includes("updateStepsCount(totalSteps)")) {
    throw new Error("selectChat does not update step counter on chat load");
  }
});

test("selectChat reconnects to running active_task and preserves pending user turn", () => {
  if (!agentAppTs.includes("detail.active_task")) {
    throw new Error("selectChat does not check detail.active_task");
  }
  if (!agentAppTs.includes("isPendingUserTurn")) {
    throw new Error("selectChat does not guard against showing previous turn result for pending turn");
  }
  if (!agentAppTs.includes("!isPendingUserTurn && lastAgentMsg")) {
    throw new Error("selectChat improperly renders completion when user turn is still in-flight");
  }
});

test("agentApp persists and restores active chat across reloads", () => {
  if (!agentAppTs.includes('localStorage.setItem("privagent_active_chat_id"')) {
    throw new Error("activeChatId is not saved to localStorage on selection");
  }
  if (!agentAppTs.includes('localStorage.getItem("privagent_active_chat_id"')) {
    throw new Error("saved activeChatId is not restored from localStorage on load");
  }
});

console.log("frontend recovery & task dispatch tests\n" + tests.join("\n"));
