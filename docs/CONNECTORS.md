# Connectors — Gmail / Calendar / Drive auto-routing (Task 14)

PrivAgent can answer Gmail, Google Calendar, and Google Drive tasks
through the native Google API connectors instead of driving the web
UI. This document states the routing rules, the setup, the failure
modes, and the privacy guarantees. The implementation is
`backend/app/connector_router.py` (detection) and
`backend/app/connector_runner.py` (execution).

## Routing rules

`route_instruction(instruction)` returns a `ConnectorRoute` or
`None` (the browser / conversation owns the instruction). Rules, in
order:

1. **Explicit UI navigation wins.** "open Gmail", "go to my inbox",
   "open Google Calendar" — the operator asked to *see* the site, so
   the browser owns it even when a connector could answer the same
   question.
2. **Other subsystems keep their instructions.** Email dispatch
   ("email me this", "send an email to Priya") and email-mission
   setup ("continuously scan my emails") are not connector tasks and
   are never stolen by the router.
3. **Calendar** reuses the existing companion detector
   (`is_calendar_query`): reads ("what's on my calendar tomorrow",
   "do I have any meetings today") route as `list`; a create verb
   ("schedule/create/add/book a meeting/event") routes as `create`
   (mutating — confirmation required).
4. **Gmail reads**: "check my inbox" (no query) is `list`; "find the
   email from X", "show me unread emails", "what did X email me" are
   `search` with the sender/subject/keyword as the query. "unread" /
   "new" translate to the Gmail search operator `is:unread`; "for
   anything new" is a plain inbox check, not a search for those
   words.
5. **Gmail writes** (mutating, confirmation required): "draft an
   email to X (about Y)" → `draft` (recipient + subject extracted);
   "trash/delete the email from X" → `trash` (the concrete email is
   found first, then confirmed by subject + sender).
6. **Drive reads**: "find the Q3 deck in Drive", "search Drive for
   X" → `search` with the file name / keyword as the query.

The routing decision is recorded on the task record (`record.route`,
`{"via", "connector", "operation", "mutating", "reason"}` — names and
reasons only) and shown in the workspace task view ("via Gmail
connector"). It is also mirrored to `GET /api/task/{id}` as
`route`.

## What happens when the connector is not configured

The task **fails honestly**. The runner returns:

> ### Gmail connector is not connected
>
> I can't answer this through the Gmail API because no account is
> connected. To connect it: open the **Connectors** tab in the
> Control Center, sign in with Google OAuth or paste an access
> token, then ask again. Or ask me to open it in the browser
> instead — e.g. 'open Gmail' — and I'll drive the website UI.

It never silently falls back to driving the web UI with the user's
credentials, and it never fabricates connector results. No network
call is attempted when the connector has no token (pinned by test:
the fake connector records zero calls).

## Writes: confirmation first, never self-approved

Draft / trash / calendar-create go through the confirmation gate
before anything executes — the same gate as any other sensitive
action (120 s timeout; a denial or timeout fails the task honestly
with nothing executed). Trash searches first (by message id, not
thread id) and confirms naming the concrete subject + sender.
Calendar creates are a behaviour change from before Task 14: they
used to execute immediately via the companion handler; they now
require confirmation, like every other mutating connector
operation. The confirmation names the actual event (title + the time
that will be booked + attendees) — the runner parses the instruction
first (`parse_calendar_create`), and an instruction that does not
parse as a create fails honestly before any confirmation is shown,
because the handler would otherwise fall through to listing existing
events. Note the named time is shown as written AND as booked: the
handler books tomorrow 10:00 AM UTC when the instruction gives no
usable time, so the two can differ and the operator must see that
before approving. Reads execute directly (read-only, like the MCP
registry's read-only tools).

## Privacy guarantees

- **Connector results never reach the planner.** A connector read
  is answered directly to the operator; the planner is never called
  with connector data. (This satisfies "redaction before planner"
  by construction.) The answer is still scrubbed before return
  (`scrub_persistent` + a labelled-secret pass for "password is
  …" patterns the shared scrubber misses — a bare unlabelled value
  cannot be told apart from ordinary text and is left alone; the
  operator owns the mailbox, so this scrub is defence-in-depth for
  the persisted chat copy, not a leak to a third party.)
- **Egress is pinned.** Every connector call is checked against
  `CONNECTOR_EGRESS_HOSTS` (`gmail.googleapis.com`,
  `www.googleapis.com`, `oauth2.googleapis.com`,
  `accounts.google.com`) before any request is made — Google APIs
  only. A connector's fixed `origin` is the only host ever
  contacted.
- **Audit, metadata only.** Every call appends one `connector_call`
  audit event (connector + operation + outcome); summaries never
  carry content.
- **Zero retention is honoured.** The runner persists nothing; the
  caller persists the answer through `_persist_agent_message`,
  which is already zero-retention-gated (Task 10 review), so under
  `PRIVAGENT_ZERO_RETENTION=true` no connector result is stored.
- **No browser for reads.** A configured connector answers reads
  via the API and never touches the browser. The browser is used
  only for actions the API cannot do (e.g. composing in the Gmail
  UI when the API path is unavailable), and that escalation is
  explicit and gated by the normal confirmation/takeover rules.

## Setup

Connect Google in the Control Center → Connectors tab (OAuth flow
or pasted access token; tokens are stored encrypted — Task 2).
Connection status is shown in the Privacy Center (`GET
/api/control/privacy` → `connectors.google.configured`, a local
check — credentials present in the encrypted store; liveness is
only known when a call is made).

## What is NOT routed

- Email dispatch, email missions, and companion status queries
  ("is my email connected", "what did you delete", "pause email
  monitoring") — none matches a router pattern.
- Research operations (export / resume / refine / new research) —
  the router hook in `run_task` skips them, and ambiguous
  classifications are never routed.
- Anything the connector API cannot do — the browser owns it, with
  the escalation explicit.

## Tests

`tests/test_connector_router.py` (39: detection incl. negatives),
`tests/test_connector_runner.py` (12: fake connectors — reads,
confirmed/denied writes, honest not-connected failure, scrubbed
answers, metadata-only audit), `tests/test_task14_loop.py`
(run_task hook: "check my inbox" with no credentials fails honestly
with zero browser actions).
