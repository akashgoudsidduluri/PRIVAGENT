# NAVIGATOR Architecture & Technical Reference
## SIH 26171 | Privacy-first AI browser agent

> Rewritten in Task 13 (Nov 2026). The base-era version of this file
> described the pre-Tasks-1–12 codebase (81 backend tests, 25-step cap,
> mock-only benchmark presented as evaluation). This version describes
> the codebase as it is after Tasks 1–13: the module map after the god
> files were split, the three design flaws Task 13 fixed, and the
> observability surface. For the honest evaluation record (measured
> checker ASR, privacy leak rate, mock-only benchmark) see
> `docs/EVALS.md`; for latency see `docs/LATENCY.md`.

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

## 2. Module map — backend (`backend/app/`)

Task 13 split the two god files. Line counts are current as of Task 15
(measured 2026-11-16; base: `agentLoop.py` 3,883, `planner.py` 2,044).
Task 14 grew the loop again (connector / WebMCP / UCP dispatch lives in
`agentLoop.py` and `planner_validation.py`), so the counts below are
larger than the post-Task-13 figures (2,876 / 873).

| Module | Lines | Responsibility |
|---|---|---|
| `agentLoop.py` | 3,103 | The loop itself: `_run_loop` (~1,890 lines) and `run_task` (~830 lines) plus config-mirror constants. Everything else moved out; this file re-exports the moved names so existing imports keep working. |
| `loop_state.py` | 127 | Cancel / pause / takeover registries and `request_cancel`, `is_cancelled`, `request_pause`, `request_resume`, `_notify_extension_takeover`, `_await_takeover`. |
| `loop_gates.py` | 169 | Confirmation + takeover gates (`_ConfirmGate`, `_TakeoverGate`, `_request_confirmation`, `_gate_message`). Gates resolve the hub lazily via `from . import agentLoop` so test patches of `agentLoop.real_hub` are honoured. |
| `loop_checkpoints.py` | 218 | Tab-registry helpers, `_compact_history` / `_maybe_compact_history`, `save_cancel_checkpoint`, `_save_checkpoint_defensive`, `_persist_agent_message`, `_persist_derived_memory`. |
| `loop_helpers.py` | 646 | Pure helpers: host/URL predicates, target naming, `_call_planner`, `_actions_match`, redundancy dedup, `_plan_tree_and_refs`, `_state_digest`, `_execute_with_retries`, `_fresh_observation`, `_to_obs`, `extract_mission_facts`, plus the Task 13 freshness helpers `fresh_tree_for_observation` / `ref_freshness`. |
| `loop_research.py` | 352 | `_finalize_research_artifacts` + `_finalize_research_summary` (research-dispatch finalisation). |
| `planner.py` | 911 | Planner entry points (`build_prompt`, `plan_from_observation`, provider selection). The validation cluster moved out; this file re-exports it. |
| `planner_validation.py` | 1,314 | `_normalize_raw_plan` + the whole validation cluster (`_validate_step`, `_validate_plan` and helpers) + its 6 constants. References only function-local imports, which is what made the move safe. |
| `connector_router.py` | 338 | Task 14: data-driven Gmail / Calendar / Drive intent detection; never steals explicit UI navigation, dispatch, mission setup, or research. |
| `connector_runner.py` | 387 | Task 14: executes a routed connector call (reads answered directly, writes gated by confirmation, calendar creates parsed before confirming); fails honestly when the connector is unconfigured. |
| `webmcp.py` | 330 | Task 14: validates page-declared WebMCP tools (name regex, JSON Schema, caps) and renders them `<webmcp-tools untrusted="true">`. |
| `ucp.py` | 440 | Task 14: UCP merchant profiles (`<link rel="ucp">` + `/.well-known/ucp`, pinned versions) and `ucp_checkout` through the Task 4 payment path. |
| `grounding.py` | 461 | AX-tree build/resolve/freshness (`build_ax_tree`, `resolve_ref`, `check_freshness` — now also compares the stable target id), `format_ax_tree`, glyph normalisation. |
| `observability.py` | 64 | One JSON log line per executed step (see §5). |
| `long_memory.py` | 1,123 | Facts/skills/files, recall, scheduler-facing APIs. List reads now skip undecryptable rows (see §4c). |
| `latency.py` | ~200 | `LoopTimings` / `StepTiming` — the ONE timing system (see §5). |
| `obs_cache.py` | ~205 | Per-task in-memory observation cache + DOM fingerprint. |
| `batch.py` | ~170 | Batch-prefix selection (`batch_prefix`, `is_ref_grounded`, `is_non_submitting_type`). |
| `checker.py` | ~325 | Web-blind injection checker (regex rules + provenance tags). |
| `privacy.py` | ~690 | Fail-closed redaction (`classify_and_redact`). |
| `secrets.py` | ~495 | Fernet secret store; `decrypt_text` raises `ValueError` on an undecryptable `enc:v1:` value (fail-closed by design). |

### Import graph (loop modules only)

```
main.py ──► agentLoop.run_task / is_cancelled / request_cancel / ...
                 ▲  (function-local imports at call time)
                 │
agentLoop.py ──► loop_state, loop_gates, loop_checkpoints,
                loop_helpers, loop_research, observability
                (re-exports the moved names)
loop_gates ──► agentLoop           (lazy, inside _resolve_hub — test patches)
loop_checkpoints ──► loop_helpers?  (no — checkpoints import db/long_memory only)
loop_helpers ──► grounding, privacy (function-local, to avoid cycles)
planner.py ──► planner_validation  (re-export)
providers/* ──► agentLoop._provider_for_config   (kept in agentLoop by design)
control_center.py ──► agentLoop.TASK_TIMEOUT_S / PLANNER_TIMEOUT_S (runtime writes)
```

### What was deliberately NOT moved — and why

* **`_run_loop` and `run_task` stay in `agentLoop.py`.** Tests
  monkeypatch attributes *on the `agentLoop` module* and expect the
  loop to observe them (`MAX_STEPS`, `MAX_RETRIES`, `real_hub`,
  `_provider_for_config`, `PLANNER_TIMEOUT_S`), and
  `control_center.py` writes `agentLoop.TASK_TIMEOUT_S` /
  `PLANNER_TIMEOUT_S` at runtime. Moving the loop would silently
  break those patch points. The constraint is load-bearing, not
  cosmetic — see `test_agentLoop.py`, `test_latency.py`,
  `test_provider_recovery.py`, `test_intent_and_sessions.py`.
* **Research-dispatch branches A–D inside `run_task` stay.** They
  share ~15 locals plus nested closures with the surrounding loop;
  extracting them would mean threading that state through parameters
  or a context object — a redesign, not a move. The *finalisation*
  half did move (`loop_research.py`).
* **`_LiveObservation` / `_live_observation_factory`,
  `_provider_for_config` / `_real_planner`, and the config-mirror
  constants stay** for the same patch-point reasons.

---

## 3. Core subsystems (unchanged by Task 13, summarised)

* **Contracts.** TypeScript source of truth
  `extension/src/contracts.ts`; Python Pydantic mirror
  `backend/app/contracts.py`; `test_contracts.py` guarantees zero
  drift. 15 action types; planner caps a plan at 3 steps; only
  idempotent actions retry after TIMEOUT.
* **Perception / grounding.** DOM-derived AX tree (not CDP
  `getFullAXTree` — an MV3 content script cannot attach
  `chrome.debugger` to its own tab without the debug banner).
  Refs `e0…` in list order; planner quotes `elementRef`; loop
  resolves refs just before execution. Limits: 200 elements,
  8,000 chars page text (Task 7). Duplicate names are annotated in
  the tree and rejected for name-only targets (Moodle fix).
* **Privacy layer.** `classify_and_redact` runs locally before any
  payload reaches a planner; fail-closed on error. See
  `docs/PRIVACY.md` and `docs/DPIA.md`.
* **Safety gates.** Confirmation gate (sensitive risk, auth pages,
  WhatsApp QR, strict write-confirm), takeover mode (login/payment/
  OTP/CAPTCHA/2FA — loop pauses, stops observing), vault
  placeholders, mock-first payments with a hard cap. See
  `docs/CREDENTIALS_PAYMENTS.md`, `docs/ISOLATION.md`.
* **Memory / long-horizon.** Facts/skills/files in encrypted SQLite,
  provenance-enforced writes, checkpoints + resume, scheduler with
  the same gates as live tasks. See `docs/MEMORY_LONG_HORIZON.md`.
* **Latency.** Observation cache (DOM fingerprint), batch execution
  of ref-grounded non-submitting types + one terminator, per-step
  timings. Measured: planner calls 7→4, observation calls 2→1 on
  the login+search bench. See `docs/LATENCY.md`.

---

## 4. The three design flaws Task 13 fixed

### (a) `check_freshness` was vacuous — now real

**Before (Task 7 design flaw, found by the Moodle-fix review).**
At the ref-resolution site the loop did:

```python
_node = _resolve_ref(_tree, ref)          # _tree is the PLAN-TIME tree
_fresh = _check_freshness(_node, _tree)    # node vs ITS OWN tree → always "ok"
```

`check_freshness` itself was correct — it compares a node against a
tree — but it was always handed the tree the node came from, so
`"changed"` was unreachable and the loop proceeded blind whenever
the page moved between planning and execution.

**After.** Two fresh sources, both wired into the loop:

1. **Batch path (free, no round trip).** In a batch continuation the
   post-action observation is still in memory (`cached_obs` — the
   batch branch takes precedence over the `cached_obs` branch at
   iteration top, so it is not consumed). The loop builds a fresh
   tree from it with `fresh_tree_for_observation` and compares the
   plan-time node against that tree via `ref_freshness`. A changed
   or stale ref **ends the batch** (re-observe + replan once, then
   fail honestly) — the same one-retry counter as the stale path.
   A fresh tree that **cannot be built** (redaction failed on the
   post-action observation) is "cannot verify", not "ok": it ends
   the batch on the same counter instead of executing the pinned
   step blind — `ref_freshness` returns "ok" when given no fresh
   tree, which is correct for a single step but wrong here, where a
   fresher observation existed and failed. (Task 13 review fix;
   the post-wait path already handled this correctly.)
2. **Single-step path (one re-observation).** The window where the
   page can move is the operator wait (confirmation and/or pause),
   which happens *after* ref resolution and *before* execution. The
   loop tracks a `_waited_for_operator` flag; after the waits, if
   the step has an elementRef, it re-observes once and checks the
   ref against the fresh tree. Changed → proceed on the **live**
   node (target re-pinned to the fresh name/role/targetId/coords)
   with a warning; stale → re-observe + replan once, then fail
   honestly; an observation that cannot be redacted → treated as
   "cannot verify", never execute blind (same one-retry counter, so
   a broken observation source cannot loop the operator through
   confirmations forever). No wait → no extra observation (pinned
   by test).

`fresh_tree_for_observation` runs the fresh observation through the
**same** redaction pipeline as the plan-time tree
(`classify_and_redact` → `_plan_tree_and_refs`), because redaction
is a 1:1 order-preserving map — so a difference between the two
trees is a real page change, not a redaction artifact (names are
truncated identically on both sides). And `check_freshness` now
also compares the stable target id when both sides carry one — a
re-render that keeps the name/role while reassigning the id is
still a changed element.

**Agreement with the Task 11 fingerprint gate.** The fingerprint
(url | title | element count | first-12 target ids) ends the batch
on a coarse DOM change; `check_freshness` adds the per-ref
name/role/target-id check the fingerprint misses (same count/ids,
different name/role). Both end the batch; neither can contradict
the other into executing a stale plan. The bench still reproduces
exactly (planner 4, observations 1, wall median 825.01 ms vs the
committed 824.32 ms — within first-run variance; `docs/LATENCY.md`
unchanged).

**Tests:** `backend/tests/test_task13_freshness.py` (12 tests) —
grounding-level `check_freshness` target-id change, `ref_freshness`
ok/changed/stale verdicts, fresh-tree parity with the plan-time
pipeline, unredactable → `None`; loop-level batch ends on rename /
disappearance / target-id move / **unredactable fresh tree**; loop-level post-wait changed
(re-pinned to the live node), stale (replans, never clicks), and
unchanged (no extra replan); no-wait means no extra observation.

### (b) Vite build broke on any cross-entry import — now per-entry builds

**Before (Moodle-fix landmine).** One config, three inputs
(`background`, `content`, `companion`), `format: "es"`, with an
IIFE wrapper around `content.js`/`companion.js`. Any module
imported by two entries (e.g. `semantic.ts`, imported by the
content entry and — via `targeting.ts` — by the background entry)
made Rollup split it into a shared chunk that `content.js`
imported at the top level, and the IIFE wrapper cannot contain a
top-level import. The production build broke twice (the Moodle fix
and its review), which forced `targeting.ts` to carry a local copy
of `stripIconGlyphs`.

**After.** One config **per entry**, each with a single input:

* `vite.config.ts` — background (the default `vite build`; the
  only config with `emptyOutDir: true`)
* `vite.content.config.ts` — content script (+ IIFE wrapper)
* `vite.companion.config.ts` — companion page (+ IIFE wrapper)
* `vite.shared.ts` — the two plugins (IIFE wrapper, manifest copy)

With one entry per build Rollup cannot create a shared chunk —
every import is inlined into its own entry's bundle. (An array of
configs was tried first and rejected: Vite 6.4.4's config loader
requires an object, not an array — "config must export or return
an object".) `npm run build` runs the three builds in order.
`@types/node` was added as a devDependency (the build test needs
`node:child_process` / `node:fs` types).

**Dedup unlocked by the fix.** `targeting.ts` now imports
`stripIconGlyphs` from `./semantic` — the 25-line local copy
(`TARGET_GLYPH_RANGES` / `targetGlyphRe` /
`stripIconGlyphsForTarget`) is deleted. The old parity test is
gone with it; the strip itself is pinned in
`tests/semanticPerception.test.ts`, the glyph-prefixed
`findTarget` test exercises it through targeting's path, and the
build test proves the cross-entry import. The glyph ranges are
inlined into both `background.js` and `content.js` (verified:
`57344,63743` present in both).

**Tests:** `extension/tests/viteSelfContainedEntries.test.ts` —
runs the real `vite build` three times into a temp dir and asserts
each succeeds, the temp dist holds exactly `background.js` /
`content.js` / `companion.js` / `manifest.json`, no entry has a
top-level `import`, and the IIFE-wrapped entries have no dynamic
`import()` either. One real bug found by the test while writing it:
the manifest copy hardcoded `dist/`, so a `--outDir <tmp>` build
left the temp dist unloadable — the plugin now copies to the actual
output dir via `writeBundle(options)`.

### (c) Undecryptable rows 500'd the whole list — now skipped + counted

**Before (Task 2 design flaw, found by the Task 10 review).**
`secrets.decrypt_text` raises `ValueError` on an `enc:v1:` value it
cannot decrypt (wrong master key / corrupt data). `list_facts` and
`list_skills` called it per row with no `try/except`, so one bad
row raised out of the endpoint and the whole list 500'd.

**After.** `long_memory._decrypt_column` catches `ValueError` per
row, logs a warning with the row id only (never the value), and
returns `None`; the row is dropped from the result. New
`list_facts_with_skips` / `list_skills_with_skips` return
`(rows, skipped_count)`; `list_facts` / `list_skills` delegate to
them (all existing callers — `recall_*`, `update_skill`, tests —
unchanged, and recall now benefits automatically). The API
endpoints return `{"facts": [...], "skipped_undecryptable": n}` /
`{"skills": [...], "skipped_undecryptable": n}` so the operator can
see memory is degraded instead of silently losing rows.
Fail-closed is preserved for writes; only the LIST read degrades
gracefully. Single-row reads elsewhere in `db.py` still raise —
that is the correct fail-closed behaviour for a row you asked for
by id.

**Review fix (Task 13 audit): the same `ValueError` still escaped
from every other list read.** `db.get_messages` /
`get_recent_messages` (→ `GET /api/chat/{id}` via `get_chat`),
`db.get_chat_memory`, `SemanticMemory.recall` (→ `GET
/api/memory/facts`), and `db.get_email_classifications` /
`get_email_notifications` (→ the email dashboard) all 500'd on
one bad row. They now skip undecryptable rows per row — `db.py`
gained `_decrypt_or_none` (with a `_UNDECRYPTABLE` sentinel, since
`decrypt_text` legitimately returns `None` for a NULL column such
as a message with no evidence) and `tiers.py` catches per row —
logged with the row id only, never the value. The other list reads
that already degraded gracefully (`get_task_checkpoint`,
`get_research_package*`, `list_email_missions`,
`control_center` timeline) were verified and left alone.

**Tests:** `backend/tests/test_task13_encrypted_rows.py` (11 tests)
— a fact/skill row encrypted under a different Fernet key is
skipped, the endpoint still returns the good rows, the skip is
counted and logged (row id present, plaintext absent), all-bad
rows return an empty list (not a 500), and the API reports the
skip count; two bad rows among good ones are skipped with the
count at 2; the same skip is pinned for semantic facts, chat
messages / chat memory (including via `GET /api/chat/{id}` and
`GET /api/memory/facts`), and the email classification /
notification lists; a NULL-evidence message is never dropped.

---

## 5. Observability

One surface, two halves — both reuse Task 11's `LoopTimings`, so
there is no second timing system.

* **Live:** `backend/app/observability.py` — `log_step(task_id,
  entry, outcome)` emits one JSON line per executed step on the
  dedicated `navigator.observe` logger, at the `flush_step` site
  in `_run_loop`, from the same `StepTiming` object. Format (keys
  sorted):
  `{"action": "type", "batched": false, "cache": "miss",
  "checker_skipped": false, "checkpoint_ms": 2,
  "confirm_wait_ms": 0, "event": "loop_step", "execute_ms": 0,
  "observe_ms": 0, "outcome": "ok", "overhead_ms": 5,
  "plan_ms": 201, "redact_ms": 5, "step": 1, "task_id": "abc123",
  "total_ms": 207}`. `log_step` never raises.
* **After the fact:** the per-task summary is already in
  `GET /api/task/{id}` → `timings` (`LoopTimings.summary()`:
  durations, counts, action types only).

What to grep for: `navigator.observe` (every line),
`"event": "loop_step"`, `"outcome": "failed"`,
`"batched": true`, `"cache": "hit"`.

Privacy: a line carries only the task id, step number, action
TYPE, durations, and flags — never page text, element names,
typed values, URLs, or target ids. Safe under zero retention
(Task 10). Tests: `backend/tests/test_task13_observability.py`
(4 tests) — format + outcome normalisation, never raises, one
line per step with batched flags, and a page full of distinctive
strings (typed values, names, title, URL, evidence, instruction)
produces a log containing none of them.

---

## 6. Dead code removed in Task 13

Every removal was verified repo-wide (definition is the only
occurrence, including tests and dynamic lookups) before removal;
the full suite stayed green. Python (vulture 2.16 + grep):

| Removed | Evidence it was dead |
|---|---|
| `agentLoop._get_planner_semaphore` | def only; docstring "Retained for backward compatibility" |
| `agentLoop._ret` (nested in `_run_loop`) | def only; never called |
| `db.delete_chat_memory` | def only |
| `hub.Hub.unregister_tab` | def only (`register_tab` is test-used — kept) |
| `step_narrator._extract_clean_product_name` | def only |
| `schemas.ProgressEnvelope` | def only |
| `collector.extract_page_images(topic_keywords)` param | body never used it; callers pass 5 args / kwargs without it |
| `product_extractor.parse_emi_details(price_num)` param | body never used it; the one caller updated |
| `diagram_generator.generate_mermaid_diagram(diagram_type)` param | body never used it; no caller passed it |
| `docx_renderer` imports `OxmlElement`, `qn` | import lines only (`parse_xml`, `nsdecls` are used) |
| `image_pipeline` import of `validate_redirect_chain` | import line only (the function itself is test-used — kept) |

TypeScript (export-usage scan over `extension/src`, `lib`, `tests`):

| Removed | Evidence it was dead |
|---|---|
| `extension/src/contracts.ts`: `TaskProgress`, `PrivacyDecision`, `PermissionRequest`, `TaskPlan`, `UserTask` | exported interfaces, zero references anywhere in the extension (the backend has its own Python `PrivacyDecision` / `TaskPlan` / `UserTask` — unrelated) |
| `extension/src/targeting.ts`: local glyph-strip copy | superseded by the shared import (see §4b) — a dedup, not a deletion |

Frontend (`src/`): 22 exports scanned, 0 dead.

Deliberately **kept** (not dead, or out of scope): connector
interface methods (Gmail/Outlook/Slack/GitHub/Drive/local-filesystem
— the Task 14 surface), FastAPI route functions (mounted routers),
test-pinned names, pydantic `@field_validator` methods (vulture
false positives), and the per-row `decrypt_text` raises in `db.py`
(fail-closed by design, see §4c).

---

## 7. Verification & provider configuration

| Suite | Command | Current result |
|---|---|---|
| Backend | `cd backend && <venv> -m pytest tests -q` (proxy env stripped) | 991 passed, 9 skipped, 18 failed — the 18 are the pre-existing baseline set, byte-identical since Task 1 (verified on clean `main`); Task 15 added 1 test on top of the Task 14 review's 990 |
| Extension | `cd extension && npm test` | 180 Vitest tests |
| Extension types | `cd extension && npx tsc --noEmit` | clean |
| Extension build | `cd extension && npm run build` | three single-input builds; dist holds exactly `background.js`, `content.js`, `companion.js`, `manifest.json`; no top-level imports |
| Frontend | `npm test` | checks pass (see README) |
| Frontend types/build | `npx tsc --noEmit` / `npm run build` | clean / succeeds |
| Latency bench | `backend/scripts/bench_loop.py --planner-delay-ms 200 --runs 3` | planner 4, observations 1, wall median 825.39 ms (committed 824.32 ms — within first-run variance; Task 15 re-run 2026-11-16) |

Provider selection (`PRIVAGENT_PLANNER` alias for
`NAVIGATOR_PROVIDER`, else auto-detected from keys, else `mock`):
Gemini, Groq, mock, OpenAI-compatible, DeepAI Gateway, plus local
Ollama (`PRIVAGENT_PLANNER=ollama`, `LOCAL_FIRST` / `LOCAL_ONLY` —
Task 10). API keys are read from `backend/.env` or the environment
and are never logged or returned by an endpoint.

**Not verified in Task 13 (honest limits):** no live Chrome run
(extension verified by Vitest + build only; the freshness fix is
unit- and loop-tested on synthetic pages); no live LLM; the
`check_freshness` target-id comparison can end a batch spuriously
if a page re-renders with fresh ids — a performance cost in the
safe direction, with the extension's live name/role agreement as
backstop.
