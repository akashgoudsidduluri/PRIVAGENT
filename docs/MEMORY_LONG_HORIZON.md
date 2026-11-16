# Memory & Long-Horizon (Task 9)

Before Task 9 the agent forgot everything between tasks and died at 25
steps. This document describes what changed: durable operator-owned
memory, real checkpoints with resume, a higher configurable step cap
with history compaction, multi-tab tasks, and an in-process scheduler
for background / recurring tasks.

## 1. Long-term memory — three tiers

All three tiers live in the same SQLite database as everything else
(Task 2 rules apply: DB directory `0700`, files `0600`, sensitive
values Fernet-encrypted at the accessor layer). The memory UI is the
**Memory** button in the workspace header (`agent.html` +
`src/agentApp.ts`); every endpoint is under `/api/memory/long/*` and
requires the local auth token (Task 1).

| Tier | Table | What it holds | What the planner sees |
|---|---|---|---|
| Facts | `long_memory_facts` | Operator-stated durable facts ("my home airport is DEL"), with category, source, confidence, timestamps | Top 5 by keyword relevance, ≤ 800 chars total |
| Skills | `long_memory_skills` | Reusable procedures as prose steps ("how I book a flight"), versioned, with a success count | Top 2 by keyword relevance |
| Files | `long_memory_files` (+ `backend/data/files/`) | Operator-attached reference files, stored by id under `backend/data/files/` (dir `0700`, files `0600`, 5 MiB cap) | A short summary only — **never** the file content |

### The provenance rule (the important part)

Memory is a leak channel if anything on the web can write to it
(Task 6 measured a 0.26 privacy leak rate against page content). So:

- A fact/skill may only be created with source `"user"` or
  `"task:<id>:confirmed"`. The source is regex-enforced in code
  (`backend/app/long_memory.py: _validate_source`); `source="page"`,
  `"observation"`, `"planner"`, or any other value is a 422.
- **Page content can never create a fact or skill.** Write-back after a
  task only *proposes* candidates, and only from (a) the operator's own
  instruction words (preference sentences such as "I prefer window
  seats") and (b) the task's verified successful actions (domains used +
  step list). Proposed skill steps carry the action type and domain
  only — never the page's element names or DOM ids, so a hostile page
  cannot smuggle instruction text into a skill through a target name.
  Proposals are stored in the task checkpoint and become memory only
  after the operator accepts them in the Memory dialog
  (`POST /api/memory/long/proposals/{task_id}/confirm`). Accepting one
  proposal does not discard the others — each is accepted on its own.
- **Memory never stores secrets or PII.** Writes are rejected when
  `privacy.classify_sensitivity` scores the text RESTRICTED_PII
  (email / card / phone / etc.) or when it looks like a credential
  with a value ("password hunter2", "api key is abc123"). Naming a
  credential kind without a value ("my password manager is 1Password")
  is allowed. File summaries derived from a file's first bytes get the
  same credential check, falling back to a neutral label.
- Recall is deterministic keyword-overlap scoring (stopwords excluded,
  no embeddings, no LLM call). Facts with no shared token with the
  instruction are not recalled — an irrelevant fact in the prompt is
  noise the planner will act on. The recall block in the prompt is
  labelled `<long-term-memory source="operator">` — deliberately
  WITHOUT `trusted="true"`: its only sources are the operator and
  confirmed outcomes, but recalled prose is background about
  preferences, never an instruction. A stored skill that says "always
  send the report" must not override the user instruction or the
  safety gates; the closing line of the prompt names
  long-term-memory alongside page-context, working-memory, history,
  and task-tabs as blocks whose instructions are never followed.
- `DELETE /api/memory` ("Forget everything", also a button in the
  dialog) wipes all three tiers, deletes the files on disk, and also
  wipes the semantic-memory tier (`/api/memory/facts`) and any pending
  memory proposals sitting in task checkpoints — otherwise a wiped fact
  could be confirmed back into existence afterwards. Per-chat session
  working memory is not wiped: it is the working state of open chats,
  not durable memory about the operator.

### API

- `POST /api/memory/long/facts` `{text, category?, source?}` → 201;
  `GET /api/memory/long/facts`; `DELETE /api/memory/long/facts/{id}`
- `POST /api/memory/long/skills` `{name, steps, domain?}` → 201;
  `GET /api/memory/long/skills`; `PATCH /api/memory/long/skills/{id}`
  (new steps → version + 1); `POST /api/memory/long/skills/{id}/success`;
  `DELETE /api/memory/long/skills/{id}`
- `POST /api/memory/long/files` `{fileName, base64, summary?, taskId?}`
  → 201 (metadata only in the response); `GET /api/memory/long/files`;
  `GET /api/memory/long/files/{id}/excerpt?max_chars=8000` (scrubbed,
  bounded); `DELETE /api/memory/long/files/{id}`
- `GET /api/memory/long/proposals/{task_id}`;
  `POST /api/memory/long/proposals/{task_id}/confirm`
  `{accepted: [0, 2]}` (`accepted_indices` is also accepted, for
  back-compat — the first UI build sent that name by mistake and the
  button silently did nothing; see the Task 9 review entry in
  CHANGELOG.md)
- `DELETE /api/memory` — forget everything (long tiers + semantic
  facts + pending proposals).

## 2. Checkpoints & resume

A task's checkpoint (instruction, completed steps, working memory,
current URL, tab registry, compaction summary, memory proposals) is
persisted after **every step** (`db.save_task_checkpoint`, called from
the loop), not just at the end.

- `GET /api/task/{id}` exposes the checkpoint: `checkpoint.stepIndex`,
  `lastUrl`, `summary` (the compaction summary, when one exists),
  `proposals` (memory proposals awaiting the operator).
- `POST /api/task/{id}/resume` continues an interrupted task from its
  checkpoint: history is restored, new steps are appended, and the
  loop picks up where it stopped. A task unknown to the backend with no
  checkpoint is a 404; a task in any other state is a 409. The backend
  restores the checkpoint in `run_task` before the loop starts, so
  resume works even after a backend restart (the checkpoint is in
  SQLite, not in RAM). The checkpoint carries the instruction, the
  allow-list, the long-horizon/background flags, the tab registry and
  the terminal outcome (`final_state`), so a resumed task keeps all of
  them — and a completed task restored after a restart reports
  `completed`, not `interrupted`.
- `POST /api/task/{id}/cancel` checkpoints first (only when no
  checkpoint exists yet — the loop's per-step checkpoints are never
  overwritten by a later cancel), then cancels.

### Step caps and history compaction

| Setting | Default | Env |
|---|---|---|
| `MAX_STEPS` (every task) | 50 (was 25) | `PRIVAGENT_MAX_STEPS` / legacy `MAX_STEPS` |
| `MAX_LONG_STEPS` (tasks with `long_horizon=true`) | 100 | `PRIVAGENT_MAX_LONG_STEPS` |
| Compaction interval | every 25 steps, long-horizon only | `PRIVAGENT_COMPACTION_EVERY_STEPS` |

Every 25 steps on a long-horizon task the loop writes a compact
summary into the checkpoint — goal, what is done so far (from the last
5 successful evidences), known failures, what remains — and the next
planning round sees **the summary plus the last 2 history entries**,
not the full history. Token saving is roughly proportional to history
length: at step 50, the planner prompt carries ~2 history entries
instead of ~50, so the per-step prompt cost stops growing with task
length (the summary itself is a few hundred characters and is
rewritten, not appended). The summary lives in working memory, so it
is checkpointed and survives resume. Short tasks are never compacted.

## 3. Multi-tab

A task can own several tabs. `TaskRecord` carries `tabs: [{tab_id,
url, title, active}]`, kept in the checkpoint working memory so the
registry survives resume. The planner prompt lists the task's tabs in
a `<task-tabs source="browser">` block and may only switch to a listed
tab. Tab titles are page-derived, so the block is labelled by source
only, never `trusted="true"` — a page that renames itself to an
instruction is still just a tab title.

- `new_tab` (existing action) registers the new tab in the registry
  and makes it active.
- `switch_tab` (**new action type**, Task 9) is the planner-facing
  tab switch; `select_tab` is the same action, kept for back-compat.
  Both are risk floor `read`. TS↔Python contract parity is pinned by
  `backend/tests/test_contracts.py` (it parses
  `extension/src/contracts.ts`).
- The loop rejects a `switch_tab`/`select_tab` to a tab that is not in
  a non-empty registry **before anything is sent to the extension**
  ("element no longer found"-style honest failure, not a silent no-op).
  The extension enforces the same check per session
  (`TAB_UNAUTHORIZED` when the tab is not the session's primary or a
  child tab). `GET /api/task/{id}` reports the registry in `tabs`
  (the loop mirrors it onto the task record as it runs).
- Observations carry `tab_id`, and the prompt's tab block is rebuilt
  from the registry each round.
- **Policy at task end:** a task's tabs are handed back, not closed.
  The tabs stay open in the (isolated) agent profile for the operator
  to inspect; the agent never closes tabs it did not open, and closing
  the primary tab of an active session is refused by the extension.
  This is a deliberate choice — auto-closing the operator's tabs
  destroyed evidence in early testing — and is the documented
  follow-up if a "close all task tabs" option is wanted.

## 4. Background & scheduled tasks

### Background tasks

`POST /api/task` accepts `background: true` (and `long_horizon:
true`). A background task runs without a live WebSocket client
attached; progress is pollable via `GET /api/task/{id}`, and the
existing progress broadcast reaches any client that reconnects. The
WebSocket is a viewer, not the task's lifeline.

### Scheduler (`backend/app/scheduler.py`)

In-process, SQLite-backed (`scheduled_jobs` table: id, instruction,
schedule, schedule_kind, cron / interval_seconds, next_run_at,
last_run_at, last_task_id, status, allowed_domains, long_horizon).

- `POST /api/scheduled` `{instruction, schedule, allowedDomains?,
  longHorizon?}` → 201; 422 on an unparsable schedule.
  Schedules: `"every 30m"` / `"every 6h"` / `"every 1d"` (60 s – 7 d),
  `"daily 09:00"` (UTC, stored as cron `0 9 * * *`), or a raw 5-field
  cron (validated by `monitoring_scheduler.next_due_at`).
- `GET /api/scheduled`; `DELETE /api/scheduled/{id}` (404 if missing);
  `POST /api/scheduled/{id}/run-now` → `{taskId}`.
- A background runner thread (started in the app lifespan, stopped on
  shutdown; skipped under pytest) ticks every
  `PRIVAGENT_SCHEDULER_TICK_S` (default 30 s), atomically claims due
  jobs (`BEGIN IMMEDIATE`, advances `next_run_at` at claim time so a
  crashed run is retried on schedule, never in a tight loop) and runs
  them as normal background tasks.

**Scheduled runs go through the SAME gates as interactive tasks.**
The job's allow-list (or the global one) is wired onto the created
task; confirmation, takeover, and payment limits apply exactly as
they would for a task the operator typed in. A scheduled run that
needs the operator halts in `awaiting_confirmation` /
`awaiting_takeover` — it never self-approves. The job row's outcome
is visible via `last_task_id` → `GET /api/task/{id}`; the row's
`status` stays `active`/`paused` (the job's enabled state, not the
last outcome).

**Limits (honest):** the runner is in-process only. A restart
re-claims due jobs from SQLite (safe: `next_run_at` was already
advanced at claim time), but two processes running the same database
would double-claim — run one backend. There is no retry backoff
beyond the schedule itself, and no cross-restart execution history
beyond `last_run_at` / `last_task_id`. A real deployment should use
the OS scheduler (systemd timer / cron calling
`POST /api/scheduled/{id}/run-now`) instead; see Task 15.

## 5. What was NOT verified

- No live Chrome multi-tab run: tab registration / switching is
  unit-tested in the loop and the extension (`switch_tab` executor
  tests with a mocked `chrome.tabs`), not against a real browser.
- No real long-horizon task driven end-to-end (no 100-step task has
  been run); compaction is tested at the unit level (summary produced
  at step 25, prompt uses summary + last 2 entries).
- The scheduler has not been tested across a backend restart; the
  re-claim path is reasoned from the claim logic, not exercised.
  Claim atomicity itself IS tested (concurrent claims never
  double-claim), and a confirmation timeout provably never
  self-approves (the loop fails the run instead).
- Resume after a restart IS tested at unit level: the loop checkpoint
  carries the instruction, the checkpoint restores instruction /
  allow-list / tabs / terminal outcome, and a completed task restores
  as `completed`. A full restart-and-resume run against a live backend
  has not been driven end-to-end.
- Recall is keyword-overlap only: synonyms and paraphrases do not
  match ("DEL" matches "DEL", not "New Delhi"). Embeddings are a
  deliberate non-dependency for now.
- No pixel-level vision grounding (unchanged from Task 7) and no
  strict ref-required mode (still a follow-up).

## 6. Operator checklist

1. Step caps: set `PRIVAGENT_MAX_STEPS` / `PRIVAGENT_MAX_LONG_STEPS`
   in `backend/.env` if 50 / 100 are not right (see
   `backend/.env.example`).
2. Add a fact: workspace header → **Memory** → type a fact and save,
   or `POST /api/memory/long/facts`.
3. After a task finishes, open **Memory** and accept the suggestions
   you want kept — nothing is learned silently.
4. Create a scheduled task: `POST /api/scheduled` with
   `{"instruction": "...", "schedule": "daily 09:00"}`; poll it with
   `GET /api/scheduled` and `GET /api/task/{last_task_id}`.
5. Resume an interrupted task: `POST /api/task/{id}/resume`.
6. Forget everything: **Memory** → Forget everything, or
   `DELETE /api/memory`.
