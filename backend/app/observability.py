"""Task 13 — observability: one structured JSON log line per executed step.

The loop is a black box while it runs: the only per-step record was
the in-memory ``LoopTimings`` summary (Task 11), readable after the
fact via ``GET /api/task/{id}``. This module adds the live half — a
single JSON line per executed step on the dedicated ``navigator.observe``
logger — reusing the exact ``StepTiming`` object ``flush_step`` already
builds, so there is no second timing system to drift.

Log format (one line per executed step, keys sorted)::

    {"action": "type", "batched": false, "cache": "miss",
     "checker_skipped": false, "checkpoint_ms": 2, "confirm_wait_ms": 0,
     "event": "loop_step", "execute_ms": 0, "observe_ms": 0,
     "outcome": "ok", "overhead_ms": 5, "plan_ms": 201, "redact_ms": 5,
     "step": 1, "task_id": "abc123", "total_ms": 207}

What to grep for::

    navigator.observe        — every observability line (logger name)
    "event": "loop_step"     — the per-step lines
    "outcome": "failed"      — steps the extension reported as failed
    "batched": true          — steps that ran inside a Task 11 batch
    "cache": "hit"           — steps that reused a cached observation

Privacy: a line carries ONLY the task id, the step number, the action
TYPE, durations, and flags. It never carries page text, element names,
typed values, URLs, target ids, or any other page-derived content —
the helper only ever reads the ``StepTiming`` fields and the outcome
boolean, so even a hostile page cannot smuggle its text into the log.
That also makes the lines safe under zero retention (Task 10): they
are the same shape as the timings already exposed via the API.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("navigator.observe")

#: The one event type this module emits. Kept singular on purpose:
#: a second event type is a second surface to document and grep.
EVENT_LOOP_STEP = "loop_step"


def log_step(task_id: str, entry: Any, outcome: str) -> None:
    """Emit one JSON log line for an executed step.

    ``entry`` is the :class:`app.latency.StepTiming` returned by
    ``LoopTimings.flush_step``; ``outcome`` is ``"ok"`` or ``"failed"``.
    Never raises — a logging failure must not fail the task it
    observes.
    """
    try:
        record: dict[str, Any] = {
            "event": EVENT_LOOP_STEP,
            "task_id": str(task_id),
            "outcome": "ok" if outcome == "ok" else "failed",
        }
        record.update(entry.as_dict())
        logger.info(json.dumps(record, sort_keys=True))
    except Exception:
        pass
