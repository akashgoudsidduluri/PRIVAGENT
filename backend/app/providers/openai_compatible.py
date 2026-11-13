"""Generic OpenAI-compatible provider (e.g. the local Antseed gateway).

Mirrors GroqProvider's shape so the agent loop can treat it identically:
`chat_call`, `plan_call`, and the telemetry tuple. Differences from Groq:

- Endpoint and credential are entirely configurable (`base_url` + `api_key`);
  both requests are built ONLY from the validated base URL, so the key can
  never be forwarded to an unrelated host.
- No cross-provider fallback cascade. Failures raise ProviderUnavailable, so
  behavior stays attributable to the operator's provider choice.
- Model listing via GET {base_url}/models for discovery-driven UIs.

Never logs the API key. `response_format` is best-effort: gateways/models
that reject it get one retry without the constraint, then
`extract_json_from_text` handles whatever shape comes back.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

import httpx

from ..privacy import PrivacyFilteredPayload
from ..planner import SYSTEM_FRAME, build_prompt
from .errors import ProviderUnavailable
from .gemini import extract_json_from_text
from .limits import log_provider_call

_DEFAULT_TIMEOUT_S = 30.0

DEEPAI_FREE_MODELS = {
    "deepseek-v4.1-flash": {
        "name": "DeepSeek V4.1 Flash",
        "role": "Primary reasoning and coding (Default)",
        "category": "reasoning_coding",
        "tier": "free",
        "default": True,
    },
    "glm-5.3-flash": {
        "name": "GLM 5.3 Flash",
        "role": "Coding and agent tasks",
        "category": "agent_coding",
        "tier": "free",
        "default": False,
    },
    "qwen-3.8-flash": {
        "name": "Qwen 3.8 Flash",
        "role": "General-purpose tasks and information extraction",
        "category": "extraction_summary",
        "tier": "free",
        "default": False,
    },
    "mimo-2.6-pro": {
        "name": "MiMo 2.6 Pro",
        "role": "Complex reasoning and analysis",
        "category": "complex_analysis",
        "tier": "free",
        "default": False,
    },
    "minimax-m3.1-flash": {
        "name": "MiniMax M3.1 Flash",
        "role": "Alternative coding and reasoning",
        "category": "alternative_reasoning",
        "tier": "free",
        "default": False,
    },
}

_SEEDED_MODELS = list(DEEPAI_FREE_MODELS.keys())


def select_model_for_task(task_type: str = "planning", requested_model: Optional[str] = None) -> str:
    """Return an appropriate free-tier model id.

    Guarantees that only free-tier models are selected, defaulting to deepseek-v4.1-flash.
    Never silently routes requests to unverified or paid models.
    """
    if requested_model and requested_model in DEEPAI_FREE_MODELS:
        return requested_model
    if task_type in ("extraction", "summarization", "harvest"):
        return "qwen-3.8-flash"
    if task_type in ("deep_analysis", "synthesis", "comparison"):
        return "mimo-2.6-pro"
    if task_type in ("coding", "agent_navigation"):
        return "glm-5.3-flash"
    return "deepseek-v4.1-flash"


def validate_base_url(base_url: str) -> str:
    """Normalize and validate a provider base URL.

    Returns scheme://host[:port][/version]. Raises ValueError on anything
    that is not a plain http(s) URL — this is the guard that keeps the
    credential pinned to the configured host.
    """
    raw = (base_url or "").strip()
    if not raw:
        raise ValueError("Base URL is required.")
    if "://" not in raw:
        raw = f"http://{raw}"
    from urllib.parse import urlsplit
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ValueError("Base URL scheme must be http or https.")
    if not parts.hostname:
        raise ValueError("Base URL must include a host.")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Base URL must not contain credentials, query, or fragment.")
    host = parts.hostname.lower() or ""
    if not _HOST_RE.fullmatch(host):
        raise ValueError("Base URL host must be a plain hostname, IPv4, or IPv6 literal.")
    netloc = host if parts.port in (None, 80, 443) else f"{host}:{parts.port}"
    segs = [s for s in (parts.path or "").split("/") if s]
    if segs:
        if len(segs) > 1 or not _is_version_segment(segs[0]):
            raise ValueError("Base URL may include only a version segment, e.g. /v1.")
        netloc = f"{netloc}/{segs[0]}"
    return f"{parts.scheme}://{netloc}"


def _is_version_segment(seg: str) -> bool:
    s = seg.lower()
    return s.startswith("v") and (s[1:].isdigit() or s[1:] == "")


# Plain DNS name (letters/digits/hyphen/dot), IPv4-style digit/dot, or
# bracket-stripped IPv6 (urlsplit already unwraps it, so hex/colon runs).
_HOST_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
    r"|\d{1,3}(?:\.\d{1,3}){3}"
    r"|[0-9a-f:]+:[0-9a-f:.]+)$"
)


class OpenAICompatibleProvider:
    """Provider for any OpenAI ChatCompletions-style HTTP endpoint."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: Optional[str] = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        transport: Any = None,
        provider_name: str = "deepai",
    ) -> None:
        self._api_key = api_key or ""
        self.base_url = validate_base_url(base_url)
        self._model = (model or "").strip()
        self._timeout_s = timeout_s
        self.provider_name = provider_name
        self._client = httpx.Client(timeout=timeout_s, transport=transport)
        # Telemetry for the UI and audit logs
        self.last_usage: dict[str, Any] = {}
        self.last_latency_ms: Optional[int] = None
        self.last_provider: str = self.name()
        self.last_model: str = self._model
        self.last_status: Optional[int] = None
        self.fallback_reason: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Identification / URL / headers
    # ------------------------------------------------------------------ #

    def name(self) -> str:
        tag = self.provider_name or "deepai"
        return f"{tag}:{self._model}" if self._model else tag

    def _url(self, path: str) -> str:
        # The only two paths this class ever builds; both derive from the
        # validated base_url, so the credential stays host-pinned.
        if path not in ("chat/completions", "models"):
            raise ValueError(f"unsupported endpoint {path!r}")
        return f"{self.base_url}/{path}"

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    # ------------------------------------------------------------------ #
    # Model discovery
    # ------------------------------------------------------------------ #

    def list_models(self) -> list[str]:
        """GET {base_url}/models -> model id strings; raises when unusable."""
        try:
            resp = self._client.get(self._url("models"), headers=self._headers())
        except Exception as exc:  # connection refused, DNS, timeout
            raise ProviderUnavailable(f"Model discovery failed: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderUnavailable(
                f"Model discovery failed with HTTP {resp.status_code}."
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderUnavailable("Model discovery returned non-JSON.") from exc
        ids = _model_ids(data)
        if not ids:
            raise ProviderUnavailable("Model discovery returned no models.")
        return ids

    # ------------------------------------------------------------------ #
    # Conversational / capability answers
    # ------------------------------------------------------------------ #

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
        messages: list[dict[str, str]] = [{"role": "system", "content": sys_prompt}]
        messages.extend(normalize_history(history))
        messages.append({"role": "user", "content": prompt})

        content, _body = self._completion(
            payload={
                "model": self._model,
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": 800,
            },
            context="Conversational call failed",
        )
        return content

    def chat_stream(
        self, prompt: str, history: Optional[list[dict[str, Any]]] = None
    ):
        """Stream completion tokens from the gateway."""
        try:
            from ..capabilities import SYSTEM_CAPABILITY_PROMPT
            sys_prompt = SYSTEM_CAPABILITY_PROMPT
        except Exception:
            sys_prompt = "You are PrivAgent, a helpful, privacy-preserving AI browser assistant."
        from .history import normalize_history
        messages: list[dict[str, str]] = [{"role": "system", "content": sys_prompt}]
        messages.extend(normalize_history(history))
        messages.append({"role": "user", "content": prompt})

        payload = {"model": self._model, "messages": messages, "stream": True, "temperature": 0.7}
        with self._client.stream(
            "POST", self._url("chat/completions"), headers=self._headers(), json=payload
        ) as resp:
            if resp.status_code != 200:
                err_text = resp.read().decode("utf-8", errors="replace")[:200]
                raise ProviderUnavailable(f"Streaming failed with HTTP {resp.status_code}: {err_text}")
            for line in resp.iter_lines():
                if not line:
                    continue
                if line.startswith("data: "):
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        token = delta.get("content") or delta.get("reasoning_content") or ""
                        if token:
                            yield token
                    except Exception:
                        continue

    # ------------------------------------------------------------------ #
    # Planning
    # ------------------------------------------------------------------ #

    def plan_call(
        self,
        instruction: str,
        payload: PrivacyFilteredPayload,
        history: list[dict[str, Any]],
        working_memory: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        prompt = build_prompt(instruction, payload, history, working_memory=working_memory)
        base_payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_FRAME},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
            "max_tokens": 4096,
        }
        content, _body = self._completion(payload=base_payload, context="Planner call failed")
        try:
            return extract_json_from_text(content)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"PLAN_INVALID: provider returned non-JSON: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Shared completion plumbing
    # ------------------------------------------------------------------ #

    def _completion(self, payload: dict[str, Any], context: str) -> tuple[str, dict[str, Any]]:
        """POST one chat completion; returns (content, response_body).

        On a 400 that looks like a `response_format` rejection, retries once
        without the constraint. Raises ProviderUnavailable with an
        operator-readable message — endpoint and status are safe to include;
        the API key never is.
        """
        if not self._model:
            raise ProviderUnavailable(f"{context}: no model id is configured.")
        started = time.perf_counter()

        try:
            resp = self._client.post(
                self._url("chat/completions"),
                headers=self._headers(),
                json=payload,
            )
        except httpx.TimeoutException as exc:
            self._log_fail(0, None, None, f"{context}: timeout after {int(self._timeout_s)}s")
            raise ProviderUnavailable(
                f"{context}: request timed out after {int(self._timeout_s)}s."
            ) from exc
        except Exception as exc:
            self._log_fail(0, None, None, f"{context}: {type(exc).__name__}")
            raise ProviderUnavailable(f"{context}: {exc}") from exc

        if resp.status_code != 200:
            recovered = self._handle_http_error(context, payload, resp, started)
            if recovered is not None:
                return recovered  # response_format retry succeeded
        body = _safe_json(resp)
        content = _content_of(body)
        self._record_success(body, started)
        log_provider_call(
            provider=self.name(),
            model=self._model,
            request_duration_ms=self.last_latency_ms,
            token_usage=self.last_usage,
            http_status=200,
            fallback_reason=None,
        )
        return content, body

    def _handle_http_error(
        self, context: str, payload: dict[str, Any], resp: Any, started: float
    ) -> None:
        """All non-200 outcomes: raise, or return recovered (content, body)."""
        err_text = (resp.text or "")[:200]
        dur_ms = int((time.perf_counter() - started) * 1000)

        # Structured-output rejection: retry once without response_format.
        if resp.status_code == 400 and "response_format" in err_text:
            degraded = {k: v for k, v in payload.items() if k != "response_format"}
            try:
                resp2 = self._client.post(
                    self._url("chat/completions"), headers=self._headers(), json=degraded
                )
            except Exception as exc:
                self._log_fail(dur_ms, 400, {}, f"response_format rejected; retry errored: {type(exc).__name__}")
                raise ProviderUnavailable(
                    f"{context}: retry without response_format failed: {exc}"
                ) from exc
            if resp2.status_code == 200:
                body = _safe_json(resp2)
                self._record_success(body, started)
                self.fallback_reason = "gateway rejected response_format; retried without it"
                log_provider_call(
                    provider=self.name(),
                    model=self._model,
                    request_duration_ms=self.last_latency_ms,
                    token_usage=self.last_usage,
                    http_status=200,
                    fallback_reason=self.fallback_reason,
                )
                return _content_of(body), body
            err2 = (resp2.text or "")[:200]
            self._log_fail(dur_ms, resp2.status_code, {}, f"retry without response_format -> HTTP {resp2.status_code}")
            raise ProviderUnavailable(f"{context}: HTTP {resp2.status_code} ({err2})")

        self._log_fail(dur_ms, resp.status_code, {}, err_text)
        err_msg = ""
        try:
            err_json = resp.json()
            if isinstance(err_json, dict) and "error" in err_json:
                raw_e = err_json["error"]
                err_msg = raw_e.get("message") if isinstance(raw_e, dict) else str(raw_e)
        except Exception:
            pass
        detail = f" — {err_msg}" if err_msg else f" ({err_text})"

        if resp.status_code in (401, 403):
            raise ProviderUnavailable(
                f"{context}: HTTP {resp.status_code}{detail}. Check the API key and permissions for this provider."
            )
        if resp.status_code == 404:
            raise ProviderUnavailable(
                f"{context}: HTTP 404{detail}. Model '{self._model}' or endpoint not found on {self.base_url}."
            )
        if resp.status_code == 429:
            raise ProviderUnavailable(
                f"{context}: HTTP 429 (Rate Limit / Quota Exceeded){detail}."
            )
        if resp.status_code in (502, 503, 504):
            raise ProviderUnavailable(
                f"{context}: HTTP {resp.status_code} (Gateway / Upstream Service Unavailable){detail}."
            )
        raise ProviderUnavailable(f"{context}: HTTP {resp.status_code}{detail}")

    def _record_success(self, body: dict[str, Any], started: float) -> None:
        self.last_usage = body.get("usage") or {}
        self.last_latency_ms = int((time.perf_counter() - started) * 1000)
        self.last_provider = self.name()
        self.last_model = self._model
        self.last_status = 200
        self.fallback_reason = None

    def _log_fail(
        self, dur_ms: int, http_status: Optional[int], usage: dict, fallback_reason: Optional[str]
    ) -> None:
        try:
            log_provider_call(
                provider=self.name(),
                model=self._model,
                request_duration_ms=dur_ms,
                token_usage=usage,
                http_status=http_status,
                fallback_reason=fallback_reason,
            )
        except Exception:
            pass  # audit logging must never take the call path down


def _safe_json(resp: Any) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _content_of(body: dict[str, Any]) -> str:
    try:
        choice = body["choices"][0]
        msg = choice.get("message", {})
        content = (msg.get("content") or "").strip()
        if not content and choice.get("text"):
            content = (choice.get("text") or "").strip()
        if not content and msg.get("reasoning_content"):
            content = (msg.get("reasoning_content") or "").strip()
        return content
    except (KeyError, IndexError, TypeError):
        return ""


def _model_ids(data: Any) -> list[str]:
    """Extract model id strings from an OpenAI-style /models payload."""
    items = data.get("data", []) if isinstance(data, dict) else data
    ids: list[str] = []
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and isinstance(it.get("id"), str):
                ids.append(it["id"])
            elif isinstance(it, str):
                ids.append(it)
    seen: set[str] = set()
    ordered: list[str] = []
    for m in ids:
        if m not in seen:
            seen.add(m)
            ordered.append(m)
    for seed in _SEEDED_MODELS:
        if seed not in seen:
            seen.add(seed)
            ordered.append(seed)
    return ordered


def seeded_models() -> list[str]:
    return list(_SEEDED_MODELS)
