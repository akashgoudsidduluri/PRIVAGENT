"""Task intent routing module.

Classifies task instructions before any browser-related operation occurs.
Non-browser tasks (conversations, coding, calculations, explanations) route directly
to the conversational model path without triggering browser tab discovery,
tab creation, extension action dispatch, or browser observation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class TaskIntent(str, Enum):
    NON_BROWSER = "NON_BROWSER"
    NEW_BROWSER_TASK = "NEW_BROWSER_TASK"
    CONTINUE_BROWSER_TASK = "CONTINUE_BROWSER_TASK"
    USE_EXISTING_USER_TAB = "USE_EXISTING_USER_TAB"


@dataclass
class IntentClassification:
    intent: TaskIntent
    confidence: float
    reason: str
    target_tab_hint: Optional[str] = None
    is_ambiguous: bool = False

    @property
    def is_browser(self) -> bool:
        """True if the classified intent requires a browser session."""
        return self.intent != TaskIntent.NON_BROWSER

    @property
    def is_conversational(self) -> bool:
        """True if the classified intent is conversational (non-browser)."""
        return self.intent == TaskIntent.NON_BROWSER

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value,
            "confidence": self.confidence,
            "reason": self.reason,
            "targetTabHint": self.target_tab_hint,
            "isAmbiguous": self.is_ambiguous,
            "isBrowser": self.is_browser,
        }


# 1. Non-browser patterns
GREETING_PATTERN = re.compile(
    r"^(?:hi|hello|hey|good\s+morning|good\s+afternoon|good\s+evening|greetings|howdy|sup|yo)(?:\s+(?:there|man|agent|privagent|buddy|friend|assistant|team|all|everyone))?[!.\s]*$",
    re.IGNORECASE,
)

CONVERSATIONAL_PATTERN = re.compile(
    r"^(?:how\s+are\s+you|who\s+are\s+you|what\s+can\s+you\s+do|thank\s+you|thanks|bye|goodbye|tell\s+me\s+a\s+joke|help|what\s+is\s+your\s+name)[!.\s\?]*$",
    re.IGNORECASE,
)

CODE_PATTERN = re.compile(
    r"\b(?:write|code|implement|generate|create)\s+(?:a|an)\s+(?:c\+\+|python|java|javascript|typescript|c#|rust|go|sql|html|css|algo|algorithm|function|script|program|class|bfs|dfs|quicksort|mergesort)\b"
    r"|\b(?:write|code|implement)\s+(?:bfs|dfs|binary\s+search|linked\s+list|fibonacci|factorial)\b"
    r"|\b(?:how\s+to\s+write|help\s+me\s+debug|debug\s+this|explain\s+this\s+code)\b",
    re.IGNORECASE,
)

MATH_PATTERN = re.compile(
    r"^(?:calculate|compute|solve|what\s+is)\s+[\d\s\+\-\*\/\%\^\.\(\)]+(?:\?|$)"
    r"|\b(?:calculate|compute|what\s+is)\s+\d+(?:\.\d+)?\s*%\s+of\s+\d+(?:\.\d+)?\b"
    r"|^\s*\d+[\d\s\+\-\*\/\%\^\.\(\)]+\s*$",
    re.IGNORECASE,
)

EXPLANATION_PATTERN = re.compile(
    r"^(?:what\s+is|what\s+are|define|explain)\s+"
    r"(?:browser\s+automation|automation|web\s+scraping|ai|artificial\s+intelligence|llm|llms|transformers|attention\s+mechanism|neural\s+networks|recursion|polymorphism|gravity|relativity|photosynthesis|entropy|dna|rna|quantum\s+computing|machine\s+learning|deep\s+learning|quicksort|binary\s+search|backpropagation|google\s+search)\b"
    r"|^(?:how\s+does|how\s+do)\s+.*?\s+(?:work|function|operate)\??$"
    r"|^(?:explain|define|describe|what\s+is|what\s+are|how\s+does|why\s+is)\s+"
    r"(?:transformers|attention\s+mechanism|neural\s+networks|recursion|polymorphism|gravity|relativity|photosynthesis|entropy|dna|rna|quantum\s+computing|machine\s+learning|deep\s+learning|quicksort|binary\s+search|backpropagation)\b"
    r"|^(?:explain|describe)\s+how\s+.*\s+works\??$",
    re.IGNORECASE,
)

# 2. Explicit User Tab request patterns
USE_TAB_PATTERNS = [
    re.compile(r"\b(?:i\s+)?(?:have\s+)?opened\s+(?:a|the)\s+(?:new\s+)?tab\b", re.IGNORECASE),
    re.compile(r"\buse\s+(?:the|my)\s+(?:new\s+tab|tab\s+i\s+(?:just\s+)?opened)\b", re.IGNORECASE),
    re.compile(r"\bcontinue\s+in\s+that\s+tab\b", re.IGNORECASE),
    re.compile(r"\b(?:in|on|switch\s+to)\s+the\s+tab\s+i\s+(?:just\s+)?opened\b", re.IGNORECASE),
    re.compile(r"\buse\s+my\s+active\s+tab\b", re.IGNORECASE),
    re.compile(r"\b(?:in|on|use|attach\s+to|from)\s+(?:my\s+|the\s+)?(?:currently\s+open|currently\s+active|existing|active|current)\s+(?:browser\s+)?tab\b", re.IGNORECASE),
    re.compile(r"\buse\s+(?:the\s+)?(?:existing|current|active)\s+(?:user\s+)?tab\b", re.IGNORECASE),
    re.compile(r"\b(?:in|on)\s+the\s+(?:open|existing)\s+tab\s+(?:for|with|named|titled|at)\s+([^\.,;]+)", re.IGNORECASE),
    re.compile(r"\bon\s+the\s+open\s+(?:amazon|wikipedia|youtube|google|flipkart|github|reddit)\s+tab\b", re.IGNORECASE),
]

# 3. Continuation markers (only valid when an active browser session exists)
CONTINUATION_PATTERNS = [
    re.compile(r"^(?:now\s+|then\s+)?(?:search\s+for|search\s+in|look\s+for)\s+.+", re.IGNORECASE),
    re.compile(r"^(?:now\s+|then\s+)?(?:click|open|select)\s+(?:on\s+)?(?:the\s+)?(?:first|second|third|fourth|fifth|\d+(?:st|nd|rd|th)?)\s+(?:result|product|link|item|one)\b", re.IGNORECASE),
    re.compile(r"^(?:go\s+back|previous\s+page|back\s+button)(?:\s+(?:to\s+the\s+previous\s+page|and\s+.*))?", re.IGNORECASE),
    re.compile(r"\bgo\s+back\s+and\s+(?:open|click)\b", re.IGNORECASE),
    re.compile(r"\bcontinue\s+where\s+you\s+left\s+off\b", re.IGNORECASE),
    re.compile(r"^(?:now\s+|then\s+)?(?:continue|scroll(?:\s+down|\s+up)?|click(?:\s+on)?|next\s+page|go\s+back|add\s+to\s+cart|buy\s+now|type\s+in|select|read\s+more|open\s+the\s+(?:first|second|third|fourth|next)\s+one)\b", re.IGNORECASE),
    re.compile(r"\b(?:what\s+is\s+its\s+price|how\s+much\s+is\s+it|buy\s+it|summarize\s+that(?:\s+page)?|what\s+else\s+does\s+it\s+say|from\s+that\s+page)\b", re.IGNORECASE),
    re.compile(r"^(?:continue|proceed|next\s+step|scroll(?:\s+down|\s+up)?)[!.\s]*$", re.IGNORECASE),
    re.compile(r"\b(?:is\s+there\s+any\s+other|any\s+other|what\s+about\s+(?:the\s+)?other|check\s+other)\s+(?:mail|email|message|unread|product|item|flight|result)s?\b", re.IGNORECASE),
    re.compile(r"\b(?:more\s+important|more\s+urgent|cheaper|better|higher|lower)\s+than\s+(?:this|that|the\s+first\s+one)\b", re.IGNORECASE),
    re.compile(r"\b(?:is\s+there\s+any\s+other\s+mail|which\s+mail\s+is\s+more|check\s+my\s+other\s+mail)\b", re.IGNORECASE),
    re.compile(r"\b(?:in\s+my\s+(?:inbox|mailbox|email)|from\s+my\s+(?:inbox|mailbox|email))\b", re.IGNORECASE),
    # Explicit request to finish or resume the task left mid-flight (checkpoint resumption)
    re.compile(r"\b(?:complete|finish|resume|pick\s+up)\s+(?:the\s+)?(?:unfinished|incomplete|previous|prior|last|pending|halted|paused)\s+(?:task|mission|job|work|run)\b", re.IGNORECASE),
    re.compile(r"\b(?:complete|finish|resume)\s+the\s+unfinished\b", re.IGNORECASE),
    re.compile(r"\b(?:where\s+(?:were|did)\s+(?:you|we)\s+(?:leave|stop)|from\s+where\s+(?:you|we)\s+(?:left|stopped))\b", re.IGNORECASE),
]

# 4. Explicit browser task markers
DIRECT_SITE_TRIGGER = re.compile(
    r"\b(?:open|visit|go\s+to|navigate\s+to|browse\s+to)\s+"
    r"(?:the\s+)?(?:official\s+)?(?:website\s+|webpage\s+|site\s+|page\s+)?"
    r"(?:google|youtube|amazon|wikipedia|flipkart|reddit|github|twitter|x|linkedin|facebook|instagram|netflix|bing|yahoo|duckduckgo|react|vue|angular|python|rust|golang|huggingface|arxiv|mdn|apple|microsoft|spotify|mail|gmail|email|outlook|webmail)"
    r"(?:\s+(?:docs|documentation|homepage|website|search|site|store|app|portal|page|inbox))?\b",
    re.IGNORECASE,
)

EMAIL_TRIGGER = re.compile(
    r"\b(?:open|check|go\s+to|access|view|read)\s+(?:my\s+)?(?:e-?mail|mail|inbox|gmail|outlook|webmail)\b"
    r"|^(?:open\s+mail|check\s+mail|open\s+gmail|check\s+email|go\s+to\s+inbox|open\s+inbox)[!.\s]*$",
    re.IGNORECASE,
)

OFFICIAL_DOCS_TRIGGER = re.compile(
    r"\b(?:open|navigate\s+to|go\s+to|browse\s+to)\s+(?:the\s+)?official\s+.+\s+(?:docs|documentation|website|page|site)\b",
    re.IGNORECASE,
)

SEARCH_WEB_TRIGGER = re.compile(
    r"\b(?:search\s+(?:google|amazon|wikipedia|youtube|bing|yahoo|duckduckgo|flipkart|reddit|github)\s+for\s+.+)\b"
    r"|\b(?:search\s+for\s+.+\s+on\s+(?:google|amazon|wikipedia|youtube|bing|flipkart|reddit|github|the\s+web))\b"
    r"|\b(?:search|look\s+up|find)\s+.+\s+(?:online|on\s+the\s+web|on\s+the\s+internet)\b",
    re.IGNORECASE,
)

PRODUCT_SEARCH_TRIGGER = re.compile(
    r"\b(?:find|search|check|look\s+up|buy|order|price\s+of)\s+.+\s+(?:on\s+|from\s+)(?:amazon|flipkart|ebay|walmart|bestbuy)\b"
    r"|\b(?:find|check\s+price\s+of)\s+[a-zA-Z0-9\s-]+\s+(?:on\s+amazon|on\s+flipkart)\b",
    re.IGNORECASE,
)

BROWSER_TRIGGERS = [
    re.compile(r"https?://[^\s]+", re.IGNORECASE),
    EMAIL_TRIGGER,
    DIRECT_SITE_TRIGGER,
    OFFICIAL_DOCS_TRIGGER,
    SEARCH_WEB_TRIGGER,
    PRODUCT_SEARCH_TRIGGER,
    re.compile(r"\b(?:navigate\s+to|go\s+to|open|browse\s+to|visit)\s+(?:the\s+)?(?:website\s+|site\s+)?(?:https?://|[a-zA-Z0-9-]+\.(?:com|org|in|net|edu|gov|io|co|ai|dev|app|info))\b", re.IGNORECASE),
    re.compile(r"\b(?:search\s+(?:on\s+)?(?:amazon|google|wikipedia|youtube|flipkart|bing|github|reddit|twitter|ebay)|search\s+for\s+.*\s+on\s+[a-zA-Z0-9-]+)\b", re.IGNORECASE),
    re.compile(r"\b(?:look\s+up|find\s+products?|find\s+prices?|check\s+weather|check\s+flights?)\s+on\b", re.IGNORECASE),
    re.compile(r"\b(?:buy|order|purchase)\s+.*?\s+(?:from|on)\s+(?:amazon|flipkart|ebay)\b", re.IGNORECASE),
]


def classify_intent(
    instruction: str,
    context: Optional[dict[str, Any]] = None,
) -> IntentClassification:
    """Classify user task intent before any browser operation.

    Parameters
    ----------
    instruction : str
        The raw task instruction from the user.
    context : Optional[dict[str, Any]]
        Execution context, including active browser session info:
        - ``active_session``: Optional active BrowserSession
        - ``conversation_history``: list of previous turn messages

    Returns
    -------
    IntentClassification
        The classified intent and supporting metadata.
    """
    text = (instruction or "").strip()
    ctx = context or {}
    has_active_browser_session = bool(ctx.get("active_session"))

    if not text:
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=1.0,
            reason="empty instruction defaults to non-browser",
        )

    # 1. Check for conflicting or ambiguous tab instructions
    ambiguous_tab_pattern = re.compile(
        r"\b(?:use\s+a\s+tab|pick\s+any\s+tab|some\s+tab|random\s+tab)\b",
        re.IGNORECASE,
    )
    if ambiguous_tab_pattern.search(text):
        return IntentClassification(
            intent=TaskIntent.USE_EXISTING_USER_TAB,
            confidence=0.5,
            reason="ambiguous tab request requires resolution or failure",
            is_ambiguous=True,
        )

    # 2. Check for explicit existing user tab request
    for pat in USE_TAB_PATTERNS:
        m = pat.search(text)
        if m:
            hint = m.group(1).strip() if m.lastindex and m.lastindex >= 1 else None
            return IntentClassification(
                intent=TaskIntent.USE_EXISTING_USER_TAB,
                confidence=0.95,
                reason="explicit user request to attach to existing tab",
                target_tab_hint=hint,
            )

    # 3. Check for continuation of an active browser session
    if has_active_browser_session:
        for pat in CONTINUATION_PATTERNS:
            if pat.search(text):
                return IntentClassification(
                    intent=TaskIntent.CONTINUE_BROWSER_TASK,
                    confidence=0.9,
                    reason="instruction continues existing active browser session",
                )

    # 4. Check for unsupported desktop OS operations (P5.2 integrity)
    from .capabilities import check_supported_capability
    is_supp, supp_reason = check_supported_capability(text)
    if not is_supp:
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=1.0,
            reason=f"unsupported capability: {supp_reason}",
        )

    # 5. Check for explicit email / webmail directive (must route to browser, never refusal)
    if EMAIL_TRIGGER.search(text):
        return IntentClassification(
            intent=TaskIntent.NEW_BROWSER_TASK,
            confidence=0.99,
            reason="instruction requests email / webmail navigation",
            target_tab_hint="mail.google.com",
        )

    # 6. Check for explicit non-browser categories
    # 6a. Greetings
    if GREETING_PATTERN.match(text):
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=1.0,
            reason="greeting matches non-browser conversational intent",
        )

    # 4b. Conversational / Meta
    if CONVERSATIONAL_PATTERN.match(text):
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=0.98,
            reason="conversational meta request matches non-browser intent",
        )

    # 4c. Code writing / programming questions
    if CODE_PATTERN.search(text):
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=0.95,
            reason="programming and coding request matches non-browser intent",
        )

    # 4d. Calculations and arithmetic
    if MATH_PATTERN.search(text):
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=0.95,
            reason="calculation request matches non-browser intent",
        )

    # 4e. Explanations and conceptual questions (without web browse commands)
    if EXPLANATION_PATTERN.match(text):
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=0.92,
            reason="conceptual explanation matches non-browser conversational intent",
        )

    # 5. Check for explicit browser triggers
    for pat in BROWSER_TRIGGERS:
        if pat.search(text):
            return IntentClassification(
                intent=TaskIntent.NEW_BROWSER_TASK,
                confidence=0.98,
                reason="instruction contains explicit URL or web browsing target",
            )

    # 6. Web research keywords (search, find, look up, buy on the web)
    web_keywords = re.compile(
        r"\b(?:search|browse|look\s+up|find|research|visit|open\s+site|webpage|website|url)\b",
        re.IGNORECASE,
    )
    if web_keywords.search(text):
        return IntentClassification(
            intent=TaskIntent.NEW_BROWSER_TASK,
            confidence=0.85,
            reason="instruction specifies web research or website interaction",
        )

    # 7. Default to NON_BROWSER for general questions without browser indicators
    # e.g. "What is the capital of France?", "Help me understand photosynthesis", "Give me a recipe"
    return IntentClassification(
        intent=TaskIntent.NON_BROWSER,
        confidence=0.8,
        reason="general knowledge/conversational request without browsing directives",
    )

