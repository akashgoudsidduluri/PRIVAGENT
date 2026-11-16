# Latency — Task 11

How the loop got faster, what was measured, and what the numbers do
(and do not) mean.

## What was slow

Before this task the loop executed exactly **one step per planner
call**: for every action it re-observed the page, re-redacted, called
the planner, ran the checker, and only then executed `steps[0]` of
the plan. A five-step login + search therefore cost five planner
round trips and two live observations, even though the plan for the
whole login was already valid after the first call.

The extension had the same shape in miniature: `doType` collected
the candidate list to resolve its target, then collected it **again**
to find the submit button, and every action handler re-collected it
once more for the trailing observation.

## What changed

### 1. Profiling (`backend/app/latency.py`)

`LoopTimings` records per-step durations for the phases `observe`,
`redact`, `plan`, `checker`, `confirm_wait`, `takeover_wait`,
`execute`, `checkpoint`, plus counters: planner calls, observation
calls, cache hits/misses, stale detections, batched steps, and
checker-skipped steps. Confirmation and takeover waits are recorded
separately as operator time and excluded from overhead totals.

Timings are safe by construction: a step entry carries only the
action type, the phase durations, and flags (`batched`, `cache`,
`checker_skipped`) — never target names, typed values, page text, or
URLs. They are in-memory only (never persisted to checkpoints), are
returned in the `run_with_injection` result dict, attached to
`TaskRecord.timings`, and readable via `GET /api/task/{id}`.

### 2. Observation cache (`backend/app/obs_cache.py`)

Per-task, in-memory only, never persisted — so it cannot interact
with zero-retention guarantees. One entry: the last observation,
keyed by tab + URL, with a DOM fingerprint (SHA-256 of
url | title | element count | first 12 target ids — the same
ingredients as the loop's state digest).

A hit requires the same tab, the same URL, and the `valid` flag set.
A hit is still re-redacted via `classify_and_redact` on every use,
exactly like the loop's existing post-action observation reuse.
Invalidated on: a failed action, a tab switch/close, takeover entry,
pause/resume, a stale element ref, or a successful action that
returned no observation. When a fresh observation arrives after a
miss while an entry still exists for that tab+URL, the fingerprint
is compared and a mismatch counts as `stale_detected`.

Residual risk, stated plainly: the backend cannot know the DOM
changed without asking the extension, so the cache only serves when
the loop can prove no mutation occurred since the observation was
stored. That is the same policy the loop already applied to
post-action observations; the cache extends it to replan iterations.
One gap the fingerprint does **not** close: a cache *hit* never
verifies the fingerprint (there is nothing to verify it against
without a fresh observation, which is the round trip the cache
exists to avoid), so a page that changes on its own — a timer, a
live feed, an ad — between the store and the hit is served stale.
The entry is invalidated on every agent- or operator-caused
mutation the loop knows about, and every executed step is still
resolved live by the extension (name/role agreement on refs), so a
stale tree can mislead the planner but cannot silently retarget an
action. The fingerprint *is* consulted by the batch gate (below),
which ends a batch when the post-action observation's fingerprint
differs from the plan's.

### 3. Batch actions (`backend/app/batch.py`, loop pinning)

After a plan is validated — and after the Requirement 8 dedup and
the checker have seen the **full** plan — the loop trims it to its
**batch prefix** just before execution and pins the remainder: after
each successful non-submitting `type` step whose post-action
observation is on the same URL as the plan **and whose DOM
fingerprint still matches the observation the plan was built from**,
the next pinned step executes without re-observing or re-planning.
URL equality alone was not enough — a type step can mutate the DOM
(autocomplete dropdown, validation message, dynamic fields) without
navigating, and the pinned plan's refs were quoted against the
pre-type tree. The fingerprint (url | title | element count | first
12 target ids) catches count/title/identity changes with no extra
round trip, since the post-action observation is already in memory;
a mismatch ends the batch and the loop re-plans on the fresh tree.
The fingerprint is coarse — a name/role change past the first 12
elements does not move it — so the extension's live name/role
agreement check on every executed step remains the backstop.

The trim placement matters and is load-bearing: trimming before the
dedup broke it. A plan whose `steps[0]` is a navigation already
completed in history must advance to `steps[1]`; trimming first
discarded `steps[1]`, so the loop re-planned forever with nothing
executed (this regressed
`test_provider_recovery::test_never_repeat_completed_browser_actions_after_fallback`,
found by the full-suite gate and fixed in the same task; a
regression test pins it). The checker likewise sees the full plan,
so a flag on any step blocks the whole plan before anything
executes — the pre-Task-11 behaviour.

Rules (safety-critical, unchanged from the brief):

- Every step is validated individually by `_validate_plan` /
  `_validate_step` + the checker **before** the batch starts.
- A non-first step is batched only if it is **ref-grounded** in the
  observation the plan was built from (elementRef present, or a
  stable author targetId). Name-only steps are never batched, and a
  step whose ref is not in the observation cannot be batched at all —
  so a dependent step (type into an element that only appears after
  a prior click) is never batched by construction.
- A step that navigates, submits a form, or otherwise changes the
  page **ends** the batch; the loop re-observes before continuing.
  `MAX_BATCH_STEPS = 3` is a backstop (the plan cap binds first).
- A failed step stops the batch honestly: the cache is invalidated
  and the loop replans from a fresh observation.
- **Confirmation is not batched.** Every step keeps its own
  confirmation / takeover / allow-list gates in the loop; batching
  elides only the planner call. A risky step in a batch is therefore
  still presented to the operator on its own (and a rejection stops
  the batch at that step). The checker scan + `check_plan` are skipped
  only for pinned steps — the full batch was already approved on the
  same observation, and a subset cannot newly flag — and the skip is
  counted as `checker_skipped_steps`. The navigate allow-list check
  still runs per step.
- The AX tree is built once per plan and reused for per-step ref
  resolution (previously rebuilt per step).

### 4. Extension (`extension/src/actionExecutor.ts`)

`doType`'s submit-control lookup now tries the candidate list
already collected for target resolution first, and polls with fresh
lists only if the button is not there yet (React may expose it a
moment later). One `list()` collection saved per type+submit action
when the button is already on the page; the late-render poll path
is unchanged.

Deliberately **not** done: a new `executeBatch` / `send_batch`
websocket message type for the whole batch. The backend batch
already delivers the planner-round-trip win, and a parallel
execution path in the extension cannot be verified live (no Chrome
here). If it is ever added, it needs a live-Chrome test first.

No new dependencies. No new environment variable (the plan cap of
3 already bounds batches).

## Measured results

Benchmark: `backend/scripts/bench_loop.py` — a scripted login +
search task (type username, type password, click Log in, type query,
click Search) against the mock provider with a mocked extension and
a 200 ms simulated planner delay, 3 runs each. The scripted planner
raises one `ValueError` PLAN_INVALID the first time it sees the
search page, to exercise the replan path. Checkpoints go to a temp
DB (`PRIVAGENT_DB_PATH`), never the operator's real one.

Reproduce:

```bash
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  -u no_proxy -u NO_PROXY -u all_proxy -u ALL_PROXY \
  backend/venv/bin/python backend/scripts/bench_loop.py \
  --runs 3 --planner-delay-ms 200 \
  --out evals/results/latency_after_2026-11-16.json
```

| Metric (median of 3 runs) | Before | After | Change |
|---|---|---|---|
| Planner calls | 7 | 4 | **−43%** |
| Observation calls | 2 | 1 | **−50%** |
| Actions dispatched | 5 | 5 | same work |
| Steps | 5 | 5 | same work |
| Wall time | 1417.59 ms | 824.32 ms | **−42%** |

Runs: before 1478.13 / 1417.59 / 1413.25 ms; after 1050.72 /
824.32 / 822.28 ms. After-run timings: cache 1 hit / 1 miss,
3 batched steps, 3 checker skips, 0 stale detections; `plan_ms`
802 (≈4 × 200 ms simulated delay), `redact_ms` 5, `checkpoint_ms`
204 (of which ~200 ms is the first-write SQLite init in the
benchmark's fresh temp DB — later checkpoints are 2–3 ms),
observe/execute/checker ≈0 with the mock.

Task 15 re-run (2026-11-16, same command, `--planner-delay-ms 200
--runs 3`): planner 4, observations 1, wall median 825.39 ms
(runs 1066.73 / 825.39 / 824.10 ms) — within first-run variance of
the committed 824.32 ms; the mechanism (7 → 4 planner calls, 2 → 1
observations) reproduces exactly.

Verification re-run (2026-11-17, same command, `--planner-delay-ms 200
--runs 3`): planner 4, observations 1, wall median 846.19 ms
(runs 1805.41 / 846.19 / 841.08 ms). The first run is slow for the
documented reason (first-write SQLite init in the fresh temp DB);
the two steady-state runs (846.19 / 841.08 ms) sit ~20 ms above the
committed 824.32 ms, consistent with machine load during the
verification pass. The mechanism (7 → 4 planner calls, 2 → 1
observations) reproduces exactly, which is the durable claim.

Full results: `evals/results/latency_baseline_2026-11-16.json`,
`evals/results/latency_after_2026-11-16.json`, and
`evals/results/latency_task15_2026-11-17.json` (the verification re-run).

## Honest limits

- These numbers are **loop overhead with the mock provider**. In
  production a real LLM call dominates wall-clock time, so the win
  is in round trips and overhead, not wall-clock against a frontier
  model. The planner-call reduction (7 → 4) is the durable win:
  with a real planner each elided call saves a full LLM round trip
  (and its cost), not just 200 ms.
- The benchmark is one scripted task on synthetic pages. It proves
  the mechanism (batching, cache, fewer round trips), not a
  production latency distribution.
- No live Chrome run was done (none available here): the extension
  change is verified by Vitest counting `list()` collections, not by
  a real browser. The backend batch is verified by unit + loop tests
  with a fake extension.
- `check_freshness` in the loop is still vacuous (a Task 7 design
  flaw noted in earlier reviews: the node is compared against its
  own tree, so "changed" is unreachable). The DOM-change detection
  that actually runs is the fingerprint: compared when a fresh
  observation arrives after a cache miss, and consulted by the batch
  gate before continuing a batch. A cache *hit* never verifies it
  (see §2) — an autonomously changing page can be served stale
  until the next fresh observation.
