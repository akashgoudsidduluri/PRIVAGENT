import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const root = new URL("..", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const src = readFileSync(join(root, "src/controlViews.ts"), "utf8");

// ---------------------------------------------------------------------------
// Email auto-sync countdown must run on the mission's own frequency.
// A 60-minute Gmail sweep used to sync every 5 minutes anyway while the
// banner printed the real frequency (3600s).
// ---------------------------------------------------------------------------

test("email sync period is derived from mission.frequency_seconds, not hardcoded", () => {
  assert.match(src, /syncPeriodSeconds\s*=\s*Number\.isFinite\(mission\.frequency_seconds\)/);
  assert.match(src, /let remainingSeconds = syncPeriodSeconds;/);
  assert.match(src, /diff <= syncPeriodSeconds/);
});

test("email sync resets reuse the mission period, never 300", () => {
  // The `remainingSeconds = diff` assignment is the server's next_run_at
  // countdown, not a reset — it is covered by the clamp test above.
  const resets = [...src.matchAll(/remainingSeconds = (\w+);/g)]
    .map((m) => m[1])
    .filter((v) => v !== "diff");
  assert.ok(resets.length >= 3, `expected >=3 remainingSeconds resets, got ${resets}`);
  assert.ok(resets.every((v) => v === "syncPeriodSeconds"), `resets must all be syncPeriodSeconds, got ${resets}`);
  assert.doesNotMatch(src, /remainingSeconds = 300/);
  assert.doesNotMatch(src, /diff <= 300/);
});

test("email banner names the mission period instead of 'every 5 mins'", () => {
  assert.doesNotMatch(src, /auto-sync every 5 mins/);
  assert.match(src, /auto-sync every ' \+ formatSyncPeriod\(syncPeriodSeconds\)/);
  assert.doesNotMatch(src, /id="emailSyncCountdown"[^>]*>05:00</);
});
