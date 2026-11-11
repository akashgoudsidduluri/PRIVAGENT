# PrivAgent Memory Architecture — Design Spec

Date: 2026-11-11 · Status: PROPOSED (awaiting approval) · Scope: document only — no code, no migrations, no tables yet.

## 0. Ground rules (binding)

- Continue with SQLite (WAL). No vector DB, no Postgres. Storage stays replaceable behind one interface (`MemoryStore`).
- No second planner, no separate retrieval agent. Memory enters the EXISTING flow between intent classification and planning, and after verification at commit time.
- Memory is context, never authorization. The confirmation gate, permission boundaries, and privacy redaction are unchanged and always apply.
- Minimize stored content: compact facts + task outcomes only. Never store whole browser observations, raw screenshots, or unrestricted LLM output.
- No redesign of what works. `task_checkpoints`, `chat_memory`, `user_preferences`, `messages`, `browser_sessions` keep their current roles; the design adds structure around them.

## 1. Evidence taxonomy (refinement 1)

Facts are never auto-promoted. Every stored fact carries an `evidence_level` and `confidence`:

| evidence_level | Meaning | How it is earned |
|---|---|---|
| `user_stated` | The user wrote/said it | Extracted from instruction or preferences UI; high trust for intent, but NOT proof of world state |
| `observed` | Read from a page/source during execution | Extractor matched against a real observation; single-source |
| `verified` | Independently confirmed | Cross-site agreement, or re-observed consistent on a later run, or provider-API-confirmed |
| `inferred` | Model/heuristic produced it | Regex/model summary without direct observation backing — includes anything from an LLM answer |

Rules:
- A **successful task does not confirm facts** — it only earns `observed` (if extractor-backed) at best. `verified` requires multiplicity (2 sources agree) or temporal stability (same value re-observed) or API confirmation.
- `inferred` facts decay in confidence unless re-earned.
- Contradiction resolution: higher evidence_level wins; ties → higher confidence → newer observation time. Losers are kept with lowered confidence and a `superseded_by` note (audit), not deleted.

## 2. Memory scopes (refinement 2)

Every fact carries a `scope`:

| scope | Home | Cross-chat recall |
|---|---|---|
| `global` | `user_preferences` (user-authored) + `semantic_facts(scope=global)` | ✅ always available |
| `conversation` | `chat_memory` (exists) + `chat_summaries` (new, later phase) | ✅ only within the chat |
| `task` | `task_checkpoints.working_memory` (exists) | ❌ never — dies with task |
| `source` | `semantic_facts(scope=source, source=key)` — per-site/domain knowledge | ✅ only when the current task targets that same source (URL/domain match) |

Cross-chat retrieval literally filters by scope: a `conversation`-scoped fact is unreachable from chat B; a `source`-scoped fact ("flipkart price node layout") is recalled only when the planner is operating on flipkart. Global is the only scope that flows everywhere.

## 3. Target homes per memory category (unchanged verdicts, refined)

- Working → existing `task_checkpoints`. No change.
- Episodic → future table `episodes` (chat_id, instruction, compact outcome, steps-summary, lessons, ts). Compact by design (§5).
- Semantic → future table `semantic_facts(subject, predicate, value, scope, source, evidence_level, confidence, importance, last_confirmed_at, created_at)`. Existing `chat_memory` remains the conversation-scoped KV store; `semantic_facts` is for global + source + durable cross-chat facts.
- Conversational → existing `messages` (raw, capped window) + later `chat_summaries` (rolling rollup).
- Procedural → rows in `semantic_facts` with `subject="strategy"` (refinement 4 below). Promote to own table only if queries diverge.

## 4. Procedural memory (refinement 4)

A strategy fact IS a semantic fact with structure:

```
subject="strategy" predicate="<source>+<goal-class>" value=<short JSON:
  { steps_summary, works_when, failure_modes }>
source="amazon.in|flipkart.com|…"      ← source identity
last_validated_at=<ts>                  ← updated only on a run where it demonstrably worked
success_count=<n> failure_count=<m>
```

Rules:
- A strategy is offered to the planner (as an untrusted hint block) only if: `source` matches the current task source AND `last_validated_at` is fresher than the staleness window (default 14 days) AND `success_count > failure_count`.
- A run where the strategy was applied and the step failed increments `failure_count` and can demote the strategy below the offer threshold.
- Stale/failed strategies are never silently applied; they may be re-validated (offered once more) on explicit new evidence.

## 5. Minimization (refinement 5)

- `commit()` stores: extracted compact facts (≤240 chars each, regex/structured), a 2–4 line outcome summary, a compact steps ledger `[{type, ok}]` (≤25 entries), and lessons strings. NOT: full observations, page text, screenshots, full LLM responses.
- Every candidate fact passes the `privacy` redaction filters BEFORE the write; rejected (still-sensitive) candidates are dropped, not scrubbed-partially.
- The retrieval context given to the planner is bounded (~600 tokens target cap) regardless of store size.

## 6. Retrieval (refinement 3)

Phased, embedding-optional:
1. **Phase R1 — structured SQL:** deterministic recall = user prefs (always, scoped global) + facts for active chat + source facts matching the current target + top-N episodes via indexed SQL (recency + exact/prefix key matches). No FTS yet.
2. **Phase R2 — FTS5** when row counts justify (heuristic: >200 episodes or failed LIKE-hit rate): `episodes_fts`/`facts_fts` virtual tables; query = tokenized instruction + tracked entity values.
3. **Phase R3 — local embeddings, OPTIONAL and only if** a retrieval-eval harness (~30 instruction→expected-fact pairs) demonstrates FTS lexical misses on semantically-similar-but-lexically-different queries. Local index only (e.g. sqlite-vec), behind `MemoryStore` — never an external provider.

Ranking: `score = w1·match + w2·recency_decay + w3·confidence + w4·importance + scope/filter gating (hard)`. Contradictions resolved by §1 rules before assembly.

## 7. Flow in the existing pipeline (no new loop)

```
run_task (unchanged shape)
  ├─ memory.load(instruction, chat_id) → MContext
  │     { global prefs, conversation facts, source facts (target-matched), bounded episodic hints }
  │     — replaces today's scattered initial_memory assembly, same data at M0/M1
  ├─ intent → plan (planner consumes MContext via existing labeled blocks; memory stays context-only)
  ├─ policy/approval → execute → observe → verify (ALL unchanged; memory never authorizes)
  └─ memory.commit(outcome) → validate → redact → dedup/upsert → evidence assignment
        (task success grants at most `observed`; `verified` needs §1 multiplicity/stability)
```

## 8. Lifecycle

create → validate (structured, drop garbage) → redact (pre-write) → dedup (normalized subject+predicate upsert) → classify evidence_level → age/decay → consolidate (chat rollups; episode pruning keeps N=200/chat, summaries forever) → correct (user edit/contradiction → supersession) → delete (user revocation; chat FK cascade for conversation scope) → audit (who/when/source per fact).

Crash recovery: inherited from WAL + per-action checkpoints — no new machinery.

Schema evolution: `PRAGMA user_version` migration runner added in M0 (the one real infra gap in `init_db` today).

## 9. Security & privacy (refinement 6, binding)

1. Memory never authorizes: planner contract unchanged; memory content cannot construct `write`/`sensitive` actions; confirmation gate always fires on its existing triggers regardless of remembered state.
2. Redaction BEFORE persistent writes: every fact through `privacy._redact_text`/`redact_text_urls`; sensitive-marker categories (health/finance/credentials) default to store-but-mask or reject.
3. Redaction BEFORE external LLM calls: memory blocks pass through `classify_and_redact` at prompt assembly (same path as page payloads today).
4. Tokens/credentials never become facts (extractor denylist + redaction).
5. Tests: extend the zero-leak live test to new tables; gate-bypass regression test; scope-leak test (fact in chat A not visible to chat B task).

## 10. Phased implementation (M0/M1 next, after immediate reliability work)

- **M0 — seams only (no behavior change):** extract `recall`/`commit` seams in `run_task`; migration runner; redact-before-write on the existing fact fan-out. Existing suite stays green.
- **M1 — reliability:** `episodes` + `semantic_facts` tables; `MemoryStore` with evidence taxonomy + scopes + dedup + retention; `recall()` wired into the prompt blocks. Acceptance tests §11 (1–7).
- **M2 — retrieval intelligence:** FTS5 + deterministic ranking + scope/contradiction tests + retrieval-eval harness (≥90% hit).
- **M3 — summarization + user controls:** `chat_summaries` rollups; `/api/memory` management endpoints + minimal UI; acceptance tests §11 (8–11).
- **M4 — convergence:** monitoring baselines = source-scoped facts; strategy facts feed procedural memory; monitoring relevance uses the same ranker.

## 11. Acceptance tests (write first, per phase)

M1: (1) fact in chat A → recalled in chat B only if scope=global/source-matched, never conversation-scoped; (2) restart → memory restored; (3) duplicate upsert collapses; (4) planted secret stored redacted or dropped; (5) successful task yields `observed` not `verified`; two agreeing runs → `verified`; (6) remembered "auto-approve" string does NOT bypass confirmation gate; (7) recall context under token cap with pruning.
M2: (8) contradiction resolved to higher evidence/confidence; (9) stale strategy not offered; (10) retrieval-eval ≥90%.
M3: (11) user delete removes from recall immediately; rollup survives message pruning.

## 12. Storage verdict

SQLite sufficient through M3+ (single user; small indexed/FTS workloads; WAL). Future triggers: Postgres only for multi-user; lateral embedding index only per §6 R3 evidence. Interface (`MemoryStore`) keeps both replaceable without rewrites.
