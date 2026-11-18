# UCP support (Task 14)

UCP — the Universal Commerce Protocol — is the open standard for
agentic commerce (Google + Shopify, announced NRF 11 January 2026;
spec on GitHub under `Universal-Commerce-Protocol`, Apache 2.0;
versions 2026-01-11, 2026-01-23, 2026-04-08, 2026-08-25). A
merchant publishes a profile at `/.well-known/ucp` (RFC 8615) with
mandatory `ucp.version`, `ucp.services`, and `ucp.payment_handlers`;
services run over REST / MCP / A2A / embedded bindings; payment goes
through AP2 (separate). Four stages: discovery, negotiation
(`UCP-Agent` header), transaction, payment. Live in Google AI Mode /
Gemini for select US/Canada/Australia merchants.

PrivAgent implements detection + the payment-gated checkout path
against the best-documented version of the spec. **The wire shape
is unvalidated against a real merchant** — no live UCP merchant was
available, and the checkout session/confirm request/response shapes
follow the spec's capability model, not a captured trace. That
boundary is stated here, not hidden. If a real merchant's shape
differs, the single place to update is `execute_ucp_checkout` in
`backend/app/ucp.py`.

## What is supported

- **Detection during observation/discovery.** The extension reads
  `<link rel="ucp">` from the page (same-origin only) into
  `BrowserObservation.ucpProfileUrl`. The loop fetches the profile
  (`GET {origin}/.well-known/ucp`, redirects rejected — the spec
  says profile endpoints MUST NOT use them — 5 s, 64 KiB cap,
  egress-gated, cached per host for the task, dict form cached so
  checkpoints stay JSON-serializable) and validates it:
  mandatory `version` / `services` / `payment_handlers` (bare keys
  accepted too), endpoints must be same-origin `https` with no
  userinfo, version must be one of the four supported versions
  (unknown — newer — versions refuse checkout rather than guess at
  the shape). A profile whose services all fail validation raises
  instead of returning a profile with no services.
- **Standard checkout preferred over driving the checkout UI.**
  When the task is a purchase and the merchant publishes a REST
  checkout service, the planner may emit a `ucp_checkout` step
  (`{merchant, amount, currency}` — amount is a decimal string, no
  float crosses the TS boundary). The prompt carries a
  `<ucp-profile untrusted="true">` block with capabilities only —
  no endpoint URLs.
- **The Task 4 payment path, unchanged.** A UCP checkout:
  1. passes `validate_payment` first (cap
     `PRIVAGENT_MAX_PAYMENT_AMOUNT`, default 5000 INR, + currency —
     approval cannot lift it; blocked before any network call);
  2. creates a checkout session with the merchant's REST checkout
     service (**no payment data** in this call) and verifies the
     returned total matches the planned amount exactly — a mismatch
     aborts before any payment is attempted;
  3. mints + approves a provider virtual card scoped to this
     merchant and amount (last-4 only);
  4. charges it — the mock provider always raises here, honestly;
  5. on success, confirms the session with the mandate reference
     only (never card data).
  It runs **exactly once, never retried** (a payment step must
  never retry blindly), and is executed by the backend, never sent
  to the extension (the extension rejects a `ucp_checkout` step
  outright). It always needs the operator's approval **and**
  takeover (risk must be `sensitive`; the loop forces takeover
  before execution). The audit log records amount / merchant /
  last-4 only.
- **No credentials or payment details to a UCP endpoint except
  through the vault substitution at execution time** — the planner
  sees placeholders only, and the UCP step itself carries no card
  data at all (the card is minted by the provider at step 3).

## What is NOT implemented

- **Non-REST bindings.** Only REST checkout services are
  supported; MCP / A2A / embedded bindings are not.
- **Negotiation / discovery services.** The profile is read for
  its checkout service; product discovery still goes through the
  normal browsing/research path.
- **No live payment provider.** With the default (mock) provider
  every UCP checkout fails honestly at the charge step, after the
  session was created and the total verified. The failure is
  reported ("UCP checkout payment failed: the mock payment
  provider never charges a card"), not hidden, and the task ends
  failed — the loop never retries a payment. Wire a real provider
  (see `docs/CREDENTIALS_PAYMENTS.md`) to move money.
- **No live merchant verification.** Detection, parsing, and the
  payment-gated flow are tested against mocked HTTP
  (`httpx.MockTransport`) and synthetic pages only.

## Files

- `backend/app/ucp.py` — profile parsing/fetching, checkout
  execution, supported versions.
- `backend/app/planner_validation.py` — `ucp_checkout` branch in
  `_validate_step` (sensitive risk, merchant/profile match,
  supported version, REST checkout service, payment policy);
  `backend/app/checker.py` — the checkout counts as a write for
  the exfiltration-lure rule.
- `backend/app/privacy.py` — `ucp_profile_url` rides the redacted
  payload (same-origin only); `backend/app/planner.py` — prompt
  block (capabilities only); `backend/app/agentLoop.py` — profile
  fetch + stash, takeover branch, backend-side dispatch.
- `extension/src/contracts.ts` — `ucp_checkout` action + observation
  field; `extension/src/actionExecutor.ts` — UCP link collection in
  `observe()`, rejection in the dispatch switch.
- `backend/tests/test_ucp.py` (17), `tests/test_task14_loop.py`
  (backend-side dispatch, total-mismatch abort, cap block,
  no-card-data-to-planner), `extension/tests/webmcpUcp.test.ts`.
