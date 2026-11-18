import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const root = new URL("..", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const agentApp = readFileSync(join(root, "src/agentApp.ts"), "utf8");
const capabilities = readFileSync(
  join(root, "backend/app/capabilities.py"),
  "utf8",
);

// ---------------------------------------------------------------------------
// Task 15: no personal address baked into shipped source.
// The "Email this to me" chip used to carry a hardcoded personal Gmail
// address in its prompt, and the planner capability prompt used the same
// address as its example. The operator's address is resolved at runtime
// from the connected account (or asked for); it must never ship in source.
// ---------------------------------------------------------------------------

const EMAIL_RE = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g;

test("suggested-moves email chip carries no address in its prompt", () => {
  const chip = agentApp.match(
    /class="suggested-chip" data-prompt="([^"]*Email[^"]*)"/,
  );
  assert.ok(chip, "email suggested chip must exist");
  assert.equal(chip[1], "Email this comparison summary to me");
  assert.ok(!EMAIL_RE.test(chip[1]), "chip prompt must not contain an email address");
});

test("agentApp.ts ships no personal email address anywhere", () => {
  const matches = [...agentApp.matchAll(EMAIL_RE)];
  assert.deepEqual(
    matches.map((m) => m[0]),
    [],
    `agentApp.ts must not contain a hardcoded email address, found: ${matches.map((m) => m[0]).join(", ")}`,
  );
});

test("planner capability prompt uses a synthetic example address only", () => {
  const matches = [...capabilities.matchAll(EMAIL_RE)].map((m) => m[0]);
  assert.ok(
    matches.every((addr) => addr.endsWith("@example.com")),
    `capabilities.py must only use @example.com addresses, found: ${matches.join(", ")}`,
  );
  assert.ok(!capabilities.includes("b21650406"), "personal address must not appear");
});
