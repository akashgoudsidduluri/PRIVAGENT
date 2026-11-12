"""Task intent routing module.

Classifies task instructions before any browser-related operation occurs.
Non-browser tasks (conversations, coding, calculations, explanations) route directly
to the conversational model path without triggering browser tab discovery,
tab creation, extension action dispatch, or browser observation.

Autonomous research and multi-format artifact generation requests (DOCX, PDF, PPTX,
XLSX, CSV, Markdown, HTML) route to isolated browser research sessions and
preserve deliverable format metadata.
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
    RESEARCH_TASK = "RESEARCH_TASK"


class ResearchOperationType(str, Enum):
    NEW_RESEARCH = "NEW_RESEARCH"
    EXPORT_EXISTING_PACKAGE = "EXPORT_EXISTING_PACKAGE"
    REFINE_EXISTING_RESEARCH = "REFINE_EXISTING_RESEARCH"
    RESUME_EXISTING_MISSION = "RESUME_EXISTING_MISSION"
    NORMAL_CONVERSATION = "NORMAL_CONVERSATION"


@dataclass
class IntentClassification:
    intent: TaskIntent
    confidence: float
    reason: str
    target_tab_hint: Optional[str] = None
    is_ambiguous: bool = False
    requested_format: Optional[str] = None
    is_research: bool = False
    research_topic: Optional[str] = None
    deliverable_type: Optional[str] = None
    operation_type: Optional[ResearchOperationType] = None

    @property
    def is_browser(self) -> bool:
        """True if the classified intent requires a browser session."""
        return self.intent in (
            TaskIntent.NEW_BROWSER_TASK,
            TaskIntent.CONTINUE_BROWSER_TASK,
            TaskIntent.USE_EXISTING_USER_TAB,
            TaskIntent.RESEARCH_TASK,
        )

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
            "requestedFormat": self.requested_format,
            "isResearch": self.is_research,
            "researchTopic": self.research_topic,
            "deliverableType": self.deliverable_type,
            "operationType": self.operation_type.value if self.operation_type else None,
        }


# ---------------------------------------------------------------------------
# Format and Deliverable Detection Patterns (Step 4: Research & Artifacts)
# ---------------------------------------------------------------------------

FORMAT_PDF_RE = re.compile(r"(?:\bpdf\b|\.pdf\b)", re.IGNORECASE)
FORMAT_DOCX_RE = re.compile(r"(?:\bdocx\b|\bword(?:\s+doc(?:ument)?)?\b|\.docx\b)", re.IGNORECASE)
FORMAT_PPTX_RE = re.compile(r"(?:\bpptx\b|\bpowerpoint\b|\bpresentation\b|\bslide\s*deck\b|\bslides?\b|\.pptx\b)", re.IGNORECASE)
FORMAT_XLSX_RE = re.compile(r"(?:\bxlsx\b|\bexcel\b|\bspreadsheet\b|\bworkbook\b|\.xlsx\b)", re.IGNORECASE)
FORMAT_CSV_RE = re.compile(r"(?:\bcsv\b|\.csv\b)", re.IGNORECASE)
FORMAT_MD_RE = re.compile(r"(?:\bmarkdown\b|\.md\b)", re.IGNORECASE)
FORMAT_HTML_RE = re.compile(r"(?:\bhtml\b|\bwebpage\b|\.html?\b)", re.IGNORECASE)


def detect_requested_format(text: str) -> Optional[str]:
    """Detect explicitly or strongly requested output file format."""
    if not text:
        return None
    if FORMAT_PDF_RE.search(text):
        return "pdf"
    if FORMAT_DOCX_RE.search(text):
        return "docx"
    if FORMAT_PPTX_RE.search(text):
        return "pptx"
    if FORMAT_XLSX_RE.search(text):
        return "xlsx"
    if FORMAT_CSV_RE.search(text):
        return "csv"
    if FORMAT_MD_RE.search(text):
        return "markdown"
    if FORMAT_HTML_RE.search(text):
        return "html"
    return None


DELIVERABLE_PRESENTATION_RE = re.compile(
    r"\b(?:presentation|slide\s*deck|slides?|powerpoint|pptx)\b",
    re.IGNORECASE,
)
DELIVERABLE_SPREADSHEET_RE = re.compile(
    r"\b(?:excel(?:\s+comparison)?|spreadsheet|workbook|xlsx|csv|matrix|data\s+table)\b",
    re.IGNORECASE,
)
DELIVERABLE_REPORT_RE = re.compile(
    r"\b(?:report|whitepaper|dossier|research\s+paper|assignment|document|doc|pdf|docx|word)\b",
    re.IGNORECASE,
)


def detect_deliverable_type(text: str) -> Optional[str]:
    """Detect high-level deliverable type (presentation, dataset, report)."""
    if not text:
        return None
    if DELIVERABLE_PRESENTATION_RE.search(text):
        return "presentation"
    if DELIVERABLE_SPREADSHEET_RE.search(text):
        return "dataset"
    if DELIVERABLE_REPORT_RE.search(text):
        return "report"
    return None


RESEARCH_DIRECTIVE_RE = re.compile(
    r"\b(?:research(?:ing|es)?|conduct\s+research|investigate|investigation|deep\s+dive|survey|comprehensive\s+analysis)\b",
    re.IGNORECASE,
)
PREPARE_ARTIFACT_RE = re.compile(
    r"\b(?:prepare|create|generate|write|make|build|produce|export|need|want|give\s+me|can\s+(?:i|we)\s+get)\s+(?:a|an)\s+(?:(?:new|detailed|comprehensive|fresh|research)\s+)*(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|12)[- ](?:page|slide)s?\s+)?(?:report|presentation|slides?|slide\s*deck|excel(?:\s+comparison)?|spreadsheet|workbook|document|whitepaper|dossier|pdf|docx|pptx|xlsx|csv|html|markdown|comparison)\b",
    re.IGNORECASE,
)
EXPLANATION_WITH_DELIVERABLE_RE = re.compile(
    r"\b(?:explain|describe|analyze|summarize)\b.+?\b(?:in\s+(?:a\s+)?(?:pdf|word|docx|excel|spreadsheet|presentation|pptx|markdown|html)|as\s+(?:a\s+)?(?:pdf|word|docx|excel|spreadsheet|presentation|pptx|markdown|html)|with\s+diagrams\s+in\s+pdf|in\s+an?\s+excel\s+(?:comparison|sheet|workbook))\b",
    re.IGNORECASE,
)
COMPARISON_WITH_DELIVERABLE_RE = re.compile(
    r"\b(?:compare|comparison)\b.+?\b(?:in\s+(?:an?\s+)?(?:excel|spreadsheet|workbook|report|pdf|xlsx|csv)|generate\s+(?:an?\s+)?(?:excel|spreadsheet|workbook|report|pdf|xlsx|csv)\s+comparison|matrix|table)\b",
    re.IGNORECASE,
)

FOLLOW_UP_TRIGGERS_RE = re.compile(
    r"^(?:(?:nice|good|great|awesome|cool|thanks|thank\s+you|well|ok|okay|hey|hi|yo|hello|bro|dude|man|please|pls)\b(?:\s*,\s*|\s+)*(?:but|however|now|then|so)?\s*)*"
    r"(?:(?:could|can|would)\s+(?:you|u)\b"
    r"|(?:now|then|next|also|and\s+also)\s+(?:give|make|create|prepare|generate|provide|write|add|show|include|expand|elaborate|research|search|compare)\b"
    r"|(?:add|include|expand\s+(?:on\s+)?|elaborate\s+(?:on\s+)?|give|show|provide)\s+(?:more|further|additional|better|technical)\b"
    r"|(?:make\s+it|turn\s+it\s+into|convert\s+it\s+to|change\s+(?:it\s+)?to|switch\s+(?:it\s+)?to)\b"
    r"|(?:make|turn|convert|change|format|prepare)\s+(?:the\s+)?(?:previous\s+|prior\s+)?(?:report|analysis|document|research|deliverable|it)\b"
    r"|(?:i|we)\s+(?:need|want|would\s+like)\s+(?:a|an)?\s*(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|12)?\s*[- ]?(?:page|slide)s?\b"
    r"|\b(?:more\s+(?:[a-zA-Z]+\s+)?details?|detailed\s+version|in\s+depth|expand\s+on|make\s+it\s+better|elaborate|supporting\s+images|with\s+diagrams|with\s+formulas)\b)",
    re.IGNORECASE,
)

EXPORT_DIRECTIVE_PATTERN = re.compile(
    r"^(?:(?:nice|good|great|awesome|cool|thanks|thank\s+you|well|ok|okay|hey|hi|yo|hello|bro|dude|man|please|pls)\b(?:\s*,\s*|\s+)*(?:but|however|now|then|so)?\s*)*"
    r"(?:(?:can\'t|cant|can|could|would)\s+(?:you|u)\s+)?(?:please\s+|pls\s+)?"
    r"(?:(?:now|then|also|just)\s+)?"
    r"(?:(?:can|could|may)\s+(?:i|we)\s+(?:get|have|receive)|(?:i|we)\s+(?:want|need|would\s+like)|generate|create|export|save|compile|download|produce|make|render|output|convert|turn|give|format|put|send|prepare|provide)\s+"
    r"(?:(?:me\s+|us\s+)?(?:the\s+|this\s+|that\s+|my\s+|it\s+)?)?"
    r"(?:previous\s+|prior\s+|current\s+|last\s+|collected\s+|above\s+)?(?:research\s+|analysis\s+|report\s+|findings\s+|results\s+|data\s+|it\s+|page\s+|content\s+|summary\s+)?\s*"
    r"(?:(?:in|into|as|to|of)\s+)?"
    r"(?:(?:a|an|the)\s+)?"
    r"(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|12)[- ](?:page|slide)s?\s+)?"
    r"(pdf|docx|word(?:\s+doc(?:ument)?)?|pptx|powerpoint|presentation|slides?|slide\s*deck|xlsx|excel(?:\s+sheet)?|spreadsheet|workbook|csv|markdown|html|report|document)\b"
    r"(?:\s+(?:file|document|deck|sheet|format))?"
    r"(?:\s+(?:and\s+)?(?:also\s+)?(?:add|include|with)\s+[^\n]+)?"
    r"(?:\s+(?:bro|man|dude|pls|please|for\s+me|thanks|thank\s+you|of\s+this|from\s+this|instead|of\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|12)\s+slides?))?[!.\s]*$",
    re.IGNORECASE,
)

REFORMAT_PAGES_IMAGES_PATTERN = re.compile(
    r"^(?:(?:nice|good|great|awesome|cool|thanks|thank\s+you|well|ok|okay|hey|hi|yo|hello|bro|dude|man|please|pls)\b(?:\s*,\s*|\s+)*(?:but|however|now|then|so)?\s*)*"
    r"(?:(?:can\'t|cant|can|could|would)\s+(?:you|u)\s+)?(?:please\s+|pls\s+)?"
    r"(?:(?:now|then|also|just)\s+)?"
    r"(?:make|turn|convert|change|format|prepare|render|generate|create|produce|put|give)\s+"
    r"(?:(?:me\s+|us\s+)?(?:the\s+|this\s+|that\s+|my\s+|our\s+)?(?:previous\s+|prior\s+|current\s+|last\s+|above\s+)?(?:report|analysis|document|research|findings|results|deliverable|it)\s+)"
    r"(?:into\s+|as\s+|to\s+|in\s+)?"
    r"(?:(?:a|an|the)\s+)?"
    r"(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|12)[- ](?:page|pages|slide|slides)?\b)?"
    r".*$",
    re.IGNORECASE,
)

CONTINUATION_DIRECTIVE_PATTERN = re.compile(
    r"^(?:(?:nice|good|great|awesome|cool|thanks|thank\s+you|well|ok|okay|hey|hi|yo|hello|bro|dude|man|please|pls)\b(?:\s*,\s*|\s+)*(?:but|however|now|then|so)?\s*)*"
    r"(?:(?:can|could|would)\s+(?:you|u)\s+)?(?:please\s+|pls\s+)?"
    r"(?:continue|resume|proceed|keep\s+going|pick\s+up(?:\s+where\s+(?:you|we)\s+left\s+off)?|finish\s+(?:the\s+)?(?:mission|task|research))"
    r"(?:\s+(?:bro|man|dude|pls|please))?[!.\s]*$",
    re.IGNORECASE,
)

REFINEMENT_DIRECTIVE_PATTERN = re.compile(
    r"^(?:(?:nice|good|great|awesome|cool|thanks|thank\s+you|well|ok|okay|hey|hi|yo|hello|bro|dude|man|please|pls)\b(?:\s*,\s*|\s+)*(?:but|however|now|then|so)?\s*)*"
    r"(?:(?:can|could|would)\s+(?:you|u)\s+)?(?:please\s+|pls\s+)?"
    r"(?:(?:now|then|also|and\s+also)\s+)?"
    r"(?:(?:make|turn|convert|change|format|prepare)\s+(?:the\s+)?(?:previous\s+|prior\s+)?(?:report|analysis|document|research|deliverable|it)\b|add|include|compare|incorporate|expand(?:\s+on)?|elaborate(?:\s+on)?|give\s+more|provide\s+more|i\s+(?:need|want))\s+"
    r".+",
    re.IGNORECASE,
)


def is_export_deliverable_directive(
    text: str,
    context: Optional[dict[str, Any]] = None,
) -> tuple[bool, Optional[str], Optional[str]]:
    """Detect whether user prompt is a pure export or deliverable compilation directive."""
    if not text:
        return False, None, None
    clean = text.strip()
    m = EXPORT_DIRECTIVE_PATTERN.match(clean)
    if m:
        raw_fmt = m.group(1).lower()
        fmt = detect_requested_format(raw_fmt) or detect_requested_format(clean)
        if not fmt and context:
            fmt = context.get("requested_format")
        if not fmt:
            fmt = "pdf"
        deliv = detect_deliverable_type(clean) or ("presentation" if fmt == "pptx" else "report")
        return True, fmt, deliv

    # Check reformat / page-adjustment / image-adding export directives on existing deliverables
    m_reformat = REFORMAT_PAGES_IMAGES_PATTERN.match(clean)
    if m_reformat:
        fmt = detect_requested_format(clean)
        if not fmt and context:
            fmt = context.get("requested_format")
        if not fmt:
            fmt = "pdf"
        deliv = detect_deliverable_type(clean) or ("presentation" if fmt == "pptx" else "report")
        return True, fmt, deliv

    return False, None, None


def resolve_topic_from_context(context: Optional[dict[str, Any]]) -> Optional[str]:
    """Resolve active research subject from working memory, checkpoint, or prior messages."""
    if not context or not isinstance(context, dict):
        return None
    # 1. Direct explicit context keys
    for k in ("research_topic", "target_item"):
        val = context.get(k)
        if val and isinstance(val, str) and val.strip():
            clean = val.strip()
            if len(clean) > 2 and clean.lower() not in {"it", "this", "that"}:
                return clean

    # 2. Session / chat memory keys
    sess_mem = context.get("session_memory") or context.get("chat_memory") or {}
    if isinstance(sess_mem, dict):
        for k in ("research_topic", "target_item"):
            val = sess_mem.get(k)
            if val and isinstance(val, str) and val.strip():
                clean = val.strip()
                if len(clean) > 2 and clean.lower() not in {"it", "this", "that"}:
                    return clean

    # 3. Knowledge package topic if present
    pkg_data = context.get("research_knowledge_package")
    if pkg_data:
        topic_obj = pkg_data.get("topic") if isinstance(pkg_data, dict) else getattr(pkg_data, "topic", None)
        if topic_obj:
            t_title = topic_obj.get("title") if isinstance(topic_obj, dict) else getattr(topic_obj, "title", None)
            if t_title and isinstance(t_title, str) and t_title.strip():
                return t_title.strip()

    # 4. Scan conversation history backwards for original inquiry topic
    conv_hist = context.get("conversation_history") or []
    if isinstance(conv_hist, list):
        for msg in reversed(conv_hist):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = str(msg.get("content") or "").strip()
                if content:
                    cand = extract_research_topic(content, context=None)
                    if cand and len(cand.split()) >= 2 and cand.lower() not in {"it", "this", "that", "them"}:
                        return cand
    return None


def extract_research_topic(text: str, context: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Extract core topic string from research or deliverable prompt.

    Handles conversational follow-ups and inherits/augments the active topic
    from prior context when anaphora or partial modifiers are supplied.
    """
    if not text:
        return None
    cleaned = text.strip()

    # If pure export/deliverable directive, return prior topic directly
    is_exp, _, _ = is_export_deliverable_directive(cleaned, context=context)
    if is_exp:
        return resolve_topic_from_context(context)

    # 1. Strip conversational slang openers
    cleaned = re.sub(
        r"^(?:(?:hey|hi|yo|hello|ok|okay|bro|dude|man|please|pls|buddy|pal|friend|so|well)\s+)+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    # 2. Strip conversational command openers and directives
    cleaned = re.sub(
        r"^(?:could|can|would)\s+(?:you|u)\s+(?:please\s+|pls\s+)?(?:give|make|create|prepare|generate|provide|write|add|show|include|expand|elaborate|do|research|investigate)?\s*(?:me\s+)?",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(
        r"^(?:please\s+|pls\s+)?(?:give|make|create|prepare|generate|provide|write|add|show|include|expand|elaborate|do)\s+(?:me\s+)?",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(
        r"^(?:make\s+it|turn\s+it\s+into|convert\s+it\s+to|change\s+(?:it\s+)?to|switch\s+(?:it\s+)?to)\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(
        r"^(?:add|include|expand\s+(?:on\s+)?|elaborate\s+(?:on\s+)?|give|show|provide)\s+(?:more|further|additional|better|technical)\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(r"^(?:now|then|next|also)\s+", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(
        r"^(?:(?:hey|hi|yo|hello|ok|okay|bro|dude|man|please|pls|buddy|pal|friend|so|well)\s+)+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    # 3. Clean leading command verbs
    cleaned = re.sub(
        r"^(?:research(?:ing)?|conduct\s+research\s+on|investigate|study|deep\s+dive\s+into|survey|explain|describe|analyze|summarize|compare(?:\s+the)?|prepare|create|generate|write|make|build)\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    # 4. Clean trailing deliverable / format / illustration clauses BEFORE stripping
    #    the deliverable nouns they contain.
    cleaned = re.sub(
        r"\s+(?:and\s+)?(?:bro\s+|please\s+)?(?:prepare|create|generate|write|make|build|export|output)\s+(?:a|an)?\s*(?:more\s+)?(?:detailed\s+)?(?:research\s+)?(?:report|presentation|slides?|slide\s*deck|excel(?:\s+comparison)?|spreadsheet|workbook|document|whitepaper|dossier|pdf|docx|pptx|xlsx|csv|html)\b.*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(
        r"\s+(?:with\s+(?:relevant\s+)?(?:diagrams|references|equations|formulas|examples|figures|citations|charts|tables).*)$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(
        r"\s+(?:with\s+diagrams\s+)?(?:in|as)\s+(?:a\s+)?(?:pdf|word|docx|excel|spreadsheet|presentation|pptx|markdown|html)\b.*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    # 5. Strip deliverable artifacts & slide/page counts (numbers or words)
    cleaned = re.sub(
        r"\b(?:a\s+|an\s+)?(?:more\s+)?(?:detailed\s+)?(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|12)[- ](?:page|slide)s?\s+)?(?:research\s+)?(?:report|presentation|slides?|slide\s*deck|excel(?:\s+comparison)?|spreadsheet|workbook|document|whitepaper|dossier|pdf|docx|pptx|xlsx|csv|html|comparison)(?:\s+(?:of|with)\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|12)\s+(?:slides?|pages?))?\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(r"\b(?:of\s+)?(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|12)\s*[- ]?(?:slides?|pages?)\b", "", cleaned, flags=re.IGNORECASE).strip()

    # 6. Strip inclusion clauses for visuals / formulas
    cleaned = re.sub(
        r"\b(?:it\s+should\s+include|including|with)\b(?:(?!\b(?:and\s+also|and\s+research|also\s+research|research)\b).)*\b(?:formulas|equations|diagrams|figures|charts|tables|citations|references)(?:\s+(?:too|also|as\s+well))?",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    # 7. Strip connectors / research commands
    cleaned = re.sub(
        r"\b(?:and\s+)?(?:also\s+)?(?:research(?:ing)?|conduct\s+research\s+on|investigate|study|look\s+into)\s+(?:different\s+)?",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    # 8. Clean punctuation, trailing slang, leftover connectors, dangling prepositions and verbs
    cleaned = re.sub(r"\s+(?:bro|man|dude|buddy|pal|friend|pls|please|thanks|thank\s+you|for\s+me|as\s+well|too)$", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^(?:and|also|too|with|in|for|about|on)\s+", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\s+(?:too|also|as\s+well|for\s+it|about\s+it|instead)$", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"[.!?]+$", "", cleaned).strip()
    cleaned = re.sub(
        r"\s+(?:and\s+)?(?:prepare|create|generate|write|make|build|export|output)$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(r"\s+(?:in|on|at|for|with|about|from|of)$", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\s+(?:bro|man|dude|buddy|pal|friend|pls|please|thanks|thank\s+you|for\s+me|as\s+well|too)$", "", cleaned, flags=re.IGNORECASE).strip()

    prior_topic = resolve_topic_from_context(context)

    generic_words = {
        "platforms", "different platforms", "frameworks", "implementations",
        "tools", "architectures", "details", "more details", "comparison", "overview",
        "diagrams", "formulas", "equations", "technical details", "more technical details",
        "better", "it better", "elaborate", "can you elaborate", "clarify",
        "bro", "man", "dude", "generate", "create", "export", "save", "download",
    }
    is_explicit_research_cmd = bool(
        re.match(
            r"^(?:(?:hey|hi|yo|ok|okay|bro|dude|man|please|pls)\s+)*(?:(?:could|can|would)\s+(?:you|u)\s+)?(?:please\s+)?(?:research(?:ing)?|conduct\s+research\s+on|investigate|study|deep\s+dive\s+into|survey|create\s+(?:a\s+)?new\s+report\s+on)\b",
            text.strip(),
            re.IGNORECASE,
        )
    )
    if not cleaned or cleaned.lower() in {
        "it", "this", "that", "them", "instead", "report", "slides", "presentation",
        "details", "technical details", "more details", "more technical details", "better",
        "it better", "elaborate", "can you elaborate", "clarify",
        "bro", "man", "dude", "generate", "create", "export", "save", "download",
    }:
        return prior_topic or None
    elif not is_explicit_research_cmd and prior_topic and cleaned.lower() in generic_words:
        return f"{prior_topic} {cleaned}"
    elif (
        not is_explicit_research_cmd
        and prior_topic
        and prior_topic.lower() not in cleaned.lower()
        and len(cleaned.split()) <= 10
        and any(w in cleaned.lower() for w in ("platform", "framework", "tool", "code", "implementation", "detail", "equation", "diagram", "comparison", "compare", "jax", "pytorch", "latest", "development", "2024", "2025", "2026", "versus", "vs"))
    ):
        return f"{prior_topic} - {cleaned}"

    return cleaned or prior_topic or None


def is_research_or_deliverable_request(
    text: str,
    context: Optional[dict[str, Any]] = None,
) -> tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """Detect whether user prompt represents an autonomous research or deliverable request.

    Returns
    -------
    (is_research, deliverable_type, requested_format, topic_hint)
    """
    if not text:
        return False, None, None, None

    has_research = bool(RESEARCH_DIRECTIVE_RE.search(text))
    has_artifact = bool(PREPARE_ARTIFACT_RE.search(text))
    has_expl_deliverable = bool(EXPLANATION_WITH_DELIVERABLE_RE.search(text))
    has_comp_deliverable = bool(COMPARISON_WITH_DELIVERABLE_RE.search(text))
    req_format = detect_requested_format(text)
    has_format_directive = bool(
        req_format
        and re.search(
            r"\b(?:generate|create|prepare|make|build|produce|export|output|in|as|into)\b",
            text,
            re.IGNORECASE,
        )
    )

    prior_is_research = bool((context or {}).get("is_research"))
    prior_topic = resolve_topic_from_context(context)
    is_follow_up = bool(
        FOLLOW_UP_TRIGGERS_RE.search(text)
        or (prior_is_research and REFINEMENT_DIRECTIVE_PATTERN.match(text))
        or (prior_is_research and req_format)
        or (prior_is_research and any(k in text.lower() for k in ["page", "pages", "slide", "slides", "report", "image", "images", "diagram", "diagrams", "table", "tables", "formula", "formulas", "compare", "comparison"]))
    )

    if not (has_research or has_artifact or has_expl_deliverable or has_comp_deliverable or has_format_directive or (prior_is_research and is_follow_up)):
        return False, None, None, None

    deliv_type = detect_deliverable_type(text)
    if not deliv_type and is_follow_up and context:
        deliv_type = context.get("deliverable_type")

    # Standard default format based on deliverable type when unspecified
    if not req_format:
        if is_follow_up and context and context.get("requested_format"):
            req_format = context["requested_format"]
        elif deliv_type == "presentation":
            req_format = "pptx"
        elif deliv_type == "dataset":
            req_format = "xlsx"
        elif deliv_type == "report":
            req_format = "docx"

    topic_hint = extract_research_topic(text, context=context) or prior_topic
    return True, deliv_type, req_format, topic_hint

    topic_hint = extract_research_topic(text, context=context) or prior_topic
    return True, deliv_type, req_format, topic_hint


# ---------------------------------------------------------------------------
# 1. Non-browser patterns
# ---------------------------------------------------------------------------

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
    # Current-tab page inspection patterns (e.g. "Summarize this webpage", "Read this page")
    re.compile(r"\b(?:summarize|read|explain|inspect|analyze|extract(?:\s+from)?|what\s+is\s+on)\s+(?:this|the\s+current)\s+(?:webpage|page|website|article|tab|screen)\b", re.IGNORECASE),
    re.compile(r"^(?:summarize|explain|inspect|read)\s+this\s+page[!.\s]*$", re.IGNORECASE),
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
    # Conversational follow-ups and refinements
    FOLLOW_UP_TRIGGERS_RE,
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

COMPARISON_BROWSER_TRIGGER = re.compile(
    r"\b(?:compare|comparison\s+of)\s+.*?\s+(?:prices?|costs?|rates?|deals?|specs?|features?|models?|options?|between|across|on\b|at\b|vs\.?|versus)\b"
    r"|\b(?:compare|comparison)\s+(?:amazon|flipkart|ebay|walmart|croma|best\s*buy|google\s+flights|makemytrip)\b"
    r"|\b(?:compare|comparison)\s+.*?\s+(?:and|vs\.?|versus)\s+.*?\s+(?:prices?|costs?|on\b|at\b)",
    re.IGNORECASE,
)

SHOPPING_DISCOVERY_TRIGGER = re.compile(
    r"\b(?:find|search(?:\s+for)?|look\s+for|shop(?:\s+for)?|buy|order|purchase)\s+(?:the\s+)?(?:cheapest|best|top|lowest\s+price|budget|discounted)?\s+.*?\b(?:laptop|phone|iphone|mobile|gpu|rtx|shoes|watch|camera|tv|headphones|earbuds|tablet|monitor|pc|deal|deals)\b"
    r"|\b(?:find|get|show)\s+(?:three|four|five|\d+)?\s*(?:good|best|cheapest|top)\s+.*?\b(?:under|below|for)\s+(?:₹|\$|rs\.?|inr|usd)?\s*\d+",
    re.IGNORECASE,
)

TRAVEL_SEARCH_TRIGGER = re.compile(
    r"\b(?:find|search(?:\s+for)?|check|book|look\s+up)\s+(?:flights?|hotels?|trains?|cabs?|trips?|tickets?)\s+(?:from|to|in|between|for)\b"
    r"|\b(?:flight|hotel|train)\s+(?:prices?|fares?|rates?|booking)\b",
    re.IGNORECASE,
)

GENERIC_SITE_OPEN_TRIGGER = re.compile(
    r"^(?:open|visit|go\s+to|navigate\s+to|browse\s+to)\s+([a-zA-Z0-9_\-\.\:\/]+(?:\s+[a-zA-Z0-9_\-\.]+)?)(?:\s+(?:and|,)\s+.*)?$",
    re.IGNORECASE,
)

BROWSER_TRIGGERS = [
    re.compile(r"https?://[^\s]+", re.IGNORECASE),
    EMAIL_TRIGGER,
    DIRECT_SITE_TRIGGER,
    OFFICIAL_DOCS_TRIGGER,
    SEARCH_WEB_TRIGGER,
    PRODUCT_SEARCH_TRIGGER,
    COMPARISON_BROWSER_TRIGGER,
    SHOPPING_DISCOVERY_TRIGGER,
    TRAVEL_SEARCH_TRIGGER,
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

    # 3. Check for continuation of an active browser session or mission
    if CONTINUATION_DIRECTIVE_PATTERN.match(text):
        prior_is_res = bool((context or {}).get("is_research"))
        prior_fmt = (context or {}).get("requested_format")
        prior_deliv = (context or {}).get("deliverable_type")
        prior_topic = resolve_topic_from_context(context)
        return IntentClassification(
            intent=TaskIntent.CONTINUE_BROWSER_TASK if has_active_browser_session else TaskIntent.NON_BROWSER,
            confidence=0.98,
            reason="instruction requests continuing or resuming the active mission or research task",
            operation_type=ResearchOperationType.RESUME_EXISTING_MISSION,
            requested_format=prior_fmt,
            is_research=prior_is_res,
            research_topic=prior_topic,
            deliverable_type=prior_deliv,
        )

    if has_active_browser_session:
        for pat in CONTINUATION_PATTERNS:
            if pat.search(text):
                is_res_cont, deliv_cont, fmt_cont, topic_cont = is_research_or_deliverable_request(text, context=ctx)
                prior_is_res = bool((context or {}).get("is_research"))
                prior_fmt = (context or {}).get("requested_format")
                prior_deliv = (context or {}).get("deliverable_type")
                prior_topic = resolve_topic_from_context(context)
                return IntentClassification(
                    intent=TaskIntent.CONTINUE_BROWSER_TASK,
                    confidence=0.9,
                    reason="instruction continues existing active browser session",
                    operation_type=ResearchOperationType.RESUME_EXISTING_MISSION if prior_is_res else None,
                    requested_format=fmt_cont or prior_fmt,
                    is_research=is_res_cont or prior_is_res,
                    research_topic=topic_cont or prior_topic,
                    deliverable_type=deliv_cont or prior_deliv,
                )

    # 4. Check for unsupported desktop OS operations (P5.2 integrity)
    from .capabilities import check_supported_capability
    is_supp, supp_reason = check_supported_capability(text)
    if not is_supp:
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=1.0,
            reason=f"unsupported capability: {supp_reason}",
            operation_type=ResearchOperationType.NORMAL_CONVERSATION,
        )

    # 5. Check for explicit email / webmail directive (must route to browser, never refusal)
    if EMAIL_TRIGGER.search(text):
        return IntentClassification(
            intent=TaskIntent.NEW_BROWSER_TASK,
            confidence=0.99,
            reason="instruction requests email / webmail navigation",
            target_tab_hint="mail.google.com",
            operation_type=ResearchOperationType.NEW_RESEARCH,
        )

    # 5b. Check for pure deliverable export / compilation directive (e.g. "bro generate a pdf man", "export the previous analysis as a pdf")
    is_exp, exp_fmt, exp_deliv = is_export_deliverable_directive(text, context=ctx)
    if is_exp:
        prior_top = resolve_topic_from_context(ctx)
        has_prior_context = bool(ctx.get("is_research") or prior_top or ctx.get("conversation_history"))
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=0.98,
            reason=f"instruction requests deliverable compilation ({exp_fmt or 'document'}) from existing research findings",
            operation_type=ResearchOperationType.EXPORT_EXISTING_PACKAGE,
            requested_format=exp_fmt,
            is_research=has_prior_context,
            research_topic=prior_top,
            deliverable_type=exp_deliv,
        )



    # 6. Check for autonomous research & multi-format deliverable generation (Step 4)
    #    Evaluated BEFORE simple conceptual/explanation patterns to prevent requests
    #    with explicit deliverable artifacts (e.g. "Explain transformers in a PDF")
    #    from being trapped as pure text chat.
    is_res, deliv_type, req_fmt, topic_hint = is_research_or_deliverable_request(text, context=ctx)
    if is_res:
        prior_top = resolve_topic_from_context(ctx)
        prior_is_res = bool(ctx.get("is_research") or prior_top)
        is_explicit_new = bool(re.search(r"\b(?:new\s+(?:research|report|topic|paper)|start\s+a\s+new|instead\s+research)\b", text, re.IGNORECASE))
        is_refinement = False
        if prior_is_res and not is_explicit_new:
            if REFINEMENT_DIRECTIVE_PATTERN.match(text) or FOLLOW_UP_TRIGGERS_RE.search(text):
                is_refinement = True
            elif topic_hint and prior_top and (prior_top.lower() in topic_hint.lower()):
                is_refinement = True

        op_type = ResearchOperationType.REFINE_EXISTING_RESEARCH if is_refinement else ResearchOperationType.NEW_RESEARCH
        return IntentClassification(
            intent=TaskIntent.RESEARCH_TASK,
            confidence=0.96,
            reason=f"instruction specifies {'refinement of existing research' if is_refinement else 'autonomous new research'} / {deliv_type or 'artifact'} deliverable generation",
            operation_type=op_type,
            requested_format=req_fmt,
            is_research=True,
            research_topic=topic_hint or resolve_topic_from_context(context),
            deliverable_type=deliv_type,
        )

    # 7. Check for explicit non-browser categories
    # 7a. Greetings
    if GREETING_PATTERN.match(text):
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=1.0,
            reason="greeting matches non-browser conversational intent",
            operation_type=ResearchOperationType.NORMAL_CONVERSATION,
        )

    # 7b. Conversational / Meta
    if CONVERSATIONAL_PATTERN.match(text):
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=0.98,
            reason="conversational meta request matches non-browser intent",
            operation_type=ResearchOperationType.NORMAL_CONVERSATION,
        )

    # 7c. Code writing / programming questions
    if CODE_PATTERN.search(text):
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=0.95,
            reason="programming and coding request matches non-browser intent",
            operation_type=ResearchOperationType.NORMAL_CONVERSATION,
        )

    # 7d. Calculations and arithmetic
    if MATH_PATTERN.search(text):
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=0.95,
            reason="calculation request matches non-browser intent",
            operation_type=ResearchOperationType.NORMAL_CONVERSATION,
        )

    # 7e. Explanations and conceptual questions (without web browse commands or artifact deliverables)
    if EXPLANATION_PATTERN.match(text):
        return IntentClassification(
            intent=TaskIntent.NON_BROWSER,
            confidence=0.92,
            reason="conceptual explanation matches non-browser conversational intent",
            operation_type=ResearchOperationType.NORMAL_CONVERSATION,
        )

    # 8. Check for explicit direct site navigation (with ambiguity verification)
    gen_site_m = GENERIC_SITE_OPEN_TRIGGER.match(text)
    if gen_site_m:
        cand_entity = gen_site_m.group(1).strip().rstrip(".?!")
        from .website_resolver import is_ambiguous_entity
        is_ambig, ambig_term, ambig_candidates = is_ambiguous_entity(cand_entity)
        if is_ambig:
            return IntentClassification(
                intent=TaskIntent.NEW_BROWSER_TASK,
                confidence=0.7,
                reason=f"destination '{cand_entity}' is ambiguous and could refer to multiple distinct entities",
                is_ambiguous=True,
                target_tab_hint=cand_entity,
            )
        return IntentClassification(
            intent=TaskIntent.NEW_BROWSER_TASK,
            confidence=0.98,
            reason=f"instruction specifies direct navigation to website '{cand_entity}'",
            target_tab_hint=cand_entity,
        )

    # 9. Check for explicit browser triggers
    for pat in BROWSER_TRIGGERS:
        if pat.search(text):
            return IntentClassification(
                intent=TaskIntent.NEW_BROWSER_TASK,
                confidence=0.98,
                reason="instruction contains explicit URL or web browsing target",
            )

    # 10. Web research keywords (search, find, look up, buy on the web)
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

    # 10. Default to NON_BROWSER for general questions without browser indicators
    # e.g. "What is the capital of France?", "Help me understand photosynthesis", "Give me a recipe"
    return IntentClassification(
        intent=TaskIntent.NON_BROWSER,
        confidence=0.8,
        reason="general knowledge/conversational request without browsing directives",
    )
