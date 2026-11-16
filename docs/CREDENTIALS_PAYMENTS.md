# Credentials & Payments — Policy, Takeover, Vault (Task 4)

This is the exact policy for anything the agent must never see, anything
that needs your hands in the browser, and anything that is blocked
outright. The code enforces it in three places — the takeover detector
(`backend/app/takeover.py`), the agent loop (`backend/app/agentLoop.py`),
and the planner (`backend/app/planner.py`) — and the tests in
`backend/tests/test_credentials_payments.py` prove the enforcement.

## What the agent can NEVER see

* Full card number (PAN), CVV / CVC / security code.
* Full password.
* OTP / TOTP / one-time code / one-time verification code.

Those values are never typed by the agent, never stored in the vault,
never screenshotted, never sent to a planner, and never written to a
log, a checkpoint, or task history. If a page shows one of those
fields, the correct action is **takeover**, not typing.

## Takeover mode (the core of this task)

When the loop reaches a login / payment / OTP / CAPTCHA / 2FA surface,
it **pauses the agent and hands control to you**:

1. The loop detects the surface on a fresh redacted observation —
   *before* planning on it. Detection order: OTP field first, then
   payment surface, then CAPTCHA, then a sign-in URL.
2. The task state becomes `awaiting_takeover`. The workspace shows a
   **Takeover — You're in Control** banner (amber state badge) with the
   reason and the URL, plus **Resume** / **Cancel** buttons. The landing
   console shows the same state in its summary area (it never opens the
   confirm modal for a takeover — that modal posts to `/confirm`).
3. **While you have the browser, the agent observes nothing.** No
   screenshots, no DOM snapshots, no page text to the planner. The
   extension stops its observer and its screenshots
   (`TAKEOVER_START` / `TAKEOVER_END` over the relay) as defence in
   depth; the backend loop is the authoritative gate. Any observation
   collected during takeover is discarded, not stored, not sent to any
   LLM.
4. You complete the step yourself in the tab (sign in, enter the OTP,
   pay), then click **Resume**. The loop re-observes from scratch; its
   first post-takeover observation carries **no screenshot**, because
   you may have left a password, card, or OTP visible on screen.
5. **Cancel** fails the task. If nobody resumes within
   `PRIVAGENT_TAKEOVER_TIMEOUT_S` (default 600 s), the task fails with
   an honest timeout message. A REST cancel wakes the takeover wait
   immediately instead of waiting out the timeout.
6. At most `PRIVAGENT_MAX_TAKEOVERS_PER_TASK` (default 2) takeovers per
   task, and a surface that was already taken over once is not taken
   over again — the loop proceeds to the normal confirmation gate
   instead of looping on manual steps.
7. Takeover needs a gate. Production (`run_task`) always wires one. A
   caller without one (a test harness driving only the confirmation
   gate) keeps the pre-Task 4 behaviour: the planner plans on, and the
   confirmation gate halts consequential actions. Failing closed there
   would break that legitimate flow.

Endpoints (all behind the local-operator bearer auth):

* `GET /api/task/{id}/takeover` — the active takeover request
  (kind / reason / URL only).
* `POST /api/task/{id}/takeover/resume` — "I finished it, resume".
* `POST /api/task/{id}/takeover/cancel` — cancel the task.
* `GET /api/task/{id}` now also returns `pending_takeover`.

## Credential vault (placeholders, not secrets)

Saved logins live in the Task 2 encrypted store
(`backend/app/vault.py`, on top of `backend/app/secrets.py`):

* Store per-domain credentials in the workspace: header **Vault**
  button → domain, kind, label, username, secret. The secret is sent
  once over the authenticated API, encrypted, and **never shown again**
  — not even to you. The list endpoint returns metadata only
  (domain / kind / label / placeholder).
* The planner / LLM sees **only a placeholder**, e.g.
  `{{CREDENTIAL:example.com:login:personal}}`. Saved placeholders for
  the current page's domain are listed in the planner prompt (metadata
  only). The model may copy one exactly into a `type` action; it must
  never invent one — the planner rejects unknown or malformed
  placeholders, and the loop fails closed again at substitution time.
* The real value is substituted **in the loop, after the confirmation
  gate, just before the action is sent to the extension**. History keeps
  the placeholder form, and any echo of the real value in action
  evidence is scrubbed before it is recorded.
* Cards: the vault stores the **last 4 digits only**. A full card
  number is rejected, not truncated. Logins store username + password.
* OTP / TOTP / one-time-code kinds are **rejected by the vault** — a
  one-time code is never a stored credential. When an OTP field is
  detected, the agent forces takeover instead.
* The agent will **never ask for a password in chat**. You can also use
  Chrome's built-in password manager / autofill instead of the vault —
  the agent never sees those values either (the extension never reads
  field values at observation time).

API: `POST /api/vault/credentials`, `GET /api/vault/credentials`
(metadata only), `DELETE /api/vault/credentials/{domain}/{kind}/{label}`.

## Payments (mock-first, honest)

**No real card processing exists in this task. There is deliberately
no live Stripe integration.** `backend/app/payments.py` provides:

* `validate_payment` — the policy gate: every payment requires
  explicit user approval **and** takeover, regardless of amount, and a
  payment above `PRIVAGENT_MAX_PAYMENT_AMOUNT` (default **5000**,
  currency `PRIVAGENT_PAYMENT_CURRENCY`, default **INR**) is blocked
  outright unless you explicitly raise the limit. No currency
  conversion is performed — the limit applies to the amount as stated,
  in the configured currency; a payment in a different currency is
  treated as above the limit (fail closed) unless you confirm it in
  takeover, where you can see the real amount in the merchant's own
  checkout.
* `PaymentProvider` — the interface a real provider must implement:
  Stripe Link-style one-time virtual card (`create_virtual_card`,
  `approve_virtual_card`, `charge`), scoped to one merchant and amount,
  approved by the operator in the provider's own app, last-4 only to
  the agent. The loop is written against this interface, so a real
  provider can be added later without changing the agent loop.
* `MockPaymentProvider` — the default. It never moves money and never
  charges a card: it mints a clearly-marked mock virtual card (last-4
  `4242`) and its `charge` method raises. A mock must not be able to
  "succeed" at taking money.
* Audit: every payment attempt is logged to
  `backend/data/payments_audit.jsonl` (mode `0600`) with amount,
  merchant domain, last-4, approval id, and outcome only — never card
  details. `GET /api/payments/audit` reads it back;
  `GET /api/payments/config` reports the limit and the mock provider;
  `POST /api/payments/validate` answers whether a payment would be
  allowed (no charge).

To raise the limit, set `PRIVAGENT_MAX_PAYMENT_AMOUNT` in the backend
environment (e.g. `PRIVAGENT_MAX_PAYMENT_AMOUNT=20000`) and restart the
backend. There is no UI for raising it — that is deliberate: raising a
payment limit should be an explicit operator decision, not a click in
the middle of a checkout.

## Blocked outright (no gate, no override)

* **Agent-initiated account creation.** The agent never creates an
  account for you (sign-up / register surface + typing a password or
  submitting the form). Create it yourself in the browser, then ask the
  agent to continue.
* **Permanent account deletion.** The agent never clicks a control
  whose job is to permanently delete / close an account.

Both are name/URL heuristics, not proofs — a human who wants to create
or delete an account does it themselves, outside the agent.

## What is still NOT done (follow-ups)

* **No live Stripe (or any real payment provider) integration.** The
  interface exists and the mock is wired; wiring a real provider with
  real keys is a separate, reviewed task. Until then, every payment is
  completed by the operator in takeover, in the merchant's own
  checkout.
* **No biometric vault trigger.** Unlocking the vault is currently the
  local-operator bearer token (Task 1) plus the encrypted store
  (Task 2). There is no fingerprint / OS-prompt step before a vault
  value is substituted.
* **No measured attack-success rate** for the takeover detector or the
  web-blind checker: the rules are deterministic regex/name heuristics.
  A paraphrased OTP label or an unusual checkout layout can miss
  detection (false negative → the loop's confirmation gate is the
  backstop) or over-trigger (false positive → you click Resume).
* **No cloud VM sandbox** (see Task 3): takeover happens in the local
  isolated agent profile, not in a disposable VM.
