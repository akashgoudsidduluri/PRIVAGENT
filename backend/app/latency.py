"""Per-step timing instrumentation for the agent loop (Task 11).

Records how long each loop phase takes — observe, redact, plan,
checker, confirmation wait, takeover wait, execute, checkpoint —
plus the round-trip counters that matter for latency (planner calls,
observation calls, cache hits, batched steps).

Privacy: a timing entry carries ONLY an action type, durations in
milliseconds, and counts. It never carries a target name, a typed
value, page text, a URL, or any other page-derived content, so it is
safe to keep in memory and to expose via ``GET /api/task/{id}`` even
under zero retention (Task 10) — timings are never persisted to a
checkpoint or a trajectory.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

# Phases the loop measures. confirm_wait / takeover_wait are operator
# time, not loop overhead — they are recorded separately so they never
# inflate the overhead totals.
PHASES = (
    "observe_ms",
    "redact_ms",
    "plan_ms",
    "checker_ms",
    "confirm_wait_ms",
    "takeover_wait_ms",
    "execute_ms",
    "checkpoint_ms",
)

# Loop overhead = everything except the two operator waits.
OVERHEAD_PHASES = (
    "observe_ms",
    "redact_ms",
    "plan_ms",
    "checker_ms",
    "execute_ms",
    "checkpoint_ms",
)


@dataclass
class StepTiming:
    """One executed step's timings. ``action`` is the action type only."""

    step: int
    action: str
    batched: bool = False
    cache: str = "n/a"  # "hit" | "miss" | "n/a" (no observation needed)
    checker_skipped: bool = False
    observe_ms: int = 0
    redact_ms: int = 0
    plan_ms: int = 0
    checker_ms: int = 0
    confirm_wait_ms: int = 0
    takeover_wait_ms: int = 0
    execute_ms: int = 0
    checkpoint_ms: int = 0
    total_ms: int = 0

    @property
    def overhead_ms(self) -> int:
        return sum(getattr(self, p) for p in OVERHEAD_PHASES)

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "action": self.action,
            "batched": self.batched,
            "cache": self.cache,
            "checker_skipped": self.checker_skipped,
            "observe_ms": self.observe_ms,
            "redact_ms": self.redact_ms,
            "plan_ms": self.plan_ms,
            "checker_ms": self.checker_ms,
            "confirm_wait_ms": self.confirm_wait_ms,
            "takeover_wait_ms": self.takeover_wait_ms,
            "execute_ms": self.execute_ms,
            "checkpoint_ms": self.checkpoint_ms,
            "total_ms": self.total_ms,
            "overhead_ms": self.overhead_ms,
        }


class LoopTimings:
    """Accumulates timings for one task run.

    Usage in the loop: :meth:`record` for a phase duration (totals
    accumulate across the whole run, including iterations that never
    execute a step — a failed planner call still cost time), and
    :meth:`flush_step` once per executed step to snapshot the current
    iteration's phases as a :class:`StepTiming`. :meth:`begin_step`
    resets the per-iteration bucket at the top of each iteration.
    """

    def __init__(self) -> None:
        self.started_at = time.perf_counter()
        self.totals: dict[str, int] = {p: 0 for p in PHASES}
        self.steps: list[StepTiming] = []
        self.planner_calls = 0
        self.observation_calls = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.stale_detected = 0
        self.checker_skipped_steps = 0
        self.batched_steps = 0
        self.steps_executed = 0
        self._current: dict[str, int] = {p: 0 for p in PHASES}
        self._current_cache: str = "n/a"
        self._current_checker_skipped: bool = False
        self._step_started: float = self.started_at

    # -- per-iteration bucket -------------------------------------------
    def begin_step(self) -> None:
        """Reset the per-iteration bucket at the top of an iteration."""
        self._current = {p: 0 for p in PHASES}
        self._current_cache = "n/a"
        self._current_checker_skipped = False
        self._step_started = time.perf_counter()

    def record(self, phase: str, ms: int) -> None:
        """Add ``ms`` to a phase — both the run total and the current
        iteration's bucket."""
        key = phase if phase.endswith("_ms") else f"{phase}_ms"
        if key not in self.totals:
            return
        ms = max(0, int(ms))
        self.totals[key] += ms
        self._current[key] += ms

    def note_cache(self, status: str) -> None:
        """Record how this iteration's observation was obtained."""
        if status in ("hit", "miss", "n/a"):
            self._current_cache = status
        if status == "hit":
            self.cache_hits += 1
        elif status == "miss":
            self.cache_misses += 1

    def note_checker_skipped(self) -> None:
        self._current_checker_skipped = True
        self.checker_skipped_steps += 1

    def note_planner_call(self) -> None:
        self.planner_calls += 1

    def note_observation_call(self) -> None:
        self.observation_calls += 1

    def note_stale_detected(self) -> None:
        self.stale_detected += 1

    def flush_step(self, step: int, action_type: str, batched: bool) -> StepTiming:
        """Snapshot the current iteration's bucket as one executed step."""
        entry = StepTiming(
            step=step,
            action=action_type,
            batched=batched,
            cache=self._current_cache,
            checker_skipped=self._current_checker_skipped,
            total_ms=int((time.perf_counter() - self._step_started) * 1000),
            **self._current,
        )
        self.steps.append(entry)
        self.steps_executed += 1
        if batched:
            self.batched_steps += 1
        return entry

    # -- reporting --------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """JSON-safe summary: durations, counts, action types only."""
        overhead = sum(self.totals[p] for p in OVERHEAD_PHASES)
        waits = self.totals["confirm_wait_ms"] + self.totals["takeover_wait_ms"]
        return {
            "planner_calls": self.planner_calls,
            "observation_calls": self.observation_calls,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "stale_detected": self.stale_detected,
            "checker_skipped_steps": self.checker_skipped_steps,
            "batched_steps": self.batched_steps,
            "steps_executed": self.steps_executed,
            "totals_ms": dict(self.totals),
            "overhead_ms": overhead,
            "operator_wait_ms": waits,
            "total_ms": int((time.perf_counter() - self.started_at) * 1000),
            "steps": [s.as_dict() for s in self.steps],
        }


def phase_ms(started: float) -> int:
    """Milliseconds elapsed since ``started`` (a perf_counter value)."""
    return max(0, int((time.perf_counter() - started) * 1000))
