"""PrivAgent Capability Registry and Awareness System.

Defines supported capabilities, unsupported operation detection,
and honest system awareness prompts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from .connectors import GmailConnector, OutlookConnector, GoogleCalendarConnector, GoogleDriveConnector, SlackConnector, GitHubConnector, LocalFileSystemAdapter


class Capability(str, Enum):
    BROWSER_AUTOMATION = "BROWSER_AUTOMATION"
    PRIVACY_PRESERVATION = "PRIVACY_PRESERVATION"
    AUTH_HANDOFF = "AUTH_HANDOFF"
    MULTI_SITE_RESEARCH = "MULTI_SITE_RESEARCH"
    CONVERSATIONAL_REASONING = "CONVERSATIONAL_REASONING"


@dataclass
class CapabilityDeclaration:
    capability: Capability
    name: str
    description: str
    supported: bool = True


SUPPORTED_CAPABILITIES: dict[Capability, CapabilityDeclaration] = {
    Capability.BROWSER_AUTOMATION: CapabilityDeclaration(
        capability=Capability.BROWSER_AUTOMATION,
        name="Real-Browser Automation",
        description="Autonomous navigation, DOM interaction (clicking, typing, scrolling), multi-tab management, web search, webmail (Gmail/Outlook), e-commerce research, and LeetCode inspection via connected Chrome extension.",
        supported=True,
    ),
    Capability.PRIVACY_PRESERVATION: CapabilityDeclaration(
        capability=Capability.PRIVACY_PRESERVATION,
        name="Fail-Closed Privacy Filter",
        description="Client-side and server-side fail-closed PII redaction of passwords, credit cards, SSNs, Aadhaar numbers, and private authentication tokens before DOM data reaches models.",
        supported=True,
    ),
    Capability.AUTH_HANDOFF: CapabilityDeclaration(
        capability=Capability.AUTH_HANDOFF,
        name="Human Authentication Handoff",
        description="Secure pause and handoff for CAPTCHAs, MFA, two-factor challenges, and sign-in screens, allowing the user to sign in manually while preserving tab session authorization.",
        supported=True,
    ),
    Capability.MULTI_SITE_RESEARCH: CapabilityDeclaration(
        capability=Capability.MULTI_SITE_RESEARCH,
        name="Multi-Site Cross-Store Research",
        description="Structured product search and price comparison across multiple retailers (Amazon, Flipkart, Samsung, etc.), tracking verified listings, unavailable stores, and unvisited sites.",
        supported=True,
    ),
    Capability.CONVERSATIONAL_REASONING: CapabilityDeclaration(
        capability=Capability.CONVERSATIONAL_REASONING,
        name="Conversational & Analytical Reasoning",
        description="Fast-path conversational answers, algorithms, coding assistance, calculations, and explanations without launching browser sessions.",
        supported=True,
    ),
}

# Patterns of explicitly unsupported system/desktop operations
UNSUPPORTED_SYSTEM_PATTERNS = [
    re.compile(r"\b(?:reboot|restart|shut\s*down|power\s*off)\s+(?:(?:my|the|this)\s+)?(?:physical\s+)?(?:computer|pc|laptop|windows|machine|system|device)\b", re.IGNORECASE),
    re.compile(r"\b(?:format|wipe|erase)\s+(?:(?:my|the|this)\s+)?(?:hard\s*drive|disk|drive|partition|c:)\b", re.IGNORECASE),
    re.compile(r"\b(?:hack|crack|bypass|steal)\s+(?:into\s+)?(?:someone's|any|a|the)?\s*(?:bank|account|password|database|wifi)\b", re.IGNORECASE),
    re.compile(r"\b(?:install|run|execute)\s+(?:malware|virus|trojan|ransomware|keylogger)\b", re.IGNORECASE),
    re.compile(r"\b(?:open|launch|run)\s+(?:calculator|notepad|paint|excel|word|terminal|cmd|powershell)\s+(?:on\s+my\s+desktop|app|application|locally)\b", re.IGNORECASE),
]

# Patterns where user explicitly asks about agent capabilities
CAPABILITY_QUERY_PATTERN = re.compile(
    r"^(?:what\s+can\s+you\s+do|what\s+are\s+your\s+capabilities|what\s+are\s+you\s+capable\s+of|tell\s+me\s+what\s+you\s+can\s+do|who\s+are\s+you|what\s+is\s+privagent)\b",
    re.IGNORECASE,
)

SYSTEM_CAPABILITY_PROMPT = """You are PrivAgent, an advanced privacy-preserving AI browser automation and web co-pilot.

YOUR REAL CAPABILITIES:
1. Browser Automation: You have a connected Chrome extension that can open websites, navigate, click, type, search, and extract information across the web (Amazon, Flipkart, Croma, Google, YouTube, LeetCode, GitHub, etc.).
2. Continuous Background Monitoring Engine: You have an active SQLite interval monitoring scheduler running on port 5173 that continuously checks product prices, DOM changes, and emails in the background and notifies the user immediately of drops or updates. NEVER claim you cannot do background monitoring.
3. Native API & OAuth Connectors: You have live authenticated connectors for Gmail, Google Calendar, Google Drive, Slack, GitHub, and Local Filesystem. When an account is connected (e.g. b21650406@gmail.com), you can dispatch emails, check unread mail, summarize inbox, and read calendar events directly.
4. Privacy Redaction: Passwords, credit cards, Aadhaar, SSNs, and private tokens are filtered automatically fail-closed.
5. Conversational Co-Pilot Voice: You are interactive, friendly, and natural. Speak with genuine warmth and relatable cadence (e.g. "Alright, let's check Flipkart now... here we go!", "I've got the watch running on port 5173. I'm leaving buddy, let's see you later!"). Avoid cold robotic disclaimers, dry monospace dumps, or forced comedy jokes.
6. Honesty: Be clear about verified facts versus unconfirmed listings, but always guide the user proactively on the next best move.
"""


def check_supported_capability(instruction: str) -> tuple[bool, Optional[str]]:
    """Check if an instruction requests an unsupported OS-level or harmful capability.

    Returns (is_supported, explanation_if_unsupported).
    """
    text = (instruction or "").strip()
    if not text:
        return True, None

    for pat in UNSUPPORTED_SYSTEM_PATTERNS:
        if pat.search(text):
            return False, (
                "PrivAgent is an autonomous web browser and privacy assistant. "
                "It cannot perform physical desktop OS operations (such as rebooting your computer, formatting drives, or launching desktop applications) "
                "or bypass security controls."
            )

    return True, None


def is_capability_query(instruction: str) -> bool:
    """True if user is asking what PrivAgent can do."""
    return bool(CAPABILITY_QUERY_PATTERN.search((instruction or "").strip()))


def format_capability_overview() -> str:
    """Return a comprehensive capability overview markdown."""
    return (
        "### PrivAgent Autonomous Capabilities\n\n"
        "- **🌐 Real-Browser Automation**: Navigate web pages, search Google, open webmail (Gmail/Outlook), research products, and interact with web applications via the connected Chrome extension.\n"
        "- **🔒 Fail-Closed Privacy**: Client-side and server-side redaction of credentials, payment cards, SSNs, and private tokens before sending DOM data to LLMs.\n"
        "- **🛡️ Tab Isolation**: Tasks run in authorized session tabs; unrelated tabs (like YouTube or work tabs) remain strictly untouched.\n"
        "- **🔑 Human Authentication Handoff**: When a site requires login, 2FA, or CAPTCHA, PrivAgent pauses safely for user completion without credential exposure.\n"
        "- **📊 Multi-Site Comparison**: Multi-retailer price comparisons (Amazon, Flipkart, Samsung) with full status tracking across visited and unavailable stores.\n"
        "- **💬 Conversational Assistance**: Code writing, mathematical calculations, and conceptual explanations."
    )


@dataclass(frozen=True)
class CapabilityRoute:
    capability: str
    adapter: str
    method: str
    operation: str
    available: bool
    url: str | None = None


class BrowserAdapter:
    def __init__(self, name: str, url: str):
        self.name = name
        self.url = url


class CapabilityRegistry:
    """Intent maps to an adapter without giving a planner credentials or tools."""

    _OPERATIONS = {
        "EmailCapability": {
            "search_threads": False, "summarize_messages": False, "list_messages": False, "get_message": False,
            "draft_email": True, "manage_labels": True, "trash_message": True, "untrash_message": True,
        },
        "CalendarCapability": {"list_events": False, "check_availability": False, "create_invite": True},
        "DocumentCapability": {"search_documents": False, "extract_text": False, "upload_deliverable": True},
        "TeamChatCapability": {"query_channels": False, "summarize_discussions": False, "post_notification": True},
        "CodeRepoCapability": {"query_issues": False, "inspect_prs": False, "inspect_commit_diff": False},
    }

    def __init__(self, adapters: dict[str, list[Any]] | None = None, *, browser_available: bool = False):
        self.browser_available = browser_available
        from .mcp.policy import ApprovalPolicy
        self._policy = ApprovalPolicy()
        from .connectors.oauth import oauth_manager
        from pathlib import Path
        self.adapters = adapters or {
            "EmailCapability": [oauth_manager.connector("gmail"), oauth_manager.connector("outlook"), BrowserAdapter("BrowserEmailAdapter", "https://mail.google.com")],
            "CalendarCapability": [oauth_manager.connector("google_calendar"), BrowserAdapter("BrowserCalendarAdapter", "https://calendar.google.com")],
            "DocumentCapability": [oauth_manager.connector("google_drive"), LocalFileSystemAdapter(Path(__file__).resolve().parents[1] / "data" / "artifacts"), BrowserAdapter("BrowserDriveAdapter", "https://drive.google.com")],
            "TeamChatCapability": [oauth_manager.connector("slack"), BrowserAdapter("BrowserChatAdapter", "https://app.slack.com/client")],
            "CodeRepoCapability": [oauth_manager.connector("github")],
        }

    def resolve(self, capability: str, operation: str) -> CapabilityRoute:
        if capability not in self._OPERATIONS or operation not in self._OPERATIONS[capability]:
            raise ValueError("unsupported capability operation")
        for adapter in self.adapters.get(capability, []):
            if getattr(adapter, "configured", False):
                return CapabilityRoute(capability, type(adapter).__name__, "native", operation, True)
        for adapter in self.adapters.get(capability, []):
            if isinstance(adapter, BrowserAdapter) and self.browser_available:
                return CapabilityRoute(capability, adapter.name, "browser", operation, True, adapter.url)
        return CapabilityRoute(capability, "", "unavailable", operation, False)

    def approve_native(self, capability: str, operation: str, params: dict[str, Any], task_id: str) -> str:
        """Called only by a trusted user-confirmation handler."""
        route = self.resolve(capability, operation)
        if route.method != "native" or not self._OPERATIONS[capability][operation]:
            raise ValueError("no mutable native route")
        return self._policy.approve(f"{capability}.{operation}", params, task_id)

    def execute_native(self, capability: str, operation: str, params: dict[str, Any], *,
                       approval_token: str | None = None, task_id: str = "") -> Any:
        route = self.resolve(capability, operation)
        if route.method != "native":
            raise RuntimeError("native connector not configured; use browser route or connect an account")
        if self._OPERATIONS[capability][operation]:
            self._policy.authorize(f"{capability}.{operation}", params, approval_token, task_id)
        adapter = next(a for a in self.adapters[capability] if type(a).__name__ == route.adapter)
        from .mcp.sanitizer import sanitize_tool_output
        result = getattr(adapter, operation)(**params)
        return sanitize_tool_output(result)
