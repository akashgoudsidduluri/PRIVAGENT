"""Task 10 — Local Ollama planner.

No real Ollama server is used: the provider is tested against a fake
HTTP client that mimics Ollama's /api/chat responses. Covers plan
generation, the local-only failure mode (fail honestly, never fall
back to cloud), timeout / malformed-response handling, the loopback
guard, and provider selection (explicit ollama, local-first,
local-only, runtime selection).
"""
from __future__ import annotations

import json

import httpx
import pytest

from app import agentLoop
from app.contracts import BrowserObservation
from app.privacy import classify_and_redact
from app.providers.errors import ProviderUnavailable
from app.providers import ollama as ollama_mod
from app.providers import provider_settings
from app.providers.ollama import OllamaProvider


def _payload(url: str = "https://example.com/"):
    obs = BrowserObservation.model_validate({
        "tabId": 1, "url": url, "title": "T",
        "interactive": [], "scroll": {}, "dpr": 1,
    })
    return classify_and_redact(obs, instruction="do the thing")[0]


class FakeResp:
    def __init__(self, status_code: int = 200, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeClient:
    """Minimal httpx.Client stand-in: records calls, returns scripted responses."""
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append((url, json or {}))
        return self.handler(url, json or {})


def _chat_payload(content: str) -> dict:
    return {"model": "qwen2.5:7b", "message": {"role": "assistant", "content": content},
            "done": True}


def _plan_provider(handler=None):
    client = FakeClient(handler or (lambda url, body: FakeResp(200, _chat_payload(
        '{"goal": "do the thing", "steps": []}'))))
    return OllamaProvider(client=client), client


# --- Plan generation -------------------------------------------------------

def test_plan_call_returns_parsed_json_plan():
    provider, client = _plan_provider()
    plan = provider.plan_call("do the thing", _payload(), [])
    assert plan == {"goal": "do the thing", "steps": []}
    url, body = client.calls[0]
    assert url == "http://127.0.0.1:11434/api/chat"
    assert body["model"] == "qwen2.5:7b"
    assert body["stream"] is False
    assert body["format"] == "json"
    assert provider.name() == "ollama:qwen2.5:7b"


def test_plan_call_accepts_json_in_markdown_fence():
    provider, _ = _plan_provider(lambda url, body: FakeResp(200, _chat_payload(
        'Here is the plan:\n```json\n{"goal": "g", "steps": []}\n```')))
    assert provider.plan_call("do the thing", _payload(), []) == {"goal": "g", "steps": []}


def test_chat_call_returns_text():
    provider, client = _plan_provider()
    assert provider.chat_call("hello") == '{"goal": "do the thing", "steps": []}'
    assert client.calls[0][1]["messages"][-1] == {"role": "user", "content": "hello"}


# --- Failure modes -----------------------------------------------------------

def test_unreachable_ollama_raises_provider_unavailable(monkeypatch):
    monkeypatch.setattr("app.config.PLANNER_LOCAL_ONLY", True)
    def _boom(url, body):
        raise httpx.ConnectError("connection refused", request=None)
    provider, _ = _plan_provider(_boom)
    with pytest.raises(ProviderUnavailable, match="unreachable"):
        provider.plan_call("do the thing", _payload(), [])


def test_timeout_raises_provider_unavailable(monkeypatch):
    monkeypatch.setattr("app.config.PLANNER_LOCAL_ONLY", True)
    def _slow(url, body):
        raise httpx.TimeoutException("timed out", request=None)
    provider, _ = _plan_provider(_slow)
    with pytest.raises(ProviderUnavailable, match="unreachable"):
        provider.plan_call("do the thing", _payload(), [])


def test_non_200_raises_provider_unavailable(monkeypatch):
    monkeypatch.setattr("app.config.PLANNER_LOCAL_ONLY", True)
    provider, _ = _plan_provider(lambda url, body: FakeResp(500, text="model not found"))
    with pytest.raises(ProviderUnavailable, match="HTTP 500"):
        provider.plan_call("do the thing", _payload(), [])


def test_malformed_response_is_plan_invalid():
    provider, _ = _plan_provider(lambda url, body: FakeResp(200, _chat_payload(
        "not json at all, just prose")))
    with pytest.raises(ValueError, match="PLAN_INVALID"):
        provider.plan_call("do the thing", _payload(), [])


def test_empty_response_raises_provider_unavailable(monkeypatch):
    monkeypatch.setattr("app.config.PLANNER_LOCAL_ONLY", True)
    provider, _ = _plan_provider(lambda url, body: FakeResp(200, {"model": "qwen2.5:7b"}))
    with pytest.raises(ProviderUnavailable, match="empty response"):
        provider.plan_call("do the thing", _payload(), [])


def test_non_loopback_base_url_refused():
    with pytest.raises(ProviderUnavailable, match="must be loopback"):
        OllamaProvider(base_url="http://192.168.1.5:11434")
    with pytest.raises(ProviderUnavailable, match="must be loopback"):
        OllamaProvider(base_url="https://ollama.example.com")


@pytest.mark.parametrize("url", [
    # DNS tricks / alternate spellings that still resolve to loopback
    # (or to an attacker host) — the check is an exact hostname match,
    # so all of these must be refused, never "helpfully" resolved.
    "http://127.0.0.1.nip.io:11434",
    "http://127.0.0.1.nip.io",
    "http://0.0.0.0:11434",
    "http://foo.localhost:11434",
    "http://sub.localhost",
    "http://2130706433:11434",      # decimal for 127.0.0.1
    "http://0177.0.0.1:11434",      # octal for 127.0.0.1
    "http://127.0.0.1@evil.example:11434",  # userinfo trick
    "http://[::ffff:127.0.0.1]:11434",      # IPv4-mapped IPv6
    "http://localhost.:11434",      # trailing dot
    "http://[::1%25lo0]:11434",     # zone id
])
def test_loopback_dns_tricks_refused(url):
    with pytest.raises(ProviderUnavailable, match="must be loopback"):
        OllamaProvider(base_url=url)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:11434",
    "http://localhost:11434",
    "http://[::1]:11434",
    "http://LOCALHOST:11434",
])
def test_loopback_spellings_accepted(url):
    provider = OllamaProvider(base_url=url)
    assert provider._chat_url().endswith("/api/chat")


def test_loopback_egress_passes():
    # 127.0.0.1 is a loopback egress host (Task 3) — no duplicate entry needed.
    from app.allowlist import egress_allowed
    assert egress_allowed("http://127.0.0.1:11434/api/chat") is True
    assert egress_allowed("http://localhost:11434/api/chat") is True


# --- Local-only vs fallback --------------------------------------------------

def test_local_only_never_falls_back_to_cloud(monkeypatch):
    monkeypatch.setattr("app.config.PLANNER_LOCAL_ONLY", True)
    def _boom(url, body):
        raise httpx.ConnectError("connection refused", request=None)
    provider, _ = _plan_provider(_boom)
    with pytest.raises(ProviderUnavailable, match="unreachable"):
        provider.plan_call("do the thing", _payload(), [])


def test_local_only_fallback_provider_never_invoked(monkeypatch):
    # Stronger than the exception above: prove the fallback path is
    # never even entered in local-only mode (a fallback that silently
    # returned a plan would be a cloud leak, not just a wrong error).
    monkeypatch.setattr("app.config.PLANNER_LOCAL_ONLY", True)
    calls: list[str] = []

    def _boom(url, body):
        raise httpx.ConnectError("connection refused", request=None)
    provider, _ = _plan_provider(_boom)

    def _spy():
        calls.append("fallback")
        raise AssertionError("fallback must never be called in local-only mode")

    monkeypatch.setattr(provider, "_fallback_provider", _spy)
    with pytest.raises(ProviderUnavailable, match="unreachable"):
        provider.plan_call("do the thing", _payload(), [])
    assert calls == []


def test_chat_call_local_only_raises(monkeypatch):
    monkeypatch.setattr("app.config.PLANNER_LOCAL_ONLY", True)
    def _boom(url, body):
        raise httpx.ConnectError("connection refused", request=None)
    provider, _ = _plan_provider(_boom)
    with pytest.raises(ProviderUnavailable, match="unreachable"):
        provider.chat_call("hello")


def test_non_local_only_falls_back_to_mock(monkeypatch):
    monkeypatch.setattr("app.config.PLANNER_LOCAL_ONLY", False)
    monkeypatch.setattr("app.config.PROVIDER", "mock")
    provider_settings.reset_for_tests()
    def _boom(url, body):
        raise httpx.ConnectError("connection refused", request=None)
    provider, _ = _plan_provider(_boom)
    plan = provider.plan_call("do the thing", _payload(), [])
    assert provider.fallback_reason is not None
    assert "falling back" in provider.fallback_reason
    assert isinstance(plan, dict) and "steps" in plan


# --- Provider selection ------------------------------------------------------

def test_provider_for_config_explicit_ollama(monkeypatch):
    provider_settings.reset_for_tests()
    monkeypatch.setattr("app.config.PROVIDER", "ollama")
    monkeypatch.setattr("app.config.PLANNER_LOCAL_ONLY", False)
    monkeypatch.setattr("app.config.PLANNER_LOCAL_FIRST", False)
    provider = agentLoop._provider_for_config()
    assert isinstance(provider, OllamaProvider)


def test_provider_for_config_local_first_prefers_ollama(monkeypatch):
    provider_settings.reset_for_tests()
    monkeypatch.setattr("app.config.PROVIDER", "groq")
    monkeypatch.setattr("app.config.PLANNER_LOCAL_ONLY", False)
    monkeypatch.setattr("app.config.PLANNER_LOCAL_FIRST", True)
    provider = agentLoop._provider_for_config()
    assert isinstance(provider, OllamaProvider)


def test_provider_for_config_default_chain_unchanged(monkeypatch):
    # Without any ollama flag, the default chain is untouched: with no
    # keys configured the provider is still the mock.
    provider_settings.reset_for_tests()
    monkeypatch.setattr("app.config.PROVIDER", "mock")
    monkeypatch.setattr("app.config.PLANNER_LOCAL_ONLY", False)
    monkeypatch.setattr("app.config.PLANNER_LOCAL_FIRST", False)
    from app.providers.mock import MockProvider
    assert isinstance(agentLoop._provider_for_config(), MockProvider)


def test_local_only_rejects_cloud_selection(monkeypatch):
    provider_settings.reset_for_tests()
    monkeypatch.setattr("app.config.PLANNER_LOCAL_ONLY", True)
    monkeypatch.setattr("app.config.PROVIDER", "groq")
    with pytest.raises(ProviderUnavailable, match="local-only mode"):
        agentLoop._provider_for_config()


def test_local_only_allows_ollama(monkeypatch):
    provider_settings.reset_for_tests()
    monkeypatch.setattr("app.config.PLANNER_LOCAL_ONLY", True)
    monkeypatch.setattr("app.config.PROVIDER", "ollama")
    assert isinstance(agentLoop._provider_for_config(), OllamaProvider)


def test_local_only_guards_stale_runtime_selection(monkeypatch):
    # A runtime selection of groq made before local-only was turned on
    # must not silently send data to the cloud.
    provider_settings.reset_for_tests()
    monkeypatch.setattr("app.config.PLANNER_LOCAL_ONLY", True)
    monkeypatch.setattr("app.config.PROVIDER", "ollama")
    provider_settings.configure(provider="groq")
    try:
        with pytest.raises(ProviderUnavailable, match="local-only mode"):
            agentLoop._provider_for_config()
    finally:
        provider_settings.reset_for_tests()


def test_runtime_selection_ollama_needs_no_key():
    provider_settings.reset_for_tests()
    try:
        state = provider_settings.configure(provider="ollama", model="qwen2.5:7b")
        assert state["provider"] == "ollama"
        assert state["keyConfigured"] is True  # no key exists for Ollama
        provider = provider_settings.provider_for_runtime()
        assert isinstance(provider, OllamaProvider)
        assert provider.name() == "ollama:qwen2.5:7b"
    finally:
        provider_settings.reset_for_tests()


def test_skip_ollama_fallback_guard(monkeypatch):
    # The Ollama provider's own fallback asks for the normal chain
    # without re-entering Ollama (no infinite recursion).
    provider_settings.reset_for_tests()
    monkeypatch.setattr("app.config.PROVIDER", "mock")
    monkeypatch.setattr("app.config.PLANNER_LOCAL_ONLY", False)
    monkeypatch.setattr("app.config.PLANNER_LOCAL_FIRST", True)
    from app.providers.mock import MockProvider
    assert isinstance(agentLoop._provider_for_config(skip_ollama=True), MockProvider)


def test_privagent_planner_alias_wins_over_navigator_provider(monkeypatch):
    # PRIVAGENT_PLANNER is the operator-facing alias for
    # NAVIGATOR_PROVIDER; when both are set the alias wins (config.py).
    import importlib

    from app import config

    monkeypatch.setenv("NAVIGATOR_PROVIDER", "groq")
    monkeypatch.setenv("PRIVAGENT_PLANNER", "ollama")
    try:
        importlib.reload(config)
        assert config.PROVIDER == "ollama"
        monkeypatch.delenv("PRIVAGENT_PLANNER")
        importlib.reload(config)
        assert config.PROVIDER == "groq"
    finally:
        monkeypatch.delenv("NAVIGATOR_PROVIDER", raising=False)
        monkeypatch.delenv("PRIVAGENT_PLANNER", raising=False)
        importlib.reload(config)
