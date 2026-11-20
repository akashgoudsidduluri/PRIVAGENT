"""Shared plain-text helpers for research report generation.

No imports from the rest of the package (so ``document_plan``,
``adaptive_planner``, ``collector`` and ``loop_research`` can all use
these without creating import cycles):

- :func:`smart_title_case` — title-casing that keeps acronyms uppercase.
- :func:`strip_llm_meta_disclaimers` — removes LLM meta-apologies and
  format notes (e.g. "I cannot render a binary PDF...") from synthesized
  text before it is rendered into a report.
- :func:`markdown_inline_to_reportlab` — converts inline Markdown
  (``**bold**``, ``*italic*``, inline code, links) into the HTML-like
  markup ReportLab paragraphs understand, so raw ``**`` / ``|`` never
  reach the PDF.
"""
from __future__ import annotations

import re

# Acronyms / initialisms that must stay uppercase in titles.
TITLE_ACRONYMS = {
    "AI", "ML", "NLP", "LLM", "RAG", "MCP", "API", "PDF", "DOCX", "XLSX",
    "PPTX", "CSV", "HTML", "CSS", "JS", "TS", "JSON", "SQL", "OCR",
    "GPU", "CPU", "TPU", "RAM", "SSD", "HDD", "USB", "HTTP", "HTTPS",
    "URL", "URI", "IP", "TCP", "UDP", "DNS", "VPN", "SSH", "TLS", "SSL",
    "UI", "UX", "OS", "QA", "CI", "CD", "AWS", "GCP", "SDK", "CLI",
    "ID", "IoT", "AR", "VR", "DNA", "RNA", "3D", "2D", "4G", "5G",
    "QKD", "NMR", "MRI", "CT", "EEG", "ECG", "EV", "WHO", "NASA",
    "ISRO", "SIH", "EU", "UK", "US", "USA",
}

_WORD_RE = re.compile(r"^(\W*)(.*?)(\W*)$", re.DOTALL)


def _title_case_word(word: str) -> str:
    """Title-case one word, preserving acronyms and existing capitals."""
    if not word:
        return word
    # Hyphenated compounds: fix each part ("state-of-the-art" stays
    # readable, "AI-driven" keeps its acronym).
    if "-" in word:
        return "-".join(_title_case_word(part) for part in word.split("-"))
    m = _WORD_RE.match(word)
    if not m:
        return word
    pre, core, post = m.group(1), m.group(2), m.group(3)
    if not core:
        return word
    if core.upper() in TITLE_ACRONYMS:
        return f"{pre}{core.upper()}{post}"
    # An all-caps word that is NOT a known acronym is shouting, not an
    # acronym: "QUANTUM MECHANICS AND AI" -> "Quantum Mechanics And AI".
    # (Known acronyms are handled by the branch above.)
    if (
        core[0].isupper()
        and any(c.isupper() for c in core[1:])
        and any(c.islower() for c in core)
    ):
        # CamelCase / mixed-case token (e.g. "McKinsey", "iPhone") — keep.
        return word
    return f"{pre}{core[:1].upper()}{core[1:].lower()}{post}"


def smart_title_case(text: str) -> str:
    """Title-case ``text`` while keeping acronyms (AI, LLM, API, PDF)
    uppercase instead of mangling them to Ai / Llm / Api / Pdf."""
    if not text:
        return text
    return " ".join(_title_case_word(w) for w in text.split(" "))


# ---------------------------------------------------------------------------
# LLM meta-disclaimers
# ---------------------------------------------------------------------------

# Whole sentences / lines that are the model talking about itself or the
# deliverable format instead of reporting findings. Matched case-insensitively.
_META_DISCLAIMER_PATTERNS = [
    re.compile(r"\bi\s+cannot\s+render\b", re.IGNORECASE),
    re.compile(r"\bi\s+cannot\s+(?:generate|create|produce|provide|make)\s+(?:a\s+)?(?:binary\s+)?(?:pdf|docx?|document|file)\b", re.IGNORECASE),
    re.compile(r"\bi\s+can'?t\s+(?:render|generate|create|produce|provide|make)\b", re.IGNORECASE),
    re.compile(r"\bformat\s+note\s*:", re.IGNORECASE),
    re.compile(r"\bas\s+an\s+ai\b", re.IGNORECASE),
    re.compile(r"\bas\s+a\s+language\s+model\b", re.IGNORECASE),
    re.compile(r"\bi\s+apologiz\w*\b", re.IGNORECASE),
    re.compile(r"\bi'?m\s+sorry\b", re.IGNORECASE),
    re.compile(r"\bi\s+am\s+unable\s+to\b", re.IGNORECASE),
    re.compile(r"\bi\s+don'?t\s+have\s+(?:the\s+ability|access)\b", re.IGNORECASE),
    re.compile(r"\bi\s+cannot\s+(?:access|browse|open)\b", re.IGNORECASE),
    re.compile(r"\bthis\s+is\s+a\s+text-based\b", re.IGNORECASE),
    re.compile(r"\bnote\s*:\s*i\s+(?:cannot|can'?t|am\s+unable)\b", re.IGNORECASE),
]

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[]|$)")


def strip_llm_meta_disclaimers(text: str) -> str:
    """Drop sentences/lines that are LLM meta-talk, keep the findings.

    A sentence is dropped only when it matches a meta-disclaimer
    pattern; the rest of the text is returned unchanged (apart from
    whitespace normalisation).
    """
    if not text:
        return ""
    kept_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # A whole line that is a format note goes entirely.
        if re.match(r"(?i)format\s+note\s*:", stripped):
            continue
        sentences = _SENTENCE_SPLIT_RE.split(stripped)
        kept = [
            s.strip()
            for s in sentences
            if s.strip()
            and not any(p.search(s) for p in _META_DISCLAIMER_PATTERNS)
        ]
        if kept:
            kept_lines.append(" ".join(kept))
    return "\n".join(kept_lines).strip()


# ---------------------------------------------------------------------------
# Inline Markdown -> ReportLab markup
# ---------------------------------------------------------------------------

_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__")
_ITALIC_RE = re.compile(r"(?<!\w)\*([^*\n]+)\*(?!\w)|(?<!\w)_([^_\n]+)_(?!\w)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BARE_AMP_RE = re.compile(r"&(?!(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);)")
_HEADING_PREFIX_RE = re.compile(r"^\s*#{1,6}\s+")


def markdown_inline_to_reportlab(text: str) -> str:
    """Convert inline Markdown to ReportLab paragraph markup.

    ``**bold**`` -> ``<b>bold</b>``, ``*italic*`` -> ``<i>italic</i>``,
    back-tick code -> Courier, ``[label](url)`` -> link. Bare ``&`` is
    escaped (ReportLab parses the text as XML); already-valid entities
    are left alone. Heading markers (``## ``) are stripped — headings
    belong in block titles, not body text.
    """
    if not text:
        return ""
    out = _HEADING_PREFIX_RE.sub("", text.strip())
    out = _BARE_AMP_RE.sub("&amp;", out)
    out = _CODE_RE.sub(r'<font face="Courier">\1</font>', out)
    out = _BOLD_RE.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", out)
    out = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", out)
    out = _LINK_RE.sub(r'<link href="\2">\1</link>', out)
    return out
