# Phase 4: continuous monitoring

- SQLite missions persist schedule, condition, status, baseline hash, and the most recent sanitized state. Existing preview tables migrate in place.
- A FastAPI lifespan loop checks due missions every ten seconds and atomically claims at most five. Interval, UTC cron, and authenticated webhook triggers are supported. Monitors start paused until the user activates them.
- Each run creates a normal task record, executes the browser loop, checks the observed domain, and evaluates text changes, price drops, regex matches, or accessible element appearance. Baseline observations do not alert; unchanged states do not create duplicate events.
- Triggered events retain a short sanitized diff, are broadcast through `MISSION_EVENT_TRIGGERED`, and can be acknowledged. Webhook calls require `PRIVAGENT_MONITOR_WEBHOOK_SECRET` from the environment.

Verification: `backend/tests/test_continuous_monitoring.py` covers persistence, atomic claims, schedules, baseline and duplicate behavior, price/regex/element conditions, redaction, webhook authorization, browser task dispatch, and WebSocket delivery. Existing control-center tests verify migration compatibility. Real monitoring requires a running extension and reachable public target; local tests use a controlled task observation.
