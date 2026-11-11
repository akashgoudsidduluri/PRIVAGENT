"""Tests for the generic OpenAI-compatible provider (Antseed gateway).

Covers: base-URL validation / host pinning, model discovery payload shapes,
chat completion, planner JSON (with and without response_format support),
honest auth/URL errors, and that the API key never appears in any raised
error or log payload.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.providers.openai_compatible import (
    OpenAICompatibleProvider,
    _model_ids,
    seeded_models,
    validate_base_url,
)
from app.providers.errors import ProviderUnavailable
from app.privacy import classify_and_redact
from app.contracts import BrowserObservation


def _payload(instruction: str = "test instruction"):
    obs = BrowserObservation(
        tab_id=1, url="http://example.com", title="Example",
        interactive=[], scroll={}, dpr=1.0,
    )
    payload, _ = classify_and_redact(obs, instruction=instruction)
    return payload


# ---------------------------------------------------------------------------
# Base URL validation — the credential host-pinning guard
# ---------------------------------------------------------------------------

def test_base_url_normalizes_trailing_slash_and_host():
    assert validate_base_url(" http://localhost:8377/v1 ") == "http://localhost:8377/v1"
    assert validate_base_url("https://api.example.com/") == "https://api.example.com"
    assert validate_base_url("localhost:8377") == "http://localhost:8377"


def test_base_url_rejects_non_http_schemes():
    with pytest.raises(ValueError, match="scheme"):
        validate_base_url("ftp://example.com/v1")
    with pytest.raises(ValueError, match="scheme"):
        validate_base_url("file:///etc/passwd")


def test_base_url_rejects_credentials_query_and_fragment():
    with pytest.raises(ValueError):
        validate_base_url("http://user:pass@example.com/v1")
    with pytest.raises(ValueError):
        validate_base_url("http://example.com/v1?next=/admin")
    with pytest.raises(ValueError):
        validate_base_url("http://example.com/v1#frag")


def test_base_url_rejects_deeper_paths():
    with pytest.raises(ValueError):
        validate_base_url("http://example.com/v1/admin")
    with pytest.raises(ValueError, match="version"):
        validate_base_url("http://example.com/admin")


def test_configure_rejects_bad_base_url_before_any_key_is_stored():
    from app.providers import provider_settings
    with pytest.raises(ValueError):
        provider_settings.configure(
            provider="antseed", base_url="not a url", api_key="k1"
        )
    provider_settings.reset_for_tests()


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------

def test_model_ids_from_openai_style_list():
    data = {"data": [{"id": "glm-5.3-flash"}, {"id": "deepseek-v4-flash"}, {"id": "glm-5.3-flash"}]}
    models = _model_ids(data)
    assert models[:2] == ["glm-5.3-flash", "deepseek-v4-flash"]
    # seeded defaults always present and never duplicated
    assert seeded_models()[0] in models


def test_model_ids_accept_plain_string_list():
    assert "m1" in _model_ids(["m1", "m1", "m2"])


def test_list_models_posts_get_to_models_endpoint(monkeypatch):
    captured: dict = {}

    def fake_get(url, headers=None):
        captured["url"] = url
        captured["headers"] = headers

        class R:
            status_code = 200
            def json(self):
                return {"data": [{"id": "glm-5.3-flash"}]}

        return R()

    p = OpenAICompatibleProvider(api_key="sk-test", base_url="http://localhost:8377/v1")
    monkeypatch.setattr(p._client, "get", fake_get)
    models = p.list_models()
    assert captured["url"] == "http://localhost:8377/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert models[0] == "glm-5.3-flash"


def test_list_models_connection_failure_is_honest(monkeypatch):
    p = OpenAICompatibleProvider(api_key="sk-test", base_url="http://localhost:8377/v1")

    def boom(url, headers=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(p._client, "get", boom)
    with pytest.raises(ProviderUnavailable, match="discovery failed"):
        p.list_models()


# ---------------------------------------------------------------------------
# Chat completion
# ---------------------------------------------------------------------------

def test_chat_call_returns_content_and_sends_bearer_header():
    captured: dict = {}

    def fake_post(url, headers=None, json=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json

        class R:
            status_code = 200
            text = ""
            def json(self):
                return {"choices": [{"message": {"content": "Hello!"}}], "usage": {"total_tokens": 7}}

        return R()

    p = OpenAICompatibleProvider(api_key="sk-live", base_url="http://localhost:8377/v1", model="glm-5.3-flash")
    # bind the fake without monkeypatch (works on method-level fakes too)
    p._client.post = fake_post  # type: ignore[method-assign]
    out = p.chat_call("hi")
    assert out == "Hello!"
    assert captured["url"] == "http://localhost:8377/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-live"
    assert captured["json"]["model"] == "glm-5.3-flash"
    assert p.last_usage.get("total_tokens") == 7


def test_auth_error_message_names_the_key_not_its_value():
    p = OpenAICompatibleProvider(api_key="secret-value-xyz", base_url="http://localhost:8377/v1", model="m")

    class R:
        status_code = 401
        text = '{"error": "invalid api key"}'

    p._client.post = lambda *a, **k: R()  # type: ignore[method-assign]
    with pytest.raises(ProviderUnavailable) as exc:
        p.chat_call("hi")
    assert "401" in str(exc.value)
    assert "secret-value-xyz" not in str(exc.value)


def test_http_error_message_never_contains_the_api_key():
    p = OpenAICompatibleProvider(api_key="secret-value-xyz", base_url="http://localhost:8377/v1", model="m")

    class R:
        status_code = 500
        text = "internal error"

    p._client.post = lambda *a, **k: R()  # type: ignore[method-assign]
    with pytest.raises(ProviderUnavailable) as exc:
        p.chat_call("hi")
    assert "secret-value-xyz" not in str(exc.value)
    assert not exc.value.args or "Bearer" not in str(exc.value.args[0])


# ---------------------------------------------------------------------------
# Planner JSON compatibility
# ---------------------------------------------------------------------------

def test_plan_call_parses_json_plan(monkeypatch):
    p = OpenAICompatibleProvider(api_key="k", base_url="http://localhost:8377/v1", model="glm-5.3-flash")
    plan_json = json.dumps({
        "goal": "open example",
        "steps": [{"why": "go", "action": {
            "type": "navigate", "risk": "write", "verify": "url changed",
            "timeoutMs": 5000, "params": {"url": "https://example.com"},
        }}],
    })

    class R:
        status_code = 200
        text = ""
        def json(self):
            return {"choices": [{"message": {"content": plan_json}}]}

    monkeypatch.setattr(p._client, "post", lambda *a, **k: R())
    res = p.plan_call("open example", _payload(), [])
    assert res["goal"] == "open example"
    assert res["steps"][0]["action"]["type"] == "navigate"


def test_plan_call_retries_without_response_format_when_gateway_rejects(monkeypatch):
    p = OpenAICompatibleProvider(api_key="k", base_url="http://localhost:8377/v1", model="glm-5.3-flash")
    calls: list[dict] = []
    plan_json = json.dumps({"goal": "g", "steps": []})

    class R400:
        status_code = 400
        text = "response_format is not supported"

    class R200:
        status_code = 200
        text = ""
        def json(self):
            return {"choices": [{"message": {"content": plan_json}}]}

    def fake_post(url, headers=None, json=None):
        calls.append(json or {})
        return R400() if "response_format" in (json or {}) else R200()

    monkeypatch.setattr(p._client, "post", fake_post)
    res = p.plan_call("go", _payload(), [])
    assert len(calls) == 2
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]
    assert res["goal"] == "g"
    assert p.fallback_reason and "response_format" in p.fallback_reason


def test_plan_call_non_json_output_raises_plan_invalid(monkeypatch):
    p = OpenAICompatibleProvider(api_key="k", base_url="http://localhost:8377/v1", model="m")

    class R:
        status_code = 200
        text = ""
        def json(self):
            return {"choices": [{"message": {"content": "I cannot do that as prose, sorry."}}]}

    monkeypatch.setattr(p._client, "post", lambda *a, **k: R())
    with pytest.raises(ValueError, match="PLAN_INVALID"):
        p.plan_call("go", _payload(), [])


# ---------------------------------------------------------------------------
# Provider settings / runtime switching
# ---------------------------------------------------------------------------

def test_provider_settings_roundtrip_without_echoing_key(monkeypatch):
    from app.providers import provider_settings
    provider_settings.reset_for_tests()
    state = provider_settings.configure(
        provider="antseed",
        base_url="http://localhost:8377/v1",
        api_key="runtime-secret",
        model="glm-5.3-flash",
    )
    assert state["provider"] == "antseed"
    assert state["keyConfigured"] is True
    assert "runtime-secret" not in json.dumps(state)

    impl = provider_settings.provider_for_runtime()
    assert isinstance(impl, OpenAICompatibleProvider)
    assert impl._api_key == "runtime-secret"
    assert impl._model == "glm-5.3-flash"
    provider_settings.reset_for_tests()


def test_provider_settings_unknown_provider_rejected():
    from app.providers import provider_settings
    provider_settings.reset_for_tests()
    with pytest.raises(ValueError):
        provider_settings.configure(provider="skynet")
    provider_settings.reset_for_tests()


def test_runtime_selection_switches_provider_without_restart(monkeypatch):
    """Check 8: _provider_for_config honors the runtime selection immediately."""
    from app.providers import provider_settings

    class FakeProv:
        def name(self):
            return "fake-mock-provider"

    monkeypatch.setattr(provider_settings, "provider_for_runtime", lambda: FakeProv())
    monkeypatch.setattr(provider_settings, "active", lambda: True)
    from app.agentLoop import _provider_for_config
    assert _provider_for_config().name() == "fake-mock-provider"
    provider_settings.reset_for_tests()


# ---------------------------------------------------------------------------
# main.py API surface
# ---------------------------------------------------------------------------

def test_llm_config_endpoints_never_return_the_key():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.providers import provider_settings

    provider_settings.reset_for_tests()
    client = TestClient(app)
    r = client.post("/api/llm/config", json={
        "provider": "antseed",
        "baseUrl": "http://localhost:8377/v1",
        "apiKey": "endpoint-secret",
        "model": "deepseek-v4-flash",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["keyConfigured"] is True
    assert "endpoint-secret" not in r.text

    r2 = client.get("/api/llm/config")
    assert "endpoint-secret" not in r2.text
    assert r2.json()["keyConfigured"] is True
    provider_settings.reset_for_tests()


def test_llm_config_rejects_invalid_base_url():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.providers import provider_settings

    provider_settings.reset_for_tests()
    client = TestClient(app)
    r = client.post("/api/llm/config", json={"baseUrl": "http://evil.example.com/a/b?x=1", "provider": "antseed"})
    assert r.status_code == 400
    provider_settings.reset_for_tests()


def test_llm_models_reports_discovery_failure_honestly(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.providers import provider_settings

    provider_settings.reset_for_tests()
    provider_settings.configure(
        provider="antseed", base_url="http://localhost:59999/v1",
        api_key="k", model="glm-5.3-flash",
    )

    class Failing:
        def list_models(self):
            raise ProviderUnavailable("connection refused")

    monkeypatch.setattr(provider_settings, "provider_for_runtime", lambda: Failing())
    monkeypatch.setattr(provider_settings, "active", lambda: True)
    client = TestClient(app)
    r = client.get("/api/llm/models")
    data = r.json()
    assert data["discovered"] is False
    assert "refused" in (data["reason"] or "")
    # seeded fallback always present so the dropdown is usable
    assert "glm-5.3-flash" in data["models"]
    provider_settings.reset_for_tests()
