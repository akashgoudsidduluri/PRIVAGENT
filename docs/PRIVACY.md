# Privacy beyond redaction — Task 10

Redaction (Task 1) stops secrets from reaching the planner. This
page covers what happens *after* that: local planning, zero
retention, and the audit log.

## Zero-retention mode

Set in `backend/.env`:

```bash
PRIVAGENT_ZERO_RETENTION=true
```

When on, for every task run while the flag is set:

| Guarantee | How |
|---|---|
| Planner payloads are never written to logs, trajectories, or history beyond the current step | Provider telemetry logging (`log_provider_call`) becomes a no-op; trajectory records are scrubbed summaries; the in-memory history is pruned after each step |
| Page text and screenshots are not persisted | Screenshots are dropped before planning; checkpoints are skipped entirely (see below) |
| In-memory history is pruned to the minimum the loop needs | After each step the history is cut to the last `PRIVAGENT_ZERO_RETENTION_HISTORY_KEEP` entries (default 3), stripped to action type / result / status / scrubbed error — the fields loop detection reads. Evidence and narratives (which can quote page text) are dropped |

**The trade-off is real: checkpoints are skipped entirely, so a
zero-retention task cannot be resumed after a restart** (there is
nothing to resume from). `POST /api/task/{id}/resume` will report
the task as not found after a restart. Long-horizon compaction still
runs in memory, but its summary is never written to disk either.

### What zero retention does NOT cover

- **Your long-term memory store (Task 9)** is a separate, explicit
  store (facts/skills/files you asked to keep). Existing memory is
  unaffected — it is kept and still recalled, and a fact written
  directly (API/UI) while the mode is on still lands — because
  memory is *your* data, deliberately saved. One caveat: a
  zero-retention task produces **no memory write-back**. Proposals
  are stored in the task checkpoint and there is no checkpoint, so
  nothing from such a task is learned; the operator's own user
  message and instruction-derived research metadata still persist
  (they are your own words, not page text).
- **The browser itself** keeps its own state (history, cache,
  cookies) in the agent profile. Zero retention is a backend
  guarantee, not a browser guarantee.
- **The running task's in-memory history** is visible to the
  operator through the live API until the process ends. Zero
  retention is about persistence, not about hiding the task from
  its own operator.
- **The audit log** still records that security-relevant events
  happened (a confirmation, a denial, a takeover). Audit summaries
  are metadata (ids, counts, decisions) and are scrubbed — never
  page text or secrets.

## Audit log

`backend/data/audit.log` — one JSON object per line, append-only,
hash-chained (each entry carries the SHA-256 of the previous entry).
Directory `0700`, file `0600`.

Events recorded: `auth_failure` (bad/missing local token, disallowed
origin/referer), `allowlist_denied` (navigation blocked by the domain
allow-list), `confirmation_approved` / `confirmation_denied`,
`takeover_started` / `takeover_ended`, `payment_attempt` (amount /
merchant / decision only — never card details), `memory_write` /
`memory_delete` / `memory_forget_all`, `scheduler_claim`.

```bash
# Read it (operator token required):
curl -H "Authorization: Bearer $PRIVAGENT_LOCAL_TOKEN" \
  "http://localhost:8000/api/audit?event=confirmation_denied&limit=50"

# Verify the chain (also returned as chainOk / brokenIndex by the endpoint):
#   python -c "from app.audit import verify_chain; print(verify_chain())"
```

Tampering is detected: editing, deleting (other than truncating the
tail), or reordering any line breaks the hash chain, and
`verify_chain()` returns the index of the first broken entry. Note
the honest limit: the log is a local file, not a remote tamper-proof
store — an attacker with write access to `backend/data/` could
rewrite the whole file including its chain, and deleting the final
lines leaves a shorter but still-valid chain, so tail truncation is
not detectable by the chain alone. The log proves *later
modification* of surviving entries, not *who* modified it, and it
assumes the operator's filesystem is trusted (same assumption as the
encrypted secret store).

Appending never raises: if the log cannot be written, the failure is
logged and the caller's flow continues — a broken audit trail must
not break the task it records.

## Local planner

See `docs/LOCAL_PLANNER.md`: run the planner on your own machine
with Ollama (`PRIVAGENT_PLANNER=ollama`,
`PRIVAGENT_PLANNER_LOCAL_ONLY=true`).

## Tracker / fingerprint hygiene

See `docs/ISOLATION.md` §1: 24-host DNR blocklist, agent-profile
launch flags, and the remaining gaps (no fingerprint randomisation,
no full blocklist).
