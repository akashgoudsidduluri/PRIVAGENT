"""General-Purpose Research Harvester and Evidence Provenance Engine.

Harvests structured research evidence from general web pages (articles, documentation,
academic papers, news, benchmarks) and populates the canonical ResearchKnowledgePackage:
1. Captures source metadata (title, URL, domain, author, publication date, content type).
2. Extracts verifiable evidence snippets and links them to synthesized claims.
3. Detects derivative/syndicated reporting vs genuinely independent corroboration.
4. Identifies contradictory claims (divergent figures, dates, or assertions) and preserves both.
5. Strictly treats page content as untrusted data; neutralizes prompt injection vectors.
6. Handles inaccessible pages, duplicate URLs, and missing metadata fail-closed and honestly.
"""
from __future__ import annotations

import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Optional

from .models import (
    EvidenceClassification,
    EvidenceItem,
    ResearchClaim,
    ResearchKnowledgePackage,
    SourceReference,
    VisualAsset,
)


# ---------------------------------------------------------------------------
# Prompt Injection Neutralization (Untrusted Page Content Quarantine)
# ---------------------------------------------------------------------------

PROMPT_INJECTION_PATTERNS = [
    re.compile(r"\bignore\s+(?:all\s+)?previous\s+instructions\b", re.IGNORECASE),
    re.compile(r"\bdisregard\s+(?:all\s+)?prior\s+(?:instructions|prompts)\b", re.IGNORECASE),
    re.compile(r"\byou\s+are\s+now\s+(?:a|an)\s+[a-z0-9_\-\s]+\b", re.IGNORECASE),
    re.compile(r"\b(?:system\s+prompt|prompt\s+leak|jailbreak|developer\s+mode)\b", re.IGNORECASE),
    re.compile(r"\b(?:output\s+the\s+above|print\s+your\s+instructions)\b", re.IGNORECASE),
    re.compile(r"\binstruction\s+override\b", re.IGNORECASE),
    re.compile(r"\b(?:act\s+as\s+a\s+DAN|DAN\s+mode)\b", re.IGNORECASE),
]


def detect_prompt_injection(text: str) -> bool:
    """True if untrusted web text contains known adversarial prompt injection markers."""
    if not text:
        return False
    return any(p.search(text) for p in PROMPT_INJECTION_PATTERNS)


def sanitize_snippet(text: str) -> str:
    """Neutralize potential delimiter attacks and normalize whitespace in extracted text."""
    if not text:
        return ""
    # Strip dangerous HTML/XML-like wrapper attempts
    cleaned = re.sub(r"<\s*/?\s*(?:system|user|assistant|instruction|prompt)[^>]*>", "", text, flags=re.IGNORECASE)
    # Normalize excessive newlines and whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    from ..adversarial_guard import cleanse_untrusted
    return cleanse_untrusted(cleaned)


# ---------------------------------------------------------------------------
# Source Metadata Extraction
# ---------------------------------------------------------------------------

ISO_DATE_RE = re.compile(
    r"\b(20\d\d[-/](?:0[1-9]|1[0-2])[-/](?:0[1-9]|[12]\d|3[01]))\b"
)
ENGLISH_DATE_RE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+(?:20\d\d)\b",
    re.IGNORECASE,
)
BYLINE_RE = re.compile(
    r"(?i)(?:^|\n|\.\s+)\s*(?:By|Author:|Written by|Published by)\s+([A-Z][a-zA-Z0-9\s,\.\-]{2,80}?)(?:\n|,|\.\s+|\s{2,}|$)"
)

# Content type classifications by domain pattern
ACADEMIC_DOMAINS = {"arxiv.org", "nature.com", "ieee.org", "acm.org", "science.org", "cell.com", "springer.com", "biorxiv.org"}
DOCS_DOMAINS = {"w3.org", "developer.mozilla.org", "docs.python.org", "react.dev", "kubernetes.io", "github.com"}
NEWS_DOMAINS = {"reuters.com", "apnews.com", "bloomberg.com", "bbc.com", "theverge.com", "techcrunch.com", "arstechnica.com", "wsj.com", "nytimes.com"}
ENCYCLOPEDIA_DOMAINS = {"wikipedia.org", "britannica.com", "wikimedia.org"}
FORUM_DOMAINS = {"reddit.com", "stackoverflow.com", "quora.com", "news.ycombinator.com"}

# Inaccessible/blocking page indicators
INACCESSIBLE_PATTERNS = [
    re.compile(r"\b(?:404\s+not\s+found|page\s+not\s+found|403\s+forbidden|access\s+denied|500\s+internal\s+server\s+error|503\s+service\s+unavailable)\b", re.IGNORECASE),
    re.compile(r"\b(?:cloudflare|checking\s+your\s+browser|verify\s+you\s+are\s+human|attention\s+required|ddos-guard)\b", re.IGNORECASE),
    re.compile(r"\b(?:login\s+to\s+continue|sign\s+in\s+to\s+view|subscribe\s+to\s+read)\b", re.IGNORECASE),
]


def is_page_inaccessible(url: str, title: str, text: str) -> tuple[bool, Optional[str]]:
    """Detect whether a page is an error page, CAPTCHA wall, paywall, or blank."""
    if not text or len(text.strip()) < 30:
        return True, "Page content is empty or unreadable."
    combined = f"{title}\n{text[:1000]}"
    for pat in INACCESSIBLE_PATTERNS:
        m = pat.search(combined)
        if m:
            return True, f"Inaccessible or gated page: {m.group(0)}"
    return False, None


SEARCH_PATH_PATTERNS = [
    re.compile(r"/search(?:/|\?|$)", re.IGNORECASE),
    re.compile(r"\bSpecial:Search\b", re.IGNORECASE),
    re.compile(r"/action/doSearch", re.IGNORECASE),
    re.compile(r"[?&](?:q|query|search_query|searchtype)=", re.IGNORECASE),
]

SEARCH_TITLE_PATTERNS = [
    re.compile(r"\b(?:search\s+results?|results\s+for|search\s*[-|–—]\s*wikipedia)\b", re.IGNORECASE),
    re.compile(r"\b(?:search\s*\|\s*(?:nature|arxiv|springer|science))\b", re.IGNORECASE),
]

SEARCH_CONTENT_PATTERNS = [
    re.compile(r"\b(?:showing\s+\d+[-–]\d+\s+of\s+\d+|search\s+results\s+for|refine\s+your\s+search|results\s+sorted\s+by)\b", re.IGNORECASE),
]


def is_search_results_page(url: str, title: str = "", text: str = "") -> bool:
    """True if URL, title, or content indicates an index or search results page rather than substantive content."""
    if not url:
        return False
    u = url.lower()
    t = (title or "").lower()

    if any(p.search(u) for p in SEARCH_PATH_PATTERNS):
        return True
    if any(p.search(t) for p in SEARCH_TITLE_PATTERNS):
        return True
    if text:
        sample = text[:1500].lower()
        if any(p.search(sample) for p in SEARCH_CONTENT_PATTERNS):
            return True
    return False


def extract_substantive_links_from_search(
    current_url: str,
    interactive_elements: list[Any],
    page_text: str = "",
) -> list[dict[str, str]]:
    """Extract candidate substantive article or document links from a search-results page."""
    substantive_links: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    article_patterns = [
        re.compile(r"arxiv\.org/abs/\d+\.\d+", re.IGNORECASE),
        re.compile(r"nature\.com/articles/[a-z0-9\-_]+", re.IGNORECASE),
        re.compile(r"wikipedia\.org/wiki/(?!Special:|File:|Help:|Portal:|Wikipedia:)[^#?]+", re.IGNORECASE),
        re.compile(r"huggingface\.co/(?:docs|models)/[a-z0-9\-_/]+", re.IGNORECASE),
        re.compile(r"science\.org/doi/\d+\.\d+", re.IGNORECASE),
        re.compile(r"pytorch\.org/docs/[a-z0-9\-_/]+", re.IGNORECASE),
        re.compile(r"paperswithcode\.com/paper/[a-z0-9\-_/]+", re.IGNORECASE),
    ]

    for el in (interactive_elements or []):
        href = ""
        name = ""
        target_id = ""
        if isinstance(el, dict):
            href = el.get("href") or ""
            name = el.get("name") or el.get("text") or ""
            target_id = el.get("targetId") or el.get("target_id") or ""
        else:
            href = getattr(el, "href", "") or ""
            name = getattr(el, "name", "") or getattr(el, "text", "") or ""
            target_id = getattr(el, "target_id", "") or ""

        if not href and not name:
            continue

        full_url = ""
        if href:
            full_url = urllib.parse.urljoin(current_url, href)

        is_substantive = False
        if full_url:
            for ap in article_patterns:
                if ap.search(full_url):
                    is_substantive = True
                    break

        name_clean = name.strip()
        if len(name_clean) > 20 and not any(kw in name_clean.lower() for kw in ("search", "next", "previous", "filter", "sign in", "cookie", "privacy")):
            if target_id or full_url:
                is_substantive = True

        if is_substantive:
            norm = normalize_url(full_url) if full_url else name_clean.lower()
            if norm and norm not in seen_urls:
                seen_urls.add(norm)
                substantive_links.append({
                    "url": full_url,
                    "title": name_clean or "Substantive Article",
                    "target_id": target_id,
                })
                if len(substantive_links) >= 8:
                    break

    # Also extract raw URLs matching article patterns from page text
    if page_text and len(substantive_links) < 8:
        raw_urls = re.findall(r"https?://[^\s<>\"')]+", page_text)
        for u in raw_urls:
            u_clean = u.rstrip(".,;:")
            for ap in article_patterns:
                if ap.search(u_clean):
                    norm = normalize_url(u_clean)
                    if norm and norm not in seen_urls:
                        seen_urls.add(norm)
                        substantive_links.append({
                            "url": u_clean,
                            "title": "Substantive Academic Reference",
                            "target_id": "",
                        })
                    break
            if len(substantive_links) >= 8:
                break

    return substantive_links


def normalize_url(raw_url: str) -> str:
    """Canonicalize URL by stripping tracking parameters, hashes, and trailing slashes."""
    if not raw_url:
        return ""
    try:
        parts = urllib.parse.urlsplit(raw_url)
        # Filter tracking query parameters
        clean_query = []
        if parts.query:
            for pair in parts.query.split("&"):
                k = pair.split("=")[0].lower()
                if not (k.startswith("utm_") or k in ("ref", "fbclid", "gclid", "source", "feature")):
                    clean_query.append(pair)
        q_str = "&".join(clean_query)
        path = parts.path.rstrip("/") or "/"
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc.lower().replace("www.", ""), path, q_str, ""))
    except Exception:
        return raw_url.strip()


def extract_domain(url: str) -> str:
    """Extract clean domain name without www or port."""
    try:
        host = urllib.parse.urlsplit(url).netloc.lower()
        host = re.sub(r":\d+$", "", host)
        return host.replace("www.", "").strip()
    except Exception:
        return ""


def clean_page_title(title: str, domain: str) -> str:
    """Clean page title by removing site names and platform suffixes."""
    t = (title or "").strip()
    # Strip suffixes like " - Wikipedia", " | Nature", " | TechCrunch"
    t = re.sub(r"\s*[-|–—:]\s*(?:Wikipedia|Nature|TechCrunch|The Verge|Ars Technica|Reuters|Bloomberg|Medium|Reddit|Stack Overflow|GitHub).*$", "", t, flags=re.IGNORECASE)
    return t.strip() or domain or "Web Document"


def classify_content_type(domain: str, url: str) -> tuple[str, bool]:
    """Classify domain type and determine if it represents a primary/authoritative source."""
    dom = domain.lower()
    url_low = url.lower()

    if any(ad in dom for ad in ACADEMIC_DOMAINS) or dom.endswith(".edu") or dom.endswith(".gov"):
        return "academic", True
    if any(dd in dom for dd in DOCS_DOMAINS) or "docs." in dom or "/docs/" in url_low or "/documentation/" in url_low:
        return "documentation", True
    if any(ed in dom for ed in ENCYCLOPEDIA_DOMAINS):
        return "encyclopedia", False
    if any(nd in dom for nd in NEWS_DOMAINS):
        return "news", False
    if any(fd in dom for fd in FORUM_DOMAINS):
        return "forum", False
    if "blog" in dom or "/blog/" in url_low:
        return "blog", False
    return "general", False


def detect_derivative_reporting(text: str, current_domain: str) -> tuple[bool, Optional[str]]:
    """Detect whether text cites an external source as the originator of its claims."""
    if not text:
        return False, None
    sample = text[:3000]
    deriv_patterns = [
        re.compile(r"(?i)\b(?:according\s+to\s+(?:a\s+report\s+by|reports\s+in|data\s+from)?)\s+([a-zA-Z0-9\s\.\-]{2,50}?)(?:\n|,|\.\s+|\s+that|\s+which|$)", re.IGNORECASE),
        re.compile(r"(?i)\b(?:first\s+reported\s+by|originally\s+reported\s+by)\s+([a-zA-Z0-9\s\.\-]{2,50}?)(?:\n|,|\.\s+|\s+that|\s+which|$)", re.IGNORECASE),
        re.compile(r"(?i)\b(?:citing\s+(?:a\s+paper\s+in|a\s+study\s+by|a\s+source\s+at)?)\s+([a-zA-Z0-9\s\.\-]{2,50}?)(?:\n|,|\.\s+|\s+that|\s+which|$)", re.IGNORECASE),
    ]
    for pat in deriv_patterns:
        m = pat.search(sample)
        if m:
            cited_entity = m.group(1).strip()
            # Do not self-attribute
            if cited_entity.lower() not in current_domain:
                return True, cited_entity
    return False, None


def extract_source_metadata(url: str, title: str, text: str) -> dict[str, Any]:
    """Extract comprehensive metadata for a visited research source."""
    canon_url = normalize_url(url)
    domain = extract_domain(canon_url)
    clean_title = clean_page_title(title, domain)
    content_type, is_primary = classify_content_type(domain, canon_url)

    # 1. Author extraction
    author = None
    byline_match = BYLINE_RE.search(text[:2500])
    if byline_match:
        cand_author = byline_match.group(1).strip()
        if len(cand_author) < 80 and not any(kw in cand_author.lower() for kw in ("the", "all rights", "subscribe", "cookies")):
            author = cand_author

    # 2. Date extraction
    pub_date = None
    iso_m = ISO_DATE_RE.search(text[:2500])
    if iso_m:
        pub_date = iso_m.group(1)
    else:
        eng_m = ENGLISH_DATE_RE.search(text[:2500])
        if eng_m:
            pub_date = eng_m.group(0)

    # 3. Derivative reporting detection
    is_derivative, cited_origin = detect_derivative_reporting(text, domain)

    # Defensible reliability tier and basis
    reliability_tier = "credible_reference"
    reliability_basis = "General technical reference"
    reliability = 0.80

    if is_primary:
        if content_type == "academic":
            reliability_tier = "peer_reviewed"
            reliability_basis = "Academic peer-reviewed publication with published scientific methodology"
            reliability = 0.95
        elif content_type == "documentation":
            reliability_tier = "official_documentation"
            reliability_basis = "Official technical documentation from framework authors or maintainers"
            reliability = 0.92
    elif content_type == "encyclopedia":
        reliability_tier = "credible_reference"
        reliability_basis = "Curated encyclopedia reference with community citation standards"
        reliability = 0.85
    elif content_type == "news":
        reliability_tier = "secondary_reporting"
        reliability_basis = "Journalistic reporting with editorial review"
        reliability = 0.75
    elif content_type in ("blog", "forum"):
        reliability_tier = "unverified"
        reliability_basis = "Unverified community post or personal blog"
        reliability = 0.50

    if is_derivative:
        reliability = max(0.4, reliability - 0.20)
        reliability_basis = f"{reliability_basis} (Derivative reporting; cites {cited_origin or 'external source'})"

    return {
        "url": canon_url,
        "domain": domain,
        "title": clean_title,
        "author": author,
        "publication_date": pub_date,
        "content_type": content_type,
        "is_primary": is_primary,
        "is_derivative": is_derivative,
        "cited_origin": cited_origin,
        "reliability_score": reliability,
        "reliability_tier": reliability_tier,
        "reliability_basis": reliability_basis,
    }


# ---------------------------------------------------------------------------
# Evidence Snippet Extraction & Factual Mining
# ---------------------------------------------------------------------------

FACTUAL_SIGNALS_RE = re.compile(
    r"\b(?:"
    r"\d+(?:\.\d+)?%|"                     # Percentages (e.g. 99.9%)
    r"\d+(?:,\d{3})*(?:\.\d+)?\s*(?:ms|s|secs?|seconds?|minutes?|hours?|days?|kb|mb|gb|tb|pb|ghz|mhz|qubits?|flops|tflops|pflops|eflops|params?|parameters?|nodes?|clusters?|km|miles?|mw|gw|kwh|bleu|gpus?|cpus?|tpus?|tokens?|wh/kg|ms/cm|mah/g|mah|ma/cm2|a/cm2|v|kv|nm)|"  # Units & Benchmarks
    r"₹|\$|€|£|"                            # Financial symbols
    r"\b(?:202\d|203\d)\b|"                 # Timeline years (milestones / roadmaps)
    r"(?:increas|decreas|improv|achiev|reduc|exceed|outperform|measur|releas|introduc|develop|benchmark|demonstrat|support|replac|implement|establish|evaluat|yield|train|project|schedul|forecast|target|estimat|predict|expect|anticipat|launch|deploy|produc|reach|scale|report|announc)(?:e|ed|es|s|ing)?\b"
    r")",
    re.IGNORECASE,
)

BOILERPLATE_RE = re.compile(
    r"\b(?:"
    r"cookie\s+policy|terms\s+of\s+service|all\s+rights\s+reserved|privacy\s+notice|"
    r"sign\s+up\s+for|newsletter|please\s+enable\s+javascript|browser\s+is\s+out\s+of\s+date|"
    r"submitted\s+\d+\s+[a-z]+,?\s+\d{4}|originally\s+announced\s+[a-z]+\s+\d{4}|"
    r"comments:\s+\d+\s+pages"
    r")\b",
    re.IGNORECASE,
)


def extract_evidence_snippets(text: str, max_snippets: int = 8) -> list[dict[str, Any]]:
    """Extract substantive, factual evidence sentences from page text.

    Filters out boilerplates, prompt injection attempts, and trivial statements.
    """
    if not text:
        return []

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    candidates: list[dict[str, Any]] = []
    seen_hashes: set[int] = set()

    for line in lines:
        if len(candidates) >= max_snippets:
            break

        # Discard lines with prompt injection or boilerplate
        if detect_prompt_injection(line) or BOILERPLATE_RE.search(line):
            continue

        # Split line into sentences
        sentences = re.split(r"(?<=[.!?])\s+", line)
        for s in sentences:
            s_clean = sanitize_snippet(s)
            s_len = len(s_clean)
            if s_len < 30 or s_len > 400:
                continue

            # Check for factual or metric indicators
            if not FACTUAL_SIGNALS_RE.search(s_clean):
                continue

            # Deduplicate similar sentences
            sh = hash(s_clean[:60].lower())
            if sh in seen_hashes:
                continue
            seen_hashes.add(sh)

            confidence = 0.9 if any(sym in s_clean for sym in ("%", "₹", "$", "±", "=")) else 0.8
            candidates.append({
                "quote": s_clean,
                "context": line[:250],
                "confidence": confidence,
            })
            if len(candidates) >= max_snippets:
                break

    return candidates


# ---------------------------------------------------------------------------
# Contradiction and Discrepancy Detection
# ---------------------------------------------------------------------------

def detect_contradiction_between_claims(statement_a: str, statement_b: str) -> tuple[bool, Optional[str]]:
    """Detect whether two statements provide contradictory numbers, dates, or assertions."""
    if not statement_a or not statement_b:
        return False, None

    sa = statement_a.lower()
    sb = statement_b.lower()

    # 1. Divergent years for the same milestone
    years_a = set(re.findall(r"\b(202\d|203\d)\b", sa))
    years_b = set(re.findall(r"\b(202\d|203\d)\b", sb))
    if years_a and years_b and not (years_a & years_b):
        # Shared subject words?
        words_a = set(re.findall(r"\b[a-z]{4,}\b", sa))
        words_b = set(re.findall(r"\b[a-z]{4,}\b", sb))
        milestone_keywords = {"commercial", "production", "deployment", "launch", "release", "market", "delivery", "vehicle", "automotive", "scale", "manufacturing"}
        has_milestone_overlap = bool((words_a & milestone_keywords) and (words_b & milestone_keywords))
        if len(words_a & words_b) >= 2 or (has_milestone_overlap and len(words_a & words_b) >= 1):
            return True, f"Conflicting target timelines: {', '.join(years_a)} vs {', '.join(years_b)}"

    # 2. Divergent percentage / error rates
    pcts_a = re.findall(r"(\d+(?:\.\d+)?)\s*%", sa)
    pcts_b = re.findall(r"(\d+(?:\.\d+)?)\s*%", sb)
    if pcts_a and pcts_b:
        val_a = float(pcts_a[0])
        val_b = float(pcts_b[0])
        if abs(val_a - val_b) > 0.05 and abs(val_a - val_b) / max(val_a, val_b, 0.001) > 0.3:
            words_a = set(re.findall(r"\b[a-z]{4,}\b", sa))
            words_b = set(re.findall(r"\b[a-z]{4,}\b", sb))
            if len(words_a & words_b) >= 2:
                return True, f"Conflicting percentage metrics: {val_a}% vs {val_b}%"

    # 3. Direct affirmation vs negation on shared entity
    negations = {"not", "never", "cannot", "failed", "unsupported", "delayed", "lacks"}
    has_neg_a = any(n in sa.split() for n in negations)
    has_neg_b = any(n in sb.split() for n in negations)
    if has_neg_a != has_neg_b:
        words_a = set(re.findall(r"\b[a-z]{4,}\b", sa)) - negations
        words_b = set(re.findall(r"\b[a-z]{4,}\b", sb)) - negations
        overlap = words_a & words_b
        if len(overlap) >= 3:
            return True, f"Direct assertion conflict (affirmation vs negation regarding {', '.join(list(overlap)[:3])})"

    return False, None


# ---------------------------------------------------------------------------
# Claim Categorization & Type Classification
# ---------------------------------------------------------------------------

def classify_claim_type(statement: str) -> str:
    """Classify a factual statement into an explicit research claim type.

    Returns one of:
    - 'definition': terminology and concept definition.
    - 'mechanism': process, operational mechanism, or algorithmic flow.
    - 'advantage': strength, benefit, speedup, or efficiency improvement.
    - 'disadvantage': limitation, bottleneck, failure mode, or cost overhead.
    - 'statistic': measured metric, percentage, benchmark number, or price.
    - 'general': general research finding.
    """
    if not statement:
        return "general"
    s = statement.lower()

    if re.search(r"\b(?:\d+(?:\.\d+)?%|\$\d+|₹\d+|\d+(?:\.\d+)?\s*(?:ms|secs?|gb|tb|qubits?|flops|tflops|params?|ghz|mw|wh/kg|mah))\b", s):
        return "statistic"
    if re.search(r"\b(?:is defined as|refers to|denotes|is a type of|is a class of|is an architecture|is a novel)\b", s):
        return "definition"
    if re.search(r"\b(?:disadvantage|limitation|drawback|bottleneck|vulnerability|flaw|degradation|costly|overhead|unstable|failure rate|delayed)\b", s):
        return "disadvantage"
    if re.search(r"\b(?:advantage|benefit|superior|outperforms?|faster|speedups?|higher density|more efficient|reduces latency|breakthrough|improves?|enhanced)\b", s):
        return "advantage"
    if re.search(r"\b(?:works by|operates via|consists of|mechanism|processes|computes|propagates|transforms|architecture of|protocol)\b", s):
        return "mechanism"

    return "general"


# ---------------------------------------------------------------------------
# Website Image Extraction & Visual Provenance
# ---------------------------------------------------------------------------

TRACKER_AD_PATTERNS = re.compile(
    r"(?i)(?:tracker|pixel|analytics|doubleclick|googleads|ad[s_\-/]|advertisement|beacon|telemetry|stat[s\._]|spacer\.gif|blank\.gif|1x1|dot\.gif|badge|spinner|loader|wp-includes|avatar|user[-_]profile|author[-_]image|favicon)"
)

LOGO_ICON_PATTERNS = re.compile(
    r"(?i)(?:header[-_]logo|footer[-_]logo|site[-_]logo|brand[-_]logo|icon[-_]|/icons?/|nav[-_]icon|social[-_]share|share[-_]btn)"
)

VALID_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".avif", ".gif"}


def extract_page_images(
    url: str,
    text_or_html: str,
    title: str,
    domain: str,
    source_id: str,
    topic_keywords: Optional[list[str]] = None,
    interactive_elements: Optional[list[dict]] = None,
) -> list[VisualAsset]:
    """Extract authentic substantive images from web page content, filtering trackers/ads/logos.

    Produces VisualAsset instances with is_generated=False, authentic attribution,
    and original source URLs.
    """
    if not text_or_html and not interactive_elements:
        return []

    content = text_or_html or ""
    candidates: list[dict[str, Any]] = []

    # 1. Parse HTML <img> tags
    img_tag_re = re.compile(r"<img\s+([^>]+)>", re.IGNORECASE)
    for m in img_tag_re.finditer(content):
        attrs_str = m.group(1)
        src_m = re.search(r'(?:src|data-src|data-original)=["\']([^"\']+)["\']', attrs_str, re.IGNORECASE)
        if not src_m:
            continue
        src = src_m.group(1).strip()
        alt_m = re.search(r'alt=["\']([^"\']*)["\']', attrs_str, re.IGNORECASE)
        alt = alt_m.group(1).strip() if alt_m else ""
        title_m = re.search(r'title=["\']([^"\']*)["\']', attrs_str, re.IGNORECASE)
        img_title = title_m.group(1).strip() if title_m else ""
        w_m = re.search(r'width=["\']?(\d+)', attrs_str, re.IGNORECASE)
        h_m = re.search(r'height=["\']?(\d+)', attrs_str, re.IGNORECASE)
        w = int(w_m.group(1)) if w_m else None
        h = int(h_m.group(1)) if h_m else None
        candidates.append({"src": src, "alt": alt, "title": img_title, "width": w, "height": h, "context": ""})

    # 2. Parse OpenGraph and Twitter Meta Images
    og_re = re.compile(
        r'<meta\s+[^>]*?(?:property=["\']og:image["\']|name=["\']twitter:image["\'])\s+[^>]*?content=["\']([^"\']+)["\']',
        re.IGNORECASE,
    )
    for m in og_re.finditer(content):
        candidates.append({"src": m.group(1).strip(), "alt": title, "title": title, "width": None, "height": None, "context": "og:image"})

    og_rev_re = re.compile(
        r'<meta\s+[^>]*?content=["\']([^"\']+)["\']\s+[^>]*?(?:property=["\']og:image["\']|name=["\']twitter:image["\'])',
        re.IGNORECASE,
    )
    for m in og_rev_re.finditer(content):
        candidates.append({"src": m.group(1).strip(), "alt": title, "title": title, "width": None, "height": None, "context": "og:image"})

    # 3. Parse Markdown images: ![alt](url "optional title")
    md_re = re.compile(r'!\[([^\]]*)\]\((https?://[^\s\)]+)(?:\s+["\']([^"\']*)["\'])?\)', re.IGNORECASE)
    for m in md_re.finditer(content):
        candidates.append({
            "src": m.group(2).strip(),
            "alt": m.group(1).strip(),
            "title": m.group(3).strip() if m.group(3) else "",
            "width": None,
            "height": None,
            "context": "markdown",
        })

    # 4. Parse <figure> with <figcaption>
    fig_re = re.compile(r'<figure[^>]*>(.*?)</figure>', re.DOTALL | re.IGNORECASE)
    for fig_m in fig_re.finditer(content):
        fig_content = fig_m.group(1)
        cap_m = re.search(r'<figcaption[^>]*>(.*?)</figcaption>', fig_content, re.DOTALL | re.IGNORECASE)
        caption_txt = re.sub(r'<[^>]+>', '', cap_m.group(1)).strip() if cap_m else ""
        f_img_m = re.search(r'<img\s+[^>]*?src=["\']([^"\']+)["\']', fig_content, re.IGNORECASE)
        if f_img_m:
            candidates.append({
                "src": f_img_m.group(1).strip(),
                "alt": caption_txt,
                "title": caption_txt,
                "width": None,
                "height": None,
                "context": caption_txt,
            })

    # 5. Parse interactive elements if provided from DOM inspection
    if interactive_elements:
        for el in interactive_elements:
            if isinstance(el, dict):
                tag = str(el.get("tag", "")).lower()
                attrs = el.get("attributes", {}) or {}
                if tag == "img" or "src" in attrs:
                    src = attrs.get("src") or attrs.get("data-src")
                    if src:
                        candidates.append({
                            "src": str(src),
                            "alt": str(attrs.get("alt") or el.get("name") or el.get("text") or ""),
                            "title": str(attrs.get("title") or ""),
                            "width": None,
                            "height": None,
                            "context": "dom_element",
                        })

    # 6. Filter, Canonicalize, Deduplicate and Create VisualAssets
    visuals: list[VisualAsset] = []
    seen_urls: set[str] = set()

    for cand in candidates:
        raw_src = cand["src"]
        if not raw_src or raw_src.startswith("data:"):
            continue

        # Resolve to absolute URL
        abs_url = urllib.parse.urljoin(url, raw_src)
        parsed = urllib.parse.urlparse(abs_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            continue

        # Normalize canonical image URL by stripping tracking parameters
        clean_query = urllib.parse.parse_qs(parsed.query)
        clean_query = {
            k: v for k, v in clean_query.items()
            if not any(t in k.lower() for t in ("utm_", "fbclid", "gclid", "campaign", "source", "ref"))
        }
        # If the path already points to a static raster image file, omit tracking query entirely if clean_query has no semantic parameters
        final_query = urllib.parse.urlencode(clean_query, doseq=True)
        if any(parsed.path.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif")):
            final_query = ""
        canon_img_url = urllib.parse.urlunparse((
            parsed.scheme,
            parsed.netloc.lower(),
            parsed.path,
            parsed.params,
            final_query,
            "",
        ))

        if canon_img_url in seen_urls:
            continue

        # Filtering heuristics:
        # Prohibit raw SVG or AVIF vectors if renderers require raster formats
        p_path_lower = parsed.path.lower()
        if p_path_lower.endswith(".svg") or "/svg/" in p_path_lower:
            # If it's a thumbnail with a raster extension like .svg.png, allow it; otherwise skip raw svg
            if not any(p_path_lower.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp")):
                continue

        # Check explicit dimension attributes
        w = cand.get("width")
        h = cand.get("height")
        if (w is not None and w < 100) or (h is not None and h < 100):
            continue

        # Skip tiny thumbnail icons (e.g. Wikipedia 16px, 20px, 32px icons)
        if re.search(r"/(?:[1-4]\d|50)px-", canon_img_url):
            continue

        # Check tracking and advertisement patterns
        alt_str = cand.get("alt") or ""
        title_str = cand.get("title") or ""
        combined_text = f"{canon_img_url} {alt_str} {title_str} {cand.get('context') or ''}"

        if TRACKER_AD_PATTERNS.search(combined_text):
            continue
        if LOGO_ICON_PATTERNS.search(combined_text):
            continue

        # Specific logo or site branding checks
        if alt_str.lower() in {"logo", "site logo", "company logo", "brand", "icon", "arrow", "spacer", "pixel"}:
            continue

        seen_urls.add(canon_img_url)

        # Asset classification & titles
        descriptive_title = title_str or alt_str
        if not descriptive_title or len(descriptive_title) < 5:
            descriptive_title = f"{clean_page_title(title, domain)} Figure {len(visuals) + 1}"
        else:
            descriptive_title = descriptive_title[:80].strip()

        caption = cand.get("context") or alt_str or f"Evidence figure from {domain}"
        if len(caption) > 200:
            caption = caption[:197] + "..."

        is_diagram_like = any(
            k in (alt_str + " " + title_str + " " + canon_img_url).lower()
            for k in ["diagram", "chart", "figure", "graph", "architecture", "flowchart", "schematic"]
        )
        asset_type = "illustration" if is_diagram_like else "photograph"

        vis = VisualAsset(
            asset_id=f"vis_ext_{source_id}_{len(visuals) + 1}",
            asset_type=asset_type,
            title=descriptive_title,
            caption=caption,
            url_or_path=canon_img_url,
            source_url=url,
            source_id=source_id,
            attribution=f"Source: {domain} ({title})",
            is_generated=False,
            metadata={
                "extracted_from": domain,
                "canonical_image_url": canon_img_url,
                "original_alt": alt_str,
                "width": w,
                "height": h,
            },
        )
        visuals.append(vis)

    return visuals


# ---------------------------------------------------------------------------
# Cross-Source Corroboration & Provenance Integration
# ---------------------------------------------------------------------------

@dataclass
class HarvestResult:
    """Outcome of harvesting one visited page."""
    source_id: str
    url: str
    domain: str
    status: str  # "success" | "inaccessible" | "duplicate" | "error"
    evidence_count: int
    claims_added: int
    claims_corroborated: int
    claims_conflicted: int
    notes: Optional[str] = None


def harvest_page_evidence(
    url: Any = None,
    title: Any = None,
    text: Any = None,
    pkg: Any = None,
    **kwargs: Any,
) -> HarvestResult:
    """Harvest sources, evidence snippets, authentic images, and claims from a visited page into the package.

    Preserves source-to-claim provenance, identifies independent corroboration vs conflicts,
    and extracts authentic page figures. Supports both (url, title, text, pkg) and (pkg, url, title, text),
    positionally or as keyword arguments.
    """
    # Check if pkg was passed as first argument positionally
    if isinstance(url, ResearchKnowledgePackage):
        actual_pkg: ResearchKnowledgePackage = url
        actual_url: str = str(title or "")
        actual_title: str = str(text or "")
        actual_text: str = str(pkg or "")
    else:
        actual_pkg = pkg or kwargs.get("pkg")
        actual_url = str(url or kwargs.get("url") or "")
        actual_title = str(title or kwargs.get("title") or "")
        actual_text = str(text or kwargs.get("text") or "")

    pkg = actual_pkg
    url = actual_url
    title = actual_title
    text = actual_text

    canon_url = normalize_url(url)
    domain = extract_domain(canon_url)

    if not canon_url or not domain:
        return HarvestResult(
            source_id="", url=url, domain="", status="error",
            evidence_count=0, claims_added=0, claims_corroborated=0, claims_conflicted=0,
            notes="Invalid URL or missing domain.",
        )

    # 1. Check for Duplicate Source in Package
    for existing_src in pkg.sources.values():
        if existing_src.url == canon_url:
            return HarvestResult(
                source_id=existing_src.source_id, url=canon_url, domain=domain, status="duplicate",
                evidence_count=0, claims_added=0, claims_corroborated=0, claims_conflicted=0,
                notes="Source URL already harvested; skipping duplicate.",
            )

    # 2. Check for Inaccessible / Error Page
    inaccessible, inacc_reason = is_page_inaccessible(canon_url, title, text)
    if inaccessible:
        src_id = f"src_{len(pkg.sources) + 1:03d}"
        inacc_source = SourceReference(
            source_id=src_id,
            url=canon_url,
            domain=domain,
            title=clean_page_title(title, domain),
            reliability_score=0.0,
            metadata={"status": "inaccessible", "reason": inacc_reason or "Page unreadable"},
        )
        pkg.add_source(inacc_source)
        return HarvestResult(
            source_id=src_id, url=canon_url, domain=domain, status="inaccessible",
            evidence_count=0, claims_added=0, claims_corroborated=0, claims_conflicted=0,
            notes=inacc_reason,
        )

    # 2b. Check for Search Results Page (must NOT be counted as a substantive research source)
    if is_search_results_page(canon_url, title, text):
        return HarvestResult(
            source_id="", url=canon_url, domain=domain, status="search_results",
            evidence_count=0, claims_added=0, claims_corroborated=0, claims_conflicted=0,
            notes="Search results page; skipped substantive evidence harvesting.",
        )

    # 3. Extract Metadata and Register Source
    meta = extract_source_metadata(canon_url, title, text)
    src_id = f"src_{len(pkg.sources) + 1:03d}"
    source = SourceReference(
        source_id=src_id,
        url=canon_url,
        domain=domain,
        title=meta["title"],
        author=meta["author"],
        publication_date=meta["publication_date"],
        reliability_score=meta["reliability_score"],
        reliability_tier=meta.get("reliability_tier"),
        is_primary=meta["is_primary"],
        metadata={
            "content_type": meta["content_type"],
            "is_derivative": meta["is_derivative"],
            "cited_origin": meta["cited_origin"],
            "reliability_basis": meta.get("reliability_basis"),
        },
    )
    pkg.add_source(source)

    # 4. Extract Evidence Snippets
    snippets = extract_evidence_snippets(text)
    ev_ids: list[str] = []
    for snip in snippets:
        ev_id = f"ev_{len(pkg.evidence) + 1:03d}"
        ev_item = EvidenceItem(
            evidence_id=ev_id,
            source_id=src_id,
            quote=snip["quote"],
            context=snip["context"],
            confidence=snip["confidence"],
        )
        pkg.add_evidence(ev_item)
        ev_ids.append(ev_id)

    # 5. Synthesize Claims & Cross-Correlate
    claims_added = 0
    claims_corroborated = 0
    claims_conflicted = 0

    for ev_id in ev_ids:
        ev = pkg.evidence[ev_id]
        statement = ev.quote

        # Compare against existing claims in the package
        matched_claim: Optional[ResearchClaim] = None
        has_conflict = False
        conflict_reason: Optional[str] = None

        words_statement = set(re.findall(r"\b[a-z]{4,}\b", statement.lower()))

        for existing_clm in pkg.claims.values():
            # Check for direct contradiction first
            is_conflict, reason = detect_contradiction_between_claims(existing_clm.statement, statement)
            if is_conflict:
                has_conflict = True
                conflict_reason = reason
                matched_claim = existing_clm
                break

            # Check for semantic overlap / corroboration
            words_existing = set(re.findall(r"\b[a-z]{4,}\b", existing_clm.statement.lower()))
            overlap = words_statement & words_existing
            # Substantial subject token overlap indicates discussion of the same factual point
            if len(overlap) >= 3 and len(overlap) / max(len(words_statement), 1) >= 0.4:
                matched_claim = existing_clm
                break

        if has_conflict and matched_claim:
            # Conflicting claim: preserve disagreement honestly
            matched_claim.counter_evidence_ids.append(ev_id)
            matched_claim.classification = EvidenceClassification.CONFLICTING
            matched_claim.contradiction_notes = (
                f"{matched_claim.contradiction_notes or ''}\n"
                f"Discrepancy with {source.domain}: {conflict_reason}"
            ).strip()
            pkg.contradictions.append(
                f"Conflict between {matched_claim.primary_source_id} and {src_id}: {conflict_reason}"
            )
            claims_conflicted += 1

        elif matched_claim:
            # Check independence of the corroborating source
            primary_src = pkg.sources.get(matched_claim.primary_source_id or "")
            is_independent = True

            # If the current source is derivative of the primary source or vice versa, do not count as independent
            if meta["is_derivative"] and meta["cited_origin"]:
                if primary_src and primary_src.domain in meta["cited_origin"].lower():
                    is_independent = False
            if primary_src and primary_src.metadata.get("is_derivative") and primary_src.metadata.get("cited_origin"):
                if domain in str(primary_src.metadata.get("cited_origin", "")).lower():
                    is_independent = False
            # Same domain or common publisher subdomains cannot independently corroborate itself
            if primary_src and (primary_src.domain == domain or primary_src.domain.endswith(f".{domain}") or domain.endswith(f".{primary_src.domain}")):
                is_independent = False

            if src_id not in matched_claim.supporting_source_ids:
                matched_claim.supporting_source_ids.append(src_id)
            if ev_id not in matched_claim.evidence_item_ids:
                matched_claim.evidence_item_ids.append(ev_id)

            if is_independent and len(matched_claim.counter_evidence_ids) == 0:
                matched_claim.classification = EvidenceClassification.CORROBORATED
                claims_corroborated += 1

        else:
            # Novel factual claim backed by this single source
            clm_id = f"clm_{len(pkg.claims) + 1:03d}"
            c_type = classify_claim_type(statement)
            new_clm = ResearchClaim(
                claim_id=clm_id,
                statement=statement,
                classification=EvidenceClassification.SINGLE_SOURCE,
                claim_type=c_type,
                primary_source_id=src_id,
                supporting_source_ids=[],
                evidence_item_ids=[ev_id],
                confidence=source.reliability_score * ev.confidence,
                tags=[c_type],
            )
            pkg.add_claim(new_clm)
            claims_added += 1

    # 6. Extract Substantive Website Images & Associate Provenance
    try:
        html_content = text
        # If text does not contain HTML tags and the URL is a safe public HTTP/HTTPS URL,
        # fetch the page HTML with proper User-Agent to extract genuine diagrams and schematics
        if ("<img" not in text.lower() and "<figure" not in text.lower()) and canon_url.startswith(("http://", "https://")):
            try:
                from ..website_resolver import is_safe_public_url
                safe, _ = is_safe_public_url(canon_url)
                if safe:
                    import httpx
                    fetch_headers = {
                        "User-Agent": "PrivAgentResearchBot/1.0 (https://github.com/privagent; contact@privagent.local)",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                    }
                    with httpx.Client(timeout=4.0, follow_redirects=True) as client:
                        resp = client.get(canon_url, headers=fetch_headers)
                        if resp.status_code == 200 and resp.text:
                            html_content = resp.text
            except Exception:
                pass

        extracted_images = extract_page_images(canon_url, html_content, meta["title"], domain, src_id)
        for img in extracted_images[:4]:
            pkg.add_visual(img)
    except Exception:
        pass

    return HarvestResult(
        source_id=src_id,
        url=canon_url,
        domain=domain,
        status="success",
        evidence_count=len(ev_ids),
        claims_added=claims_added,
        claims_corroborated=claims_corroborated,
        claims_conflicted=claims_conflicted,
        notes=f"Harvested {len(ev_ids)} evidence snippets ({meta['content_type']}).",
    )

