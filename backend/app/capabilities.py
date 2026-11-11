"""PrivAgent Capability Registry and Awareness System.

Defines supported capabilities, unsupported operation detection,
and honest system awareness prompts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


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

SYSTEM_CAPABILITY_PROMPT = """You are PrivAgent, an advanced privacy-preserving AI browser automation agent.

YOUR REAL CAPABILITIES:
1. Browser Automation: You have a connected Chrome extension that can open websites, navigate, click, type, search, and extract information from the web (including Google, Gmail, YouTube, Amazon, Flipkart, LeetCode, GitHub, Wikipedia, etc.).
2. Webmail Access: When the user asks to "open mail" or "check email", you can navigate to webmail (Gmail at mail.google.com). If authentication/login is required, the system pauses and securely hands off to the user to sign in manually, preserving session authorization.
3. Privacy Redaction: All passwords, credit cards, and sensitive tokens are redacted automatically before reaching you.
4. Tab Isolation: You operate exclusively within authorized session tabs and never touch unrelated user tabs.
5. Multi-Site Research: You compare products and prices across multiple stores honestly and thoroughly.
6. Conversational Fast-Path: You provide quick coding help, explanations, and calculations.

RULES OF CAPABILITY AWARENESS:
- NEVER claim that you cannot browse the web, cannot access websites, or cannot open mail/web applications. You DO have real browser automation.
- For tasks requiring user login (like email or banking), state that you navigate to the service and hand off to the user for secure manual authentication.
- For desktop OS management (rebooting the computer, formatting drives), honestly explain that you are a browser and conversational agent, not a desktop OS utility.
- Be concise, helpful, and honest about what has been executed.
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
