import test from "node:test";
import assert from "node:assert/strict";

import { taskRouteLabel } from "../src/routeLabel.ts";

// ---------------------------------------------------------------------------
// Task 14: routing-decision labels — names only, never content.
// ---------------------------------------------------------------------------

test("taskRouteLabel returns the connector label verbatim", () => {
  assert.equal(
    taskRouteLabel({ via: "via Gmail connector", connector: "gmail", operation: "list" }),
    "via Gmail connector",
  );
  assert.equal(
    taskRouteLabel({ via: "via Google Drive connector", connector: "google_drive" }),
    "via Google Drive connector",
  );
});

test("taskRouteLabel builds WebMCP / UCP labels from names only", () => {
  assert.equal(
    taskRouteLabel({ via: "webmcp_tool", tool: "search_products" }),
    "via WebMCP tool search_products",
  );
  assert.equal(
    taskRouteLabel({ via: "ucp_checkout", merchant: "shop.example.com" }),
    "via UCP checkout at shop.example.com",
  );
});

test("taskRouteLabel returns null for plain browser tasks and junk", () => {
  assert.equal(taskRouteLabel(null), null);
  assert.equal(taskRouteLabel(undefined), null);
  assert.equal(taskRouteLabel({}), null);
  assert.equal(taskRouteLabel({ via: "browser" }), null);
  assert.equal(taskRouteLabel({ via: "webmcp_tool" }), null); // no tool name
  assert.equal(taskRouteLabel({ via: "ucp_checkout" }), null); // no merchant
  assert.equal(taskRouteLabel("via Gmail connector"), null); // not an object
});

test("taskRouteLabel never echoes arguments or amounts", () => {
  const label = taskRouteLabel({
    via: "webmcp_tool",
    tool: "search_products",
    arguments: { query: "hunter2" },
  });
  assert.ok(!label.includes("hunter2"));
  const ucp = taskRouteLabel({
    via: "ucp_checkout",
    merchant: "shop.example.com",
    amount: "1299.00",
    currency: "INR",
  });
  assert.ok(!ucp.includes("1299"));
});
