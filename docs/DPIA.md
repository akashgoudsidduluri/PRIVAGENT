# Data Protection Impact Assessment (DPIA) — PrivAgent (NAVIGATOR, SIH 26171)

Date: 2026-11-16. Author: PrivAgent contributors (Task 10).
Status: a working assessment, not a legal opinion. Numbers cited are
measured in this repo (see `docs/EVALS.md`); nothing here is invented.

## 1. What PrivAgent is

A local-first AI browser agent: a Chrome MV3 extension observes the
page you point it at, a FastAPI backend on `127.0.0.1:8000` plans the
next browser action with an LLM, and the extension executes it. The
operator is the data subject for most of the data below (their
credentials, their browsing, their tasks); pages they visit may
contain other people's data (names, contact details in a mailbox or
CRM), which the agent can observe while acting on the operator's
instruction.

## 2. Data inventory

| Data | Where it lives | What leaves the machine |
|---|---|---|
| Page text + interactive elements (post-redaction) | In-memory observation; checkpointed to SQLite (`task_checkpoints`, scrubbed + Fernet-encrypted at rest) | **Sent to the planner provider** on every step — this is the core data flow (see §3) |
| Screenshots | In-memory only; never checkpointed | Sent to the planner only if `ALLOW_SCREENSHOTS=true` (default false) |
| Credentials / payment values | Never in planner payloads: the planner sees `{{CREDENTIAL:domain:kind:label}}` placeholders; real values are substituted just before execution and are stored only in the Task 2 encrypted secret store (`backend/data/secrets.enc.json`, Fernet, PBKDF2 600k) | Never sent to a planner. The vault UI shows last-4 only for cards; OTP kinds are rejected |
| Long-term memory (facts/skills/files) | SQLite, Fernet-encrypted at rest | Recalled facts/skills (top 5 / 800 chars, top 2 skills) enter the planner prompt as background — see §4 |
| Task history / checkpoints | SQLite (`task_checkpoints`), scrubbed + encrypted | Not sent anywhere except as planner history (last 3 steps in the prompt, from a 5-step view; summary + last 2 on long tasks) |
| Audit log | `backend/data/audit.log` (0600), hash-chained | Nowhere — local file, read only via the authenticated API |
| Provider telemetry | Backend logs (model, latency, token counts, fallback reasons) | Nowhere — local logs |
| Browser state | Agent Chrome profile (`backend/data/agent-profile`) | Nowhere by design — separate profile, wiped on start |
| OAuth tokens (Gmail/Calendar/Drive) | Task 2 encrypted store, never plaintext `.env` | Only to the provider's own API, via the connector, on the operator's instruction |

## 3. What leaves the machine, per planner provider

| Provider | Destination | Data sent |
|---|---|---|
| Groq (default when key set) | `api.groq.com` | Redacted page text, AX tree, history, instruction |
| Gemini | `generativelanguage.googleapis.com` | Same |
| DeepAI gateway | Local `127.0.0.1:8787` → its upstream (DeepSeek) | Same — the gateway is local, the model is not |
| Antseed (OpenAI-compatible) | Configured base URL | Same |
| **Ollama (Task 10)** | **Nowhere — `127.0.0.1:11434`, local model** | Nothing leaves the machine |
| Mock | Nowhere | Nothing (deterministic local plans) |

The default chain is deepai → groq → gemini → mock. An operator who
does not want page text leaving the machine must use Ollama with
`PRIVAGENT_PLANNER_LOCAL_ONLY=true` — otherwise a cloud fallback can
engage silently (the fallback is logged, but it is still a fallback).
Cloud providers' own retention policies apply to whatever is sent;
PrivAgent cannot control them. This is the single largest residual
risk (§6).

## 4. Lawful basis & necessity (operator's own use)

PrivAgent processes data only on the operator's instruction
(`POST /api/task`, scheduled jobs the operator created). There is no
background collection, no telemetry sent to the project, no analytics.
Processing is limited to what the task needs: the planner sees a
capped observation (200 elements / 8,000 chars of page text), not
the whole page; files surface as summaries only; screenshots are
off by default.

## 5. Measured risks (not assertions)

From `docs/EVALS.md`, measured 2026-11-16 on hand-written fixtures
(not a sampled benchmark — see the caveats there):

- **Checker ASR 0.50 overall** (42 synthetic injection cases):
  canonical injections 0.25 (21/28 flagged at plan level), **evasion
  1.0 (0/14 paraphrases flagged)**, benign false-positive rate 0.0.
  The checker catches loud injections and is blind to paraphrase.
- **Privacy leak rate 0.2609** — 6 of 23 synthetic PII tokens
  survived redaction into the planner prompt: bare OTP 2/2, labelled
  OTP 1/1, bare password 1/1, postal address 1/1, phone in an element
  value 1/3. Every type the redactor's pattern set claims to handle
  leaks 0; the leaks are all in types the redactor does not cover.
- **No live benchmark has ever been run** (WebArena-Verified, OSWorld
  2.0, PrivacyLens: 12 blocked / 0 attempted at the time of writing).
  The numbers above measure the checker/redactor against fixtures,
  not a live model on live sites.

## 6. Mitigations, mapped to risks

| Risk | Mitigation (task) | Residual |
|---|---|---|
| Secret/PII reaches the planner | Fail-closed redaction (Task 1); credential guard in memory writes (Task 9); vault placeholders (Task 4) | **Leak rate 0.26 on uncovered types** — bare OTP/password/address still leak. The redactor is pattern-based, not semantic |
| Injected page text steers the agent | Web-blind checker (Task 3); provenance tags; prompt framing: recalled memory and tab titles are background, never instruction (Task 9 review, Task 10 step 0) | **Evasion ASR 1.0** — paraphrased injections are not caught. The confirmation gate is the backstop, not the checker |
| Operator's credentials exposed to the agent | Takeover mode: loop stops observing on login/payment/OTP surfaces (Task 4) | Takeover detection is heuristic; a novel login surface may not trigger it |
| Page data sent to a cloud provider | Local Ollama planner + local-only mode (Task 10); egress allow-list (Task 3) | Default chain is still cloud-first; local-only must be opted into. Cloud providers retain what they receive per their own policies |
| Data persists after the task | Zero-retention mode (Task 10): no checkpoints, no screenshots to the planner, pruned history, silenced telemetry | The browser profile still keeps its own state; long-term memory is deliberately kept (operator's own data); the audit log deliberately records that events happened |
| Abuse / misuse of the agent's powers | Confirmation gate for consequential actions (risk-floored); payments always need approval + takeover, capped at 5,000 INR by default (Task 4); scheduled runs never self-approve (Task 9) | The operator can approve a bad action; the gate is only as good as the operator's attention |
| Tracking / fingerprinting by visited pages | 24-host DNR blocklist + agent-profile launch flags (Task 10); isolated profile (Task 3) | No fingerprint randomisation; no full blocklist; the agent is hardened, not anonymous |
| Audit trail tampered with | Hash-chained audit log (Task 10); `GET /api/audit` reports chain health | Local file only — an attacker with filesystem write access could rewrite the whole file, chain included |

## 7. Residual risks, stated plainly

1. **A cloud planner receives redacted page text on every step.**
   Redaction is imperfect (leak rate 0.26 on fixtures) and the
   provider's retention is outside PrivAgent's control. Local-only
   Ollama is the only configuration that eliminates this.
2. **Paraphrased prompt injection is not detected** (evasion ASR
   1.0). The agent relies on the confirmation gate and on the
   operator noticing a strange plan.
3. **The SQLite database file itself is plaintext** (Task 2 note):
   sensitive columns are Fernet-encrypted, but the file is not
   SQLCipher-encrypted. Anyone who can read the file can read
   non-sensitive columns (URLs, step summaries, task metadata).
4. **No live evaluation exists.** All safety numbers are fixture
   measurements. Real-world ASR and leak rate on live sites with a
   live model are unknown.
5. **The operator is the security boundary.** Local auth, the
   confirmation gate, takeover, and the allow-list all assume the
   operator's machine and token are not compromised. There is no
   defence against a compromised operator machine.

## 8. Review

Revisit this DPIA when: a planner provider is added or removed; the
redactor's pattern set changes (re-measure the leak rate); the
checker changes (re-measure ASR); a live benchmark is run; or the
deployment model changes (e.g. a hosted backend, which would void
most of the assumptions above).
