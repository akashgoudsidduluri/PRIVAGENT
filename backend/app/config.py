import os
from pathlib import Path
from dotenv import load_dotenv

_env_file = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=_env_file, override=True)
load_dotenv(override=True)

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

# DeepAI Gateway — local OpenAI-compatible service for DeepSeek & free models.
DEEPAI_GATEWAY_BASE_URL = os.getenv("DEEPAI_GATEWAY_BASE_URL", "http://127.0.0.1:8787/v1").strip()
DEEPAI_GATEWAY_KEY = os.getenv("DEEPAI_GATEWAY_KEY", "").strip()
DEEPAI_GATEWAY_MODEL = os.getenv("DEEPAI_GATEWAY_MODEL", "deepseek-v4.1-flash").strip()

# Task 2: LLM keys that were migrated out of backend/.env into the encrypted
# secret store are restored here (the environment still wins). Best effort
# only — a missing/corrupt store leaves the env value in place — and skipped
# under pytest so the test suite never picks up an operator's real keys.
# This runs after load_dotenv above, so PRIVAGENT_MASTER_KEY from .env is
# already visible to the store. config values are read once at import; the
# static provider paths in agentLoop/provider_settings therefore keep
# working unchanged after a migration.
try:
    from . import secrets as _secrets_store

    if not _secrets_store.is_pytest():
        _llm_key_vars = {
            "GEMINI_API_KEY": "GEMINI_API_KEY",
            "GROQ_API_KEY": "GROQ_API_KEY",
            "ANTSEED_API_KEY_ENV": "ANTSEED_API_KEY",
            "DEEPAI_GATEWAY_KEY": "DEEPAI_GATEWAY_KEY",
        }
        for _var, _env_key in _llm_key_vars.items():
            if not globals().get(_var):
                _stored = _secrets_store.get_secret_for_env_key(_env_key)
                if _stored:
                    globals()[_var] = _stored
except Exception:
    pass

PROVIDER = os.getenv(
    "NAVIGATOR_PROVIDER",
    "deepai" if DEEPAI_GATEWAY_KEY else ("groq" if GROQ_API_KEY else ("gemini" if GEMINI_API_KEY else "mock"))
)
# Task 9: the loop used to die at 25 steps, which is far too short for a
# real multi-site task (the OSWorld 2.0 median is ~1.6 h / ~318 calls).
# The default is now 50 steps; a task explicitly marked long_horizon
# gets 100. Both are operator-tunable; the trade-off is the same as the
# perception limits — more steps = more tokens, more time, more cost.
MAX_STEPS = int(os.getenv("PRIVAGENT_MAX_STEPS", os.getenv("MAX_STEPS", "50")))
MAX_LONG_STEPS = int(os.getenv("PRIVAGENT_MAX_LONG_STEPS", "100"))
# Long-horizon compaction: every N steps the loop writes a deterministic
# summary (done / remaining / key findings) into the checkpoint and
# plans from the summary + the last few steps instead of the full
# history. 25 keeps the prompt bounded while the history stays useful.
COMPACTION_EVERY_STEPS = int(os.getenv("PRIVAGENT_COMPACTION_EVERY_STEPS", "25"))
# Long-memory recall caps (Task 9): the planner prompt gets at most this
# many recalled facts / skills, bounded by this many chars in total.
# Recall is keyword-scored and deterministic — no embeddings, no LLM.
MEMORY_RECALL_FACTS = int(os.getenv("PRIVAGENT_MEMORY_RECALL_FACTS", "5"))
MEMORY_RECALL_SKILLS = int(os.getenv("PRIVAGENT_MEMORY_RECALL_SKILLS", "2"))
MEMORY_RECALL_MAX_CHARS = int(os.getenv("PRIVAGENT_MEMORY_RECALL_MAX_CHARS", "800"))
# Scheduled-task runner (Task 9): how often the in-process scheduler
# claims due jobs. A real deployment should use the OS scheduler
# instead for durability across restarts (see docs/MEMORY_LONG_HORIZON.md).
SCHEDULER_TICK_S = float(os.getenv("PRIVAGENT_SCHEDULER_TICK_S", "30"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
TASK_TIMEOUT_S = float(os.getenv("TASK_TIMEOUT_S", "360"))

# Screenshots are local-only by default. Remote vision is opt-in because a
# rendered page can contain PII that never appears in the DOM snapshot.
ALLOW_SCREENSHOTS = os.getenv("ALLOW_SCREENSHOTS", "false").strip().lower() in {"1", "true", "yes", "on"}
# --- Task 7: perception limits -------------------------------------------
# The planner used to see at most 50 elements and 2,500 chars of page text,
# which blinded it on any dense page. The new defaults are ~4x larger, and
# every one is operator-tunable. The trade-off is real: a bigger context
# costs more tokens per step (slower + pricier on metered providers) and a
# longer prompt dilutes the instruction. Lower these if latency or cost
# matters more than coverage; raise them only with a token budget in mind.
# The PRIVAGENT_-prefixed names win; the legacy bare names still work as a
# fallback so existing .env files keep working.
MAX_INSTRUCTION_CHARS = int(
    os.getenv("PRIVAGENT_MAX_INSTRUCTION_CHARS", os.getenv("MAX_INSTRUCTION_CHARS", "8000"))
)
MAX_ELEMENTS = int(
    os.getenv("PRIVAGENT_MAX_ELEMENTS", os.getenv("MAX_INTERACTIVE_ELEMENTS", "200"))
)
# Back-compat alias: privacy.py and tests read MAX_INTERACTIVE_ELEMENTS.
MAX_INTERACTIVE_ELEMENTS = MAX_ELEMENTS
MAX_PAGE_TEXT_CHARS = int(
    os.getenv("PRIVAGENT_MAX_PAGE_TEXT_CHARS", os.getenv("MAX_PAGE_TEXT_CHARS", "8000"))
)
# Accessibility-tree nodes kept per observation. The AX tree is built from
# the (already capped) interactive list, so this binds only when an
# observation carries more nodes than elements; 300 leaves headroom for
# the tree to include non-interactive structural nodes later.
MAX_AX_NODES = int(os.getenv("PRIVAGENT_MAX_AX_NODES", "300"))
# OCR over screenshots is disabled by default: it only runs when the
# operator already opted into screenshots (ALLOW_SCREENSHOTS) AND turns
# this on, because OCR text is extracted from a rendered image that may
# contain PII the DOM redactor never saw. See backend/app/ocr.py.
OCR_ENABLED = os.getenv("PRIVAGENT_OCR_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
OCR_MAX_CHARS = int(os.getenv("PRIVAGENT_OCR_MAX_CHARS", "2000"))

# --- Task 8: site hints -------------------------------------------------
# Advisory per-domain notes for the planner (backend/app/site_hints.json).
# Default on: they are short human-readable descriptions, never selectors,
# and never override the live accessibility tree. Set
# PRIVAGENT_SITE_HINTS=false to run in pure-general mode — the agent must
# produce the same plan with hints on or off; a plan that needs a hint to
# work is a bug.
SITE_HINTS_ENABLED = os.getenv("PRIVAGENT_SITE_HINTS", "true").strip().lower() in {"1", "true", "yes", "on"}

# --- Task 4: safe credential / payment path ---------------------------
# Takeover: how long the operator has to finish a login / payment / OTP
# step in their own browser before the loop fails the task honestly.
# The operator needs real time to type a password or read an OTP off
# their phone, so this is deliberately much longer than the 120 s
# confirmation gate.
TAKEOVER_TIMEOUT_S = float(os.getenv("PRIVAGENT_TAKEOVER_TIMEOUT_S", "600"))
# A task may enter takeover at most twice: a third login/payment/OTP
# surface after two completed takeovers means the operator's manual
# step did not clear the blocker, and looping forever helps nobody.
MAX_TAKEOVERS_PER_TASK = int(os.getenv("PRIVAGENT_MAX_TAKEOVERS_PER_TASK", "2"))

# Payments: every payment requires explicit user approval + takeover,
# regardless of amount. Above this amount (in PAYMENT_CURRENCY units)
# a payment is blocked outright unless the operator explicitly raises
# the limit. The default is deliberately low: raise it only when you
# mean to. No currency conversion is performed — the limit applies to
# the amount as stated, in PAYMENT_CURRENCY; a payment stated in a
# different currency is treated as above the limit unless the operator
# confirms it in takeover (see payments.validate_payment).
MAX_PAYMENT_AMOUNT = float(os.getenv("PRIVAGENT_MAX_PAYMENT_AMOUNT", "5000"))
PAYMENT_CURRENCY = os.getenv("PRIVAGENT_PAYMENT_CURRENCY", "INR").strip().upper() or "INR"

# --- Task 3: isolated agent profile + domain allow-list -------------------
# The agent must not run in the operator's everyday Chrome profile, where
# it would inherit cookies, saved logins and browsing history. Launch
# Chrome for agent work with:
#   chrome --user-data-dir=<AGENT_PROFILE_DIR>
# (see scripts/launch-agent-chrome.sh / .bat). The directory holds only
# agent-profile state; it is gitignored under backend/data/.
# Chrome does not expose which profile an extension runs in, so the
# backend cannot *prove* isolation — the extension reports an
# operator-attested flag (see /api/isolation/status) and the docs say so.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
AGENT_PROFILE_DIR = Path(
    os.getenv("PRIVAGENT_AGENT_PROFILE_DIR", "") or (_BACKEND_DIR / "data" / "agent-profile")
).expanduser()

# Domain allow-list (comma-separated, e.g. "google.com, *.wikipedia.org").
# Empty = not configured: navigation is allowed everywhere and task
# creation logs a warning. A configured list is a hard gate enforced in
# the planner, the agent loop, and the extension (defence in depth).
# A plain entry matches the host and its subdomains; "*.example.com"
# matches subdomains only. An invalid entry logs a warning and is
# ignored (the app must still start); the rest of the list still applies.
import logging as _logging

try:
    from .allowlist import parse_allowed_domains as _parse_allowed_domains

    ALLOWED_DOMAINS: list[str] = _parse_allowed_domains(
        os.getenv("PRIVAGENT_ALLOWED_DOMAINS", "")
    )
except Exception as _exc:  # noqa: BLE001 — a bad env value must not kill startup
    _logging.getLogger("navigator.config").warning(
        "PRIVAGENT_ALLOWED_DOMAINS is invalid (%s); running with no domain allow-list", _exc
    )
    ALLOWED_DOMAINS = []

# REVIEW (Task 9 audit): a stale backend/.env that still sets legacy
# MAX_STEPS (25 in the old .env.example) silently overrides the new
# 50-step default, because the legacy fallback above wins when
# PRIVAGENT_MAX_STEPS is unset. .env.example was fixed to 50, but an
# operator who copied the old one would never notice. Warn loudly so
# the override is visible; PRIVAGENT_MAX_STEPS always wins when both
# are set.
if "PRIVAGENT_MAX_STEPS" not in os.environ and "MAX_STEPS" in os.environ:
    _logging.getLogger("navigator.config").warning(
        "legacy MAX_STEPS=%s is set without PRIVAGENT_MAX_STEPS; it "
        "overrides the new 50-step default — set PRIVAGENT_MAX_STEPS "
        "explicitly to use the new default", os.environ["MAX_STEPS"]
    )

# --- Task 10: privacy beyond redaction -----------------------------------
# Local planner (Ollama): a planner that runs on this machine and never
# sends page data to a cloud provider. The operator installs Ollama and
# pulls a model (e.g. `ollama pull qwen2.5:7b`); PrivAgent never downloads
# a model itself. The base URL must be loopback — the provider refuses
# any non-loopback URL so a misconfigured env can never send page data
# to a remote host. See docs/LOCAL_PLANNER.md.
OLLAMA_BASE_URL = os.getenv("PRIVAGENT_OLLAMA_BASE_URL", "http://127.0.0.1:11434").strip()
OLLAMA_MODEL = os.getenv("PRIVAGENT_OLLAMA_MODEL", "qwen2.5:7b").strip()
# PRIVAGENT_PLANNER is the operator-facing alias for NAVIGATOR_PROVIDER;
# it wins when both are set. Values: mock, groq, gemini, antseed,
# deepai, ollama. The default chain is unchanged (deepai -> groq ->
# gemini -> mock); ollama joins the chain only when explicitly selected
# here or via the local-first / local-only flags below.
if os.getenv("PRIVAGENT_PLANNER", "").strip():
    PROVIDER = os.getenv("PRIVAGENT_PLANNER", "").strip().lower()
# Local-only: the planner chain is ollama -> fail. When Ollama is
# unreachable the task fails honestly; it NEVER silently falls back to
# a cloud provider, because that would send page data out after the
# operator asked for local-only.
PLANNER_LOCAL_ONLY = os.getenv("PRIVAGENT_PLANNER_LOCAL_ONLY", "false").strip().lower() in {"1", "true", "yes", "on"}
# Local-first: try Ollama first; on failure fall back to the normal
# chain (deepai -> groq -> gemini -> mock). Off by default.
PLANNER_LOCAL_FIRST = os.getenv("PRIVAGENT_PLANNER_LOCAL_FIRST", "false").strip().lower() in {"1", "true", "yes", "on"}

# Zero retention (Task 10): when on, planner payloads are never written
# to logs, trajectories, or checkpoints; page text and screenshots are
# not persisted; the in-memory history is pruned to the last few steps.
# Checkpoints are skipped entirely, so a zero-retention task cannot be
# resumed after a restart. The operator's own long-term memory store
# (Task 9) is separate and unaffected. See docs/PRIVACY.md.
ZERO_RETENTION = os.getenv("PRIVAGENT_ZERO_RETENTION", "false").strip().lower() in {"1", "true", "yes", "on"}
# In-memory history kept per task under zero retention (loop detection
# and compaction need the last few steps; older steps are dropped).
ZERO_RETENTION_HISTORY_KEEP = int(os.getenv("PRIVAGENT_ZERO_RETENTION_HISTORY_KEEP", "3"))

# Audit log (Task 10): append-only, hash-chained record of
# security-relevant events (auth failures, allow-list denials,
# confirmation decisions, takeover, payments, memory writes/deletes,
# scheduler claims). Summaries are scrubbed — never secrets or PII.
# See backend/app/audit.py.
AUDIT_LOG_PATH = Path(
    os.getenv("PRIVAGENT_AUDIT_LOG_PATH", "") or (_BACKEND_DIR / "data" / "audit.log")
).expanduser()
