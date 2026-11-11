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


def _resolve() -> tuple[str, str, str, str]:
    """(provider, base_url, api_key, model) after runtime/env resolution."""
    with _lock:
        provider = _state["provider"] or config.PROVIDER
        base_url = _state["base_url"] or config.ANTSEED_BASE_URL
        api_key = _state["api_key"] if _state["api_key"] is not None else config.ANTSEED_API_KEY_ENV
        model = _state["model"] or config.ANTSEED_MODEL
    return provider, base_url, api_key, model


def configure(
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    reset: bool = False,
) -> dict[str, Any]:
    """Apply a runtime selection. Returns public state; key is never echoed."""
    from .openai_compatible import validate_base_url

    with _lock:
        if reset:
            for k in _state:
                _state[k] = None
        if provider is not None:
            p = provider.strip().lower()
            if p not in ("mock", "groq", "gemini", "antseed"):
                raise ValueError("Unknown provider name.")
            _state["provider"] = p
        if base_url is not None:
            _state["base_url"] = validate_base_url(base_url)
        if api_key is not None:
            # Empty string clears a runtime key (falls back to env value).
            _state["api_key"] = api_key.strip()
        if model is not None:
            _state["model"] = model.strip()
    return public_state()


def active() -> bool:
    """True when a runtime (non-.env) selection has been made."""
    with _lock:
        return any(v is not None for v in _state.values())


def public_state() -> dict[str, Any]:
    with _lock:
        provider = _state["provider"] or config.PROVIDER
        base_url = _state["base_url"] or config.ANTSEED_BASE_URL
        api_key = _state["api_key"] if _state["api_key"] is not None else config.ANTSEED_API_KEY_ENV
        model = _state["model"] or config.ANTSEED_MODEL
    return {
        "provider": provider,
        "baseUrl": base_url,
        # "configured" only when a key exists — never the key itself.
        "keyConfigured": bool(api_key),
        "model": model or None,
    }


def provider_for_runtime():
    """Build the provider object for the current selection, or raise."""
    provider, base_url, api_key, model = _resolve()
    if provider == "mock":
        from .mock import MockProvider
        return MockProvider()
    if provider == "groq":
        if not config.GROQ_API_KEY:
            raise ProviderUnavailable("Groq is selected but GROQ_API_KEY is not configured.")
        from .groq import GroqProvider
        return GroqProvider(api_key=config.GROQ_API_KEY)
    if provider == "gemini":
        if not config.GEMINI_API_KEY:
            raise ProviderUnavailable("Gemini is selected but GEMINI_API_KEY is not configured.")
        from .gemini import GeminiProvider
        return GeminiProvider(api_key=config.GEMINI_API_KEY)
    if provider == "antseed":
        if not api_key:
            raise ProviderUnavailable(
                "Antseed is selected but no API key is configured. Enter one in the provider settings panel."
            )
        from .openai_compatible import OpenAICompatibleProvider
        return OpenAICompatibleProvider(api_key=api_key, base_url=base_url, model=model or None)
    raise ProviderUnavailable(f"Unknown provider '{provider}'.")


def reset_for_tests() -> None:
    with _lock:
        for k in _state:
            _state[k] = None
