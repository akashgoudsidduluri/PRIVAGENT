"""Groq provider — ultra-fast LPU inference via OpenAI-compatible endpoint.

Models: openai/gpt-oss-120b (default), openai/gpt-oss-20b, qwen/qwen3.8-27b.
The Llama 3.3/3.1 models are Enterprise-only on Groq and are intentionally
not used as defaults. Never logs the API key.

Rate limits are handled fail-fast: a *daily* quota (TPD/RPD) is account-wide,
so trying the other Groq models cannot help. Those responses cascade straight
to the Gemini fallback instead of sleeping through a multi-minute window. That
retry storm was the difference between a ~2s step and a ~40s step.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

import httpx

from ..privacy import PrivacyFilteredPayload
from .. import config
from ..planner import SYSTEM_FRAME, build_prompt
from .errors import ProviderUnavailable
from .gemini import extract_json_from_text
from .limits import parse_retry_seconds as _parse_duration, quota_scope as _quota_scope, parse_retry_headers, log_provider_call

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_PAUSE_S = 2.0

# Process-wide: once Groq reports a daily cap, every later step in every task
# should skip it until the window resets. Re-probing cost a request round trip
# per step and, worse, kept burning the same exhausted bucket.
_GROQ_BLOCKED_UNTIL: float = 0.0


def _groq_blocked() -> bool:
    return time.monotonic() < _GROQ_BLOCKED_UNTIL


def _block_groq(seconds: float) -> None:
    global _GROQ_BLOCKED_UNTIL
    _GROQ_BLOCKED_UNTIL = time.monotonic() + max(seconds, 2.0)


def get_groq_cooldown_remaining() -> float:
    return max(0.0, _GROQ_BLOCKED_UNTIL - time.monotonic())


class GroqProvider:
    DEFAULT_MODEL = "openai/gpt-oss-120b"
    FALLBACK_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]

    def __init__(self, api_key: str, model: Optional[str] = None, timeout_s: float = 20.0) -> None:
        self._api_key = api_key
        self._model = model or config.GROQ_MODEL or self.DEFAULT_MODEL
        self._timeout_s = timeout_s
        self._client = httpx.Client(timeout=timeout_s)
        # Telemetry for the UI and audit logs
        self.last_usage: dict[str, Any] = {}
        self.last_latency_ms: Optional[int] = None
        self.last_provider: str = self.name()
        self.last_model: str = self._model
        self.last_status: Optional[int] = None
        self.fallback_reason: Optional[str] = None

    def name(self) -> str:
        return f"groq:{self._model}"

    def chat_call(self, prompt: str, history: Optional[list[dict[str, Any]]] = None) -> str:
        try:
            from ..capabilities import SYSTEM_CAPABILITY_PROMPT
            sys_prompt = SYSTEM_CAPABILITY_PROMPT
        except Exception:
            sys_prompt = "You are PrivAgent, a helpful, privacy-preserving AI browser assistant."
        messages = [{"role": "system", "content": sys_prompt}]
        if history:
            for h in history[-6:]:
                if isinstance(h, dict) and h.get("role") and h.get("content"):
                    messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": prompt})

        models_to_try = [self._model] + [m for m in self.FALLBACK_MODELS if m != self._model]
        last_exc: Exception | None = None
        if not _groq_blocked():
            for curr_model in models_to_try:
                try:
                    resp = self._client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        json={
                            "model": curr_model,
                            "messages": messages,
                            "temperature": 0.7,
                        },
                        timeout=self._timeout_s,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        content = data["choices"][0]["message"]["content"]
                        return (content or "").strip()
                except Exception as exc:
                    last_exc = exc
                    continue

        if config.GEMINI_API_KEY:
            from .gemini import GeminiProvider
            fallback = GeminiProvider(api_key=config.GEMINI_API_KEY)
            return fallback.chat_call(prompt, history=history)

        raise ProviderUnavailable(f"Conversational call failed: {last_exc}")

    def plan_call(
        self,
        instruction: str,
        payload: PrivacyFilteredPayload,
        history: list[dict[str, Any]],
        working_memory: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        prompt = build_prompt(instruction, payload, history, working_memory=working_memory)
        models_to_try = [self._model] + [m for m in self.FALLBACK_MODELS if m != self._model]
        last_exc: Exception | None = None
        quota_hint = ""

        if not _groq_blocked():
            for curr_model in models_to_try:
                started = time.perf_counter()
                try:
                    resp = self._client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        json={
                            "model": curr_model,
                            "messages": [
                                {"role": "system", "content": SYSTEM_FRAME},
                                {"role": "user", "content": prompt},
                            ],
                            "response_format": {"type": "json_object"},
                            "temperature": 0.1,
                        },
                        timeout=self._timeout_s,
                    )
                except Exception as exc:  # connection reset, DNS, read timeout
                    last_exc = exc
                    time.sleep(min(0.4, _MAX_PAUSE_S))
                    continue

                if resp.status_code == 200:
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    text = (content or "").strip()
                    self.last_usage = data.get("usage") or {}
                    self.last_latency_ms = int((time.perf_counter() - started) * 1000)
                    self.last_provider = f"groq:{curr_model}"
                    self.last_model = curr_model
                    self.last_status = 200
                    self.fallback_reason = None
                    log_provider_call(
                        provider="groq",
                        model=curr_model,
                        request_duration_ms=self.last_latency_ms,
                        token_usage=self.last_usage,
                        http_status=200,
                        fallback_reason=None,
                    )
                    try:
                        return extract_json_from_text(text)
                    except (json.JSONDecodeError, ValueError) as exc:
                        raise ValueError(f"PLAN_INVALID: Groq returned non-JSON: {exc}") from exc

                err_text = resp.text or ""
                dur_ms = int((time.perf_counter() - started) * 1000)
                is_json_err = resp.status_code == 400 and (
                    "json_validate_failed" in err_text or "Failed to validate JSON" in err_text
                )
                if resp.status_code in _RETRYABLE_STATUS or is_json_err:
                    last_exc = RuntimeError(f"Groq {resp.status_code}: {err_text}")
                    log_provider_call(
                        provider="groq",
                        model=curr_model,
                        request_duration_ms=dur_ms,
                        token_usage={},
                        http_status=resp.status_code,
                        fallback_reason=f"Groq {resp.status_code} on {curr_model}: {err_text[:120]}",
                    )
                    if is_json_err:
                        # Model failed JSON formatting constraint; try next model immediately
                        continue
                    scope = _quota_scope(err_text)
                    retry_in = parse_retry_headers(getattr(resp, "headers", None)) or _parse_duration(err_text)
                    if scope == "day":
                        # Account-wide daily cap: every Groq model reports the
                        # same limit, so cascading through them only adds
                        # latency. Skip Groq until the window resets.
                        _block_groq(retry_in or 60.0)
                        quota_hint = (
                            f"Groq quota exhausted; retry in about "
                            f"{int(round((retry_in or 60.0) / 60)) or 1}m."
                        )
                        break
                    if retry_in >= 30:
                        quota_hint = (
                            f"Groq quota exhausted; retry in about "
                            f"{int(round(retry_in / 60)) or 1}m."
                        )
                    elif retry_in > 0:
                        quota_hint = f"Groq rate limit exceeded; retry in about {int(round(retry_in))}s."

                    pause = retry_in if 0 < retry_in <= _MAX_PAUSE_S else 0.5
                    time.sleep(min(pause, _MAX_PAUSE_S))
                    continue

                last_exc = RuntimeError(f"Groq API error {resp.status_code}: {err_text}")
                log_provider_call(
                    provider="groq",
                    model=curr_model,
                    request_duration_ms=dur_ms,
                    token_usage={},
                    http_status=resp.status_code,
                    fallback_reason=f"Groq error {resp.status_code}: {err_text[:120]}",
                )
                if not config.GEMINI_API_KEY:
                    raise last_exc
                break

            if not _groq_blocked() and last_exc and "429" in str(last_exc):
                cooldown_s = retry_in if 0 < retry_in <= 60.0 else (min(retry_in, 60.0) if retry_in > 0 else 15.0)
                _block_groq(cooldown_s)

        # Seamless fallback to Gemini if configured.
        if config.GEMINI_API_KEY:
            fb_reason = quota_hint or (
                f"Groq unavailable ({last_exc}); falling back to Gemini Flash"
                if last_exc
                else "Groq in cooldown; falling back to Gemini Flash"
            )
            log_provider_call(
                provider="groq",
                model=self._model,
                request_duration_ms=0,
                token_usage={},
                http_status=307,
                fallback_reason=fb_reason,
            )
            try:
                from .gemini import GeminiProvider

                fallback = GeminiProvider(api_key=config.GEMINI_API_KEY)
                result = fallback.plan_call(
                    instruction, payload, history, working_memory=working_memory
                )
                self.last_usage = fallback.last_usage
                self.last_latency_ms = fallback.last_latency_ms
                self.last_provider = fallback.name()
                self.last_model = fallback.last_model
                self.last_status = fallback.last_status
                self.fallback_reason = fallback.fallback_reason or fb_reason
                return result
            except ProviderUnavailable as gemini_prov_exc:
                raise ProviderUnavailable(
                    f"{quota_hint or 'Groq unavailable;'} Gemini fallback also failed: {gemini_prov_exc}"
                ) from gemini_prov_exc
            except ValueError:
                # Malformed model output from fallback: preserve PLAN_INVALID boundary
                raise
            except Exception as gemini_exc:
                raise ProviderUnavailable(
                    f"{quota_hint or 'Groq unavailable;'} Gemini fallback error: {gemini_exc}"
                ) from gemini_exc

        raise ProviderUnavailable(
            f"{quota_hint or 'Groq call failed after retries.'} No Gemini key is configured, so "
            "there is no fallback provider."
        ) from last_exc
