# Release Readiness Report

Baseline: `8b98ca61b970b0262573cd438a7bb41f8469d3bd`
Validation clone: isolated from the working repository; original repository left untouched.
Environment: Windows, Node `24.22.2`, npm `11.19.1`, Python `3.13.7`, Chrome `156.0.8078.80`.

## Verdict

**READY WITH DOCUMENTED LIMITATIONS**

## Verified

- **Change set is clean**: 17 modified/added source and documentation files plus `demo/`; no missing dependency, no generated build output, no `.env`, and no credential pattern in the tracked diff.
- **Frontend clean install**: `npm ci`, `npm test` (25/25 checks across structure, console, and sanitization), and `npm run build` all pass under Node 24.22.2.
- **Extension clean install**: `npm ci`, Vitest `83/83` (7 files), `tsc --noEmit`, and production build all pass. Dashboard marker guard covers 5173, 4173, and auto-assigned 5190.
- **Backend clean install**: fresh virtualenv, `pip install --require-hashes -r requirements.lock`, `pytest 151/151`, and `benchmark_eval.py` `8/8` (100% completion, 0 privacy violations) all pass.
- **Documented no-key default**: starting from `.env.example` reports `{"provider":"mock","requestedProvider":"mock","providerFallback":false}`.
- **Provider honesty**: health exposes requested/effective provider and fallback reason; explicit `mock` never falls back to a remote provider.
- **Live positive demo**: completed in 9,089 ms with planner 2,059 ms, observe 118 ms, act 994 ms; answered Bengaluru / ₹4,860.00 and saved the note.
- **Clean live repeat**: completed in 60,587 ms with planner 51,437 ms, observe 141 ms, act 421 ms.
- **Negative demo**: ends `failed` / Blocked, not `completed`; Customer ID remains `CUST-1042`, `readOnly` is `true`, no change is saved.
- **Privacy replay**: real `classify_and_redact()` redacted email, phone, card, SSN, API key, password value, session token, URL token, and prose credentials; screenshots withheld.
- **Privacy defect fixed**: the earlier phone pattern missed Indian 5+5 formats (`+91 98765 43210`). A country-prefixed rule and regression test were added; all residual-leak checks now pass.
- **Test counts and docs reconciled**: README and architecture reference 151 backend tests and 83 extension tests.

## Partially Verified

- **Live-provider performance**: two successful runs exist (9.1 s and 60.6 s), but planner latency varied from ~2 s to ~51 s. A three-trial repeatability attempt was unstable: one provider JSON/fallback error and two runs still executing after 240 s. It is preserved in `performance-live-repeatability.json` and excluded from the performance table.
- **Dashboard auto-port protection**: verified on port 5190 with a real Chrome instance; other non-standard ports rely on the same marker check and unit tests, not individual live runs.
- **Gemini provider**: an early live smoke test returned a valid plan, but the final demo battery used Groq. Both providers share the same resolver and validation path.
- **Privacy defense in depth**: verified with a synthetic pipeline replay and unit tests; not verified against real customer data or a live hostile page.

## Blocked

- None.

## Not Tested

- Remote or multi-machine deployment. The frontend and extension target `localhost:8000`.
- Multiple browsers/profiles against one backend. Routing intentionally assumes one connected browser.
- Real third-party production sites, real personal data, and long-duration soak/memory testing.
- User authentication, multi-tenancy, or public network exposure.
- Browsers other than Chrome and operating systems other than Windows.
- Screenshot-enabled mode (`ALLOW_SCREENSHOTS=true`) and its provider-side privacy implications.

## Changes in This Release

- Hash-pinned backend lock file and corrected README setup/Node/backend instructions.
- Provider resolution with truthful requested/effective/fallback health reporting; default `.env.example` is explicit `mock`.
- Port-independent dashboard protection via `data-navigator-dashboard="1"` and tab guards.
- Completion guard rejects self-reported read-only/failed outcomes as success.
- International 5+5 phone redaction and regression test.
- Synthetic demo site, real-browser demo evidence, privacy replay evidence, and measured performance data.
- Consolidated known limitations in `README.md`.

## Reproduce

```bash
npm ci && npm test && npm run build
cd extension && npm ci && npm test && npx tsc --noEmit && npm run build && cd ..
cd backend
python -m venv venv
venv/Scripts/python.exe -m pip install --require-hashes -r requirements.lock
venv/Scripts/python.exe -m pytest tests -q
venv/Scripts/python.exe scripts/benchmark_eval.py
```

The demonstration workflow and evidence paths are in `README.md` under “Demonstration Workflow & Evidence”.
