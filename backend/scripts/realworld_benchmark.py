"""Run category missions against a live PrivAgent backend and report observed evidence.

No benchmark task is auto-approved. Missing accounts, extension or sites remain
blocked, and no unexecuted scenario contributes a success or privacy-leak zero.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.benchmarks.metrics import ScenarioResult, calculate_metrics, score_receipts

CATEGORIES = (
    "ecommerce", "travel", "productivity", "email", "document_export", "social_platforms",
    "job_search", "deep_research", "dynamic_ui", "adversarial_security", "fault_recovery",
    "continuous_monitoring",
)
_LEAK = re.compile(r"(?i)(?:[\w.+-]+@[\w.-]+\.[a-z]{2,}|\b(?:ghp|sk-|xoxb-)[a-z0-9_-]{12,}|\b\d{3}-\d{2}-\d{4}\b)")


@dataclass(frozen=True)
class Scenario:
    category: str
    site: str | None
    instruction: str
    requires_extension: bool = True
    requires_connector: str | None = None
    min_actions: int = 1
    min_sources: int = 0
    min_primary_sources: int = 0
    required_formats: tuple[str, ...] = ()
    min_pdf_pages: int = 0
    sentence_citations: bool = False


def load_manifest(path: Path) -> list[Scenario]:
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or [x.get("category") for x in entries] != list(CATEGORIES):
        raise ValueError("manifest must contain each of the 12 categories exactly once in roadmap order")
    allowed = {f.name for f in fields(Scenario)}
    scenarios = []
    for entry in entries:
        if set(entry) - allowed:
            raise ValueError("unknown scenario fields")
        scenario = Scenario(**entry)
        if scenario.site:
            parts = urlsplit(scenario.site)
            if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
                raise ValueError("scenario site must be a credential-free HTTP(S) URL")
        scenarios.append(scenario)
    return scenarios


def _get(client: httpx.Client, path: str) -> dict:
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def _check_goal(scenario: Scenario, evidence: dict, result: ScenarioResult) -> str | None:
    if result.actions < scenario.min_actions:
        return "not enough observed actions"
    if result.correct_actions < scenario.min_actions:
        return "not enough verified actions"
    if scenario.site:
        expected = urlsplit(scenario.site).hostname
        observed = urlsplit(evidence.get("lastUrl") or "").hostname
        if expected != observed:
            return "final observed host differs from scenario site"
    result.source_count = evidence.get("sourceCount")
    result.claim_count = evidence.get("claimCount")
    result.anchored_claims = evidence.get("anchoredClaims")
    if (result.source_count or 0) < scenario.min_sources:
        return "not enough inspected research sources"
    if evidence.get("primarySourceCount", 0) < scenario.min_primary_sources:
        return "not enough primary sources"
    if not set(scenario.required_formats).issubset(evidence.get("artifactFormats") or []):
        return "required deliverable format is missing"
    if evidence.get("pdfPages", 0) < scenario.min_pdf_pages:
        return "PDF page count is below target"
    if scenario.sentence_citations and (not evidence.get("sentenceCount") or
        evidence.get("anchoredSentences") != evidence.get("sentenceCount")):
        return "sentence-level citation anchoring is incomplete"
    return None


def run_scenario(client: httpx.Client, scenario: Scenario, *, timeout_s: int = 180,
                 poll_s: float = 0.5) -> ScenarioResult:
    result = ScenarioResult(scenario.category, scenario.site)
    if not scenario.site:
        result.status, result.reason = "blocked", "scenario site has not been configured"
        return result
    try:
        health = _get(client, "/api/health")
        if scenario.requires_extension and not health.get("extensionConnected"):
            result.status, result.reason = "blocked", "browser extension is not connected"
            return result
        if scenario.requires_connector:
            connectors = _get(client, "/api/connectors")
            if not any(c.get("provider") == scenario.requires_connector and c.get("connected") for c in connectors):
                result.status, result.reason = "blocked", scenario.requires_connector + " connector is not connected"
                return result
        body = {"instruction": scenario.instruction.replace("{site}", scenario.site)}
        response = client.post("/api/task", json=body)
        response.raise_for_status()
        result.task_id = response.json()["taskId"]
        deadline = time.monotonic() + timeout_s
        previous_step = 0
        step_start = time.monotonic()
        task = {}
        while time.monotonic() < deadline:
            task = _get(client, "/api/task/" + result.task_id)
            steps = task.get("stepsDone", 0)
            if steps == previous_step + 1:
                result.step_latencies_ms.append((time.monotonic() - step_start) * 1000)
                step_start = time.monotonic()
            previous_step = max(steps, previous_step)
            if task.get("taskState") in ("completed", "failed", "cancelled", "awaiting_confirmation"):
                break
            time.sleep(poll_s)
        else:
            result.status, result.reason = "failed", "task exceeded benchmark timeout"
            return result
        if task.get("taskState") == "awaiting_confirmation":
            result.status, result.reason = "blocked", "task reached a human approval gate"
            return result
        try:
            evidence = _get(client, "/api/control/benchmark/" + result.task_id)
        except httpx.HTTPError:
            result.status, result.reason = "failed", "checkpoint evidence unavailable"
            return result
        history = evidence.get("history") or []
        score_receipts(history, result)
        text = json.dumps({"summary": task.get("message"), "history": history}, ensure_ascii=False)
        result.privacy_leaks = len(_LEAK.findall(text))
        if result.privacy_leaks:
            result.status, result.reason = "failed", "detected sensitive value in stored evidence"
            return result
        reason = _check_goal(scenario, evidence, result)
        if task.get("taskState") == "completed" and not reason:
            result.status = "completed"
        else:
            result.status, result.reason = "failed", reason or "backend task ended " + str(task.get("taskState"))
    except (httpx.HTTPError, KeyError, ValueError) as error:
        result.status, result.reason = "blocked", type(error).__name__ + " while contacting the backend"
    return result


def run_suite(scenarios: list[Scenario], client: httpx.Client, *, timeout_s: int = 180,
              poll_s: float = 0.5, mode: str = "live_backend") -> dict:
    results = [run_scenario(client, item, timeout_s=timeout_s, poll_s=poll_s) for item in scenarios]
    report = calculate_metrics(results)
    report["mode"] = mode
    report["complete"] = mode == "live_backend" and report["attempted"] == len(CATEGORIES) and all(r.status == "completed" for r in results)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a truthful 12-category PrivAgent benchmark")
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("realworld_scenarios.example.json"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--report", type=Path, default=Path("/tmp/privagent-realworld-benchmark.json"))
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 3600:
        parser.error("timeout must be 1 to 3600 seconds")
    scenarios = load_manifest(args.manifest)
    with httpx.Client(base_url=args.base_url, timeout=10) as client:
        report = run_suite(scenarios, client, timeout_s=args.timeout)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "scenarios"}, indent=2))
    print("Report:", args.report)
    return 0 if report["complete"] and report["privacy_leakage_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
