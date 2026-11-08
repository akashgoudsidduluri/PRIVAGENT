# NAVIGATOR Architecture & Technical Reference
## SIH 26171 | On-device Visual Perception for Lightweight Browser Agents

NAVIGATOR is a privacy-first AI browser agent designed for the Smart India Hackathon (SIH 26171). It turns a high-performance landing interface into a fully functional autonomous browser control system with local privacy filtering, spatial visual perception, schema-validated task planning, and human-in-the-loop safety confirmation.

---

## 1. System Topology & Process Boundaries

```
┌────────────────────────────────────────────────────────────────────────┐
│                        USER DESKTOP WORKSPACE                          │
│                                                                        │
│   ┌───────────────────────────┐         REST / HTTP                    │
│   │   Vite Frontend Console   ├───────────────────────────────┐        │
│   │   (http://localhost:5173) │                               │        │
│   │   - Task submission       │         WebSocket             │        │
│   │   - Live action timeline  │◄──────────────────────┐       │        │
│   │   - Confirmation modal    │      /ws/client       │       │        │
│   └───────────────────────────┘                       │       │        │
│                                                       ▼       ▼        │
│                                           ┌────────────────────────┐   │
│                                           │  FastAPI Backend Hub   │   │
│                                           │ (http://localhost:8000)│   │
│                                           │  - Agent Control Loop  │   │
│                                           │  - Local Privacy Filter│   │
│                                           │  - Schema Validation   │   │
│                                           │  - Gemini/Groq/Mock    │   │
│                                           └───────────┬────────────┘   │
│                                                       │                │
│                                                       │ WebSocket      │
│                                                       │ /ws/extension  │
│                                                       ▼                │
│                                           ┌────────────────────────┐   │
│                                           │ Chrome MV3 Extension   │   │
│                                           │  - Service Worker Relay│   │
│                                           │  - Action Executor     │   │
│                                           │  - Content Script Agent│   │
│                                           │  - Spatial Perception  │   │
│                                           └────────────────────────┘   │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Core Subsystems

### A. Shared Action & Observation Contracts
- **TypeScript Source of Truth**: `extension/src/contracts.ts`
- **Python Pydantic Mirror**: `backend/app/contracts.py`
- **Contract Parity Test**: `backend/tests/test_contracts.py` guarantees zero drift across language boundaries.
- **Allowed Action Types (`ACTION_TYPES`)**:
  - `navigate`: Open URL, go back/forward, reload. Only credential-free `http(s)` URLs; `javascript:`, `data:`, `file:`, `blob:`, `chrome:`, `view-source:`, and CR/LF/backslash injection are rejected.
  - `click`: Semantic targeting by accessible name/role with fallback to coordinates. Exactly one activation is dispatched (trusted CDP click, or a single in-page click — never both).
  - `type`: Input text with auto-clear. Submission is a separate single trigger (Send button, otherwise one Enter, otherwise one form submit).
  - `press`: Dispatch keyboard events (Enter, Escape, Tab, arrow keys).
  - `scroll`: Incremental container (`containerId`) and viewport scrolling (up, down, top, bottom).
  - `zoom`: Native browser zoom adjustments (`in`, `out`, `reset`, explicit factor 0.25–3).
  - `wait`: Pause for DOM changes or content stability.
  - `observe`: Inspect live interactive elements, scroll offsets, DPR, and viewport.
  - `new_tab`, `select_tab`, `close_tab`: Tab management and multi-tab workflows.
  - `hover`: Move the pointer over a target, wait for an overlay, and return bounded visible overlay/tooltip text with the observation.
  - `select_date`: Write an ISO date into a native `input[type=date]` or choose a verified calendar-picker cell.
  - `slider_adjust`: Set a range/ARIA slider by percentage or direction and read the resulting value back locally.
- **Validation**: every action is checked for its required target/params, risk floor, and a `timeoutMs` clamped to 100–60,000 ms. The planner additionally caps a plan at 3 steps.
- **Retry safety**: only idempotent actions (`observe`, `scroll`, `wait`, `zoom`, `select_tab`) retry after a TIMEOUT. The backend waits ~10 s longer than the extension's own action deadline, so a late click is not resent as a duplicate.

### B. Visual Perception & Spatial Grounding
- **DOM & Accessibility Extraction**: Scans visible buttons, links, inputs, and comboboxes. Elements carry their real `tag` and `inputType`; values are not read during observation.
- **Bounding Boxes**: Populates `{ x, y, width, height }` in CSS pixels for every element.
- **Coordinate Grounding**:
  - Screenshots captured via Chrome debugger/tabs API normalize physical device pixels using devicePixelRatio (DPR).
  - Hit-testing resolves coordinates to candidate DOM elements with a pre-click `elementFromPoint` recheck.
- **Screenshot Policy**: Screenshots stay local by default (`ALLOW_SCREENSHOTS=false`). Remote vision is opt-in and is suppressed whenever a sensitive field is present; the provider additionally enforces raster MIME types and a 4 MB cap.

### C. Local Privacy Layer & Prompt Injection Defense
- **Explicit Privacy Boundary**: NAVIGATOR is not a zero-PII-egress system. The **user's task instruction is forwarded to the remote planner verbatim** by design (explicit user-input exception), because the agent must act on values the user supplied. Everything else that is provider-bound is sanitized locally first: page observations, article text, table cells, overlay/tooltip text, stored preferences, conversation history, session memory, and model-written notes.
- **Local-First Sanitization**: `backend/app/privacy.py` executes on the local host before page-derived or stored payloads are dispatched to remote AI models. `sanitize_text` applies URL-token redaction and the shared PII patterns, then truncates; `sanitize_memory` applies it recursively to string leaves.
- **Zero-Exposure Credential Policy (page paths)**:
  - Input fields of type `password` or `hidden` have values stripped (`None`).
  - Fields containing tokens like `pwd`, `secret`, `token`, `api key`, `credit card`, `ssn` are wiped.
  - Regex detection for credit cards, SSNs, emails, API keys, phone numbers, and `key=value` credentials masks matching strings in titles, names, values, article prose, table rows, and overlay text.
  - URLs are stripped of userinfo and have sensitive query/fragment values masked.
  - Overlay/tooltip text is sanitized and capped at 1200 characters (`MAX_OVERLAY_LEN`).
- **Fail-Closed Guarantee**: Any error during redaction immediately triggers `PrivacyViolation` and stops task execution. No partial or unvalidated payloads are transmitted.
- **Prompt Injection Defense**: Untrusted page content is isolated within `<page-context>`, `<working-memory>`, `<conversation-history>`, `<session-memory>`, and `<history>` wrappers marked untrusted. `build_prompt` entity-escapes fence-shaped delimiters found inside untrusted data so page text cannot close its own block. The system frame treats page/tooltip text as inert data. Prompt isolation is best-effort; enforcement is deterministic: schema validation admits only whitelisted action types, risk floors are re-checked, and sensitive actions halt at the human confirmation gate.

### D. Action Risk Classification & User Confirmation
- **Risk Classes**:
  - `read` (automatic): `scroll`, `zoom`, `wait`, `observe`, `select_tab`, `hover`.
  - `write` (monitored): `navigate`, `click`, `type`, `press`, `new_tab`, `close_tab`, `select_date`, `slider_adjust`.
  - `sensitive` (confirmation required): Purchases, payments, irreversible form submissions, account changes.
- **Human-in-the-Loop Gate**: The agent halts and sets `awaiting_confirmation` when the action is `sensitive`, the URL is a credential/payment surface (whole path segments such as `/login`, `/sign-in`, `/checkout`, `/billing`), or a WhatsApp QR login is required. Cancelling the task wakes the gate with a rejection so no approval can land afterwards.

### E. Origin Isolation & Cancellation
- **HTTP origin guard**: browser requests with a non-loopback, non-extension `Origin` are rejected with 403 before routing. CORS allows only `localhost`/`127.0.0.1` and `chrome-extension://`/`moz-extension://`.
- **WebSocket guard**: `/ws/client` and `/ws/extension` close with policy code 1008 when the `Origin` is not allowlisted.
- **Cancellation propagation**: `POST /api/task/{id}/cancel` sets a loop-level marker, wakes a pending confirmation, aborts any in-flight action result slot, and the loop checks the marker before planning, after confirmation, and after each execution. Terminal tasks are never rewritten. The UI replays a cancel that races task creation once the task id exists.
- **Multi-browser routing (known limitation)**: There is no browser-identity handshake on `/ws/extension`. `Hub.send_action` sends each action and observation request to the newest live socket, falling back to older sockets only when a send fails. If several browsers or profiles are connected to one backend, the most recently connected instance receives the task, and a new connection mid-task can redirect later actions. Within the acting browser, tab targeting works normally (`select_tab`, active-tab observation). Deterministic runs require exactly one connected browser, or a freshly toggled extension socket. Per-task socket pinning is a future protocol change.
- **Dashboard protection**: `index.html` and `agent.html` set `data-navigator-dashboard="1"`. Before selecting an active tab, reusing a tab for navigation, listing tabs, or executing `select_tab`/`close_tab`, the extension checks that marker in the page itself. The check is port-independent, so the dashboard stays protected when Vite auto-selects a port other than 5173/4173; unrelated localhost sites are not blocked. The conventional ports remain a fast-path hint only.

### F. Bounded Agent Loop & Latency
- **Observation reuse**: a successful action's post-action observation is validated with Pydantic and reused for the next plan, removing one observe round trip per step. It is still re-run through `classify_and_redact()` every iteration, and is discarded after a failed action, a malformed payload, `select_tab`, or `close_tab`.
- **Cycle**: observe → plan → validate → confirm (if sensitive) → act → verify, capped at 25 steps, 3 retries, and 3 plan steps.
- **Extension settling**: fixed post-action sleeps are replaced by a DOM-quiet `MutationObserver` settle, navigation-aware tab-status polling at 50 ms, hydration polling at 40 ms, and early exit once the page is interactive. A click that opens a new tab follows it so the cached observation matches the next active surface.

---

## 3. Evaluated Tasks & Verification Suite

All 8 canonical SIH tasks are verified through automated regression suites (`backend/tests/test_scenarios_a_to_h.py`):

| Test | Objective | Automated Test Assertion | Status |
|------|-----------|--------------------------|--------|
| **Test A** | Google Search | Types query into search box, submits form, verifies results returned | **PASSED** |
| **Test B** | Search, Scroll & Compare | Scrolls product catalog, extracts specs across items, structures comparison | **PASSED** |
| **Test C** | Zoom & Visual Interaction | Zooms viewport, targets settings button via spatial coordinates, verifies options | **PASSED** |
| **Test D** | Multi-Tab Workflow | Spawns multiple tabs, maintains isolated tab state, compares information | **PASSED** |
| **Test E** | Form Handling Without Submit | Fills form fields, explicitly stops before submit, verifies no form submission | **PASSED** |
| **Test F** | Dynamic Page Recovery | Handles `STALE_TARGET` failure, re-observes DOM, replans, and succeeds | **PASSED** |
| **Test G** | Privacy & Injection Defense | Redacts fake credit cards/passwords, ignores injected page instructions | **PASSED** |
| **Test H** | Consequential Action Gate | Halts at checkout/purchase, requires human approval, aborts if rejected | **PASSED** |

---

## 4. Benchmark Evaluation Metrics

Executed via `backend/scripts/benchmark_eval.py` against the deterministic mock harness:

- **Task Completion Rate**: 100.0% (8/8)
- **Correct Action Rate**: 100.0%
- **Target-Selection Errors**: 0
- **Recovery Success Rate**: 100.0% (1/1)
- **Average Scenario Duration**: ~45–55 ms (in-process orchestration; varies by run)
- **Privacy Policy Violations**: 0
- **Unsupported Action Attempts**: 0

---

## 5. Verification & Provider Configuration

| Suite | Command | Current result |
|-------|---------|----------------|
| Frontend | `npm test` | 25 checks (10 structure, 8 console, 7 sanitization) |
| Extension | `cd extension && npm test` | 83 Vitest tests, 7 files |
| Extension types | `cd extension && npx tsc --noEmit` | clean |
| Backend | `cd backend && python -m pytest tests -q` | 151 tests |
| Benchmark | `python backend/scripts/benchmark_eval.py` | 8/8 |

Node.js `^22.22.2 || ^24.15.0 || >=26.0.0` is declared in both `package.json` files and verified on 24.22.2 LTS. Python dependencies install reproducibly from `backend/requirements.lock` with `pip install --require-hashes -r requirements.lock`.

Provider selection (`NAVIGATOR_PROVIDER`, else auto-detected from keys, else `mock`):

- **Gemini**: default `gemini-3.8-flash` (low thinking), fallbacks `gemini-3.6-flash`, `gemini-3.5-flash-lite`. Gemini 2.0 Flash is shut down and 2.5 Flash is unavailable to new callers.
- **Groq**: default `openai/gpt-oss-120b`, fallbacks `openai/gpt-oss-20b`, `qwen/qwen3.8-27b`. Groq's Llama 3.3/3.1 models are Enterprise-only and are not used as silent defaults.
- **Mock**: deterministic offline plans for tests and demos.
- `GEMINI_MODEL` / `GROQ_MODEL` optionally override the primary model without editing code.

A requested provider is not silently substituted. `config.resolve_provider()` produces `{requested, effective, fallback, reason}` and both the planner factory and `GET /api/health` consume it, so the advertised provider is always the one actually used. Health exposes `provider`, `requestedProvider`, `providerFallback`, and `providerFallbackReason`; explicit `mock` never falls back to a remote provider. API keys are read from `backend/.env` or the environment and are never logged or returned by an endpoint.
