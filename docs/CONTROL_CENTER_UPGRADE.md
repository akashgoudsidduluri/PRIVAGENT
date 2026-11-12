# Control center upgrade — implementation status

This branch is an incremental implementation of the supplied master roadmap, not a completed production release.

## Implemented

- Eleven-view navigation and responsive ink/slate/lime design system, animated overview, readable font pairing, reduced motion, keyboard focus.
- Existing mission input, cancellation, streaming timeline, human approval, screenshots and downloads retain their original live DOM nodes and listeners.
- Overview reads health and persisted conversations. Memory reads/deletes existing preferences. Artifacts aggregates the latest 25 conversations. Settings opens the existing provider dialog.
- Timeline deduplication across replayed events; status messages no longer invent executed steps. Escaped session metadata.
- SQLite monitor storage and atomic baseline/change recording; unchanged observations do not emit duplicate events. Monitors intentionally remain paused without an observation adapter.
- MCP in-process registry with schema validation, mutation default, argument-bound single-use approvals, and sanitized untrusted output. No external transport is enabled.
- Evidence-only six-layer verifier primitive; absent evidence is unknown. Not yet wired to execution or completion decisions.
- Accessible-name, open-shadow traversal, ancestor visibility and ambiguity-safe target recovery helpers. Observer uses names and traversal; injected runtime filters hidden ancestors. Full W3C naming and cross-frame geometry are not implemented.
- Closed-shadow companion mounted only when the executor injects into authorized task tabs. Eleven visual state styles and trusted dashboard button. Action observation receipts update its state. It does not claim mission success or accept page-originated approval commands.

## Still required before the full roadmap is complete

- Browser visual QA: remote browser cannot reach this executor's loopback preview.
- Full verification wiring and authoritative transport/server evidence collection.
- Live semantic tree/bounding box overlay, planner perception contract and occlusion/stale recovery integration.
- Custom dropdown/calendar/upload action contracts and integration fixtures.
- Durable companion service-worker state, navigation hydration, pause/resume, and secure in-page gate binding.
- Monitoring scheduler, authorized browser observer adapter, price/regex/element predicates and WebSocket alert/acknowledgement UI.
- Native Gmail/Calendar/Drive/Slack/GitHub OAuth adapters, credential handling and MCP stdio/SSE transports.
- Five-tier retention and sensitivity policy applied across all existing persistence channels.
- Deep-research pagination/PDF parsing, source-ranking and sentence citation enforcement.
- Twelve-category live benchmark fixtures, runner, and real-world evidence.

## Validation

Run root `npm test` and `npm run build`; extension `npm test`, `npx tsc --noEmit`, `npm run build`; backend `cd backend && python -m pytest tests -q`. The test suite must run from backend so `app` resolves. SOCKS-proxied environments additionally need the `socksio` package for httpx (environment dependency, not an application code change).
