import test from "node:test";
import assert from "node:assert/strict";

import { apiFetch } from "../src/auth.ts";

// ---------------------------------------------------------------------------
// Operator-token prompt: a dismissed prompt must not latch the prompt off
// forever (one cancel used to leave every later call 401ing with no way
// back short of reload), but a polling loop must not nag every second.
// ---------------------------------------------------------------------------

function installStubs({ promptAnswers = [], fetchStatuses = [401] } = {}) {
  const store = new Map();
  let promptCalls = 0;
  let fetchCalls = 0;
  globalThis.window = {
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, v),
    },
    prompt: () => {
      promptCalls++;
      return promptAnswers.shift() ?? null;
    },
  };
  const queue = [...fetchStatuses];
  let lastStatus = queue[queue.length - 1] ?? 401;
  globalThis.fetch = async () => {
    fetchCalls++;
    const status = queue.length > 0 ? queue.shift() : lastStatus;
    lastStatus = status;
    return { status };
  };
  return {
    store,
    promptCalls: () => promptCalls,
    fetchCalls: () => fetchCalls,
  };
}

test("a dismissed prompt is offered again after the cooldown, not never", async () => {
  const stubs = installStubs({ promptAnswers: [null, "tok-123"] });
  const origNow = Date.now;
  let now = 1_000_000;
  Date.now = () => now;
  try {
    // First 401: prompt shown, user cancels.
    let res = await apiFetch("http://localhost:8000/api/task/x");
    assert.equal(res.status, 401);
    assert.equal(stubs.promptCalls(), 1);

    // Immediate second 401: throttled, no second prompt (no storm).
    res = await apiFetch("http://localhost:8000/api/task/x");
    assert.equal(res.status, 401);
    assert.equal(stubs.promptCalls(), 1);

    // After the cooldown the prompt returns — and entering a token
    // retries the call with it.
    now += 61_000;
    res = await apiFetch("http://localhost:8000/api/task/x");
    assert.equal(stubs.promptCalls(), 2);
    assert.equal(stubs.store.get("privagentLocalToken"), "tok-123");
    // The retry went out (second fetch call for this apiFetch).
    assert.ok(stubs.fetchCalls() >= 4);
  } finally {
    Date.now = origNow;
    delete globalThis.window;
    delete globalThis.fetch;
  }
});

test("a healthy response clears the cooldown so a later 401 prompts at once", async () => {
  const stubs = installStubs({ promptAnswers: [null, "tok-456"], fetchStatuses: [401, 200, 401] });
  const origNow = Date.now;
  let now = 2_000_000;
  Date.now = () => now;
  try {
    await apiFetch("http://localhost:8000/api/task/x");
    assert.equal(stubs.promptCalls(), 1);
    // A 200 in between resets the cooldown...
    const ok = await apiFetch("http://localhost:8000/api/task/x");
    assert.equal(ok.status, 200);
    // ...so the very next 401 (same clock) prompts immediately.
    await apiFetch("http://localhost:8000/api/task/x");
    assert.equal(stubs.promptCalls(), 2);
  } finally {
    Date.now = origNow;
    delete globalThis.window;
    delete globalThis.fetch;
  }
});
