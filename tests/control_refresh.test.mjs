import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const root = new URL("..", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const views = readFileSync(join(root, "src/controlViews.ts"), "utf8");
const center = readFileSync(join(root, "src/controlCenter.ts"), "utf8");

// ---------------------------------------------------------------------------
// Live refresh must not wipe the operator's interaction state: the
// Privacy Center re-mounts every 5s and the Inspector every 4s, and both
// used to rebuild their DOM from scratch (audit filter reset, screenshot
// re-decoded and flickering).
// ---------------------------------------------------------------------------

test("Privacy Center preserves the audit filter and limit across re-mounts", () => {
  const branch = views.match(/if \(index === 9\) \{([\s\S]*?)\n    \}\n/);
  assert.ok(branch, "no Privacy Center branch in mountControlView");
  const body = branch[1];
  assert.ok(
    body.includes("querySelector<HTMLSelectElement>('#auditEventFilter')?.value"),
    "audit event filter is never read before the rebuild",
  );
  assert.ok(
    body.includes("querySelector<HTMLSelectElement>('#auditLimit')?.value"),
    "audit limit is never read before the rebuild",
  );
  assert.ok(
    body.includes("eventFilter.value = prevAuditEvent"),
    "audit event filter is never restored after the rebuild",
  );
  assert.ok(
    body.includes("limitSel.value = prevAuditLimit"),
    "audit limit is never restored after the rebuild",
  );
  // The restore must happen before the audit list loads, or the list
  // loads with the defaults anyway.
  assert.ok(
    body.indexOf("eventFilter.value = prevAuditEvent") < body.indexOf("void loadAudit()"),
    "filter is restored after the audit list already loaded",
  );
});

test("Inspector only swaps the screenshot when it actually changed", () => {
  assert.ok(
    views.includes('if (image.getAttribute("src") !== src) image.src = src;'),
    "Inspector re-assigns the same screenshot src on every 4s tick",
  );
});

test("live refresh intervals are kept (the fix preserves state, not deletes refresh)", () => {
  assert.ok(
    center.includes("setInterval(()=>mountInspector(content,()=>version===generation),4000)"),
    "Inspector 4s refresh interval is gone",
  );
  assert.ok(
    center.includes("setInterval(()=>mountControlView(index,content,()=>version===generation),5000)"),
    "Privacy Center 5s refresh interval is gone",
  );
});
