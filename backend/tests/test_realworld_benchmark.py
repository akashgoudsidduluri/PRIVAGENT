import json
from pathlib import Path

import httpx
import pytest

from scripts.realworld_benchmark import CATEGORIES, Scenario, load_manifest, run_suite
from tests.benchmarks.metrics import ScenarioResult, calculate_metrics, score_receipts


MANIFEST = Path(__file__).resolve().parents[1] / "scripts" / "realworld_scenarios.example.json"


def test_manifest_covers_twelve_categories_and_blocks_unconfigured_sites():
    scenarios = load_manifest(MANIFEST)
    assert tuple(s.category for s in scenarios) == CATEGORIES
    assert len({s.category for s in scenarios}) == 12
    assert sum(s.site is None for s in scenarios) == 3
    with pytest.raises(ValueError):
        load_manifest(Path(__file__).resolve())


def test_unobserved_work_never_gets_completion_or_zero_leak_claim():
    blocked = [ScenarioResult(category, None, status="blocked", reason="extension absent") for category in CATEGORIES]
    report = calculate_metrics(blocked)
    assert report["attempted"] == 0
    assert report["coverage_percent"] == 0
    assert report["task_completion_rate_percent"] is None
    assert report["privacy_leakage_count"] is None
    assert report["average_step_latency_ms"] is None


def test_receipt_metrics_count_unknowns_as_unverified_and_stale_recovery():
    result = ScenarioResult("dynamic_ui", "https://example.org", status="completed", privacy_leaks=0)
    score_receipts([
        {"result_ok": False, "error": "STALE_TARGET"},
        {"result_ok": True, "verification": {"verified": True, "layers": {
            "target": "passed", "dispatch": "passed", "surface": "passed", "transport": "unknown",
            "authority": "passed", "goal": "passed"}}},
    ], result)
    report = calculate_metrics([result])
    assert report["correct_action_rate_percent"] == 50
    assert report["six_layer_verification_pass_rate_percent"] == 0
    assert report["stale_element_recovery_rate_percent"] == 100


def test_mock_transport_runs_all_categories_but_labels_it_mocked():
    calls = 0
    def respond(request):
        nonlocal calls
        path = request.url.path
        if path == "/api/health": return httpx.Response(200, json={"extensionConnected": True})
        if path == "/api/connectors": return httpx.Response(200, json=[{"provider": "google", "connected": True}])
        if path == "/api/task":
            calls += 1
            return httpx.Response(200, json={"taskId": f"task-{calls}"})
        if path.startswith("/api/task/"):
            return httpx.Response(200, json={"taskState": "completed", "stepsDone": 1, "message": "Completed"})
        if path.startswith("/api/control/benchmark/"):
            return httpx.Response(200, json={"lastUrl": "https://example.org/result", "history": [
                {"result_ok": True, "verification": {"verified": True, "layers": dict.fromkeys(
                    ("target", "dispatch", "surface", "transport", "authority", "goal"), "passed")}}],
                "sourceCount": 4, "primarySourceCount": 3, "claimCount": 1,
                "anchoredClaims": 1, "sentenceCount": 1, "anchoredSentences": 1,
                "artifactFormats": ["pdf", "docx"], "pdfPages": 5})
        return httpx.Response(404)
    scenarios = [Scenario(category, "https://example.org", "Inspect {site}",
                          requires_connector="google" if category in ("email", "productivity") else None,
                          min_sources=4 if category == "deep_research" else 0,
                          required_formats=("pdf", "docx") if category == "document_export" else (),
                          min_pdf_pages=5 if category == "document_export" else 0,
                          sentence_citations=category == "deep_research") for category in CATEGORIES]
    with httpx.Client(base_url="http://localhost:8000", transport=httpx.MockTransport(respond)) as client:
        report = run_suite(scenarios, client, mode="mock_transport", poll_s=0)
    assert report["mode"] == "mock_transport"
    assert report["complete"] is False
    assert report["attempted"] == 12 and report["task_completion_rate_percent"] == 100
    assert report["correct_action_rate_percent"] == 100
    assert report["six_layer_verification_pass_rate_percent"] == 100
    assert report["privacy_leakage_count"] == 0
    assert calls == 12


def test_live_runner_stops_before_task_creation_without_extension():
    calls = []
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"extensionConnected": False})
    with httpx.Client(base_url="http://localhost:8000", transport=httpx.MockTransport(respond)) as client:
        report = run_suite([Scenario("ecommerce", "https://example.org", "Inspect {site}")], client)
    assert report["attempted"] == 0
    assert report["scenarios"][0]["reason"] == "browser extension is not connected"
    assert calls == ["/api/health"]
