import os
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
# Optional operator overrides; providers fall back to their verified defaults.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "").strip()
PROVIDER = os.getenv("NAVIGATOR_PROVIDER", "").strip().lower() or (
    "groq" if GROQ_API_KEY else ("gemini" if GEMINI_API_KEY else "mock")
)
PROVIDER_NAMES = ("groq", "gemini", "mock")
MAX_STEPS = int(os.getenv("MAX_STEPS", "25"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
TASK_TIMEOUT_S = float(os.getenv("TASK_TIMEOUT_S", "360"))

# Screenshots are local-only by default. Remote vision is opt-in because a
# rendered page can contain PII that never appears in the DOM snapshot.
ALLOW_SCREENSHOTS = os.getenv("ALLOW_SCREENSHOTS", "false").strip().lower() in {"1", "true", "yes", "on"}
MAX_INSTRUCTION_CHARS = int(os.getenv("MAX_INSTRUCTION_CHARS", "4000"))


def _selection(requested: str, effective: str, fallback: bool, reason: str) -> dict[str, object]:
    return {
        "requested": requested,
        "effective": effective,
        "fallback": fallback,
        "reason": reason,
    }


def resolve_provider() -> dict[str, object]:
    """Resolve the requested provider against the available credentials.

    ``NAVIGATOR_PROVIDER`` is a request, not a guarantee: a remote provider
    without its key falls back to the other configured provider or to the
    deterministic mock planner. ``/api/health`` reports both the requested and
    the effective provider so a deployment cannot silently believe it is
    calling a remote model. Keys are never included in the result.
    """
    requested = (PROVIDER or "").strip().lower()
    if not requested:
        requested = "groq" if GROQ_API_KEY else ("gemini" if GEMINI_API_KEY else "mock")

    if requested == "mock":
        return _selection("mock", "mock", False, "")
    if requested == "groq" and GROQ_API_KEY:
        return _selection("groq", "groq", False, "")
    if requested == "gemini" and GEMINI_API_KEY:
        return _selection("gemini", "gemini", False, "")

    if requested == "groq" and GEMINI_API_KEY:
        effective = "gemini"
    elif requested == "gemini" and GROQ_API_KEY:
        effective = "groq"
    elif GROQ_API_KEY:
        effective = "groq"
    elif GEMINI_API_KEY:
        effective = "gemini"
    else:
        effective = "mock"

    if requested in PROVIDER_NAMES:
        reason = f"{requested} is not configured; using {effective}"
    else:
        reason = f"unknown provider {requested!r}; using {effective}"
    return _selection(requested, effective, True, reason)
