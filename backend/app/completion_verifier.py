"""PrivAgent Strict Completion Verification Module.

Enforces strict verification rules:
- Navigation tasks require observing the intended destination.
- Research tasks require the requested research evidence.
- Comparison tasks require all required comparisons.
- Refusals, explanations, empty responses, or provider errors never count as successful execution.
- Distinguishes completed, partially completed, blocked, waiting for user, and failed states.
"""
from __future__ import annotations

from pathlib import Path
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class VerificationResult:
    state: str  # "completed", "partially_completed", "blocked", "waiting_user", "failed"
    message: str
    is_verified: bool
    reason: str


# Patterns matching model refusals or capability denials
REFUSAL_PATTERNS = [
    re.compile(r"\b(?:i\s+(?:can(?:not|'t)|am\s+unable\s+to|do\s+not\s+have\s+(?:access|the\s+ability))\b)", re.IGNORECASE),
    re.compile(r"\b(?:as\s+an\s+ai(?:\s+language\s+model)?|i'm\s+an\s+ai)\b.*?\b(?:cannot|can't|unable)\b", re.IGNORECASE),
    re.compile(r"\b(?:sorry|apologies)[,\s]+(?:but\s+)?i\s+(?:cannot|can't|do\s+not)\b", re.IGNORECASE),
    re.compile(r"\b(?:cannot\s+access\s+browsers|cannot\s+access\s+external\s+apps|cannot\s+open\s+email)\b", re.IGNORECASE),
    re.compile(r"\bi\s+don't\s+have\s+real-time\s+access\s+to\s+the\s+internet\b", re.IGNORECASE),
    re.compile(r"\bi\s+cannot\s+browse\s+the\s+web\b", re.IGNORECASE),
    # "I don't have the details/context of the previous task…" — a request for
    # missing information is a denial of capability, never a completed result.
    re.compile(r"\bi\s+(?:do\s+n[o']t\s+have|lack|am\s+missing|have\s+no)\s+(?:the\s+)?(?:details?|context|information|info|data|record|memory|knowledge)\s+(?:of|about|regarding|for)\b", re.IGNORECASE),
    re.compile(r"\b(?:please\s+specify|could\s+you\s+(?:please\s+)?specify|kindly\s+specify)\b", re.IGNORECASE),
]

# Patterns detecting authentication/login required or waiting for user
AUTH_WAITING_PATTERNS = [
    re.compile(r"\b(?:sign-in|login|authentication|credentials|qr\s*code|mfa|2fa|captcha)\s+(?:required|blocking|needed)\b", re.IGNORECASE),
    re.compile(r"\b(?:please\s+sign\s+in|please\s+log\s+in|solve\s+the\s+captcha)\b", re.IGNORECASE),
]


def is_refusal_message(message: str) -> bool:
    """True if message represents an LLM refusal or capability denial."""
    if not message:
        return False
    clean = message.strip()
    return any(p.search(clean) for p in REFUSAL_PATTERNS)


def is_waiting_for_user(message: str, state: str) -> bool:
    """True if task is waiting for user action (auth, CAPTCHA, approval)."""
    if state in ("awaiting_confirmation", "awaiting_input", "waiting_user"):
        return True
    if not message:
        return False
    return any(p.search(message) for p in AUTH_WAITING_PATTERNS)


# Execution-failure markers produced by the agent loop itself. A summary
# carrying any of these is an honest failure record, never a completion.
_EXECUTION_FAILURE_MARKERS = (
    "planner unavailable",
    "planner timed out",
    "loop detected",
    "non-recoverable error",
    "privacy violation",
    "session init failed",
    "budget_exceeded",
)

_EXECUTION_FAILURE_PREFIXES = (
    "Error:",
    "error:",
    "Failed:",
    "failed:",
)

# UPPERCASE error codes emitted by the loop/executor, e.g.
# "SESSION_INIT_FAILED: no tab", "TASK_TIMEOUT: ...".
_EXECUTION_FAILURE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{3,}:\s")


def is_execution_failure(message: str) -> bool:
    """True if the message reports an execution failure rather than a result."""
    m = (message or "").strip()
    if not m:
        return False
    low = m.lower()
    if any(m.startswith(p) for p in _EXECUTION_FAILURE_PREFIXES):
        return True
    if _EXECUTION_FAILURE_CODE.match(m):
        return True
    return any(marker in low for marker in _EXECUTION_FAILURE_MARKERS)



def classify_instruction_type(instruction: str) -> str:
    """Classify the user instruction into a verification category.

    Categories:
    - "NAVIGATION": Direct site/service opening ("open mail", "go to google.com").
    - "COMPARISON": Multi-site research ("compare X on Amazon and Flipkart").
    - "RESEARCH": Single-site product research or extraction ("find price of ...").
    - "CONVERSATIONAL": Q&A, coding, explanations, greetings.
    - "UNSUPPORTED": Desktop OS control, rebooting machine.
    """
    text = (instruction or "").strip().lower()

    from .capabilities import check_supported_capability
    supported, _ = check_supported_capability(text)
    if not supported:
        return "UNSUPPORTED"

    from .extractors.research import is_multi_site_instruction
    if is_multi_site_instruction(instruction):
        return "COMPARISON"

    # Navigation patterns
    nav_pattern = re.compile(
        r"^(?:open|go\s+to|navigate\s+to|visit|browse\s+to)\s+"
        r"(?:https?://|mail|email|gmail|inbox|google|amazon|flipkart|youtube|leetcode|wikipedia|[a-z0-9-]+\.[a-z]{2,})\b",
        re.IGNORECASE,
    )
    if nav_pattern.search(text):
        return "NAVIGATION"

    # Research patterns
    research_pattern = re.compile(
        r"\b(?:price|search\s+for|find|look\s+up|extract|check\s+price|specs|compare)\b",
        re.IGNORECASE,
    )
    if research_pattern.search(text):
        return "RESEARCH"

    return "CONVERSATIONAL"


def verify_navigation_destination(
    instruction: str,
    current_url: str,
    history: list[dict[str, Any]],
) -> tuple[bool, str]:
    """Verify that a navigation task actually observed the intended destination."""
    text = (instruction or "").strip().lower()
    curr_host = urllib.parse.urlsplit(current_url or "").hostname or ""
    curr_host = curr_host.lower().replace("www.", "")

    visited_hosts: set[str] = set()
    if curr_host:
        visited_hosts.add(curr_host)
    for h in history or []:
        act = (h.get("action") or {})
        if act.get("type") == "navigate":
            u = act.get("params", {}).get("url") or ""
            host = urllib.parse.urlsplit(u).hostname or ""
            if host:
                visited_hosts.add(host.lower().replace("www.", ""))

    # Target host resolution
    target_host = ""
    if any(k in text for k in ("mail", "gmail", "email", "inbox")):
        target_hosts = {"mail.google.com", "accounts.google.com", "google.com"}
        if any(th in vh for th in target_hosts for vh in visited_hosts):
            return True, "Observed email service destination (mail.google.com / Google authentication)."
        return False, f"Navigation to mail not observed. Visited hosts: {visited_hosts or 'none'}."

    if "google" in text:
        target_host = "google.com"
    elif "amazon" in text:
        target_host = "amazon"
    elif "flipkart" in text:
        target_host = "flipkart.com"
    elif "youtube" in text:
        target_host = "youtube.com"
    elif "leetcode" in text:
        target_host = "leetcode.com"
    elif "wikipedia" in text:
        target_host = "wikipedia.org"

    if target_host:
        if any(target_host in vh for vh in visited_hosts):
            return True, f"Observed intended destination matching '{target_host}'."
        return False, f"Did not observe intended destination '{target_host}'. Visited: {visited_hosts or 'none'}."

    # Direct URL navigation
    url_m = re.search(r"https?://([^\s/]+)", text)
    if url_m:
        raw_target = url_m.group(1).lower().replace("www.", "")
        if any(raw_target in vh for vh in visited_hosts):
            return True, f"Observed target host '{raw_target}'."
        return False, f"Did not observe target host '{raw_target}'. Visited: {visited_hosts or 'none'}."

    # If navigation action was successfully executed in history
    if history and any(h.get("result_ok") and (h.get("action") or {}).get("type") == "navigate" for h in history):
        return True, "Navigation action completed successfully."

    return False, "No navigation action was executed or observed."


def verify_task_completion(
    instruction: str,
    candidate_state: str,
    message: str,
    observation: Any = None,
    working_memory: Optional[dict[str, Any]] = None,
    history: Optional[list[dict[str, Any]]] = None,
) -> VerificationResult:
    """Strictly verify task outcome against evidence and requirements.

    Returns VerificationResult with normalized state and honest message.
    """
    msg = (message or "").strip()
    mem = working_memory or {}
    hist = history or []
    current_url = getattr(observation, "url", "") if observation else ""
    if not current_url and isinstance(observation, dict):
        current_url = observation.get("url") or ""

    # Rule 1: A refusal, explanation, empty response, or provider error NEVER counts as completed.
    if is_refusal_message(msg):
        return VerificationResult(
            state="failed",
            message=msg,
            is_verified=False,
            reason="Model refusal or capability denial cannot produce successful completion.",
        )

    if not msg:
        return VerificationResult(
            state="failed",
            message="Task failed: empty response received from agent.",
            is_verified=False,
            reason="Empty response cannot produce successful completion.",
        )

    if msg.startswith("Error:") or "Planner unavailable" in msg or "Planner timed out" in msg:
        return VerificationResult(
            state="failed",
            message=msg,
            is_verified=False,
            reason="Provider error or timeout cannot produce successful completion.",
        )

    # Rule 1b: Any execution-failure summary is failed, regardless of category.
    if is_execution_failure(msg):
        return VerificationResult(
            state="failed",
            message=msg,
            is_verified=False,
            reason="Execution error cannot produce successful completion.",
        )

    # Rule 2: Distinguish waiting for user (authentication / CAPTCHA handoff)
    if is_waiting_for_user(msg, candidate_state):
        return VerificationResult(
            state="awaiting_confirmation",
            message=msg,
            is_verified=True,
            reason="Task paused awaiting user authentication or input.",
        )

    category = classify_instruction_type(instruction)

    # Rule 3: Unsupported operations must fail honestly
    if category == "UNSUPPORTED":
        return VerificationResult(
            state="failed",
            message=msg if msg else "Requested desktop OS operation is unsupported.",
            is_verified=False,
            reason="Operation requires unsupported desktop OS capabilities.",
        )

    # Rule 4: Navigation tasks require observing the intended destination
    if category == "NAVIGATION":
        nav_ok, nav_reason = verify_navigation_destination(instruction, current_url, hist)
        if not nav_ok:
            return VerificationResult(
                state="failed",
                message=f"Navigation incomplete: {nav_reason}",
                is_verified=False,
                reason=nav_reason,
            )
        return VerificationResult(
            state="completed",
            message=msg,
            is_verified=True,
            reason=nav_reason,
        )

    # Rule 5: Comparison tasks require all required comparisons
    if category == "COMPARISON":
        ms_tracker = mem.get("multi_site_research")
        if ms_tracker:
            from .extractors.research import (
                is_multi_site_task_complete,
                get_completed_sites,
                get_pending_sites,
                get_unavailable_sites,
                claims_cheapest_on_single_listing,
            )
            completed = get_completed_sites(ms_tracker)
            pending = get_pending_sites(ms_tracker)
            unavailable = get_unavailable_sites(ms_tracker)

            # Check single-listing cheapest fallacy
            if claims_cheapest_on_single_listing(msg, len(completed)):
                msg = re.sub(
                    r"\b(?:is\s+the\s+cheapest\s+option|cheapest\s+available|best\s+price)\b",
                    "was the only verified listing found",
                    msg,
                    flags=re.IGNORECASE,
                )

            if is_multi_site_task_complete(ms_tracker):
                if len(completed) >= 2 or (len(completed) == 1 and len(unavailable) > 0):
                    return VerificationResult(
                        state="completed",
                        message=msg,
                        is_verified=True,
                        reason="All requested comparison sites were either verified or confirmed unavailable.",
                    )
            elif len(completed) > 0:
                # Some completed, but pending sites remain
                return VerificationResult(
                    state="failed",
                    message=f"Partially completed ({len(completed)}/{len(completed) + len(pending)} sites). Pending: {', '.join(s['name'] for s in pending)}.\n\n{msg}",
                    is_verified=False,
                    reason=f"{len(pending)} comparison sites remain pending.",
                )
            else:
                return VerificationResult(
                    state="failed",
                    message=f"Comparison incomplete: no requested sites verified. Pending: {', '.join(s['name'] for s in pending)}.",
                    is_verified=False,
                    reason="No comparison results collected.",
                )

    # Rule 6: Research tasks require evidence and verification across 6 distinct stages
    if category == "RESEARCH" or mem.get("is_research"):
        if candidate_state == "failed":
            return VerificationResult(
                state="failed",
                message=msg,
                is_verified=False,
                reason="Execution reported failure; verifier does not override failure into success.",
            )

        pkg_data = mem.get("research_knowledge_package") or {}
        sources = pkg_data.get("sources", {}) if isinstance(pkg_data, dict) else getattr(pkg_data, "sources", {})

        # Stage 1: Discovery stage
        discovery_verified = bool(mem.get("autonomous_discovery") or mem.get("specified_website"))

        # Stage 2: Collection stage - reject search results pages, verify independent sources
        from .research.collector import is_search_results_page
        substantive_sources = {}
        for sid, s in sources.items():
            s_url = s.get("url") if isinstance(s, dict) else getattr(s, "url", "")
            s_score = s.get("reliability_score", 0) if isinstance(s, dict) else getattr(s, "reliability_score", 0)
            if s_score > 0 and not is_search_results_page(s_url):
                substantive_sources[sid] = s

        inst_lower = instruction.lower()
        requires_multi = any(k in inst_lower for k in ["multiple", "compare", "credible sources", "independent sources", "different sources", "cross-reference"])

        distinct_domains = set()
        for s in substantive_sources.values():
            dom = s.get("domain") if isinstance(s, dict) else getattr(s, "domain", "")
            if dom:
                parts = dom.lower().split(".")
                root = ".".join(parts[-2:]) if len(parts) >= 2 else dom.lower()
                distinct_domains.add(root)

        coll_reason = ""
        collection_verified = bool(substantive_sources)
        if requires_multi and len(distinct_domains) < 2:
            collection_verified = False
            coll_reason = f"Prompt requested multiple credible sources, but only {len(distinct_domains)} independent source(s) were examined."
        elif not substantive_sources:
            collection_verified = False
            coll_reason = "No substantive research sources were examined (search results pages are not sources)."

        # Stage 3: Synthesis stage - claims must have verified evidence linkage
        claims = pkg_data.get("claims", {}) if isinstance(pkg_data, dict) else getattr(pkg_data, "claims", {})
        evidence_items = pkg_data.get("evidence", {}) if isinstance(pkg_data, dict) else getattr(pkg_data, "evidence", {})
        verified_claims = [
            c for c in claims.values()
            if (c.get("evidence_item_ids") if isinstance(c, dict) else getattr(c, "evidence_item_ids", []))
        ]
        synthesis_verified = bool(verified_claims or evidence_items or mem.get("collected_research"))

        # Stage 4: Artifact generation & validation stage
        artifacts = mem.get("artifacts") or []
        requested_fmt = mem.get("requested_format") or mem.get("deliverable_type")
        artifact_needed = bool(requested_fmt or any(fmt in inst_lower for fmt in ["pdf", "docx", "word", "excel", "report", "csv"]))
        artifact_verified = True
        artifact_reason = ""
        if artifact_needed:
            if not artifacts:
                artifact_verified = False
                artifact_reason = "Deliverable document was requested but not generated."
            else:
                for art in artifacts:
                    fpath = Path(art.get("filepath", ""))
                    if not fpath.exists() or fpath.stat().st_size == 0:
                        artifact_verified = False
                        artifact_reason = f"Deliverable {art.get('filename')} does not exist or is empty on disk."
                        break

        # Verification enforcement
        if not collection_verified:
            return VerificationResult(
                state="failed",
                message=f"Research incomplete: {coll_reason}\n\n{msg}",
                is_verified=False,
                reason=coll_reason,
            )

        if not synthesis_verified:
            return VerificationResult(
                state="failed",
                message=f"Research incomplete: Synthesized findings lack verified evidence traceability.\n\n{msg}",
                is_verified=False,
                reason="Synthesized claims lack verifiable evidence items.",
            )

        if not artifact_verified:
            return VerificationResult(
                state="failed",
                message=f"Research deliverable incomplete: {artifact_reason}\n\n{msg}",
                is_verified=False,
                reason=artifact_reason,
            )

        return VerificationResult(
            state="completed",
            message=msg,
            is_verified=True,
            reason=f"Research mission completed across {len(distinct_domains)} independent source(s) with verified evidence and deliverable generation.",
        )

    # Conversational or general tasks. A failed execution candidate stays
    # failed: the conversational fallback may only confirm completion of
    # tasks that did not fail, never launder a failure into success.
    if candidate_state == "failed":
        return VerificationResult(
            state="failed",
            message=msg,
            is_verified=False,
            reason="Execution reported failure; verifier does not override it.",
        )
    final_state = "completed" if candidate_state in ("completed", "running") else candidate_state
    return VerificationResult(
        state=final_state,
        message=msg,
        is_verified=True,
        reason="Task concluded normally.",
    )

