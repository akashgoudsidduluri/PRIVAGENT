# Phase 8 status — 12-category live benchmark

The runner is implemented, but **Phase 8 is not complete**. The readiness run against the local backend on 2026-11-12 reported 12 blocked categories, 0 attempted, and no completion or leakage rate. The Chrome extension is not connected in this workspace; Google account connectors are also unconnected; adversarial, fault-recovery and monitoring test-site URLs have not been configured. No live end-to-end category was claimed as passed.

The runner reads all 12 categories in roadmap order, starts tasks only when their required browser and connector are available, stops at a human approval gate, and scores persisted action receipts. It reports task completion, correct actions, six-layer passes, stale recovery, detected privacy leakage and observed step latency. Unknown denominators remain `null`. Research scenarios require four sources, primary-source coverage and one sourced claim per factual sentence; document export requires both PDF/DOCX and at least five PDF pages. Mock-transport tests exercise the scoring protocol but are labeled as mocked and cannot mark the live suite complete.

Run the live gate on a machine with the frontend, backend, model provider and Chrome extension running:

```bash
cd backend
python scripts/realworld_benchmark.py \
  --manifest scripts/realworld_scenarios.example.json \
  --base-url http://127.0.0.1:8000 \
  --report /tmp/privagent-realworld-benchmark.json
```

Configure the three `null` scenario sites with controlled test pages that contain the specified injection, retry and monitoring signals. Connect Google accounts only if email/calendar coverage is desired. The runner does not auto-approve external writes. Exit code `2` means the live suite did not pass; the JSON report lists each blocker and leaves unmeasured rates null. A fully executed 12/12 report with zero detected leaks is required before Phase 9 can be declared complete.
