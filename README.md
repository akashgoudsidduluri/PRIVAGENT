# NAVIGATOR — SIH 26171
## On-device Visual Perception for Lightweight Browser Agents

[![CI](https://github.com/Prateek2007-cmd/PRIVAGENT/actions/workflows/ci.yml/badge.svg)](https://github.com/Prateek2007-cmd/PRIVAGENT/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

NAVIGATOR is a privacy-first AI browser agent developed for **Smart India Hackathon (SIH 26171)**. It combines a zero-runtime-dependency landing page and agent workspace, a Chrome Manifest V3 extension, a local FastAPI orchestration backend with pluggable planners (Gemini, Groq, DeepAI Gateway, OpenAI-compatible, local Ollama, plus an offline mock), local fail-closed privacy redaction, spatial bounding-box perception, bounded recovery, and a human confirmation gate.

---

## What Actually Works (Verified Capabilities)

| Subsystem | Implementation | Status & Evidence |
|-----------|----------------|-------------------|
| **Landing Page & Agent Workspace** | Vite + vanilla TypeScript + hand CSS | ✓ `npm test` — 73 checks pass, 1 skipped (Task 15 breakdown: 10 structure, 8 console, 11 sanitization/security, 10 Task 12 UI, 4 Task 14 UI, 3 Task 15 UI, 18 recovery; UI first-paint fix added checks after Task 15) |
| **Chrome MV3 Extension** | Relay worker + in-page script + action executor | ✓ Vitest 180/180 (20 files), `tsc --noEmit` clean, production build |
| **Backend Orchestration** | FastAPI hub + bounded agent loop | ✓ `pytest` — 1073 tests collected; latest full run 1046 passed, 9 skipped, 18 failed (pre-existing failures in research/monitoring suites, unchanged since Task 1 — see `CHANGELOG.md`) |
| **Shared Contracts** | TypeScript source mirrored as Pydantic models | ✓ Contract parity test (`test_contracts.py`) |
| **Browser Action Control** | Navigate, click, type, press, scroll, zoom, wait, observe, tabs, select_option, set_date, upload_file, webmcp_tool, ucp_checkout | ✓ All 17 action types (`ACTION_TYPES`) |
| **Spatial Perception** | Bounding boxes (`bounds: {x,y,w,h}`), DPR grounding | ✓ `test_coordinates.py` and Test C |
| **AX-Tree Grounding (Task 7)** | DOM-derived accessibility tree; planner targets elements by stable ref (`{"elementRef": "e12"}`); unknown refs / raw CSS selectors rejected; stale refs re-observe once then fail honestly | ✓ `test_grounding.py` (28 tests); see `docs/PERCEPTION.md` |
| **Perception Limits (Task 7)** | `PRIVAGENT_MAX_ELEMENTS` 200 (was 50), `PRIVAGENT_MAX_PAGE_TEXT_CHARS` 8000 (was 2500), `PRIVAGENT_MAX_INSTRUCTION_CHARS` 8000 (was 4000), `PRIVAGENT_MAX_AX_NODES` 300; truncation logged | ✓ `test_grounding.py`; cost/latency trade-off in `docs/PERCEPTION.md` |
| **OCR, opt-in (Task 7)** | Local tesseract adapter, disabled by default, gated on `ALLOW_SCREENSHOTS`; OCR text labelled untrusted in the prompt; degrades gracefully when tesseract is absent | ✓ `test_grounding.py`; tesseract not installed in this VM |
| **Latency (Task 11)** | Per-step profiling (`LoopTimings`, readable via `GET /api/task/{id}` — durations + action types only, never names/values/page text); per-task in-memory observation cache (tab + URL + DOM fingerprint, invalidated on mutation/navigation/takeover/pause); batch execution of the valid plan prefix (ref-grounded non-submitting types + one terminator) — confirmation/takeover/allow-list/checker still run per step; `doType` reuses its candidate list for the submit lookup | ✓ `test_latency.py` + `test_latency_batch.py` (29 tests); measured on the scripted login+search benchmark: planner calls 7 → 4, observation calls 2 → 1, wall 1417.59 → 823.68 ms median (mock provider, 200 ms simulated planner delay); see `docs/LATENCY.md` |
| **Plan preview (Task 12)** | Confirmation banner shows the planned steps in plain language (type + target name/role + risk) from structured fields only — host-only navigation, masked secret-named targets, vault placeholders, no planner free text; Approve / Approve-this-step-only / Reject reuse the existing confirmation gate (approval is a gate, not a new override; approve-step re-observes and re-plans) | ✓ `test_plan_preview.py` (24 tests); see `docs/UI.md` |
| **Privacy Center (Task 12)** | One panel: planner status (local vs cloud, LOCAL_ONLY / zero-retention flags, read-only), audit log viewer (`GET /api/audit`, event filter + limit + chain status), 24 tracker hosts by category, DPIA summary + link | ✓ `test_privacy_status.py`; see `docs/UI.md` |
| **Task timeline (Task 12)** | Task 11 timings joined to history: step, action, status, duration, batched / cache-hit / checker-skipped flags; textContent only | ✓ `tests/task12_ui.test.mjs`; see `docs/UI.md` |
| **Responsive layout (Task 12)** | `@media (max-width: 480px)` layer: header, task list, plan preview, Privacy Center, timeline reflow; tap targets ≥ 44px | ✓ `tests/task12_ui.test.mjs`; layout NOT verified in a real browser — the VM's managed Chromium blocks all non-public navigation (see `docs/UI.md`); DOM smoke test `~/workspace/privagent-analysis/task12-verify-dom.mjs` (25 checks) |
| **XSS audit (Task 12)** | All 66 `innerHTML` sites classified: 3 vulnerabilities fixed (drawer timeline, unescaped sessionId, raw format/artifact id), 9 empty clears, 57 proven safe by escaping/constants; 0 `outerHTML` / `document.write` / `insertAdjacentHTML` | ✓ Repo guard `test_no_innerhtml.py` (negative-tested); see `docs/UI.md` |
| **Architecture (Task 13)** | God files split (`agentLoop.py` 3,883 → 2,876 lines, `planner.py` 2,044 → 873; new `loop_state` / `loop_gates` / `loop_checkpoints` / `loop_helpers` / `loop_research` / `planner_validation` modules, re-exported); `check_freshness` made real (fresh-tree comparison in batch + post-wait re-verification); per-entry vite builds (no shared chunks) with the glyph-strip dedup; undecryptable memory rows skipped + counted; structured JSON step logs (`navigator.observe`); dead code removed (see `docs/ARCHITECTURE.md`) | ✓ `test_task13_freshness.py` (13), `test_task13_encrypted_rows.py` (11), `test_task13_observability.py` (4); extension `tests/viteSelfContainedEntries.test.ts` (build proof) |
| **Connectors & standards (Task 14)** | Gmail / Calendar / Drive auto-routing: a data-driven router detects connector intents ("check my inbox", "what's on my calendar tomorrow", "find the Q3 deck in Drive") and answers via the Google API connectors instead of driving the web UI; the routing decision is recorded on the task record and shown in the workspace ("via Gmail connector"); an unconfigured connector fails honestly with a named remedy and never silently falls back to the browser; writes (draft/trash/calendar-create) need confirmation first — calendar creates are a behaviour change, they used to execute immediately; connector results never reach the planner (answered directly, still scrubbed), egress is pinned to Google API hosts only, one metadata-only `connector_call` audit event per call, zero retention honoured. WebMCP: the extension collects page-declared tools (`document.modelContext`, capped/validated, provenance-tagged untrusted); the planner may call one through the gated pipeline (`webmcp_tool`, risk floor `write`); the checker scans descriptions + args, and credential/payment-asking tools force takeover. UCP: merchant profiles detected from `<link rel="ucp">` + `/.well-known/ucp` (same-origin https, supported versions only); checkout prefers the standard path but goes through the Task 4 payment path unchanged (cap, approval + takeover always, mock provider never charges, single attempt, never retried); wire shape unvalidated against a real merchant | ✓ `test_connector_router.py` (44), `test_connector_runner.py` (15), `test_webmcp.py` (16), `test_ucp.py` (18), `test_task14_loop.py` (17), `test_plan_preview.py` (+3), extension `tests/webmcpUcp.test.ts` (8); see `docs/CONNECTORS.md`, `docs/WEBMCP.md`, `docs/UCP.md` |
| **Privacy Redaction** | Strips credentials, cards, SSNs, tokens, URL secrets locally | ✓ Fail-closed tests (`test_privacy_failclosed.py`, `test_security_hardening.py`) |
| **Prompt Injection Defense** | Page text isolated as untrusted data; schema-validated plans | ✓ `test_planner_injection.py` |
| **Human-in-the-Loop Gate** | Sensitive/auth/payment actions halt for approval | ✓ `test_confirmation.py` and Test H |
| **Takeover Mode (Task 4)** | Login / payment / OTP / CAPTCHA / 2FA pauses the agent, hands the browser to you, observes nothing | ✓ `test_credentials_payments.py` (20 tests); see `docs/CREDENTIALS_PAYMENTS.md` |
| **Credential Vault (Task 4)** | Encrypted per-domain credentials; planner sees only `{{CREDENTIAL:…}}` placeholders, substituted at execution | ✓ `test_credentials_payments.py`; vault API `POST/GET/DELETE /api/vault/credentials` |
| **Payments, mock-first (Task 4)** | Every payment needs approval + takeover; blocked above `PRIVAGENT_MAX_PAYMENT_AMOUNT` (default 5000 INR); mock virtual card, last-4 only, no live Stripe | ✓ `test_credentials_payments.py`; `GET /api/payments/config`, `POST /api/payments/validate`, `GET /api/payments/audit` |
| **Cancellation** | REST cancel propagates into the running loop and pending gate | ✓ `test_security_hardening.py` |
| **Long-term Memory (Task 9)** | Facts, skills, and reference files in SQLite (encrypted at rest); deterministic keyword recall — top 5 facts / 800 chars, top 2 skills — into the planner prompt; files surface as a summary only, never content | ✓ `test_long_memory.py` (26 tests); Memory dialog in the workspace header; see `docs/MEMORY_LONG_HORIZON.md` |
| **Memory provenance (Task 9)** | Only the operator (`source="user"`) or a confirmed task outcome (`source="task:<id>:confirmed"`) can write memory; page content can never create a fact/skill; write-back proposes candidates and stores them only after operator confirmation; credentials/PII rejected at write time; `DELETE /api/memory` forgets everything | ✓ `test_long_memory.py` (provenance + credential-guard tests) |
| **Checkpoints & resume (Task 9)** | Checkpoint persisted after every step; `GET /api/task/{id}` exposes it; `POST /api/task/{id}/resume` continues from the checkpoint (history preserved, survives backend restart via SQLite restore); `POST /api/task/{id}/cancel` checkpoints first | ✓ `test_long_memory.py` (checkpoint + resume tests) |
| **Step caps & compaction (Task 9)** | `PRIVAGENT_MAX_STEPS` default 50 (was 25), `PRIVAGENT_MAX_LONG_STEPS` default 100 for `long_horizon=true`; long tasks get a compact summary every 25 steps and the planner then sees summary + last 2 entries, not the full history | ✓ `test_long_memory.py` (cap + compaction tests) |
| **Multi-tab (Task 9)** | Task-owned tab registry (`tab_id` / URL / title / active) in the checkpoint; new `switch_tab` action (`select_tab` kept for back-compat); planner lists the task's tabs and may only switch to a listed one; unknown tabs rejected before the extension is contacted; tabs are handed back, not closed, at task end | ✓ `test_long_memory.py` + `extension/tests/actionExecutor.test.ts` (`switch_tab` suite) |
| **Background & scheduled tasks (Task 9)** | `background=true` tasks run without a live WebSocket and are pollable via `GET /api/task/{id}`; in-process SQLite-backed scheduler (`POST/GET/DELETE /api/scheduled`, `POST /api/scheduled/{id}/run-now`) with `"every 6h"` / `"daily 09:00"` / cron schedules; scheduled runs go through the same allow-list, confirmation, takeover, and payment gates — they halt for the operator, never self-approve | ✓ `test_scheduler.py` (10 tests); see `docs/MEMORY_LONG_HORIZON.md` |
| **Bounded Agent Loop** | 25-step task budget, 3 retries, max 3 steps per plan | ✓ `test_agentLoop.py` and Test F |
| **Benchmark Scenarios A–H** | All 8 directive tasks in the mock harness | ✓ 8/8 mock fixtures via `benchmark_eval.py` — fixture-only, explicitly **not** live metrics. Measured evals (Task 6, `docs/EVALS.md`): checker ASR 0.50 overall, privacy leak rate 0.2609. The 12-category live runner stands at 12 blocked, 0 attempted (see `docs/PHASE8_STATUS.md`) |
| **Planner Providers** | Gemini (`gemini-3.8-flash`), Groq (`openai/gpt-oss-120b`), DeepAI Gateway (`deepseek-v4.1-flash`), OpenAI-compatible (Antseed), **Ollama (Task 10, local — `qwen2.5:7b` default, no data leaves the machine)**, or `mock` | ✓ Provider tests; live key required for remote calls. Default priority (no `NAVIGATOR_PROVIDER` set): `deepai` → `groq` → `gemini` → `mock` (see `backend/app/config.py`). Ollama joins only when explicitly selected (`PRIVAGENT_PLANNER=ollama` / local-first / local-only); see `docs/LOCAL_PLANNER.md` |
| **Zero retention (Task 10)** | `PRIVAGENT_ZERO_RETENTION=true`: no checkpoints (tasks cannot resume after restart), no screenshots to the planner, in-memory history pruned to the last 3 steps, provider telemetry silenced, trajectory records scrubbed | ✓ `test_zero_retention.py`; see `docs/PRIVACY.md` |
| **Audit log (Task 10)** | Append-only, hash-chained `backend/data/audit.log` (0600) for auth failures, allow-list denials, confirmation decisions, takeover, payments, memory writes/deletes, scheduler claims; `GET /api/audit` with `?event=` / `?limit=`; `verify_chain()` detects tampering | ✓ `test_audit_log.py`; see `docs/PRIVACY.md` |
| **DPIA (Task 10)** | Data protection impact assessment: data inventory, per-provider data flows, measured risks (checker ASR 0.50, leak rate 0.2609), mitigations, residual risks stated honestly | See `docs/DPIA.md` |
| **Automation (review-prep)** | Outcome verification after every step + stale-ref recovery: the loop re-grounds a stale element instead of failing, and stops a repeated failure instead of looping it (see "Loop fixes" below). Scripted demo: a 3-step research task across 2 sites, 1 recovery | ✓ `backend/scripts/demo_automation.py`; suite 1033/9/18 at the time |
| **Live monitoring (review-prep)** | HTTP check path for watched pages (no Chrome needed), volatile-noise filtering (timestamps, counters, "x minutes ago" never alert), interval + cron missions, price-drop conditions, and a **daily price digest**: a cron mission reports the current price every morning (e.g. 9:00 AM IST) even when the price has not changed | ✓ `backend/scripts/demo_monitoring.py`; `test_live_monitoring.py`, `test_continuous_monitoring.py` |
| **Loop fixes (review-prep)** | Repeated-failure loop detection: the same action failing with the same *normalised* error 3× in a row stops the task with "Loop detected" (raw error strings embed a changing target id, so they never match — the first fix compared raw strings and the live Flipkart popup loop ran 32× through it). Success-path detection was already sound | ✓ `test_agentLoop.py` regression test with 60 distinct production-shaped failures |
| **Tab registry (review-prep)** | Tabs opened by a click (or a PDF in a new tab) now register from the *observed* tab, so the next `switch_tab` works; the gate still rejects tabs the task has never observed, so the operator's personal tabs stay out of reach | ✓ `test_tab_opened_by_click_registers_and_can_be_selected` |
| **Document export (review-prep)** | A multi-site comparison task now produces a real document: "give the comparison in a xcel" (also docx/pdf/pptx) renders a Store/Price/Variant/Link table from the verified per-site prices, downloadable via `GET /api/tasks/{id}/artifacts` | ✓ `test_comparison_package_renders_xlsx_from_tracker` (opens the .xlsx, checks headers/rows) |
| **Email sweep (review-prep)** | "Scan email every 60 minutes; delete unnecessary promotion emails; notify me of urgent mail" becomes one mission: the instruction's frequency is honoured (60 min, not the old hardcoded 300 s), an explicit "delete" authorises autonomous cleanup, and "urgent" becomes a notification keyword so urgent mail alerts immediately instead of waiting for a neglect threshold | ✓ `test_sixty_minute_sweep_deletes_promos_notifies_urgent` (demo sandbox mailbox) |

---

## What Changed in Tasks 1–15 (and the SIH review-prep work after Task 15)

Full history in [`CHANGELOG.md`](CHANGELOG.md); branch `muse/task1-security-auth`.

- **Task 1 — Local operator auth.** Every `/api/*` route except `/api/health` requires a bearer token; the unauthenticated `POST /api/test/execute_action` backdoor was removed; the Origin guard no longer lets a missing `Origin` bypass it.
- **Task 2 — Encrypted secrets at rest.** OAuth tokens, client secrets, LLM keys and the local token live in a Fernet store (`backend/data/secrets.enc.json`), never plaintext in `.env`. SQLite files are permission-hardened and sensitive columns encrypted at the accessor layer — full SQLCipher encryption is **not** achieved yet (see "SQLite at rest" below).
- **Task 3 — Isolation & injection defence.** Isolated agent Chrome profile, domain + egress allow-lists enforced in code, a web-blind injection checker with provenance tags, tracker blocking + wipe-on-start for the agent profile.
- **Task 4 — Safe credential / payment path.** Takeover mode (agent stops observing and hands you the browser), an encrypted credential vault (planner sees placeholders only), and mock-first payments (approval + takeover, blocked above 5000 INR, no live Stripe). Account creation / permanent deletion are blocked outright.
- **Task 5 — Foundations.** CI (`.github/workflows/ci.yml`), Docker + docker-compose, MIT LICENSE, pinned dependencies, CHANGELOG.md. First CI run happens on push/PR (Task 15); Docker images were not built in this VM.
- **Task 6 — Proof (measured evals).** Eval harness + `docs/EVALS.md`: checker ASR **0.50** overall (canonical 0.25, evasions 1.0), privacy leak rate **0.2609**, mock benchmark 8/8 (fixture-only). WebArena / OSWorld / PrivacyLens / AgentDojo are harness-ready but not run (no LLM key, no Chrome/VM, adapter missing).
- **Task 7 — Perception (AX-tree grounding).** DOM-derived accessibility tree (CDP `getFullAXTree` is not viable from a MV3 content script — see `docs/PERCEPTION.md`); the planner targets elements by stable ref (`{"elementRef": "e12"}`), unknown refs and raw CSS selectors are rejected, stale refs re-observe once then fail honestly. Limits raised: 50 → 200 elements, 2,500 → 8,000 page-text chars, 4,000 → 8,000 instruction chars (all configurable, with a documented token-cost trade-off). OCR is a local tesseract adapter, disabled by default and gated on `ALLOW_SCREENSHOTS`; tesseract is not installed in this VM, so OCR is unavailable here. Cross-origin iframes stay opaque.
- **Task 8 — Generalisation (works on any site).** The per-site playbooks are gone: the planner now works from one general "HOW TO ACT ON ANY SITE" section (read the AX tree, find the element by role + accessible name, act by ref, verify from the new observation) and the extension collects elements by role/name/text only — no site classes, data-testids, or input ids (`._2KpZ6l`, `.ytp-*`, `twotabsearchtextbox` all removed). The genuinely useful site knowledge survives as an optional, data-driven hints layer (`backend/app/site_hints.json`, prose only — the loader rejects anything selector-like; disable with `PRIVAGENT_SITE_HINTS=false` for pure-general mode). The site registry is now data (`backend/app/known_domains.json`), the resolver builds no per-retailer search URLs (shopping/travel go through generic web search), intent routing is domain-agnostic, and comparison-site resolution works for sites the registry never heard of. What stays site-specific on purpose: the lookalike brand list (phishing defence), identity-provider hosts, and the mail connector route (Task 14). See `docs/GENERALISATION.md`. No live Chrome run on real sites yet — generalisation is unit-tested on synthetic pages only.
- **Task 9 — Memory & long-horizon.** Durable long-term memory (facts/skills/files, encrypted at rest, provenance-enforced: only the operator or a confirmed task outcome can write), checkpoints + resume, step caps 50/100, compaction every 25 steps, multi-tab (`switch_tab`), background tasks and an in-process scheduler whose runs go through the same gates and never self-approve. See `docs/MEMORY_LONG_HORIZON.md`.
- **Task 10 — Privacy beyond redaction.** Local Ollama planner (BYOM, loopback-only, `PRIVAGENT_PLANNER_LOCAL_ONLY=true` fails honestly instead of falling back to cloud); zero-retention mode (`PRIVAGENT_ZERO_RETENTION=true`: no checkpoints, no screenshots to the planner, pruned history, silenced telemetry); append-only hash-chained audit log (`backend/data/audit.log`, `GET /api/audit`); DPIA (`docs/DPIA.md`); tracker blocklist extended 12 → 24 hosts plus agent-profile launch flags. See `docs/LOCAL_PLANNER.md`, `docs/PRIVACY.md`, `docs/DPIA.md`.
- **Task 11 — Latency.** Per-step loop profiling (`LoopTimings` via `GET /api/task/{id}`), per-task in-memory observation cache (DOM fingerprint), batch execution of the valid plan prefix. Measured on the scripted login+search benchmark (mock provider): planner calls 7 → 4, observation calls 2 → 1, wall 1417.59 → 823.68 ms median. See `docs/LATENCY.md`.
- **Task 12 — UI enhancement.** Plan preview in the confirmation banner (structured fields only; Approve / Approve-this-step-only / Reject reuse the existing gate); Privacy Center (planner status, audit viewer, 24 tracker hosts, DPIA); task timeline from Task 11 timings; responsive layout at 480px; XSS audit of all 66 `innerHTML` sites (3 vulnerabilities fixed, repo guard test added). Layout was NOT verified in a real browser — the VM's managed Chromium blocks all non-public navigation; see `docs/UI.md` for the honest verification record.
- **Task 13 — Architecture.** God files split (`agentLoop.py` 3,883 → 2,876 lines, `planner.py` 2,044 → 873; new `loop_state` / `loop_gates` / `loop_checkpoints` / `loop_helpers` / `loop_research` / `planner_validation` modules, re-exported); `check_freshness` made real; per-entry vite builds; undecryptable memory rows skipped + counted; structured JSON step logs; dead code removed. See `docs/ARCHITECTURE.md`.
- **Task 14 — Connectors & standards.** Gmail / Calendar / Drive auto-routing (a data-driven router detects connector intents and answers via the Google API connectors instead of driving the web UI; unconfigured connectors fail honestly, zero network calls); WebMCP tools (capped 50, validated, gated); UCP checkout via the Task 4 payment path (cap, single attempt, mock provider fails honestly). Calendar creates require confirmation. See `docs/CONNECTORS.md`, `docs/WEBMCP.md`, `docs/UCP.md`.
- **Task 15 — Final verification.** Full gates re-run: backend 991/9/18 (same pre-existing 18), extension 180/180, frontend 64 checks; bench planner 4, obs 1, wall median ~825 ms; evals as Task 6. Live smoke: health ok, unauthenticated `/api/tasks` → 401, Gmail fails honestly, shopping fails `EXTENSION_DISCONNECTED`. Known finding: `backend/data/privagent.db` ships as a 5-byte `dummy` file — delete it or set `PRIVAGENT_DB_PATH` or the server cannot boot against it.

### After Task 15 — SIH review prep (live-tested in the operator's own Chrome, 2026-11-17/18)

- **Automation + live monitoring demos.** Scripted `demo_automation.py` (outcome verification + stale-ref recovery) and `demo_monitoring.py` (HTTP check path, noise filtering, price-drop alert with before/after). Both are fixture/scripted runs, not live Chrome — no live external-site timings are claimed.
- **UI first-paint fix.** The workspace used to flash the old UI before the new one; it now mounts from an inert template so the first paint is the control center.
- **Latency.** STALE short-circuit + warm-up: step-1 total 435 → 219 ms, redact 60 → 0 ms, checkpoint 179 → 23 ms; wall median ~828 ms unchanged, dominated by the simulated planner delay.
- **Loop fixes (two rounds).** First fix: loop detection only ran when the last step *succeeded* — a repeatedly *failing* action never triggered it, so the Flipkart popup loop ran 32×. Second fix: error strings embed a changing target id, so raw-string comparison never matched either; the check now compares *normalised* errors (JSON blobs, refs, numbers stripped). Live-confirmed: the same price-hunt task went from a 44-step loop to 7 steps, no loop.
- **Tab-registry fix.** Tabs opened by a click never registered, so the next `switch_tab` was rejected ("unknown tab … not in this task's tab registry"). The observed tab now registers (membership only; the active marker stays owned by the post-action sync).
- **Document export.** Comparison tasks now produce a real Excel/Word/PDF/PowerPoint deliverable from the verified per-site prices ("xcel" is recognised as xlsx).
- **Email sweep.** A 60-minute Gmail mission deletes promotion mail autonomously and notifies on urgent mail (see the table above).
- **Daily 9 AM price digest.** "Every day at 9am, notify me the prices" creates a cron mission (`03:30 UTC` = 9:00 AM IST) with a `daily_price` condition that reports the current price every morning, even unchanged; the product is read from the suggestion history, never a hardcoded fallback.
- **Own Antseed gateway.** The operator's own always-on gateway (`akash-antseed.duckdns.org`, Azure B1ms VM, India South Central) is a supported planner via `PRIVAGENT_PLANNER=antseed` + `ANTSEED_BASE_URL` + `ANTSEED_MODEL`; the gateway host must be on `PRIVAGENT_ALLOWED_DOMAINS` or the Task 3 egress gate rejects the planner call before any request is sent.

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
                      │  - Planner (Gemini / Groq /    │
                      │    DeepAI / OpenAI-compat /    │
                      │    mock)                       │
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

- **Local operator auth.** Every `/api/*` route except `/api/health` requires `Authorization: Bearer <token>`. The token comes from `PRIVAGENT_LOCAL_TOKEN` in `backend/.env`, or — if unset — is generated once on first startup (32 random bytes) and stored **encrypted** in the secret store (`backend/app/secrets.py`, `backend/data/secrets.enc.json`, mode `0600`); the old plaintext `backend/data/.local_token` file is migrated into the store and deleted on first use. The token value is never logged. A missing `Origin` header no longer bypasses anything: non-browser clients must present the token. Disallowed origins are rejected with 403 even when a token is present, and state-changing browser requests need an allow-listed Origin/Referer or a valid token. `GET` requests may alternatively carry `?token=` — only because plain `<a href>` artifact preview/download links cannot send headers. The OAuth callback (`/api/connectors/*/callback`) is exempt because it is a top-level browser redirect from the provider; its `state` parameter is validated server-side instead.
- **How the clients get the token.** Web UI: on the first 401 the workspace prompts once for the token and stores it in `localStorage` (`privagentLocalToken`); get it with `backend/venv/bin/python backend/scripts/print_local_token.py` (or your `PRIVAGENT_LOCAL_TOKEN` value) — treat the output like a password. Extension: the service worker reads the same token from `chrome.storage.local` (`privagentLocalToken`) and sends it as `?token=` on the backend WebSocket URLs; until a token is stored there it connects without one (backend logs a warning). Set it once from the extension service-worker console: `chrome.storage.local.set({ privagentLocalToken: "<token>" })`. A token-entry UI for the extension is follow-up work.
- **Encrypted secret store (Task 2).** OAuth access/refresh tokens, OAuth client secrets, LLM API keys (`GEMINI_API_KEY`, `GROQ_API_KEY`, `ANTSEED_API_KEY`, `DEEPAI_GATEWAY_KEY`), runtime keys set via `POST /api/llm/config`, and the local operator token are stored encrypted with Fernet (AES-128-CBC + HMAC-SHA256), the key derived via PBKDF2-HMAC-SHA256 (600,000 iterations) from a master key. Master-key priority: `PRIVAGENT_MASTER_KEY` in the environment (recommended — a Fernet key or passphrase), else the OS keychain via `keyring` if installed, else a generated key in `backend/data/.master.key` (mode `0600`). The generated-key path is **local-only protection, not hardware-backed security**: anyone who can read the whole `backend/data/` directory can decrypt the store. Set `PRIVAGENT_MASTER_KEY` (and back it up — losing it makes every stored secret unrecoverable). On first startup after the upgrade, any plaintext secrets still in `backend/.env` are encrypted into the store, verified by round-trip, and then blanked from `.env` (never deleted without being stored first); client IDs are not secrets and stay in `.env`. Never log secret values or keys.
- **SQLite at rest (Task 2).** Full-database encryption via SQLCipher is **not** achieved: `pysqlcipher3` cannot build in this environment (`sqlcipher/sqlite3.h` is missing — no `libsqlcipher-dev`), so the database file itself is still plaintext SQLite. What Task 2 does instead: the database directory is mode `0700` and the `.db`, `-wal` and `-shm` files are forced to mode `0600`, and the most sensitive columns are Fernet-encrypted at the accessor layer — message content/evidence, chat-memory values, semantic-fact values, task-checkpoint history/working-memory, research-package payloads, and email classification/notification content. Rows written before Task 2 (legacy plaintext) still read. Monitoring state is already scrubbed before storage, and table structure, IDs, timestamps, titles and URLs are **not** encrypted — a stolen `.db` file still reveals that a chat exists and its metadata. Follow-up: `apt install libsqlcipher-dev && pip install pysqlcipher3`, switch `db.get_connection()` to `pysqlcipher3.dbapi2`, run `PRAGMA key = '<master key>'` before any other statement, and migrate the existing DB once with `sqlcipher_export`.
- **Test endpoint removed.** The former unauthenticated `POST /api/test/execute_action` endpoint — which drove the extension directly, bypassing the planner, privacy redaction, and confirmation gate — has been removed from the production app. Do not re-add an unauthenticated action-execution endpoint.
- **Isolated agent profile (Task 3).** The agent must not run in your everyday Chrome profile, where it would inherit your cookies, saved logins, and history. Launch Chrome for agent work only with `--user-data-dir=<repo>/backend/data/agent-profile` (see `scripts/launch-agent-chrome.sh` / `scripts/launch-agent-chrome.bat`, or set `PRIVAGENT_AGENT_PROFILE_DIR`), then in the extension service-worker console run `chrome.storage.local.set({ privagentIsolatedProfile: true })`. Check `GET /api/isolation/status` — `profileVerified` is true only when the extension self-reports that flag. Honest limit: Chrome does not expose the profile directory to extensions, so the backend cannot *prove* isolation; it is operator-attested. This is a local isolated-profile mode, **not** a cloud VM sandbox — there is none.
- **Domain allow-list (Task 3).** Set `PRIVAGENT_ALLOWED_DOMAINS` (comma-separated, `*.example.com` wildcards supported) or pass `allowedDomains` per task on `POST /api/task`. It is enforced in code, not just in the prompt: the planner rejects disallowed navigations, the agent loop blocks them and halts for human confirmation (approving allows that one navigation as a logged override), and the extension refuses them as defence in depth. With no list configured, task creation logs a warning and navigation is unrestricted (back-compat).
- **Web-blind checker + provenance (Task 3).** `backend/app/checker.py` scans page text for instruction-like language ("ignore previous instructions", "send your password/OTP", "navigate to <attacker domain>", download-and-run lures) and checks the *plan* — actions, targets, domains, your directive — against those findings without ever seeing page prose as instructions. A flag halts the task for human confirmation (or fails it if no gate is available). Page content entering the planner is tagged with its source URL and capture time (`<page-context untrusted="true" source="…" captured="…">`). The checker is a deterministic heuristic, not a model: it catches loud injection shapes, not paraphrased or encoded ones, and no attack-success rate has been measured for it — do not claim one.
- **Tracker hygiene for the agent profile (Task 3, best-effort).** The extension ships a `declarativeNetRequest` ruleset (`extension/rules/trackers.json`) blocking 12 well-known tracker hosts, and wipes the agent profile's cookies/cache/storage on start when the isolated-profile flag is set. Third-party cookies are partitioned by Chrome's default storage partitioning. There is no fingerprint randomisation and no full tracker blocklist — the agent profile is hardened, not anonymous.
- **Egress allow-list (Task 3).** Backend outbound calls go only to loopback gateways, the pinned planner hosts (`api.groq.com`, `generativelanguage.googleapis.com`), or hosts on the domain allow-list. A remote OpenAI-compatible gateway that is not listed is rejected before the request is sent, so it never receives the API key.
- **Takeover mode (Task 4).** When the loop reaches a login / payment / OTP / CAPTCHA / 2FA surface it pauses the agent, sets the task state to `awaiting_takeover`, and hands the browser to you — and **stops observing**: no screenshots, no DOM snapshots, no page text to the planner while you have the browser. The workspace shows a Takeover banner with Resume / Cancel; Resume re-observes from scratch and its first post-takeover observation carries no screenshot. At most 2 takeovers per task (configurable via `PRIVAGENT_MAX_TAKEOVERS_PER_TASK`), 600 s timeout (`PRIVAGENT_TAKEOVER_TIMEOUT_S`), and a surface already taken over once is not taken over again. See `docs/CREDENTIALS_PAYMENTS.md`.
- **Credential vault (Task 4).** Saved logins live in the encrypted store (`backend/app/vault.py` on top of Task 2's `secrets.py`); the planner sees only a `{{CREDENTIAL:domain:kind:label}}` placeholder, and the real value is substituted in the loop just before the action is sent to the extension — never in prompts, logs, screenshots, or history. Cards store the last 4 digits only (a full card number is rejected, not truncated); OTP/TOTP kinds are rejected outright. The agent never asks for a password in chat, and you can use Chrome's built-in password manager / autofill instead. Manage credentials from the workspace header **Vault** button (`POST/GET/DELETE /api/vault/credentials`, metadata only).
- **Payments, mock-first (Task 4).** No real card processing and no live Stripe integration. Every payment requires explicit approval **and** takeover regardless of amount, and payments above `PRIVAGENT_MAX_PAYMENT_AMOUNT` (default 5000, `PRIVAGENT_PAYMENT_CURRENCY` default INR) are blocked outright — no currency conversion is performed. `backend/app/payments.py` defines the provider interface (Stripe Link-style one-time virtual card: `create_virtual_card`, `approve_virtual_card`, last-4 only) with a `MockPaymentProvider` by default whose `charge` always raises. Payment attempts are audited (amount, merchant, last-4, approval id — never card details) in `backend/data/payments_audit.jsonl`. See `docs/CREDENTIALS_PAYMENTS.md`.
- **Blocked outright (Task 4).** The agent never creates an account for you and never permanently deletes/closes one — those actions fail the task honestly, with no gate and no override.
- **Screenshots are local-only.** `ALLOW_SCREENSHOTS=false` by default. A rendered screenshot is sent to a remote model only when the operator opts in *and* no sensitive field (password, hidden, payment/credential markers) is present. Invalid, oversized (>4 MB), or non-raster images are dropped.
- **Fail-closed redaction.** If any redaction step throws, the payload is discarded with `PrivacyViolation`; a partial payload is never sent.
- **URL hygiene.** Userinfo (`user:pass@`), sensitive query/fragment values (`token`, `key`, `session`, `password`, …), and secret patterns are masked before planning or display. Navigation rejects non-`http(s)` schemes, embedded credentials, and CR/LF/backslash injection.
- **Plan validation.** Each plan is capped at 3 steps; `timeoutMs` must be 100–60,000; required parameters/targets are enforced per action; only whitelisted action types execute.
- **Single activation.** A click resolves to exactly one activation; a typed submit follows exactly one path (Send button → Enter → form submit), never all three. A partially delivered CDP click/keypress is treated as attempted so no duplicate fallback fires.
- **Cancellation is real.** `POST /api/task/{id}/cancel` marks the loop for stop, wakes a pending confirmation, aborts any in-flight extension action slot, and no further actions execute. A cancel racing the task-create request is replayed once the task id exists.
- **Retry safety.** Only idempotent actions (`observe`, `scroll`, `wait`, `zoom`, `select_tab`) are retried after a timeout; a click/type/press/navigate timeout is reported instead of risking a duplicate side effect.
- **No committed secrets.** API keys come from the environment or the encrypted secret store (see above) — never from a committed file. `backend/.env` is gitignored; keys placed there are migrated into the encrypted store on first startup and blanked from `.env`. `backend/data/` (store, master key, salt, SQLite DB) is gitignored.

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
# Create and activate the Python 3.12+ venv
python -m venv venv
source venv/bin/activate          # Linux / macOS
# venv\Scripts\activate           # Windows
uvicorn app.main:app --port 8000 --reload --reload-dir app
# Running on http://localhost:8000
```

### 2b. Or run with Docker (Task 5)
```bash
cp backend/.env.example backend/.env   # fill in provider keys first
docker compose up --build
# Frontend: http://localhost:5173   Backend: http://localhost:8000
```
The backend is published on the host loopback only (`127.0.0.1:8000`) — do not
expose it on your network. State (encrypted secret store, SQLite DB, master
key) lives in the `privagent-data` volume. The Chrome extension is not
containerised: load it unpacked from `extension/dist` as in step 3.
See `docker-compose.yml` and `backend/Dockerfile` for details.

Configure providers in `backend/.env` (only one key is required; with no key the deterministic `mock` provider is used):

```env
# Provider selection (otherwise auto-detected from the keys present).
# Default priority: deepai -> groq -> gemini -> mock (backend/app/config.py).
NAVIGATOR_PROVIDER=gemini
GEMINI_API_KEY=your_gemini_api_key_here
# GROQ_API_KEY=your_groq_api_key_here
# DEEPAI_GATEWAY_KEY=your_deepai_gateway_key_here
# ANTSEED_API_KEY=your_antseed_key_here
# Optional primary-model overrides
# GEMINI_MODEL=gemini-3.8-flash
# GROQ_MODEL=openai/gpt-oss-120b
# DEEPAI_GATEWAY_MODEL=deepseek-v4.1-flash

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

CI runs all of this on every push and pull request (`.github/workflows/ci.yml`:
backend pytest on Python 3.12, frontend tests + typecheck on Node 22, extension
Vitest + typecheck + build, and a gitleaks secret scan). Test counts below were
recounted from the code at this commit — the old README figures (25 / 35 / 81)
were stale since long before Tasks 1–4.

The 12-category live runner is at `backend/scripts/realworld_benchmark.py`. It needs an active local backend, connected Chrome extension, and configured test sites; it reports blocked scenarios rather than treating mock tests as live completions. See `docs/PHASE8_STATUS.md` for the command and current gate status.

```bash
# 1. Frontend structure, console, sanitization/security, and recovery tests (73 checks pass, 1 skipped)
npm test

# 2. Extension Vitest suite (180 tests, 20 files) + typecheck + build
cd extension
npm test
npx tsc --noEmit
npm run build
cd ..

# 3. Backend Pytest suite (1073 test functions; latest full run 1046 passed, 9 skipped, 18 failed — the 18 are pre-existing, unchanged since Task 1)
cd backend
source venv/bin/activate          # Linux / macOS
# venv\Scripts\activate           # Windows
python -m pytest tests -q
cd ..

# 4. Benchmark evaluation suite (Tasks A through H, mock harness)
backend/venv/bin/python backend/scripts/benchmark_eval.py

# 4b. Measured evals (Task 6): checker ASR, privacy leak rate, mock benchmark
#     with trajectories — offline, mock provider, deterministic.
backend/venv/bin/python backend/scripts/eval_live.py --suite all

# 4c. External-suite readiness (WebArena / OSWorld / PrivacyLens / AgentDojo)
#     — reports what is missing; exits 2 while blocked.
backend/venv/bin/python backend/scripts/eval_external.py webarena

# 5. Live provider smoke test (requires GEMINI_API_KEY)
backend/venv/bin/python backend/scripts/check_gemini.py
```

> **Proxy note (local runs).** `httpx` reads proxy settings from the environment
> at import time, and a malformed proxy / `no_proxy` value makes the backend
> test suite fail at collection with `httpx.InvalidURL`. If you run pytest in
> an environment with proxy variables set, strip them first:
> `env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u all_proxy -u NO_PROXY -u no_proxy python -m pytest tests -q`
> CI does this for the backend job.

### Benchmark Metrics

`benchmark_eval.py` runs eight deterministic fixture smoke tests and reports only test passes and fixture duration. It does not establish live autonomy accuracy or zero privacy leakage. The 12-category runner in `backend/scripts/realworld_benchmark.py` computes evidence-backed task and action metrics from a connected runtime; its current live readiness status is documented in `docs/PHASE8_STATUS.md`.

Measured numbers now exist for what can be measured offline — see
`docs/EVALS.md` for the full report, fixtures, and limitations. Run on
2026-11-16 with the mock provider: checker ASR **0.50 overall**
(canonical 0.25, evasion 1.0 — the regex checker is blind to
paraphrase); privacy leak rate **0.2609** (6 of 23 synthetic PII tokens
reached the planner prompt; every pattern-covered type leaked 0, the
leaks are bare OTPs, unlabelled passwords, addresses, and a phone in an
element value); mock benchmark **8/8** fixtures. WebArena-Verified,
OSWorld 2.0, PrivacyLens and AgentDojo have **not** run — the readiness
checker (`backend/scripts/eval_external.py`) and exact run commands are
in `docs/EVALS.md`; all four are blocked on an LLM key and a PrivAgent
adapter that does not exist yet.

### Dependency Pins

`backend/requirements.txt` pins every direct dependency to the exact version
verified in `backend/venv`; `package.json` / `extension/package.json` (and their
lockfiles) pin exact versions too. To upgrade a dependency intentionally,
bump the pin, reinstall, run the full test suite, and commit the pin together
with the lockfile change — see the header comment in `backend/requirements.txt`.

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
│   │   └── providers/               # Gemini, Groq, DeepAI, OpenAI-compat, Ollama (local), mock
│   ├── tests/                       # 586 test functions, 62 files (contracts, privacy, loop, A–H, security, latency)
│   ├── requirements.txt             # Pinned direct dependencies (see header comment)
│   ├── Dockerfile                   # Python 3.12-slim, non-root, /api/health check
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
│   └── tests/                       # 137 Vitest cases, 15 files
├── .github/workflows/ci.yml         # Backend + frontend + extension + secret scan
├── docker-compose.yml               # Frontend (5173) + backend (8000), loopback-only
├── Dockerfile                       # Vite frontend build + preview
├── LICENSE                          # MIT
├── CHANGELOG.md                     # Tasks 1–4 and Unreleased (Task 5)
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

