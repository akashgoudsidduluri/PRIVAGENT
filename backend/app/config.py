import os
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
# Optional operator overrides; providers fall back to their verified defaults.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "").strip()
# Antseed — generic OpenAI-compatible gateway. The API key is NEVER hardcoded:
# it comes from the environment or is entered at runtime via /api/llm/config.
# An empty key leaves the provider unconfigured even if NAVIGATOR_PROVIDER=name.
ANTSEED_BASE_URL = os.getenv("ANTSEED_BASE_URL", "http://localhost:8377/v1").strip()
ANTSEED_API_KEY_ENV = os.getenv("ANTSEED_API_KEY", "").strip()
ANTSEED_MODEL = os.getenv("ANTSEED_MODEL", "").strip()
PROVIDER = os.getenv("NAVIGATOR_PROVIDER", "groq" if GROQ_API_KEY else ("gemini" if GEMINI_API_KEY else "mock"))
MAX_STEPS = int(os.getenv("MAX_STEPS", "25"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
TASK_TIMEOUT_S = float(os.getenv("TASK_TIMEOUT_S", "360"))

# Screenshots are local-only by default. Remote vision is opt-in because a
# rendered page can contain PII that never appears in the DOM snapshot.
ALLOW_SCREENSHOTS = os.getenv("ALLOW_SCREENSHOTS", "false").strip().lower() in {"1", "true", "yes", "on"}
MAX_INSTRUCTION_CHARS = int(os.getenv("MAX_INSTRUCTION_CHARS", "4000"))
MAX_INTERACTIVE_ELEMENTS = int(os.getenv("MAX_INTERACTIVE_ELEMENTS", "50"))
MAX_PAGE_TEXT_CHARS = int(os.getenv("MAX_PAGE_TEXT_CHARS", "2500"))
