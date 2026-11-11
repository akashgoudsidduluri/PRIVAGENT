"""Gemini provider — real model calls via google-genai.

Default is the current stable Flash model (gemini-3.8-flash) with low thinking
for planning latency. Fallbacks stay on live 3.x models: 2.0-flash is shut
down and 2.5-flash is no longer available to new callers.

Retries transient failures (429/5xx) with backoff and cascades across models.
Never logs the key; errors surfaced as exceptions for the loop to handle.
"""
from __future__ import annotations

import base64
import json
import re
import time
from typing import Any, Optional

import httpx
from google import genai
from google.genai import types as gtypes

from ..privacy import PrivacyFilteredPayload
from .. import config
from ..planner import SYSTEM_FRAME, build_prompt
from .errors import ProviderUnavailable
from .limits import parse_retry_seconds, quota_scope, log_provider_call

MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024
ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}
_MAX_PAUSE_S = 2.0


def extract_json_from_text(text: str) -> dict[str, Any]:
    """Extract and parse a JSON object from raw model text.

    Handles:
    - Pure JSON strings
    - Markdown code fences (```json ... ``` or ``` ... ```) with arbitrary preamble/postamble
    - Embedded JSON objects surrounded by explanatory prose

    Raises ValueError / json.JSONDecodeError if no valid JSON dict can be extracted.
    """
    if not text:
        raise ValueError("PLAN_INVALID: Model returned empty response")

    cleaned = text.strip()

    # 1. Fast path: text is already a clean JSON object
    if cleaned.startswith("{") and cleaned.endswith("}"):
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                return data
        except Exception:
            pass

    # 2. Markdown code fences: ```json ... ``` or ``` ... ```
    fence_matches = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    for fence in fence_matches:
        fence_clean = fence.strip()
        try:
            data = json.loads(fence_clean)
            if isinstance(data, dict):
                return data
        except Exception:
            idx = fence_clean.find("{")
            if idx != -1:
                try:
                    data, _ = json.JSONDecoder().raw_decode(fence_clean, idx)
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass

    # 3. Embedded JSON object in surrounding prose
    start_pos = 0
    while True:
        idx = text.find("{", start_pos)
        if idx == -1:
            break
        try:
            data, _ = json.JSONDecoder().raw_decode(text, idx)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        start_pos = idx + 1

    raise ValueError(f"PLAN_INVALID: Model returned non-JSON: {text[:200]}")

# model -> monotonic unblock time. Avoids re-probing a model that just told us
# its quota is exhausted (each probe also burns the shared request budget).
_MODEL_BLOCKED_UNTIL: dict[str, float] = {}


def _model_blocked(model: str) -> bool:
    return _MODEL_BLOCKED_UNTIL.get(model, 0.0) > time.monotonic()


def _block_model(model: str, seconds: float) -> None:
    _MODEL_BLOCKED_UNTIL[model] = time.monotonic() + max(seconds, 5.0)


class GeminiProvider:
    DEFAULT_MODEL = "gemini-3.8-flash"
    FLASH_MODELS = ["gemini-3.8-flash", "gemini-3.6-flash"]
    LITE_MODELS = ["gemini-3.5-flash-lite"]
    FALLBACK_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash-lite"]

    def __init__(self, api_key: str, model: Optional[str] = None, timeout_s: float = 20.0) -> None:
        self._timeout_s = timeout_s
        try:
            http_opts = gtypes.HttpOptions(timeout=int(timeout_s * 1000))
            self._client = genai.Client(api_key=api_key, http_options=http_opts)
        except Exception:
            self._client = genai.Client(api_key=api_key)
        self._model = model or getattr(config, "GEMINI_MODEL", "") or self.DEFAULT_MODEL
        self.last_usage: dict[str, Any] = {}
        self.last_latency_ms: Optional[int] = None
        self.last_provider: str = self.name()
        self.last_model: str = self._model
        self.last_status: Optional[int] = None
        self.fallback_reason: Optional[str] = None

    def name(self) -> str:
        return f"gemini:{self._model}"

    def chat_call(self, prompt: str, history: Optional[list[dict[str, Any]]] = None) -> str:
        try:
            from ..capabilities import SYSTEM_CAPABILITY_PROMPT
            sys_instruction = SYSTEM_CAPABILITY_PROMPT
        except Exception:
            sys_instruction = "You are PrivAgent, a helpful, privacy-preserving AI browser automation assistant."
        models_to_try = [self._model] + [m for m in self.FALLBACK_MODELS if m != self._model]
        available = [m for m in models_to_try if not _model_blocked(m)]
        if not available:
            available = models_to_try

        last_exc: Exception | None = None
        for curr_model in available:
            try:
                resp = self._client.models.generate_content(
                    model=curr_model,
                    contents=prompt,
                    config=gtypes.GenerateContentConfig(
                        system_instruction=sys_instruction,
                        temperature=0.7,
                        max_output_tokens=2048,
                    ),
                )
                if resp.text:
                    return resp.text.strip()
            except Exception as exc:
                last_exc = exc
                err_str = str(exc)
                exc_code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                if exc_code in (429, 500, 502, 503, 504) or any(k in err_str for k in ("429", "503", "UNAVAILABLE", "timeout", "timed out")):
                    _block_model(curr_model, 60.0)
                continue
        raise ProviderUnavailable(f"Gemini chat call failed: {last_exc}")

    def plan_call(self, instruction: str, payload: PrivacyFilteredPayload, history: list[dict[str, Any]], working_memory: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        prompt = f"{SYSTEM_FRAME}\n\n{build_prompt(instruction, payload, history, working_memory=working_memory)}"
        contents: list[Any] = []
        # Defense in depth: privacy.py already drops screenshots unless the
        # operator opted in. Re-check here so a caller that bypasses the
        # privacy layer still cannot ship a rendered page to a remote model.
        if getattr(config, "ALLOW_SCREENSHOTS", False) and getattr(payload, "screenshot", None):
            try:
                s_data = payload.screenshot
                mime = "image/jpeg"
                if s_data.startswith("data:"):
                    header, _, s_data = s_data.partition(",")
                    mime = header[5:].split(";", 1)[0] or mime
                if mime not in ALLOWED_IMAGE_MIMES:
                    raise ValueError(f"unsupported image mime {mime!r}")
                img_bytes = base64.b64decode(s_data, validate=True)
                if 0 < len(img_bytes) <= MAX_SCREENSHOT_BYTES:
                    contents.append(gtypes.Part.from_bytes(data=img_bytes, mime_type=mime))
            except Exception:
                # Invalid/oversized image: send text only, never the raw blob.
                pass
        contents.append(prompt)

        models_to_try = [self._model] + [m for m in self.FALLBACK_MODELS if m != self._model]
        last_exc: Exception | None = None
        last_parse_exc: Exception | None = None
        quota_hint = ""

        available = [m for m in models_to_try if not _model_blocked(m)]
        if not available:
            wait_s = min(
                _MODEL_BLOCKED_UNTIL.get(m, 0.0) - time.monotonic() for m in models_to_try
            )
            wait_sec = max(1, int(round(wait_s)))
            if wait_sec >= 60:
                raise ProviderUnavailable(
                    f"Gemini quota exhausted; retry in about {int(wait_sec // 60) + 1}m."
                )
            raise ProviderUnavailable(
                f"Gemini temporarily unavailable (demand/rate limit); retry in about {wait_sec}s."
            )

        curr_fallback_reason: Optional[str] = None
        for index, curr_model in enumerate(available):
            started = time.perf_counter()
            try:
                # Flash 3.8 defaults to a higher thinking budget than a bounded
                # JSON plan needs; "low" keeps the schema while cutting tokens.
                tuning: dict[str, Any] = {}
                if curr_model.startswith("gemini-3.8"):
                    tuning["thinking_config"] = gtypes.ThinkingConfig(thinking_level="low")
                resp = self._client.models.generate_content(
                    model=curr_model,
                    contents=contents if len(contents) > 1 else prompt,
                    config=gtypes.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.1,
                        max_output_tokens=2048,
                        **tuning,
                    ),
                )
                text = (resp.text or "").strip()
                data = extract_json_from_text(text)
                usage = getattr(resp, "usage_metadata", None)
                if usage is not None:
                    self.last_usage = {
                        "prompt_tokens": getattr(usage, "prompt_token_count", None),
                        "completion_tokens": getattr(usage, "candidates_token_count", None),
                        "total_tokens": getattr(usage, "total_token_count", None),
                    }
                self.last_latency_ms = int((time.perf_counter() - started) * 1000)
                self.last_provider = "gemini"
                self.last_model = curr_model
                self.last_status = 200
                self.fallback_reason = curr_fallback_reason
                log_provider_call(
                    provider="gemini",
                    model=curr_model,
                    request_duration_ms=self.last_latency_ms,
                    token_usage=self.last_usage,
                    http_status=200,
                    fallback_reason=curr_fallback_reason,
                )
                return data
            except (json.JSONDecodeError, ValueError) as exc:
                last_parse_exc = exc
                if index + 1 < len(available):
                    continue
                raise ValueError(f"PLAN_INVALID: Gemini returned non-JSON: {exc}") from exc
            except Exception as exc:  # transient network / quota
                last_exc = exc
                dur_ms = int((time.perf_counter() - started) * 1000)
                err_str = str(exc)
                exc_code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                is_429 = "429" in err_str or exc_code == 429 or "RESOURCE_EXHAUSTED" in err_str
                is_server_err = exc_code in (500, 502, 503, 504) or any(c in err_str for c in ("500", "502", "503", "504", "UNAVAILABLE"))
                is_timeout = isinstance(exc, (TimeoutError, httpx.TimeoutException)) or any(w in err_str.lower() for w in ("timeout", "timed out", "deadline"))
                is_transient = is_429 or is_server_err or is_timeout
                err_status = exc_code or (429 if is_429 else (503 if is_server_err else 500))

                log_provider_call(
                    provider="gemini",
                    model=curr_model,
                    request_duration_ms=dur_ms,
                    token_usage={},
                    http_status=err_status,
                    fallback_reason=f"Gemini {curr_model} error: {err_str[:120]}",
                )
                if not is_transient:
                    raise

                retry_in = parse_retry_seconds(err_str)
                daily = quota_scope(err_str) == "day"

                if index + 1 < len(available):
                    next_model = available[index + 1]
                    if next_model in self.LITE_MODELS:
                        curr_fallback_reason = f"Gemini Flash ({curr_model}) failed ({err_status}); falling back to Gemini Flash-Lite ({next_model})"
                    else:
                        curr_fallback_reason = f"Gemini Flash ({curr_model}) failed ({err_status}); cascading to next Flash ({next_model})"

                if is_429:
                    cooldown = retry_in or (3600.0 if daily else 60.0)
                    if retry_in >= 30 or daily:
                        quota_hint = (
                            f"Gemini quota exhausted; retry in about "
                            f"{int(round(cooldown / 60)) or 1}m."
                        )
                    # Never sleep through a long window: the next model (or the
                    # caller's fallback) is faster than waiting 18-20 seconds.
                    if retry_in >= 5 or daily or (retry_in == 0.0):
                        _block_model(curr_model, cooldown)
                    if index + 1 < len(available):
                        if retry_in < 5 and not daily and retry_in > 0:
                            time.sleep(min(retry_in or 1.0, 2.0))
                        continue
                elif is_server_err or is_timeout:
                    # Model is experiencing high demand (503 UNAVAILABLE) or timing out.
                    # Block this specific model for 15s so remaining steps skip straight to fallback models.
                    _block_model(curr_model, 15.0)
                    quota_hint = "Gemini service temporarily unavailable (spikes in demand); retry in about 15s."
                    if index + 1 < len(available):
                        time.sleep(0.3)
                        continue
                else:
                    time.sleep(min(2.0 * (index + 1), _MAX_PAUSE_S))

        if last_parse_exc is not None and last_exc is None:
            raise ValueError(f"PLAN_INVALID: Gemini returned non-JSON: {last_parse_exc}") from last_parse_exc

        raise ProviderUnavailable(
            quota_hint or f"Gemini call failed after retries: {last_exc}"
        ) from last_exc


def get_gemini_cooldown_remaining() -> float:
    models = [GeminiProvider.DEFAULT_MODEL] + GeminiProvider.FALLBACK_MODELS
    unblocked = [m for m in models if not _model_blocked(m)]
    if unblocked:
        return 0.0
    return max(0.0, min(_MODEL_BLOCKED_UNTIL.get(m, 0.0) - time.monotonic() for m in models))
