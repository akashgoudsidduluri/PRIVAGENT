# NAVIGATOR — SIH 26171
## On-device Visual Perception for Lightweight Browser Agents

NAVIGATOR is a privacy-first AI browser agent developed for **Smart India Hackathon (SIH 26171)**. It combines a zero-runtime-dependency landing page and agent workspace, a Chrome Manifest V3 extension, a local FastAPI orchestration backend with pluggable Gemini/Groq planners (plus an offline mock), local fail-closed privacy redaction, spatial bounding-box perception, bounded recovery, and a human confirmation gate.

---

## What Actually Works (Verified Capabilities)

| Subsystem | Implementation | Status & Evidence |
|-----------|----------------|-------------------|
| **Landing Page & Agent Workspace** | Vite + vanilla TypeScript + hand CSS | ✓ `npm test` — 25/25 checks pass (10 structure, 8 console, 7 sanitization) |
| **Chrome MV3 Extension** | Relay worker + in-page script + action executor | ✓ Vitest 35/35, `tsc --noEmit` clean, production build |
| **Backend Orchestration** | FastAPI hub + bounded agent loop | ✓ `pytest` 81/81, including 29 security-hardening tests |
| **Shared Contracts** | TypeScript source mirrored as Pydantic models | ✓ Contract parity test (`test_contracts.py`) |
| **Browser Action Control** | Navigate, click, type, press, scroll, zoom, wait, tabs | ✓ All 11 action types (`ACTION_TYPES`) |
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

---

## Quickstart & Run Instructions

### 1. Start the Frontend
```bash
npm install
npm run dev
# Running on http://localhost:5173
```

### 2. Start the Backend Hub
```bash
cd backend
# Activate the Python 3.12+ venv
venv\Scripts\activate
uvicorn app.main:app --port 8000 --reload --reload-dir app
# Running on http://localhost:8000
```

Configure providers in `backend/.env` (only one key is required; with no key the deterministic `mock` provider is used):

```env
# Choose groq | gemini | mock (otherwise auto-detected from the keys)
NAVIGATOR_PROVIDER=gemini
GEMINI_API_KEY=your_gemini_api_key_here
# GROQ_API_KEY=your_groq_api_key_here
# Optional primary-model overrides
# GEMINI_MODEL=gemini-3.8-flash
# GROQ_MODEL=openai/gpt-oss-120b

MAX_STEPS=25
ALLOW_SCREENSHOTS=false
```

### 3. Build & Install the Chrome Extension
```bash
cd extension
npm install
npm run build
powershell -Command "Copy-Item manifest.json dist/manifest.json -Force"
```

1. Open Chrome and navigate to `chrome://extensions`.
2. Enable **Developer mode**.
3. Click **Load unpacked** and select the `extension/dist` folder.
4. The service worker connects automatically to `ws://localhost:8000/ws/extension`.

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

The 12-category live runner is at `backend/scripts/realworld_benchmark.py`. It needs an active local backend, connected Chrome extension, and configured test sites; it reports blocked scenarios rather than treating mock tests as live completions. See `docs/PHASE8_STATUS.md` for the command and current gate status.

```bash
# 1. Frontend structure, console, and sanitization tests (25 checks)
npm test

# 2. Extension Vitest suite (35 tests) + typecheck + build
cd extension
npm test
npx tsc --noEmit
npm run build
cd ..

# 3. Backend Pytest suite (81 tests)
cd backend
venv\Scripts\python.exe -m pytest tests -q
cd ..

# 4. Benchmark evaluation suite (Tasks A through H, mock harness)
backend\venv\Scripts\python.exe backend/scripts/benchmark_eval.py

# 5. Live provider smoke test (requires GEMINI_API_KEY)
backend\venv\Scripts\python.exe backend/scripts/check_gemini.py
```

### Benchmark Metrics

`benchmark_eval.py` runs eight deterministic fixture smoke tests and reports only test passes and fixture duration. It does not establish live autonomy accuracy or zero privacy leakage. The 12-category runner in `backend/scripts/realworld_benchmark.py` computes evidence-backed task and action metrics from a connected runtime; its current live readiness status is documented in `docs/PHASE8_STATUS.md`.

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
│   ├── tests/                       # 81 tests (contracts, privacy, loop, A–H, security, latency)
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
│   └── tests/                       # 35 Vitest unit tests
├── .claude/
│   ├── agents/                      # 8 specialist agents
│   └── skills/                      # 9 project skills
└── docs/
    └── ARCHITECTURE.md              # Technical architecture reference
```

---

## Autonomous Research & Document Generation Engine (Phase 4)

PrivAgent features an end-to-end, privacy-hardened autonomous research and document synthesis engine capable of investigating complex subjects across credible web sources, harvesting multi-perspective evidence, validating data, and rendering publication-ready deliverables across multiple formats.

### 1. Adaptive Research & General-Purpose Document Planning
- **Topic-Agnostic Document Architecture**: The renderer does not rely on domain-specific hardcoded templates. Research on quantum computing, operating systems, solar economics, or consumer hardware uniformly generates structured outlines with executive summaries, technical findings, algorithmic/mathematical proofs, comparative tables, and verified references.
- **Adaptive Document Sizing**: Intelligently scales document length, section depth, and visual placement based on user constraints (e.g. strict five-page executive briefs vs. dense multi-page technical dossiers).
- **Mathematical & Algorithmic Formulations**: Synthesizes and formats LaTeX expressions and renders them into vector/raster images using Matplotlib/ReportLab.

### 2. Secure Image Pipeline & Attribution
- **Authentic Image Extraction**: High-substance imagery is parsed directly from researched websites, filtering out tracking pixels, 1x1 spacer GIFs, navigation icons, and marketing banners.
- **Multi-Layer SSRF & Exploit Defense**:
  - Image URLs are validated via `is_safe_public_url` against loopback, private RFC-1918 subnets, and cloud metadata endpoints.
  - Redirect chains are bounded and re-checked at every hop.
  - Payloads are screened against magic bytes (PNG, JPEG, WebP, GIF) and inspected with Pillow to eliminate decompression bomb vectors.
- **Automated Caching & Embedding**: Remote visuals are safely downloaded, cached in local session storage, and embedded near their relevant textual subsections in both PDF (`ReportLab`) and DOCX (`python-docx`) deliverables.
- **Honest Provenance & Attribution**: Images maintain strict provenance (`is_generated=False` for scraped assets vs. `is_generated=True` for system-generated charts/diagrams), displaying photographer/publisher credit and source domain attribution directly below each figure.

### 3. Category-Independent Product Research
- **Universal Product Model**: Extracts technical specifications across diverse product categories, including smartphones (e.g. Samsung Galaxy S25 5G, Apple iPhone, Google Pixel), laptops, and general consumer electronics.
- **Multi-Retailer Comparison**: Aggregates pricing, retailer links, availability, and hardware specifications (SoC, camera array, RAM, storage, battery) across competing e-commerce platforms.
- **EMI & Financing Intelligence**: Automatically extracts and compares No Cost EMI and Standard EMI terms, monthly instalment amounts, and tenures across retailers.
- **Verified Comparison Claims**: Generates structured `DataTable` matrices highlighting the verified best price (`⭐ Best Price`) along with corroborated price and availability claims.

### 4. Deliverable-Only Export & Continuation Workflow
PrivAgent separates intent handling into distinct operational paths to eliminate unnecessary browsing:
1. `NEW_RESEARCH`: Isolated research missions exploring fresh topics with full browser navigation.
2. `EXPORT_EXISTING_PACKAGE`: Zero-browser deliverable compilation (`"export as PDF"`, `"Make the previous report five pages and add more images"`, `"convert to DOCX"`). Immediately reuses persisted knowledge packages, caches visuals, applies page constraints, and renders new deliverables without opening a browser.
3. `REFINE_EXISTING_RESEARCH`: Extends active research with new conceptual subtopics while preserving all prior evidence, sources, and claims.
4. `RESUME_EXISTING_MISSION`: Seamlessly picks up interrupted or stalled research missions.
5. `NORMAL_CONVERSATION`: Answers conversational queries locally without browser overhead.

