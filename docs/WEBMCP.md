# WebMCP support (Task 14)

WebMCP is the web-agent MCP standard (Chrome/Google + Microsoft
proposal; W3C Web Machine Learning Community Group, accepted
September 2025; draft Community Group Report February 2026): a page
exposes tools that an agent can call instead of clicking. The API is
`document.modelContext` (the earlier `navigator.modelContext` was
deprecated in Chrome 150); tools have a name, a description, and
JSON Schema inputs/outputs; a manifest may be published at
`/.well-known/webmcp.json`. Early preview in Chrome 146 behind a
flag, origin trial in Chrome 149, stable targeted 2026–2027.

PrivAgent implements the conservative subset below. The invocation
API shape follows the draft spec and may need adjustment if the
final spec moves it — that boundary is documented, not hidden.

## What is supported

- **Detection during observation.** The extension collects
  page-declared tools from the tab (`document.modelContext`, with a
  `navigator.modelContext` fallback; `tools` array, or async
  `listTools()`), capped at 50, descriptions capped at 2,000 chars.
  The collection runs in the tab via an injected function — the
  background worker's own `document` is not the page's. A page that
  exposes nothing yields no tools (the normal case); a manifest at
  `/.well-known/webmcp.json` can also be fetched by the backend
  (same-origin, no redirects, 5 s, 64 KiB cap, egress-gated).
- **Planner calls through the existing action pipeline.** When a
  page offers a WebMCP tool that matches the current step, the
  planner may emit a `webmcp_tool` step (`{tool, arguments}`). It
  goes through `_execute_with_retries` to the extension, which calls
  the tool in the page (`callTool` / `invokeTool` / `executeTool`,
  first shape that exists) and reports the truncated result as
  evidence text. The tool's return value is never executed as code.
  Before that evidence reaches the planner, the loop sanitises it
  with the MCP sanitizer (`sanitize_tool_output` — bounded, redacted,
  injection patterns quarantined, wrapped as
  `<untrusted_tool_content>`): a tool result is untrusted page
  content, and truncation alone is not sanitisation.
- **Prompt block.** Declared tools render as
  `<webmcp-tools untrusted="true" source="page-declared">` with name
  + description + input schema, so the planner can see what the
  page offers without trusting it.

## What is NOT trusted

Page-declared tools are **untrusted page content**, exactly like
page text. The trust rules:

- **Structure is validated at redaction time** (`webmcp.py`):
  name `^[A-Za-z][A-Za-z0-9_.\-]{0,63}$`, description a string of
  at most 2,000 chars (over-long descriptions are dropped, not
  truncated — a truncated description could cut a credential lure
  mid-sentence and change what the checker sees), input/output
  schemas must be JSON Schema objects (validated with
  `Draft202012Validator.check_schema`), duplicates dropped, cap 50.
  Malformed declarations never become callable.
- **A call is only valid for a tool this observation actually
  declared for this origin.** Validation (`_validate_step`)
  rejects an undeclared tool, a tool from another origin, and
  arguments that do not match the tool's input schema.
- **The checker scans the tool description + arguments.**
  `scan_page_text` over `name + description + JSON(args)`; any
  `ignore_instructions` / `reveal_system_prompt` /
  `exfiltrate_secret` / `otp_exfiltration` / `credential_harvest`
  finding makes the tool uncallable (`PLAN_INVALID`).
- **A tool that asks for credentials or a payment forces
  takeover.** `tool_requests_sensitive` scans the name, description,
  and input-schema property names (snake_case split so `api_token`
  is seen as `api token`) against the sensitive-name vocabulary +
  payment words. Such a tool must be `risk=sensitive` and the loop
  forces takeover before execution — the operator supplies the
  secret in the tab; the agent never types a secret into a page
  tool.
- **Risk floor is `write`.** A tool's self-declared read-only-ness
  is page content, not trusted — a tool call can change site state.
- **Confirmation is not batched.** Every step keeps its own
  confirmation / takeover / allow-list / checker gates; the checker
  is skipped only for pinned batch steps, and `check_plan` ran on
  the full plan first.

## What is NOT implemented

- The **declarative API** (HTML forms): only the imperative
  `modelContext` API is read and called. A page that declares tools
  only via forms is treated as having no tools.
- **Manifest-driven discovery in the loop**: the loop does not fetch
  `/.well-known/webmcp.json` during observation (that would add a
  request per observation); `fetch_webmcp_manifest` exists for the
  operator/discovery path.
- **Async-only tool listings**: if the browser exposes only
  `listTools()` (no `tools` array), the extension reads it; if it
  exposes neither, the page is treated as having no tools.
- **No live Chrome verification**: collection and invocation are
  verified against mocked `document.modelContext` objects in
  `extension/tests/webmcpUcp.test.ts`, not against a real browser
  implementing the draft API. If Chrome's final API shape differs
  (method names, `tools` property), the extension fails honestly
  with `WEBMCP_UNAVAILABLE` (recoverable — the loop re-plans and
  can fall back to driving the UI) and the call site is the one
  place to update (`doWebMCPTool` in `actionExecutor.ts`).

## Files

- `backend/app/webmcp.py` — parsing, validation, sensitivity,
  manifest fetch, prompt rendering.
- `backend/app/planner_validation.py` — `webmcp_tool` branch in
  `_validate_step`; `backend/app/checker.py` — the tool call counts
  as a write for the exfiltration-lure rule.
- `backend/app/privacy.py` — tools ride the redacted payload
  (`PrivacyFilteredPayload.webmcp_tools`); `backend/app/planner.py`
  — prompt block; `backend/app/agentLoop.py` — stash into
  `working_memory`, takeover branch.
- `extension/src/contracts.ts` — `webmcp_tool` action + `WebMCPTool`
  + observation fields; `extension/src/actionExecutor.ts` —
  collection in `observe()`, `doWebMCPTool`, dispatch case.
- `backend/tests/test_webmcp.py` (16), `tests/test_task14_loop.py`
  (dispatch / validation / takeover), `extension/tests/webmcpUcp.test.ts` (7).
