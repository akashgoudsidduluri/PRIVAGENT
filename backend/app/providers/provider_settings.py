"""Runtime LLM provider/model selection — the mechanism behind
GET/POST /api/llm/config. In-memory only; `.env` remains the persisted
default that is loaded at process start.

The API key lives here for the backend process lifetime and is NEVER
serialized: `public_state()` reports only `keyConfigured: true/false`.
Selection is read fresh by `_provider_for_config()` on every planner/chat
call, so switching takes effect without restarting the application.
"""
from __future__ import annotations

import threading
from typing import Any, Optional

from .. import config
from .errors import ProviderUnavailable

_lock = threading.Lock()

_state: dict[str, Any] = {
    "provider": None,        # None = follow config.PROVIDER
    "base_url": None,        # None = config.ANTSEED_BASE_URL
    "api_key": None,         # None = config.ANTSEED_API_KEY_ENV (may be empty)
    "model": None,           # None = config.ANTSEED_MODEL (or provider default)
}


def _stored_llm_key(env_key: str) -> str:
    """Best-effort read of an LLM key from the encrypted store (Task 2).

    Deliberately NOT skipped under pytest: under pytest the store is
    hermetic (a per-process temp dir, see secrets.store_dir), so reads
    here can only return keys the test itself stored. Writes are still
    skipped under pytest (see _persist_runtime_key) so one test's fake
    runtime key can never leak into another test through the store.
    """
    try:
        from .. import secrets as secrets_store
        return secrets_store.get_secret_for_env_key(env_key) or ""
    except Exception:
        return ""


def _resolve() -> tuple[str, str, str, str]:
    """(provider, base_url, api_key, model) after runtime/env resolution."""
    with _lock:
        provider = _state["provider"] or config.PROVIDER
        if provider == "deepai":
            base_url = _state["base_url"] or getattr(config, "DEEPAI_GATEWAY_BASE_URL", "http://127.0.0.1:8787/v1")
            api_key = _state["api_key"] if _state["api_key"] is not None else getattr(config, "DEEPAI_GATEWAY_KEY", "")
            if not api_key:
                api_key = _stored_llm_key("DEEPAI_GATEWAY_KEY")
            model = _state["model"] or getattr(config, "DEEPAI_GATEWAY_MODEL", "deepseek-v4.1-flash")
        elif provider == "ollama":
            # Task 10: local planner. No API key exists or is needed; the
            # base URL must be loopback (enforced by OllamaProvider).
            base_url = _state["base_url"] or getattr(config, "OLLAMA_BASE_URL", "http://127.0.0.1:11434")
            api_key = ""
            model = _state["model"] or getattr(config, "OLLAMA_MODEL", "qwen2.5:7b")
        else:
            base_url = _state["base_url"] or config.ANTSEED_BASE_URL
            api_key = _state["api_key"] if _state["api_key"] is not None else config.ANTSEED_API_KEY_ENV
            if not api_key:
                api_key = _stored_llm_key("ANTSEED_API_KEY")
            model = _state["model"] or config.ANTSEED_MODEL
    return provider, base_url, api_key, model


def configure(
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    reset: bool = False,
) -> dict[str, Any]:
    """Apply a runtime selection. Returns public state; key is never echoed.

    A non-empty runtime API key is also persisted to the encrypted secret
    store (Task 2) so it survives a restart without ever touching
    ``.env`` in plaintext; an empty key clears both the runtime value and
    the stored one for that provider. Never persisted under pytest.
    """
    from .openai_compatible import validate_base_url

    with _lock:
        if reset:
            for k in _state:
                _state[k] = None
        if provider is not None:
            p = provider.strip().lower()
            if p not in ("mock", "groq", "gemini", "antseed", "deepai", "ollama"):
                raise ValueError("Unknown provider name.")
            _state["provider"] = p
        if base_url is not None:
            _state["base_url"] = validate_base_url(base_url)
        if api_key is not None:
            # Empty string clears a runtime key (falls back to env value).
            _state["api_key"] = api_key.strip()
        if model is not None:
            _state["model"] = model.strip()
        effective_provider = _state["provider"] or config.PROVIDER
        runtime_key = _state["api_key"]
    if api_key is not None:
        _persist_runtime_key(effective_provider, runtime_key)
    return public_state()


def _persist_runtime_key(provider: str, api_key: Optional[str]) -> None:
    """Persist/clear a runtime LLM key in the encrypted store (Task 2)."""
    store_keys = {
        "groq": "GROQ_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "antseed": "ANTSEED_API_KEY",
        "deepai": "DEEPAI_GATEWAY_KEY",
    }
    env_key = store_keys.get(provider)
    if env_key is None:
        return
    try:
        from .. import secrets as secrets_store
        if secrets_store.is_pytest():
            return
        store_key = secrets_store.ENV_TO_STORE_KEY[env_key]
        if api_key:
            secrets_store.set_secret(store_key, api_key)
        else:
            secrets_store.delete_secret(store_key)
    except Exception:
        # The runtime key still works for this process; it just will not
        # survive a restart. Never log the key itself.
        pass


def active() -> bool:
    """True when a runtime (non-.env) selection has been made."""
    with _lock:
        return any(v is not None for v in _state.values())


def public_state() -> dict[str, Any]:
    provider, base_url, api_key, model = _resolve()
    return {
        "provider": provider,
        "baseUrl": base_url,
        # "configured" only when a key exists — never the key itself.
        # Ollama needs no key: it is configured when a (loopback) base
        # URL and model are set.
        "keyConfigured": bool(api_key) or provider == "ollama",
        "model": model or None,
    }


def provider_for_runtime():
    """Build the provider object for the current selection, or raise."""
    provider, base_url, api_key, model = _resolve()
    if provider == "mock":
        from .mock import MockProvider
        return MockProvider()
    if provider == "groq":
        groq_key = config.GROQ_API_KEY or _stored_llm_key("GROQ_API_KEY")
        if not groq_key:
            raise ProviderUnavailable("Groq is selected but GROQ_API_KEY is not configured.")
        from .groq import GroqProvider
        return GroqProvider(api_key=groq_key)
    if provider == "gemini":
        gemini_key = config.GEMINI_API_KEY or _stored_llm_key("GEMINI_API_KEY")
        if not gemini_key:
            raise ProviderUnavailable("Gemini is selected but GEMINI_API_KEY is not configured.")
        from .gemini import GeminiProvider
        return GeminiProvider(api_key=gemini_key)
    if provider == "antseed":
        if not api_key:
            raise ProviderUnavailable(
                "Antseed is selected but no API key is configured. Enter one in the provider settings panel."
            )
        from .openai_compatible import OpenAICompatibleProvider
        return OpenAICompatibleProvider(api_key=api_key, base_url=base_url, model=model or None, provider_name="antseed")
    if provider == "deepai":
        if not api_key:
            raise ProviderUnavailable(
                "DeepAI Gateway is selected but DEEPAI_GATEWAY_KEY is not configured. Enter one in the provider settings panel or set DEEPAI_GATEWAY_KEY in .env."
            )
        from .openai_compatible import OpenAICompatibleProvider
        return OpenAICompatibleProvider(
            api_key=api_key,
            base_url=base_url,
            model=model or "deepseek-v4.1-flash",
            provider_name="deepai",
        )
    if provider == "ollama":
        # No key is needed or accepted: the server is local (loopback
        # enforced by the provider) and the model runs on this machine.
        from .ollama import OllamaProvider
        return OllamaProvider(base_url=base_url, model=model or None)
    raise ProviderUnavailable(f"Unknown provider '{provider}'.")


def reset_for_tests() -> None:
    with _lock:
        for k in _state:
            _state[k] = None
