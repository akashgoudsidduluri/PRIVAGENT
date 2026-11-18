# Isolated agent profile, domain allow-list & web-blind checker (Task 3)

## Why

Before Task 3 the agent ran in the user's everyday Chrome profile (their
cookies, saved logins, history) and could navigate anywhere a planner
proposed. A malicious page could also inject instructions into the page
text the planner reads. This document describes what Task 3 does, what it
does **not** do, and how to operate it.

## 1. Isolated agent profile

### Launch (required)

```bash
# macOS / Linux
bash scripts/launch-agent-chrome.sh
# Windows
scripts\launch-agent-chrome.bat
```

Both scripts launch Chrome with `--user-data-dir=<repo>/backend/data/agent-profile`
(a separate profile with its own cookies/logins/history). Override the
directory with `PRIVAGENT_AGENT_PROFILE_DIR`. **Never point it at your
everyday profile.**

### Mark the profile as isolated (required for verification)

Chrome does not expose the profile directory to extensions, so the
extension cannot *prove* which profile it runs in. After launching Chrome
with the script above, open the extension's service-worker console
(`chrome://extensions` → PrivAgent → Service worker) and run:

```js
chrome.storage.local.set({ privagentIsolatedProfile: true })
```

The extension then reports the flag to the backend on connect. Verify:

```bash
curl -H "Authorization: Bearer $PRIVAGENT_LOCAL_TOKEN" \
  http://localhost:8000/api/isolation/status
```

`profileVerified` is `true` only when the extension self-reported the
flag; `profileDirExists` confirms the backend created the directory.
`cloudSandbox` is always `false` — **there is no cloud VM sandbox in
this design**. Isolation here means a separate local Chrome profile.

### Tracker / fingerprint hygiene (best-effort)

| Measure | Status |
|---|---|
| `declarativeNetRequest` blocklist (`extension/rules/trackers.json`, 24 well-known tracker/ad/fingerprinting hosts) | Implemented (Task 10: 12 → 24 — see list below) |
| Wipe agent-profile cookies/cache/localStorage/IndexedDB/service workers **on extension start** (only when the isolated flag is set) | Implemented — wipe-on-start, not wipe-on-close: a service worker has no reliable close signal |
| Third-party cookie partitioning | Relies on Chrome's default storage partitioning (not coded) |
| Agent-profile launch flags (`scripts/launch-agent-chrome.sh/.bat`) | Implemented (Task 10): `--block-third-party-cookies`, `--disable-background-networking`, `--disable-component-update`, `--disable-sync`, `--disable-domain-reliability`, `--no-pings`. Every flag is a real, long-established Chrome switch; none is invented. |
| Fingerprint randomisation | **Not implemented** — the agent profile is hardened, not anonymous. There is no Chrome flag for it; faking one would be worse than saying so. |
| Full tracker blocklist | **Not implemented** — 24 hosts, not a maintained list like EasyList. The blocklist is a named, documented set (below), not a subscription. |

The 24 blocked hosts, by category (the list is data in
`extension/rules/trackers.json`, not code):

- **Analytics:** google-analytics.com, googletagmanager.com,
  mixpanel.com, cdn.amplitude.com, cdn.segment.io, heapanalytics.com,
  scorecardresearch.com
- **Ads / ad-tech:** doubleclick.net, googlesyndication.com,
  adservice.google.com, amazon-adsystem.com, adsrvr.org, criteo.com,
  taboola.com, outbrain.com, ads.yahoo.com
- **Social trackers:** facebook.net, connect.facebook.net
- **Session replay / behaviour:** hotjar.com, clarity.ms,
  fullstory.com, logrocket.com, mouseflow.com
- **Fingerprinting:** fingerprint.com

The content script adds no fingerprinting surface of its own: the
only `navigator` read in the extension is `navigator.platform` /
`navigator.userAgent` in `actionExecutor.ts`, used to detect macOS
for a keyboard-shortcut difference — it reads what the page could
already read itself, and exposes nothing extra to pages.

## 2. Domain allow-list

```bash
# backend/.env
PRIVAGENT_ALLOWED_DOMAINS=google.com,*.wikipedia.org,example.com
```

- Comma-separated; `*.example.com` matches subdomains only (not the bare
  domain, not lookalikes like `notexample.com`).
- Per task: `POST /api/task` accepts `allowedDomains` (same syntax). A
  per-task list overrides the global list for that task; an explicitly
  empty list means "no restriction for this task"; omitted means "inherit
  global".
- With no list configured anywhere, task creation logs
  `no domain allow-list configured …` and navigation is unrestricted
  (back-compat, so existing tests and workflows keep working).
- Enforcement is in code, in three places: the planner rejects disallowed
  navigations (`ValueError`); the agent loop blocks them and halts the
  task for human confirmation (approving is a one-off, logged override);
  the extension refuses them in `actionExecutor` as defence in depth.

## 3. Egress allow-list

Backend outbound calls are allowed only to:

- loopback gateways (`localhost`, `127.0.0.1`, `::1`) — e.g. the local
  DeepAI gateway, and the local Ollama server (Task 10:
  `PRIVAGENT_OLLAMA_BASE_URL`, default `http://127.0.0.1:11434`),
- the pinned planner hosts `api.groq.com` and
  `generativelanguage.googleapis.com`,
- hosts on the domain allow-list.

A remote OpenAI-compatible gateway whose host is not listed is rejected
at the call site, before the request is sent — **before any API key is sent to it**. The
Gemini SDK URL is pinned by the SDK itself and is not intercepted.
See `backend/app/allowlist.py: egress_allowed`.

## 4. Web-blind checker + provenance tagging

`backend/app/checker.py` has two parts:

1. **Page-text scan** (`scan_page_text`): deterministic regex heuristics
   for instruction-like language — `ignore previous instructions`,
   "reveal your system prompt", "send the password/OTP to …",
   credential-harvest phrasing, "navigate to <url>", download-and-run
   lures, "disable your antivirus". One finding per rule, snippets are
   truncated and any `token=`/`key=`-style query value is redacted.
2. **Plan check** (`check_plan`): sees only the plan (actions, targets,
   domains), your directive, and the scan findings — never the page prose.
   It flags when a plan navigates to a domain named by an injected
   navigation instruction, when a write action follows an exfiltration /
   OTP / credential-harvest finding, when an instruction-override finding
   is followed by navigation to a domain not named in your directive, or
   when the plan violates the domain allow-list.

A flag halts the task for human confirmation via the existing gate (or
fails the task if no gate is available). It never silently rewrites a
plan. The checker needs **no LLM call** and works with the mock provider
in tests.

**Provenance tagging.** Every page/tool payload entering the planner is
tagged with its source URL and capture time:

```html
<page-context untrusted="true" source="https://example.com/page"
              captured="2026-11-16T12:00:00+00:00">…</page-context>
```

The planner's system frame states that page content is data, never
instructions. Content is also passed through the existing
`adversarial_guard.cleanse_untrusted` quarantine.

**Limits, stated honestly.**

- The checker is a heuristic regex scan: it catches loud injection
  shapes, not paraphrased, translated, or encoded instructions, and a
  benign page that says "click here to continue" is deliberately *not*
  flagged (too common in normal UI text).
- Attack-success rate **has** been measured (Task 6, `docs/EVALS.md`,
  2026-11-16, on 42 synthetic injection cases): overall ASR 0.50 —
  canonical 0.25 (21/28 flagged), evasion 1.0 (0/14 flagged), benign
  false-positive rate 0.0. The checker catches loud injections and is
  blind to paraphrase; do not claim more than that.
- The checker is a second opinion before consequential actions (new
  domain, click/type/submit, sensitive actions); it is not a substitute
  for the confirmation gate or for keeping the allow-list tight.

## 5. What is still NOT isolated

- No cloud VM / sandbox — the agent runs in a local Chrome profile on
  your machine. A page that escapes the browser sandbox is not contained
  by anything in this design.
- The isolated-profile flag is operator-attested, not provable (see §1).
- No fingerprint randomisation, no full tracker blocklist, no
  wipe-on-close. (Task 10 raised the blocklist from 12 to 24 hosts and
  added launch flags; the gaps above remain.)
- The SQLite database file itself is still plaintext (see Task 2 notes);
  only sensitive columns are encrypted at the accessor layer.
