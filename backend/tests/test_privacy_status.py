"""Task 12 — Privacy Center endpoint (/api/control/privacy).

Covers: the extended privacy status returns the planner state
(provider / model / local / localOnly), the zero-retention flag, the
24 tracker hosts, and the DPIA summary + link. The tracker list must
stay in parity with the enforcement copy in
extension/rules/trackers.json — the test reads both files and
compares the host sets, so a one-sided edit fails loudly.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.tracker_hosts import TRACKER_HOSTS, tracker_hosts

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_privacy_status_returns_planner_and_flags():
    res = TestClient(app).get("/api/control/privacy")
    assert res.status_code == 200
    payload = res.json()
    # Pre-existing fields still present.
    assert "counters" in payload and "events" in payload
    assert "screenshotsAllowed" in payload
    # Task 12 fields.
    planner = payload["planner"]
    assert planner["provider"] in ("mock", "groq", "gemini", "ollama", "deepai", "openai_compatible")
    assert "model" in planner and "keyConfigured" in planner
    assert planner["local"] is (planner["provider"] == "ollama")
    assert isinstance(planner["localOnly"], bool)
    assert isinstance(payload["zeroRetention"], bool)
    assert isinstance(payload["trackerHosts"], list)
    assert len(payload["trackerHosts"]) == 24
    assert payload["dpia"]["url"].endswith("docs/DPIA.md")
    assert "redact" in payload["dpia"]["summary"].lower()
    # Task 14: connector configuration status (local check only — no
    # live API call). Google is unconfigured in the test env.
    connectors = payload["connectors"]
    assert isinstance(connectors, dict)
    assert connectors["google"]["configured"] is False


def test_tracker_hosts_match_dnr_ruleset():
    rules = json.loads((REPO_ROOT / "extension" / "rules" / "trackers.json").read_text())
    dnr_hosts = set()
    for rule in rules:
        url_filter = (rule.get("condition") or {}).get("urlFilter", "")
        assert url_filter.startswith("||"), url_filter
        dnr_hosts.add(url_filter[2:])
    backend_hosts = {host for host, _ in TRACKER_HOSTS}
    assert backend_hosts == dnr_hosts
    assert len(backend_hosts) == 24
    # Categories are non-empty plain labels; the accessor is JSON-safe.
    assert all(isinstance(h["host"], str) and isinstance(h["category"], str) and h["category"]
               for h in tracker_hosts())
    assert {h["category"] for h in tracker_hosts()} == {
        "Analytics", "Ads", "Social tracker", "Session replay", "Fingerprinting",
    }
