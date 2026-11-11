"""Rate-limit hint parsing shared by the planner providers."""
from __future__ import annotations

import re

_DURATION_RE = re.compile(
    r"(?:(\d+)h)?(?:(\d+)m)?([\d\.]+)(m?s)", re.IGNORECASE
)


def parse_retry_seconds(text: str) -> float:
    """Seconds from hints like 'in 11h15m48.8s', 'after 43s', 'in 899ms', 'retry-after: 30'."""
    if not text:
        return 0.0
    match = re.search(
        r"(?:retry|try\s+again)\s+(?:in|after)[:\s]\s*([0-9hms\.]+)", text, re.IGNORECASE
    )
    if not match:
        match = re.search(
            r"(?:retry[-_]after|retrydelay)'?\s*[:=]\s*'?([0-9hms\.]+)", text, re.IGNORECASE
        )
    if not match:
        return 0.0
    raw = match.group(1).lower()
    milli = re.fullmatch(r"([\d\.]+)ms", raw)
    if milli:
        try:
            return float(milli.group(1)) / 1000.0
        except ValueError:
            return 0.0
    total = 0.0
    matched = False
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)(h|m|s)", raw):
        matched = True
        amount = float(value)
        if unit == "h":
            total += amount * 3600
        elif unit == "m":
            total += amount * 60
        else:
            total += amount
    if matched:
        return total
    try:
        return float(raw)
    except ValueError:
        return 0.0


def parse_retry_headers(headers: Any) -> float:
    """Extract retry cooldown seconds from HTTP response headers (retry-after, x-ratelimit-reset-tokens, etc.)."""
    if not headers:
        return 0.0
    if hasattr(headers, "get"):
        for key in ("retry-after", "Retry-After", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
            val = headers.get(key)
            if val:
                sec = parse_retry_seconds(f"retry after {val}")
                if sec > 0:
                    return sec
                try:
                    return max(0.0, float(val))
                except (ValueError, TypeError):
                    pass
    return 0.0


def quota_scope(text: str) -> str:
    """'day' for a daily cap, 'minute' for a short window, '' if unknown."""
    if not text:
        return ""
    # Normalize camelCase and punctuation into spaced tokens
    norm = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    norm = re.sub(r"[_.:/\-]+", " ", norm)
    norm = re.sub(r"\s+", " ", norm).lower().strip()

    # Compact version (no spaces/punctuation) for concatenated all-lowercase checks
    compact = re.sub(r"[^a-z0-9]+", "", text.lower())

    # Daily checks: spaced or compact
    daily_spaced = (
        "tokens per day",
        "requests per day",
        "queries per day",
        "per day limit",
        "per day quota",
        "daily quota",
        "daily request",
        "daily limit",
    )
    daily_compact = (
        "tokensperday",
        "requestsperday",
        "queriesperday",
        "perdaylimit",
        "perdayquota",
        "dailyquota",
        "dailylimit",
    )
    if any(k in norm for k in daily_spaced) or any(k in compact for k in daily_compact):
        return "day"
    if re.search(r"\b(?:rpd|tpd)\b", norm):
        return "day"

    # Minute checks: spaced or compact
    minute_spaced = (
        "tokens per minute",
        "requests per minute",
        "queries per minute",
        "per minute limit",
        "per minute quota",
        "minute quota",
        "minute limit",
    )
    minute_compact = (
        "tokensperminute",
        "requestsperminute",
        "queriesperminute",
        "perminutelimit",
        "perminutequota",
        "minutelimit",
    )
    if any(k in norm for k in minute_spaced) or any(k in compact for k in minute_compact):
        return "minute"
    if re.search(r"\b(?:rpm|tpm)\b", norm):
        return "minute"

    return ""


import logging
provider_logger = logging.getLogger("privagent.providers")


def log_provider_call(
    *,
    provider: str,
    model: str,
    request_duration_ms: int,
    token_usage: dict[str, Any],
    http_status: int,
    fallback_reason: Optional[str] = None,
) -> None:
    """Structured telemetry logging for LLM provider operations and failovers."""
    prompt_tokens = token_usage.get("prompt_tokens") or token_usage.get("prompt_token_count") or 0
    completion_tokens = token_usage.get("completion_tokens") or token_usage.get("candidates_token_count") or 0
    total_tokens = token_usage.get("total_tokens") or token_usage.get("total_token_count") or (prompt_tokens + completion_tokens)

    msg = (
        f"[{provider.upper()}] model={model} status={http_status} "
        f"latency={request_duration_ms}ms "
        f"tokens=(in:{prompt_tokens}, out:{completion_tokens}, total:{total_tokens})"
    )
    if fallback_reason:
        msg += f" | fallback_reason=\"{fallback_reason}\""
    provider_logger.info(msg)

