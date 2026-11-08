# NAVIGATOR — SIH 26171
## On-device Visual Perception for Lightweight Browser Agents

NAVIGATOR is a privacy-first AI browser agent developed for **Smart India Hackathon (SIH 26171)**. It combines a zero-runtime-dependency landing page and agent workspace, a Chrome Manifest V3 extension, a local FastAPI orchestration backend with pluggable Gemini/Groq planners (plus an offline mock), local fail-closed privacy redaction, spatial bounding-box perception, bounded recovery, and a human confirmation gate.

---

## What Actually Works (Verified Capabilities)

| Subsystem | Implementation | Status & Evidence |
|-----------|----------------|-------------------|
| **Landing Page & Agent Workspace** | Vite + vanilla TypeScript + hand CSS | ✓ `npm test` — 25/25 checks pass (10 structure, 8 console, 7 sanitization) |
| **Chrome MV3 Extension** | Relay worker + in-page script + action executor | ✓ Vitest 83/83 (7 files), `tsc --noEmit` clean, production build |
| **Backend Orchestration** | FastAPI hub + bounded agent loop | ✓ `pytest` 151/151 |
| **Shared Contracts** | TypeScript source mirrored as Pydantic models | ✓ Contract parity test (`test_contracts.py`) |
| **Browser Action Control** | Navigate, click, type, press, scroll, zoom, wait, tabs, hover, date, slider | ✓ All 14 action types (`ACTION_TYPES`) |
| **Spatial Perception** | Bounding boxes (`bounds: {x,y,w,h}`), DPR grounding | ✓ `test_coordinates.py` and Test C |
| **Privacy Redaction** | Strips credentials, cards, SSNs, tokens, URL secrets locally | ✓ Fail-closed tests (`test_privacy_failclosed.py`, `test_security_hardening.py`) |
| **Prompt Injection Defense** | Page text isolated as untrusted data; schema-validated plans | ✓ `test_planner_injection.py` |
| **Human-in-the-Loop Gate** | Sensitive/auth/payment actions halt for approval | ✓ `test_confirmation.py` and Test H |
| **Cancellation** | REST cancel propagates into the running loop and pending gate | ✓ `test_security_hardening.py` |
| **Bounded Agent Loop** | 25-step task budget, 3 retries, max 3 steps per plan | ✓ `test_agentLoop.py` and Test F |
| **Benchmark Scenarios A–H** | All 8 directive tasks in the mock harness | ✓ 8/8 via `benchmark_eval.py` |
| **Planner Providers** | Gemini (`gemini-3.8-flash`), Groq (`openai/gpt-oss-120b`), or `mock` | ✓ Provider tests; live key required for remote calls |

---

## Architecture Overview

```
                      ┌────────────────────────────────┐
                      │  NAVIGATOR Frontend / Workspace │
                      │    http://localhost:5173       │
                      └───────────────┬────────────────┘
                                      │ REST / WebSocket (/ws/client)
                                      ▼
                      ┌────────────────────────────────┐
                      │    FastAPI Orchestration Hub   │
                      │    http://localhost:8000       │
                      │  - Local privacy redaction     │
                      │  - Planner (Gemini/Groq/mock)  │
                      │  - Schema + URL validation     │
                      │  - Confirmation decision gate  │
                      └───────────────┬────────────────┘
                                      │ WebSocket (/ws/extension)
                                      ▼
                      ┌────────────────────────────────┐
                      │   Chrome Manifest V3 Extension │
                      │  - Action executor (all tabs)  │
                      │  - Content-script DOM observer │
                      │  - Bounding-box perception     │
                      └────────────────────────────────┘
```

The backend binds to loopback and only accepts browser origins from `localhost`/`127.0.0.1` or `chrome-extension://`/`moz-extension://`. This is origin isolation for a single-user local tool, **not** multi-user authentication — do not expose port 8000 to a network.

---

## Security & Privacy Defaults

- **Screenshots are local-only.** `ALLOW_SCREENSHOTS=false` by default. A rendered screenshot is sent to a remote model only when the operator opts in *and* no sensitive field (password, hidden, payment/credential markers) is present. Invalid, oversized (>4 MB), or non-raster images are dropped.
- **Fail-closed redaction.** If any redaction step throws, the payload is discarded with `PrivacyViolation`; a partial payload is never sent.
- **URL hygiene.** Userinfo (`user:pass@`), sensitive query/fragment values (`token`, `key`, `session`, `password`, …), and secret patterns are masked before planning or display. Navigation rejects non-`http(s)` schemes, embedded credentials, and CR/LF/backslash injection.
- **Plan validation.** Each plan is capped at 3 steps; `timeoutMs` must be 100–60,000; required parameters/targets are enforced per action; only whitelisted action types execute.
- **Single activation.** A click resolves to exactly one activation; a typed submit follows exactly one path (Send button → Enter → form submit), never all three. A partially delivered CDP click/keypress is treated as attempted so no duplicate fallback fires.
- **Cancellation is real.** `POST /api/task/{id}/cancel` marks the loop for stop, wakes a pending confirmation, aborts any in-flight extension action slot, and no further actions execute. A cancel racing the task-create request is replayed once the task id exists.
- **Retry safety.** Only idempotent actions (`observe`, `scroll`, `wait`, `zoom`, `select_tab`) are retried after a timeout; a click/type/press/navigate timeout is reported instead of risking a duplicate side effect.
- **No committed secrets.** API keys come from `backend/.env` (gitignored) or the environment.

### Privacy boundary (explicit user-input exception)

NAVIGATOR does **not** claim zero-PII egress. The boundary is deliberate:

| Data path | Handling |
|---|---|
| User task instruction | Sent to the configured provider **verbatim** (explicit user-input exception). The agent must act on values the user supplied. |
| Page observations (title, URL, element names/values, article text, table rows, overlay/tooltip text) | Sanitized locally before egress: URL secrets stripped, PII patterns masked, length-capped (`overlayText` ≤ 1200 chars). |
| Stored memory (preferences, conversation history, session/working memory, model notes) | Recursively sanitized before every prompt; long values bounded. |
| Screenshots | Local-only by default; opt-in and suppressed when a sensitive field is present. |

So: PII that the user types into the task box may reach the provider; PII that exists only on a page, in a tooltip, in a table, or in stored memory does not. Page/memory/history blocks are fenced as untrusted, and fence-shaped delimiters inside them are entity-escaped before prompt assembly.

### Multi-browser routing (known limitation)

Each action and observation is sent to the **newest live** `/ws/extension` socket. Sockets carry no browser or tab identity, so when several browsers or profiles connect to the same backend, the most recently connected instance receives the task. Within that browser, actions target the intended tab through the active-tab/`tabId` workflow. For deterministic runs, connect exactly one browser, or make the intended extension socket the newest (toggle its service worker). Per-task socket pinning would require a protocol change and is not part of this milestone.

---

## Prerequisites

| Dependency | Supported version | Notes |
|---|---|---|
| Node.js | `^22.22.2`, `^24.15.0`, or `>=26.0.0` (verified on 24.22.2 LTS) | Required by the test tooling (`jsdom`, `undici`). Node 22.18 is below the declared engine range. |
| Python | 3.12+ (verified on 3.13.7) | Backend only. |
| Chrome | Current stable (verified on 156) | Manifest V3 extension; Developer mode required. |
| npm | Bundled with Node | Lockfiles are committed; `npm ci` is recommended. |

## Quickstart & Run Instructions

### 1. Install and start the frontend
```bash
npm ci
npm run dev
# Vite prints the actual URL, usually http://localhost:5173
```

If port 5173 is occupied, Vite selects another port. That is supported: the dashboard is identified by a page marker, not by its port.

### 2. Create and start the backend

Windows (PowerShell):
```powershell
cd backend
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install --require-hashes -r requirements.lock
Copy-Item .env.example .env
uvicorn app.main:app --port 8000 --reload
# Running on http://localhost:8000
```

macOS/Linux:
```bash
cd backend
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install --require-hashes -r requirements.lock
cp .env.example .env
uvicorn app.main:app --port 8000 --reload
```

`requirements.txt` keeps the human-maintained ranges; `requirements.lock` is the reproducible, hash-pinned install used by release validation. Refresh it only after an intentional dependency change:

```bash
pip install pip-tools
pip-compile --generate-hashes --output-file requirements.lock requirements.txt
```

Configure providers in `backend/.env` (only one key is required; with no key the deterministic `mock` provider is used):

```env
# groq | gemini | mock (empty = auto-detect from keys)
NAVIGATOR_PROVIDER=gemini
GEMINI_API_KEY=your_gemini_api_key_here
# GROQ_API_KEY=your_groq_api_key_here
# Optional primary-model overrides
# GEMINI_MODEL=gemini-3.8-flash
# GROQ_MODEL=openai/gpt-oss-120b

MAX_STEPS=25
ALLOW_SCREENSHOTS=false
```

If a requested provider has no key, the backend may fall back to the other configured key or to mock. Health always reports the truth:

```bash
curl http://localhost:8000/api/health
# {"status":"ok","provider":"mock","requestedProvider":"groq",
#  "providerFallback":true,"providerFallbackReason":"groq is not configured; using mock", ...}
```

`provider` is the planner actually in use, `requestedProvider` is what `NAVIGATOR_PROVIDER` asked for, and `providerFallback` makes any substitution visible. Explicit `NAVIGATOR_PROVIDER=mock` never falls back to a real provider.

### 3. Build & Install the Chrome Extension
```bash
cd extension
npm ci
npm run build
powershell -Command "Copy-Item manifest.json dist/manifest.json -Force"
```

1. Open Chrome and navigate to `chrome://extensions`.
2. Enable **Developer mode**.
3. Click **Load unpacked** and select the `extension/dist` folder.
4. The service worker connects automatically to `ws://localhost:8000/ws/extension`.

The dashboard (`index.html` and `agent.html`) carries `data-navigator-dashboard="1"`. The extension checks that marker before selecting, navigating, closing, or acting on a tab, so the agent cannot drive the dashboard even when Vite runs on an auto-assigned port (for example 5190). Unrelated localhost sites are unaffected.

---

## Latency Design

The loop removes one full observation round trip per step by reusing the
post-action observation the extension already returns. The cache is never
trusted blindly: it is revalidated with Pydantic, re-run through
`classify_and_redact()`, discarded after any failed action, and forced fresh on
step 0 and for `select_tab` / `close_tab`.

The extension replaces fixed sleeps with a DOM-quiet `MutationObserver` settle
(`childList`/`characterData`), navigation-aware `waitForStable()` polling at
50 ms (tab status) and 40 ms (hydration), and an early exit once the page is
interactive. Low- and empty-element pages no longer pay the old 3.5 s SPA
penalty, and a click that opens a new tab observes that tab.

Modeled on a 6-action mixed task (click, type+submit, Enter, scroll, click,
type+submit) with a 180 ms observation round trip and 60 ms action round trip:

| Profile | Observations | Elapsed | Per step |
|---------|--------------|---------|----------|
| Before (fresh observe + fixed sleeps) | 7 | 6203.8 ms | 1034.0 ms |
| After (cached observation, old sleeps) | 1 | 5158.7 ms | 859.8 ms |
| After (cached observation + new settles) | 1 | 2325.8 ms | 387.6 ms |

Net: **-62.5%** end-to-end loop latency. The "after" settle cost is modeled
from the shipped extension constants, not measured in live Chrome; the
observation-cache saving is measured in-process.

---

## Test & Benchmark Battery

```bash
# 1. Frontend structure, console, and sanitization tests (25 checks)
npm test

# 2. Extension Vitest suite (83 tests, 7 files) + typecheck + build
cd extension
npm test
npx tsc --noEmit
npm run build
cd ..

# 3. Backend Pytest suite (151 tests)
cd backend
python -m venv venv        # activate it first (see Quickstart)
pip install --require-hashes -r requirements.lock
python -m pytest tests -q
cd ..

# 4. Benchmark evaluation suite (Tasks A through H, mock harness)
backend\venv\Scripts\python.exe backend/scripts/benchmark_eval.py

# 5. Live provider smoke test (requires GEMINI_API_KEY)
backend\venv\Scripts\python.exe backend/scripts/check_gemini.py
```

### Benchmark Metrics (Phase 11)

Measured with the deterministic mock harness (`benchmark_eval.py`), so durations reflect in-process orchestration, not network latency:

- **Task Completion Rate**: 100.0% (8/8)
- **Correct Action Rate**: 100.0%
- **Target-Selection Errors**: 0
- **Recovery Success Rate**: 100.0% (1/1)
- **Average Scenario Duration**: ~45–55 ms (in-process; varies by run)
- **Privacy Policy Violations**: 0
- **Unsupported Action Attempts**: 0

### Measured Performance

Measured on Windows with Chrome `156.0.8078.80`, Node `24.22.2`, Python `3.13.7`, and the live Groq provider. Raw data is in `demo/evidence/performance-micro.json`, `performance-e2e.json`, and `benchmark-eval.txt`.

| Measurement | Trials | Median | Min | Max | Notes |
|---|---|---|---|---|---|
| Backend cold import (`app.main`) | 5 | 945.73 ms | 647.57 ms | 964.30 ms | Fresh Python process; includes FastAPI app construction |
| Privacy sanitization (`classify_and_redact`) | 300 | 0.386 ms | 0.284 ms | 1.022 ms | Synthetic fixture payload, in-process |
| Mock planner (`plan_call`) | 300 | 0.003 ms | 0.002 ms | 0.030 ms | Deterministic, in-process |
| Benchmark scenarios A–H | 8 | 4.7 ms | 0.6 ms | 217.6 ms | Mock harness; average 46.7 ms |
| Live end-to-end run #1 | 1 | 9,089 ms | — | — | planner 2,059 ms, observe 118 ms, act 994 ms |
| Live end-to-end run #2 | 1 | 60,587 ms | — | — | planner 51,437 ms, observe 141 ms, act 421 ms |

Live-provider latency dominates and varies widely between calls. A separate three-trial repeatability attempt was **not** usable as a performance sample: one trial ended in a provider JSON/fallback error and two exceeded the 240 s observation window while still running. That run is preserved in `performance-live-repeatability.json` rather than averaged into the table. Use `NAVIGATOR_PROVIDER=mock` for deterministic, offline demonstrations and reserve live providers for short, pre-warmed runs.

### Demonstration Workflow & Evidence

The demo uses only synthetic data in `demo/site/index.html` and `demo/site/readonly.html`.

```bash
# Terminal 1: synthetic CRM fixture
python -m http.server 5180 --directory demo/site

# Terminal 2: dashboard (note the actual port Vite prints)
npm run dev

# Terminal 3: backend
cd backend
uvicorn app.main:app --port 8000
```

Load `extension/dist`, open the dashboard, and run these two tasks:

1. **Positive (verified `completed`, 9,089 ms)** — “Open http://localhost:5180/ and find the customer Priya Nair. Tell me her city and the total of her most recent order. Then enter 'Called 2026-11-08' in the follow-up note field and click Save note.”
   - Answer: Priya Nair is in Bengaluru; the latest order total is ₹4,860.00.
   - Fixture: the follow-up note was saved as `Called 2026-11-08`.
2. **Negative (verified `failed` / Blocked, 14,763 ms)** — “Open http://localhost:5180/readonly.html and set the Customer ID field to CUST-999, then click Save changes.”
   - The field stays `CUST-1042`, `readOnly` is `true`, and no change is saved.
   - The dashboard reports **Blocked / Failed**, never “Mission Completed”.

Evidence files:

| File | Contents |
|---|---|
| `demo/evidence/demo-run.json` | Positive run, timings, environment, and the preserved pre-fix negative record |
| `demo/evidence/positive-dashboard.png` | Dashboard after the positive run |
| `demo/evidence/positive-fixture.png` | Saved note in the synthetic CRM |
| `demo/evidence/negative-dashboard.png` | Blocked / Failed dashboard after the negative run |
| `demo/evidence/negative-readonly.png` | Read-only field unchanged |
| `demo/evidence/negative-run.json` | Negative task state, steps, and fixture state |
| `demo/evidence/privacy-sanitization.json` | Real `classify_and_redact()` replay over synthetic PII |
| `demo/evidence/performance-micro.json` | Startup, sanitization, and mock-planner statistics |
| `demo/evidence/performance-e2e.json` | Clean successful live end-to-end trial |
| `demo/evidence/performance-live-repeatability.json` | Honest record of the failed repeatability attempt |
| `demo/evidence/benchmark-eval.txt` | Raw A–H benchmark output |

The privacy replay is **not** a live network capture: it feeds the synthetic fixture’s PII through the real backend filter. It confirmed that email, phone (including Indian 5+5 formats), card number, SSN, API key, password value, session token, URL token, and prose credentials are all replaced with `[REDACTED]`, and screenshots are withheld.

---

## Known Limitations

- **Live provider latency and availability**: planner calls are the dominant cost and varied from ~2 s to ~51 s in successful runs; providers can occasionally return non-JSON and trigger fallback. Use `mock` for deterministic demos.
- **Multi-browser routing**: `/ws/extension` has no browser-identity handshake; the newest connected socket receives actions. Run exactly one browser/profile per backend.
- **Backend URL is fixed**: the frontend and extension target `localhost:8000` (`ws://localhost:8000/ws/extension`). Remote or port-forwarded deployment would require a configuration change.
- **Dashboard port is not fixed**: Vite may choose another port; protection uses the `data-navigator-dashboard="1"` page marker, so use the URL Vite prints.
- **Read-only fields**: typing into a read-only input can appear to succeed at the DOM level, but no page change occurs. The completion guard now reports this as `failed`, not `completed`.
- **Local single-user design**: the API relies on localhost origin checks, not user authentication. Do not expose port 8000 publicly.
- **Node version**: `^22.22.2`, `^24.15.0`, or `>=26.0.0`; Node 22.18 is below the tested engine range and only emits an npm warning.
- **Screenshots are off by default** (`ALLOW_SCREENSHOTS=false`) to keep rendered page images on-device.

---

## Release Validation

See [`docs/RELEASE_REPORT.md`](docs/RELEASE_REPORT.md) for clean-install results, measured performance, the verified/partial/blocked/not-tested breakdown, and the release verdict.

---

## Repository Structure

```
sih-26171-landing/
├── index.html                       # Landing page + live task console
├── agent.html                       # Agent workspace (timeline, inspector, gates)
├── src/
│   ├── main.ts                      # Navigation, scroll reveals, starfield
│   ├── taskconsole.ts               # Task console: streaming + confirm modal
│   ├── agentApp.ts                  # Workspace: streaming, inspector, cancel
│   ├── sanitize.ts                  # Escape + validated link/image helpers
│   └── styles/                      # Design system and page styles
├── backend/
│   ├── app/
│   │   ├── main.py                  # FastAPI routes, origin guards, cancel/confirm
│   │   ├── agentLoop.py             # Bounded loop (observe→plan→act→verify)
│   │   ├── planner.py               # Prompt framing + strict plan validation
│   │   ├── privacy.py               # Fail-closed redaction + URL sanitizing
│   │   ├── hub.py                   # Task registry and socket routing
│   │   ├── contracts.py             # Mirrored Pydantic schemas
│   │   └── providers/               # Gemini, Groq, and mock planners
│   ├── tests/                       # 151 tests (contracts, privacy, loop, A–H, security, latency)
│   └── scripts/
│       ├── check_gemini.py          # Live provider smoke test
│       └── benchmark_eval.py        # Benchmark metrics runner
├── extension/
│   ├── manifest.json                # Chrome MV3 manifest
│   ├── src/
│   │   ├── contracts.ts             # Canonical TS action contracts + validation
│   │   ├── actionExecutor.ts        # Action execution and URL guards
│   │   ├── observer.ts              # DOM element and bounding-box collector
│   │   ├── targeting.ts             # Semantic and spatial target resolution
│   │   ├── background.ts            # Serialized action queue + WS relay
│   │   └── relay.ts                 # Pure WebSocket framing
│   ├── lib/content.ts               # Programmatically injected in-page agent
│   └── tests/                       # 83 Vitest unit tests (7 files)
├── .claude/
│   ├── agents/                      # 8 specialist agents
│   └── skills/                      # 9 project skills
└── docs/
    └── ARCHITECTURE.md              # Technical architecture reference
```
