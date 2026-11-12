"""Quantitative metrics from observed tasks and receipts, without invented passes."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class ScenarioResult:
    category: str
    site: str | None
    status: str = "not_run"  # completed, failed, blocked, not_run
    reason: str = ""
    task_id: str | None = None
    actions: int = 0
    correct_actions: int = 0
    six_layer_passes: int = 0
    stale_opportunities: int = 0
    stale_recovered: int = 0
    privacy_leaks: int | None = None
    step_latencies_ms: list[float] = field(default_factory=list)
    source_count: int | None = None
    anchored_claims: int | None = None
    claim_count: int | None = None


def _rate(numerator: int, denominator: int) -> float | None:
    return round(100 * numerator / denominator, 2) if denominator else None


def calculate_metrics(results: list[ScenarioResult]) -> dict[str, Any]:
    attempted = [r for r in results if r.status in ("completed", "failed")]
    actions = sum(r.actions for r in attempted)
    opportunities = sum(r.stale_opportunities for r in attempted)
    latencies = [ms for r in attempted for ms in r.step_latencies_ms]
    known_privacy = [r.privacy_leaks for r in attempted if r.privacy_leaks is not None]
    return {
        "categories": len(results), "attempted": len(attempted), "blocked": sum(r.status == "blocked" for r in results),
        "coverage_percent": _rate(len(attempted), len(results)),
        "task_completion_rate_percent": _rate(sum(r.status == "completed" for r in attempted), len(attempted)),
        "correct_action_rate_percent": _rate(sum(r.correct_actions for r in attempted), actions),
        "six_layer_verification_pass_rate_percent": _rate(sum(r.six_layer_passes for r in attempted), actions),
        "stale_element_recovery_rate_percent": _rate(sum(r.stale_recovered for r in attempted), opportunities),
        "privacy_leakage_count": sum(known_privacy) if len(known_privacy) == len(attempted) and attempted else None,
        "average_step_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "scenarios": [asdict(r) for r in results],
    }


def score_receipts(history: list[dict], result: ScenarioResult) -> None:
    """Only an evidence-backed verified receipt counts as a correct action."""
    stale_pending = False
    for item in history:
        if not isinstance(item, dict):
            continue
        result.actions += 1
        error = item.get("error") or ""
        if "STALE" in str(error).upper() or "DETACHED" in str(error).upper():
            result.stale_opportunities += 1
            stale_pending = True
        receipt = item.get("verification") or {}
        if receipt.get("verified") and item.get("result_ok"):
            result.correct_actions += 1
            if stale_pending:
                result.stale_recovered += 1
                stale_pending = False
        layers = receipt.get("layers") or {}
        if len(layers) == 6 and all(value == "passed" for value in layers.values()):
            result.six_layer_passes += 1
