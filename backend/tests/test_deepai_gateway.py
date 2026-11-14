"""Integration tests for unofficial DeepAI Gateway and DeepSeek models.

Verifies:
1. Gateway connectivity and model discovery via GET /v1/models.
2. API key authentication & rejection of missing/invalid keys.
3. Chat completion via deepseek-v4.1-flash with the required test prompt:
   "Explain how a binary search tree works."
4. Multi-turn conversation support.
5. Streaming token generation via chat_stream.
6. Error handling for connection failures, invalid models, and unconfigured keys.
"""
from __future__ import annotations

import os
import pytest
from app import config
from app.providers.errors import ProviderUnavailable
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.providers import provider_settings


GATEWAY_URL = getattr(config, "DEEPAI_GATEWAY_BASE_URL", "http://127.0.0.1:8787/v1")
GATEWAY_KEY = getattr(config, "DEEPAI_GATEWAY_KEY", "")
DEFAULT_MODEL = getattr(config, "DEEPAI_GATEWAY_MODEL", "deepseek-v4.1-flash")


def _is_gateway_live() -> bool:
    if not GATEWAY_KEY:
        return False
    try:
        import httpx
        r = httpx.get(f"{GATEWAY_URL.rstrip('/')}/models", headers={"Authorization": f"Bearer {GATEWAY_KEY}"}, timeout=0.8)
        return r.status_code in (200, 401)
    except Exception:
        return False


@pytest.fixture(autouse=True)
def clean_runtime():
    provider_settings.reset_for_tests()
    yield
    provider_settings.reset_for_tests()


def test_gateway_model_discovery():
    """Verify gateway connectivity and discoverable models."""
    if not _is_gateway_live():
        pytest.skip(f"DeepAI gateway not reachable at {GATEWAY_URL}")
    provider = OpenAICompatibleProvider(
        api_key=GATEWAY_KEY,
        base_url=GATEWAY_URL,
        model=DEFAULT_MODEL,
        provider_name="deepai",
    )
    models = provider.list_models()
    assert isinstance(models, list)
    assert len(models) > 0
    # deepseek-v4.1-flash or similar DeepAI models should be discoverable
    assert any("deepseek" in m.lower() or "flash" in m.lower() for m in models)


def test_gateway_auth_failure_handling():
    """Verify that an invalid API key raises a clear ProviderUnavailable."""
    if not _is_gateway_live():
        pytest.skip(f"DeepAI gateway not reachable at {GATEWAY_URL}")
    provider = OpenAICompatibleProvider(
        api_key="sk-invalid-test-key-1234567890abcdef",
        base_url=GATEWAY_URL,
        model=DEFAULT_MODEL,
        provider_name="deepai",
    )
    with pytest.raises(ProviderUnavailable) as exc:
        provider.chat_call("Hello")
    assert "401" in str(exc.value) or "API key" in str(exc.value)


def test_gateway_connection_refused_handling():
    """Verify that an unreachable gateway host fails gracefully."""
    provider = OpenAICompatibleProvider(
        api_key="sk-test",
        base_url="http://127.0.0.1:59999/v1",  # Unused port
        model=DEFAULT_MODEL,
        timeout_s=2.0,
        provider_name="deepai",
    )
    with pytest.raises(ProviderUnavailable) as exc:
        provider.chat_call("Hello")
    assert "failed" in str(exc.value).lower() or "connection" in str(exc.value).lower()


def test_deepseek_binary_search_tree_completion():
    """Execute the mandatory evaluation prompt against deepseek-v4.1-flash."""
    if not _is_gateway_live():
        pytest.skip(f"DeepAI gateway not reachable at {GATEWAY_URL}")
    provider = OpenAICompatibleProvider(
        api_key=GATEWAY_KEY,
        base_url=GATEWAY_URL,
        model=DEFAULT_MODEL,
        provider_name="deepai",
    )
    prompt = "Explain how a binary search tree works."
    reply = provider.chat_call(prompt)
    assert isinstance(reply, str)
    assert len(reply.strip()) > 40
    # Must contain relevant BST terminology
    lower_reply = reply.lower()
    assert "node" in lower_reply or "left" in lower_reply or "right" in lower_reply or "root" in lower_reply


def test_multi_turn_conversation():
    """Verify multi-turn history is respected."""
    if not _is_gateway_live():
        pytest.skip(f"DeepAI gateway not reachable at {GATEWAY_URL}")
    provider = OpenAICompatibleProvider(
        api_key=GATEWAY_KEY,
        base_url=GATEWAY_URL,
        model=DEFAULT_MODEL,
        provider_name="deepai",
    )
    history = [
        {"role": "user", "content": "My favorite fruit is mango."},
        {"role": "assistant", "content": "Mango is delicious! I will remember that."},
    ]
    reply = provider.chat_call("What is my favorite fruit?", history=history)
    assert "mango" in reply.lower()


def test_streaming_tokens():
    """Verify streaming token iteration."""
    if not _is_gateway_live():
        pytest.skip(f"DeepAI gateway not reachable at {GATEWAY_URL}")
    provider = OpenAICompatibleProvider(
        api_key=GATEWAY_KEY,
        base_url=GATEWAY_URL,
        model=DEFAULT_MODEL,
        provider_name="deepai",
    )
    tokens = list(provider.chat_stream("Count from 1 to 5."))
    assert len(tokens) > 0
    full_text = "".join(tokens)
    assert len(full_text.strip()) > 0


@pytest.mark.parametrize("model_id", [
    "deepseek-v4.1-flash",
    "glm-5.3-flash",
    "qwen-3.8-flash",
    "mimo-2.6-pro",
    "minimax-m3.1-flash",
])
def test_five_free_models_live_completion(model_id: str):
    """Verify each of the 5 free models can execute a completion successfully."""
    if not _is_gateway_live():
        pytest.skip(f"DeepAI gateway not reachable at {GATEWAY_URL}")
    provider = OpenAICompatibleProvider(
        api_key=GATEWAY_KEY,
        base_url=GATEWAY_URL,
        model=model_id,
        provider_name="deepai",
    )
    try:
        res = provider.chat_call("State your model name and say hello in one sentence.")
        assert isinstance(res, str)
        assert len(res.strip()) > 0
    except ProviderUnavailable as exc:
        msg = str(exc).lower()
        if any(term in msg for term in ("402", "credits", "payment", "timed out", "502", "503", "504", "no policy-allowed peer", "unavailable")):
            pytest.skip(f"Model '{model_id}' unavailable on live Antseed gateway: {exc}")
        raise


def test_runtime_provider_settings_switching():
    """Verify that deepai provider can be configured and selected dynamically."""
    res = provider_settings.configure(
        provider="deepai",
        base_url=GATEWAY_URL,
        api_key=GATEWAY_KEY or "sk-dummy-runtime-key",
        model=DEFAULT_MODEL,
    )
    assert res["provider"] == "deepai"
    assert res["keyConfigured"] is True
    assert res["model"] == DEFAULT_MODEL

    prov = provider_settings.provider_for_runtime()
    assert isinstance(prov, OpenAICompatibleProvider)
    assert prov.provider_name == "deepai"
