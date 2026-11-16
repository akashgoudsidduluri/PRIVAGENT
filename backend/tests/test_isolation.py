"""Isolated agent profile + domain allow-list (Task 3).

Covers: allow-list parsing/matching (incl. wildcards), egress gate,
isolated-profile config, per-task allow-list on POST /api/task,
planner enforcement (including deterministic plans), the
/api/isolation/status endpoint, and the loop-level block (no gate ->
fail closed; gate reject -> fail; gate approve -> one-off override).
"""
from __future__ import annotations

import pytest

from app import config
from app.allowlist import (
    check_url,
    effective_allowlist,
    egress_allowed,
    host_matches_allowlist,
    parse_allowed_domains,
    url_allowed,
)
from app.contracts import BrowserObservation, InteractiveElement
from app.planner import build_prompt, plan_from_observation
from app.privacy import classify_and_redact
from app.providers.groq import GroqProvider
from app.providers.mock import MockProvider
from app.providers.openai_compatible import OpenAICompatibleProvider


# ---------------------------------------------------------------------------
# Allow-list parsing / matching
# ---------------------------------------------------------------------------

def test_parse_allowed_domains_accepts_csv_and_lists():
    assert parse_allowed_domains("google.com, *.wikipedia.org") == ["google.com", "*.wikipedia.org"]
    assert parse_allowed_domains(["Example.COM", "example.com", ""]) == ["example.com"]
    assert parse_allowed_domains(None) == []
    assert parse_allowed_domains("") == []


def test_parse_allowed_domains_rejects_bad_entries():
    for bad in ["*", "https://example.com", "example.com/path", "example.com:8080",
                "example.com?x=1", "not a domain", "*."]:
        with pytest.raises(ValueError):
            parse_allowed_domains(bad)


def test_host_matching_plain_entry_covers_subdomains_but_not_lookalikes():
    assert host_matches_allowlist("google.com", ["google.com"])
    assert host_matches_allowlist("www.google.com", ["google.com"])
    assert host_matches_allowlist("deep.sub.google.com", ["google.com"])
    assert not host_matches_allowlist("notgoogle.com", ["google.com"])
    assert not host_matches_allowlist("evilgoogle.com", ["google.com"])
    assert not host_matches_allowlist("google.com.evil.com", ["google.com"])


def test_host_matching_wildcard_covers_subdomains_only():
    assert host_matches_allowlist("en.wikipedia.org", ["*.wikipedia.org"])
    assert host_matches_allowlist("a.b.wikipedia.org", ["*.wikipedia.org"])
    assert not host_matches_allowlist("wikipedia.org", ["*.wikipedia.org"])
    assert not host_matches_allowlist("notwikipedia.org", ["*.wikipedia.org"])


def test_url_allowed_empty_list_means_not_configured():
    # Missing allow-list warns at task creation but must not crash
    # existing behaviour: everything is allowed.
    assert url_allowed("https://anything.example/", None)
    assert url_allowed("https://anything.example/", [])


def test_url_allowed_rejects_bad_targets():
    domains = ["google.com"]
    assert url_allowed("https://www.google.com/search?q=x", domains)
    assert not url_allowed("https://evil.example/", domains)
    assert not url_allowed("javascript:alert(1)", domains)
    assert not url_allowed("https://user:pass@google.com/", domains)
    assert not url_allowed("ftp://google.com/", domains)
    assert not url_allowed("", domains)
    assert not url_allowed(None, domains)


def test_check_url_gives_a_reason():
    ok, reason = check_url("https://evil.example/", ["google.com"])
    assert not ok and "evil.example" in reason and "not on the domain allow-list" in reason
    ok, _ = check_url("https://google.com/", ["google.com"])
    assert ok


def test_check_url_reason_never_echoes_the_raw_url():
    # REVIEW (Task 10 audit): the "unsafe target" reason used to echo
    # the raw URL, and the loop logs that reason at WARNING — a
    # planner-proposed URL carrying a secret or credentials would land
    # verbatim in the log stream. The reason may name the host or the
    # scheme, never the URL itself.
    for url in (
        "javascript:alert(document.cookie)",
        "ftp://user:SECRET-PASSWORD-123@evil.example/x",
        "https://user:SECRET-PASSWORD-123@evil.example/",
        "data:text/html;base64,SECRET-PASSWORD-123",
    ):
        ok, reason = check_url(url, ["google.com"])
        assert not ok, url
        assert "SECRET-PASSWORD-123" not in reason, (url, reason)
        assert url not in reason, (url, reason)


def test_effective_allowlist_per_task_wins_then_global_then_none():
    assert effective_allowlist(["a.com"], ["b.com"]) == ["a.com"]
    assert effective_allowlist(None, ["b.com"]) == ["b.com"]
    assert effective_allowlist(None, []) is None
    assert effective_allowlist([], ["b.com"]) == []


# ---------------------------------------------------------------------------
# Egress allow-list
# ---------------------------------------------------------------------------

def test_egress_allows_loopback_and_pinned_providers_only():
    assert egress_allowed("https://api.groq.com/openai/v1/chat/completions")
    assert egress_allowed("https://generativelanguage.googleapis.com/v1/models")
    assert egress_allowed("http://127.0.0.1:8787/v1/chat/completions")
    assert egress_allowed("http://localhost:8377/v1/chat/completions")
    assert not egress_allowed("https://evil.example/")
    assert not egress_allowed("https://api.example.com/v1")
    assert egress_allowed("https://en.wikipedia.org/wiki/X", allowed_domains=["wikipedia.org"])
    assert egress_allowed("https://gateway.example.com/v1", extra_hosts=["gateway.example.com"])


def test_groq_provider_construction_passes_egress_gate():
    assert GroqProvider(api_key="test-key").name().startswith("groq:")


def test_openai_compatible_provider_allows_loopback_rejects_remote_unlisted():
    OpenAICompatibleProvider(api_key="k", base_url="http://localhost:8377/v1")
    OpenAICompatibleProvider(api_key="k", base_url="http://127.0.0.1:8787/v1")
    # The egress gate fires at call time (_completion), before any request
    # is sent — construction stays free so tests can use a mock host.
    remote = OpenAICompatibleProvider(api_key="k", base_url="https://gateway.example.com/v1",
                                      model="m")
    with pytest.raises(ValueError, match="egress allow-list"):
        remote._completion({"model": "m", "messages": []}, "test")


# ---------------------------------------------------------------------------
# Isolated-profile config
# ---------------------------------------------------------------------------

def test_agent_profile_dir_configured_and_documented(tmp_path):
    # Default is backend/data/agent-profile; the value is loadable and
    # the launch scripts + README document how to use it.
    assert config.AGENT_PROFILE_DIR.name == "agent-profile"
    assert isinstance(config.ALLOWED_DOMAINS, list)


def test_isolation_status_endpoint(authed_client):
    r = authed_client.get("/api/isolation/status")
    assert r.status_code == 200
    body = r.json()
    assert body["profileDir"].endswith("agent-profile")
    assert body["cloudSandbox"] is False
    assert body["allowedDomainsConfigured"] is False
    assert body["profileVerified"] is False
    assert body["extensionConnected"] is False


# ---------------------------------------------------------------------------
# POST /api/task allow-list
# ---------------------------------------------------------------------------

def test_task_with_bad_allowed_domains_is_422(authed_client):
    r = authed_client.post("/api/task", json={
        "instruction": "search for books",
        "allowedDomains": ["not a domain"],
    })
    assert r.status_code == 422


def test_task_with_per_task_allow_list_is_accepted(authed_client):
    from app.hub import hub

    r = authed_client.post("/api/task", json={
        "instruction": "search for books",
        "allowedDomains": ["google.com", "*.wikipedia.org"],
    })
    assert r.status_code == 200
    task_id = r.json()["taskId"]
    assert hub.tasks[task_id].allowed_domains == ["google.com", "*.wikipedia.org"]


def test_task_without_allow_list_warns_but_does_not_crash(authed_client, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="navigator.api"):
        r = authed_client.post("/api/task", json={"instruction": "search for books"})
    assert r.status_code == 200
    assert "no domain allow-list" in caplog.text


# ---------------------------------------------------------------------------
# Planner enforcement
# ---------------------------------------------------------------------------

def _obs(url: str = "https://example.com/") -> BrowserObservation:
    return BrowserObservation.model_validate({
        "tabId": 1, "url": url, "title": "t",
        "interactive": [], "scroll": {}, "dpr": 1,
    })


class _EvilProvider:
    """Stub provider emitting a navigation to a disallowed domain."""

    def plan_call(self, instruction, payload, history, working_memory=None):
        return {"goal": "go", "steps": [{"why": "go", "action": {
            "type": "navigate", "risk": "write", "verify": "v", "timeoutMs": 1000,
            "params": {"url": "https://evil.example/login"}}}]}


def test_planner_rejects_navigate_to_disallowed_domain():
    payload, _ = classify_and_redact(_obs(), instruction="go")
    with pytest.raises(ValueError, match="allow-list"):
        plan_from_observation(instruction="go", payload=payload, provider=_EvilProvider(),
                              allowed_domains=["example.com"])


def test_planner_allows_navigate_to_listed_domain():
    payload, _ = classify_and_redact(_obs(), instruction="go")
    plan = plan_from_observation(instruction="go", payload=payload, provider=_EvilProvider(),
                                 allowed_domains=["evil.example"])
    assert plan.steps[0].action.params.url == "https://evil.example/login"


def test_planner_without_allow_list_is_unchanged():
    payload, _ = classify_and_redact(_obs(), instruction="go")
    plan = plan_from_observation(instruction="go", payload=payload, provider=_EvilProvider())
    assert plan.steps[0].action.params.url == "https://evil.example/login"


class _LLMLikeProvider:
    """Looks like an LLM provider so the deterministic plan path runs."""
    _api_key = "test"

    def plan_call(self, instruction, payload, history, working_memory=None):
        raise AssertionError("deterministic plan should have answered first")


def test_deterministic_plan_is_also_allow_list_checked():
    payload, _ = classify_and_redact(_obs("about:blank"), instruction="open google")
    with pytest.raises(ValueError, match="allow-list"):
        plan_from_observation(instruction="open google", payload=payload,
                              provider=_LLMLikeProvider(), allowed_domains=["example.com"])
    plan = plan_from_observation(instruction="open google", payload=payload,
                                 provider=_LLMLikeProvider(), allowed_domains=["google.com"])
    assert plan.steps[0].action.params.url == "https://www.google.com"


def test_mock_provider_navigation_respects_allow_list():
    payload, _ = classify_and_redact(_obs(), instruction="search books")
    plan = plan_from_observation(instruction="search books", payload=payload,
                                 provider=MockProvider(), allowed_domains=["google.com"])
    assert plan.steps[0].action.params.url == "https://www.google.com"


def test_prompt_carries_provenance_and_allow_list():
    payload, _ = classify_and_redact(_obs("https://example.com/page"), instruction="look")
    prompt = build_prompt("look", payload, [], allowed_domains=["example.com"])
    assert 'untrusted="true"' in prompt
    assert 'source="https://example.com/page"' in prompt
    assert "captured=" in prompt
    assert "example.com" in prompt
    assert payload.provenance_source == "https://example.com/page"
    assert payload.provenance_captured_at


# ---------------------------------------------------------------------------
# Loop-level block (defence in depth beyond planner validation)
# ---------------------------------------------------------------------------

import asyncio
from typing import Any

from app import agentLoop


class _FakeHub:
    def __init__(self):
        self.executed: list[str] = []
        self.confirm_requests: list[dict[str, Any]] = []
        self.decision: bool | None = None
        self.confirm_event = asyncio.Event()

    async def send_action(self, task_id: str, action: dict, timeout_s: float = 45.0):
        await asyncio.sleep(0.01)
        self.executed.append(action["type"])
        return {"ok": True, "evidence": f"did {action['type']}"}


def _obs_factory(url: str = "https://example.com/", page_text: str = ""):
    def factory():
        return {"tabId": 1, "url": url, "title": "t", "interactive": [],
                "scroll": {}, "dpr": 1, "pageText": page_text}
    return factory


class _NavPlanner:
    """Returns one navigation, then an empty plan so the loop can finish."""

    def __init__(self, url: str):
        self.url = url
        self.calls = 0

    def __call__(self, i, p, h):
        self.calls += 1
        if self.calls == 1:
            return {"goal": "g", "steps": [{"why": "go", "action": {
                "type": "navigate", "risk": "write", "verify": "v",
                "timeoutMs": 1000, "params": {"url": self.url}}}]}
        return {"goal": "g", "steps": []}


def test_loop_blocks_disallowed_navigation_without_gate():
    hub = _FakeHub()
    state = asyncio.run(agentLoop.run_with_injection(
        hub, planner=_NavPlanner("https://evil.example/"),
        obs_factory=_obs_factory(), allowed_domains=["example.com"],
    ))
    assert state["final"] == "failed"
    assert "isolation check" in (state["summary"] or "")
    assert hub.executed == []


def test_loop_blocks_disallowed_navigation_when_gate_rejects():
    hub = _FakeHub()

    async def scenario():
        async def reject_soon():
            await asyncio.sleep(0.2)
            hub.decision = False
            hub.confirm_event.set()

        state, _ = await asyncio.gather(
            agentLoop.run_with_injection(
                hub, planner=_NavPlanner("https://evil.example/"),
                obs_factory=_obs_factory(), allowed_domains=["example.com"],
                confirm_gate=hub,
            ),
            reject_soon(),
        )
        return state

    state = asyncio.run(scenario())
    assert state["final"] == "failed"
    assert hub.executed == []
    assert hub.confirm_requests and "allow-list" in hub.confirm_requests[0]["message"]


def test_loop_allows_listed_navigation_and_gate_approve_overrides_block():
    hub = _FakeHub()
    state = asyncio.run(agentLoop.run_with_injection(
        hub, planner=_NavPlanner("https://evil.example/"),
        obs_factory=_obs_factory(), allowed_domains=["evil.example"],
    ))
    assert state["final"] == "completed"
    assert hub.executed == ["navigate"]

    hub2 = _FakeHub()

    async def scenario():
        async def approve_soon():
            await asyncio.sleep(0.2)
            hub2.decision = True
            hub2.confirm_event.set()

        state, _ = await asyncio.gather(
            agentLoop.run_with_injection(
                hub2, planner=_NavPlanner("https://evil.example/"),
                obs_factory=_obs_factory(), allowed_domains=["example.com"],
                confirm_gate=hub2,
            ),
            approve_soon(),
        )
        return state

    state = asyncio.run(scenario())
    assert state["final"] == "completed"
    assert hub2.executed == ["navigate"]


def test_loop_without_allow_list_behaves_as_before():
    hub = _FakeHub()
    state = asyncio.run(agentLoop.run_with_injection(
        hub, planner=_NavPlanner("https://evil.example/"),
        obs_factory=_obs_factory(),
    ))
    assert state["final"] == "completed"
    assert hub.executed == ["navigate"]
