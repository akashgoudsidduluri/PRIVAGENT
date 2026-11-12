# Phase 5 verification — Connectors and MCP

Native Gmail, Outlook, Calendar, Drive, Slack, GitHub, and local artifact adapters now route through capability names. The local API lists available routes and requires a task approval bound to exact arguments for mutations. OAuth authorization uses in-memory state, and a connector reports connected only after an access token is returned. MCP discovery uses the official SDK for user-registered stdio or SSE servers; unknown tools default to mutating. Tool output is bounded, redacted, and fenced; audit records store hashes instead of raw arguments.

Verification on the feature branch:

- Backend: `python -m pytest tests -q` — 391 passed.
- Extension: `npm test && npx tsc --noEmit && npm run build` — 127 passed; types and build passed.
- Frontend: `npm test && npm run build` — passed.

The provider tests use mock HTTP responses and an in-memory MCP session. No real account, token, remote server, or external write was exercised. An external mutation returns `effect_verified: false` until the service supplies independent authoritative confirmation. OAuth access tokens are intentionally in process memory; users reconnect after backend restart or token expiry. Native capability execution is exposed through `/api/capabilities`, while agent planning does not yet choose these routes automatically.
