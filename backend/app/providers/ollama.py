"""Ollama provider — a local planner that never sends data to the cloud.

Talks to a local Ollama server (default http://127.0.0.1:11434) over its
native ``/api/chat`` endpoint with ``"format": "json"`` so the model
returns a JSON plan object. The operator installs Ollama and pulls a
model (``ollama pull qwen2.5:7b``); PrivAgent never downloads a model
itself and sends no API key anywhere.

Safety properties (Task 10):
* The base URL must be loopback (127.0.0.1 / localhost / ::1). A
  non-loopback URL is refused at construction so a misconfigured env
  can never send page data to a remote host.
* The egress gate (Task 3) is checked before any request; loopback
  already passes it, so this is a belt-and-braces assertion.
* In local-only mode (``PRIVAGENT_PLANNER_LOCAL_ONLY=true``) an
  unreachable Ollama fails honestly with :class:`ProviderUnavailable`
  and NEVER silently falls back to a cloud provider.
* Otherwise (explicit ``PRIVAGENT_PLANNER=ollama`` or local-first) a
  failed Ollama call falls back to the normal provider chain, mirroring
  the Groq -> Gemini pattern — the fallback is logged, never silent.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from .. import config
from ..planner import SYSTEM_FRAME, build_prompt
from ..privacy import PrivacyFilteredPayload
from .errors import ProviderUnavailable
from .gemini import extract_json_from_text
from .limits import log_provider_call

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _is_loopback_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS


class OllamaProvider:
    DEFAULT_MODEL = "qwen2.5:7b"

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout_s: float = 20.0,
        client: Optional[Any] = None,
    ) -> None:
        self._base_url = (base_url or config.OLLAMA_BASE_URL or "http://127.0.0.1:11434").rstrip("/")
        if not _is_loopback_url(self._base_url):
            raise ProviderUnavailable(
                f"Ollama base URL must be loopback (127.0.0.1/localhost); refusing {self._base_url!r} "
                "so page data can never be sent to a remote host by misconfiguration"
            )
        self._model = model or config.OLLAMA_MODEL or self.DEFAULT_MODEL
        self._timeout_s = timeout_s
        # Task 3: egress gate. Loopback always passes; the check exists
        # so a future change of the endpoint cannot silently widen egress.
        from ..allowlist import egress_allowed

        if not egress_allowed(f"{self._base_url}/api/chat"):
            raise ProviderUnavailable(f"egress blocked: Ollama at {self._base_url} is not allowed")
        self._client = client if client is not None else httpx.Client(timeout=timeout_s, trust_env=False)
        # Telemetry for the UI and audit logs (same shape as Groq).
        self.last_usage: dict[str, Any] = {}
        self.last_latency_ms: Optional[int] = None
        self.last_provider: str = self.name()
        self.last_model: str = self._model
        self.last_status: Optional[int] = None
        self.fallback_reason: Optional[str] = None

    def name(self) -> str:
        return f"ollama:{self._model}"

    def _chat_url(self) -> str:
        return f"{self._base_url}/api/chat"

    def _fallback_provider(self) -> Any:
        """The normal (non-Ollama) provider chain, for non-local-only mode."""
        from ..agentLoop import _provider_for_config

        return _provider_for_config(skip_ollama=True)

    def _call(self, messages: list[dict[str, str]]) -> str:
        """One Ollama /api/chat call; returns the assistant's text."""
        started = time.perf_counter()
        try:
            resp = self._client.post(
                self._chat_url(),
                json={
                    "model": self._model,
                    "messages": messages,
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": 0.1},
                },
                timeout=self._timeout_s,
            )
        except Exception as exc:
            raise ProviderUnavailable(
                f"Ollama at {self._base_url} is unreachable ({exc}); "
                "is Ollama running? Start it with `ollama serve` and pull a model "
                f"with `ollama pull {self._model}`."
            ) from exc
        dur_ms = int((time.perf_counter() - started) * 1000)
        self.last_latency_ms = dur_ms
        self.last_status = getattr(resp, "status_code", None)
        if resp.status_code != 200:
            raise ProviderUnavailable(
                f"Ollama returned HTTP {resp.status_code}: {(resp.text or '')[:200]}"
            )
        try:
            data = resp.json()
        except Exception as exc:
            raise ProviderUnavailable(f"Ollama returned a non-JSON response: {exc}") from exc
        message = (data or {}).get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ProviderUnavailable("Ollama returned an empty response")
        log_provider_call(
            provider="ollama",
            model=self._model,
            request_duration_ms=dur_ms,
            token_usage=data.get("usage") or {},
            http_status=200,
            fallback_reason=None,
        )
        return content.strip()

    def plan_call(
        self,
        instruction: str,
        payload: PrivacyFilteredPayload,
        history: list[dict[str, Any]],
        working_memory: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        prompt = build_prompt(instruction, payload, history, working_memory=working_memory, allowed_domains=(working_memory or {}).get("allowed_domains"))
        try:
            text = self._call([
                {"role": "system", "content": SYSTEM_FRAME},
                {"role": "user", "content": prompt},
            ])
        except ProviderUnavailable as exc:
            if config.PLANNER_LOCAL_ONLY:
                raise
            fb_reason = f"Ollama unavailable ({exc}); falling back to the normal provider chain"
            self.fallback_reason = fb_reason
            log_provider_call(
                provider="ollama",
                model=self._model,
                request_duration_ms=self.last_latency_ms or 0,
                token_usage={},
                http_status=self.last_status or 0,
                fallback_reason=fb_reason,
            )
            try:
                fallback = self._fallback_provider()
                result = fallback.plan_call(instruction, payload, history, working_memory=working_memory)
            except Exception as fb_exc:
                raise ProviderUnavailable(
                    f"Ollama unavailable and the fallback provider also failed: {fb_exc}"
                ) from exc
            self.last_usage = getattr(fallback, "last_usage", {}) or {}
            self.last_latency_ms = getattr(fallback, "last_latency_ms", None)
            self.last_provider = getattr(fallback, "name", lambda: "unknown")()
            self.last_model = getattr(fallback, "last_model", "")
            self.last_status = getattr(fallback, "last_status", None)
            return result
        try:
            return extract_json_from_text(text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"PLAN_INVALID: Ollama returned non-JSON: {exc}") from exc

    def chat_call(
        self,
        prompt: str,
        history: Optional[list[dict[str, Any]]] = None,
        system_prompt: Optional[str] = None,
    ) -> str:
        try:
            from ..capabilities import SYSTEM_CAPABILITY_PROMPT
            sys_prompt = system_prompt or SYSTEM_CAPABILITY_PROMPT
        except Exception:
            sys_prompt = system_prompt or "You are PrivAgent, a helpful, privacy-preserving AI browser assistant."
        from .history import normalize_history
        messages = [{"role": "system", "content": sys_prompt}]
        messages.extend(normalize_history(history))
        messages.append({"role": "user", "content": prompt})
        try:
            return self._call(messages)
        except ProviderUnavailable as exc:
            if config.PLANNER_LOCAL_ONLY:
                raise
            fallback = self._fallback_provider()
            if hasattr(fallback, "chat_call"):
                return fallback.chat_call(prompt, history=history)
            raise ProviderUnavailable(f"Ollama unavailable ({exc}) and no chat fallback exists") from exc
