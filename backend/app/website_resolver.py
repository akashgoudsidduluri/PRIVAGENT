"""Reusable Website Resolution and Domain Verification Engine.

Separates website identification, search resolution, official verification,
lookalike detection, regional selection, and resolution caching from
content extraction.

Usable across all PrivAgent tasks (navigation, e-commerce, research, travel).
"""
from __future__ import annotations

import ipaddress
import re
import socket
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional


# Domain suffixes that mark a token as a hostname, never a search
# keyword. A TLD list, not a site list: "node.js" keeps its ".js"
# and "v1.2" keeps its version, but "nature.com" is stripped.
_QUERY_DOMAIN_TLDS = r"(?:com|org|net|io|in|gov|edu|co|uk|ai|dev|app|info|me|tv|co\.in|org\.in|ac\.in|co\.uk|org\.uk|com\.au)"
_QUERY_DOMAIN_RE = re.compile(
    rf"\b[\w-]+(?:\.[\w-]+)*\.{_QUERY_DOMAIN_TLDS}\b", re.IGNORECASE
)
_QUERY_DOMAIN_WITH_PREP_RE = re.compile(
    rf"\b(?:on|from|at|via|using)\s+[\w-]+(?:\.[\w-]+)*\.{_QUERY_DOMAIN_TLDS}\b",
    re.IGNORECASE,
)
# References to the request itself, not its subject: "for the whole
# topic", "on this topic", "the whole topic", "this topic".
_TOPIC_META_RES = [
    re.compile(
        r"\b(?:for|on|about|of|across|throughout)\s+(?:(?:the|this|that)\s+)?(?:(?:whole|entire)\s+)?topic\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:(?:the|this|that)\s+)?(?:whole|entire)\s+topic\b", re.IGNORECASE),
    re.compile(r"\b(?:the|this|that)\s+topic\b", re.IGNORECASE),
]


def _strip_query_filler(text: str) -> str:
    """Remove tokens that can never be search keywords from a query:
    domain names ("on nature.com", "nature.com") and references to
    the request itself ("for the whole topic", "this topic"). Live
    case: "research Aditya L1 for the whole topic on nature.com"
    decomposed to queries[0] == "Aditya L1 for the whole topic on
    nature.com ..." and discovery templated it verbatim into a
    garbage nature.com search URL. A query that is only filler comes
    back empty."""
    out = text or ""
    out = _strip_ui_glyphs(out)
    out = _strip_submission_preamble(out)
    out = _QUERY_DOMAIN_WITH_PREP_RE.sub(" ", out)
    out = _QUERY_DOMAIN_RE.sub(" ", out)
    for pat in _TOPIC_META_RES:
        out = pat.sub(" ", out)
    # Edge cleanup, repeated until stable: each strip can expose a
    # new dangling token ("Aditya L1 on and" -> "Aditya L1 on" ->
    # "Aditya L1"; "and generate" -> ""). Mid-string conjunctions
    # ("salt and pepper") are untouched.
    for _ in range(4):
        prev = out
        # A trailing deliverable verb left behind when its artifact
        # was stripped earlier ("and generate" from "... and generate
        # a PDF report").
        out = re.sub(
            r"\s+(?:and\s+)?(?:generate|create|prepare|make|produce|export|build|write)\s*$",
            "", out, flags=re.IGNORECASE,
        )
        out = re.sub(r"^(?:for|on|about|of|in|at|across|throughout|and|also|or)\s+", "", out, flags=re.IGNORECASE)
        out = re.sub(r"\s+(?:for|on|about|of|in|at|across|throughout|and|also|or)$", "", out, flags=re.IGNORECASE)
        out = re.sub(r"^(?:the|a|an)\s+", "", out, flags=re.IGNORECASE)
        out = re.sub(r"\s+", " ", out).strip()
        if out == prev:
            break
    if out.lower() in ("whole", "entire", "and", "also", "or"):
        return ""
    return out


# Decorative / UI glyphs that leak into an instruction when the user
# copies text from a chat or a page (an external-link arrow, a
# checkmark, a chevron). They are never search keywords: a query
# carrying one gets templated verbatim into a hub search URL.
_UI_GLYPH_RE = re.compile(
    r"[↗↘↙↖→←↑↓↔✓✔✗✕✖›‹«»•●○◆◇★☆☐☑☒⏎⏏⇧⌘⟳⟲⤴⤵⧉]"
)
# Assignment / submission scaffolding: the user states WHY they need
# the research ("i have to submit a paper", "i need to write an
# essay"), not WHAT it is about. Left in, the scaffolding eats the
# 8-word cap and the real topic is truncated away — live case:
# "i have to submit a research paper on topic : \"Quantum Mechanics
# similarities with life?\"" decomposed to queries[0] ==
# "i have to submit a paper : \"Quantum overview technical
# principles" (preamble + first word + generic research suffix).
_SUBMISSION_PREAMBLE_RES = [
    # "i have to submit a paper / research paper / assignment / essay"
    re.compile(
        r"^(?:so\s+)?i\s+(?:have\s+to|need\s+to|must|am\s+going\s+to|’m\s+going\s+to|’ve\s+got\s+to|have\s+got\s+to)\s+"
        r"(?:submit|write|prepare|present|do|complete)\s+(?:a\s+|an\s+|my\s+|the\s+)?"
        r"(?:research\s+)?(?:paper|assignment|essay|report|thesis|dissertation|project|homework|article|review)\b",
        re.IGNORECASE,
    ),
    # "submit a paper / write an essay" without the "i have to"
    re.compile(
        r"^(?:so\s+)?(?:submit|write|prepare|present|do|complete)\s+(?:a\s+|an\s+|my\s+|the\s+)?"
        r"(?:research\s+)?(?:paper|assignment|essay|report|thesis|dissertation|project|homework|article|review)\b",
        re.IGNORECASE,
    ),
    # "for my assignment / paper / class / submission"
    re.compile(
        r"\bfor\s+my\s+(?:assignment|paper|essay|report|thesis|class|submission|homework)\b",
        re.IGNORECASE,
    ),
    # "on topic", "about the topic", "the topic is", "topic:", "on the topic of"
    re.compile(
        r"\b(?:on|about|of)\s+(?:the\s+)?topic\s*(?::|is|about|of)?\b",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*topic\s*:\s*", re.IGNORECASE),
]


def _strip_ui_glyphs(text: str) -> str:
    """Drop decorative/UI glyphs (external-link arrows, checkmarks,
    chevrons) that leak into an instruction from a copy-paste."""
    out = _UI_GLYPH_RE.sub(" ", text or "")
    return re.sub(r"\s+", " ", out).strip()


def _strip_submission_preamble(text: str) -> str:
    """Remove assignment/submission scaffolding ("i have to submit a
    paper", "on topic", "topic:") so only the subject survives.
    Repeated until stable: stripping one scaffold can expose the
    next ("i have to submit a research paper on topic" -> "" after
    the topic is quoted out, but "i have to submit a paper on
    topic" needs two passes)."""
    out = text or ""
    for _ in range(3):
        prev = out
        for pat in _SUBMISSION_PREAMBLE_RES:
            out = pat.sub(" ", out)
        out = re.sub(r"\s+", " ", out).strip()
        out = re.sub(r"^[\s:;,\-–—]+", "", out).strip()
        out = re.sub(r"[\s:;,\-–—]+$", "", out).strip()
        # A bare preposition left in front of the topic ("about
        # photosynthesis" from "write an essay about photosynthesis").
        out = re.sub(r"^(?:about|on|of|regarding|concerning)\s+", "", out, flags=re.IGNORECASE).strip()
        if out == prev:
            break
    return out


def _extract_quoted_topic(text: str) -> Optional[str]:
    """Return the topic the user put in double quotes, if any.
    A quoted span is the strongest topic signal there is: the user
    quoted it because it IS the subject ("submit a paper on topic:
    \"Quantum Mechanics similarities with life?\""). An unclosed
    quote still counts (the user often drops the closing quote).
    Returns None when there is no quoted span of 3+ chars."""
    m = re.search(r'"([^"]{3,})"?', text or "")
    if not m:
        return None
    topic = m.group(1).strip()
    # Strip a trailing question mark / period the user put inside
    # the quotes ("... with life?" -> "... with life").
    topic = re.sub(r"[?.!]+$", "", topic).strip()
    return topic or None


def _host_on_allowlist(host: str, allowed_domains: Optional[list[str]]) -> bool:
    """True when *host* is covered by *allowed_domains*. An empty /
    unset allow-list means "not configured": everything is allowed
    (``host_matches_allowlist`` returns False for an empty list, so
    it cannot be used directly for this check)."""
    if not allowed_domains:
        return True
    from .allowlist import host_matches_allowlist

    return host_matches_allowlist(host, allowed_domains)


@dataclass
class ResolvedWebsite:
    """Outcome of resolving an entity, brand, or query to a canonical website."""
    target_name: str
    canonical_url: str
    primary_domain: str
    allowed_domains: list[str]
    is_verified_official: bool
    confidence: float
    region: str = "IN"
    resolution_method: str = "registry"  # "registry" | "search_fallback" | "direct_url"
    is_lookalike_risk: bool = False
    warning: Optional[str] = None
    is_ambiguous: bool = False
    ambiguity_candidates: list[dict[str, Any]] = field(default_factory=list)
    discovered_by_query: Optional[str] = None
    relevance_score: float = 1.0
    credibility_score: float = 1.0
    suitability_score: float = 1.0
    selection_rationale: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "targetName": self.target_name,
            "canonicalUrl": self.canonical_url,
            "primaryDomain": self.primary_domain,
            "allowedDomains": list(self.allowed_domains),
            "isVerifiedOfficial": self.is_verified_official,
            "confidence": self.confidence,
            "region": self.region,
            "resolutionMethod": self.resolution_method,
            "isLookalikeRisk": self.is_lookalike_risk,
            "warning": self.warning,
            "isAmbiguous": self.is_ambiguous,
            "ambiguityCandidates": list(self.ambiguity_candidates),
            "discoveredByQuery": self.discovered_by_query,
            "relevanceScore": self.relevance_score,
            "credibilityScore": self.credibility_score,
            "suitabilityScore": self.suitability_score,
            "selectionRationale": self.selection_rationale,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResolvedWebsite:
        return cls(
            target_name=data.get("targetName") or data.get("target_name") or "",
            canonical_url=data.get("canonicalUrl") or data.get("canonical_url") or "",
            primary_domain=data.get("primaryDomain") or data.get("primary_domain") or "",
            allowed_domains=list(data.get("allowedDomains") or data.get("allowed_domains") or []),
            is_verified_official=bool(data.get("isVerifiedOfficial", data.get("is_verified_official", False))),
            confidence=float(data.get("confidence", 1.0)),
            region=data.get("region", "IN"),
            resolution_method=data.get("resolutionMethod") or data.get("resolution_method") or "registry",
            is_lookalike_risk=bool(data.get("isLookalikeRisk", data.get("is_lookalike_risk", False))),
            warning=data.get("warning"),
            is_ambiguous=bool(data.get("isAmbiguous", data.get("is_ambiguous", False))),
            ambiguity_candidates=list(data.get("ambiguityCandidates") or data.get("ambiguity_candidates") or []),
            discovered_by_query=data.get("discoveredByQuery") or data.get("discovered_by_query"),
            relevance_score=float(data.get("relevanceScore", data.get("relevance_score", 1.0))),
            credibility_score=float(data.get("credibilityScore", data.get("credibility_score", 1.0))),
            suitability_score=float(data.get("suitabilityScore", data.get("suitability_score", 1.0))),
            selection_rationale=data.get("selectionRationale") or data.get("selection_rationale"),
        )


@dataclass
class DiscoveredSource:
    """A discovered candidate website from autonomous search decomposition."""
    url: str
    domain: str
    title: str
    snippet: str
    discovered_by_query: str
    relevance_score: float
    credibility_score: float
    freshness_score: float
    suitability_score: float
    final_rank_score: float
    is_primary: bool
    selection_rationale: str
    is_spam_or_ad: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "domain": self.domain,
            "title": self.title,
            "snippet": self.snippet,
            "discoveredByQuery": self.discovered_by_query,
            "relevanceScore": self.relevance_score,
            "credibilityScore": self.credibility_score,
            "freshnessScore": self.freshness_score,
            "suitabilityScore": self.suitability_score,
            "finalRankScore": self.final_rank_score,
            "isPrimary": self.is_primary,
            "selectionRationale": self.selection_rationale,
            "isSpamOrAd": self.is_spam_or_ad,
        }


# ---------------------------------------------------------------------------
# Known-Domain Directory (Task 8: DATA, not code)
# ---------------------------------------------------------------------------
# The registry of well-known public sites lives in known_domains.json next
# to this module. Adding a site is a data edit, never a code change, and
# never a behaviour playbook: the agent acts on ANY site via the
# accessibility tree (Task 7); this directory only maps a name the user
# spoke ("open X") to that site's canonical URL and verifies official
# domains. The loader keeps the same in-memory shape, so every caller is
# unchanged. Entries whose key starts with "_" are file metadata, not sites.

def _load_known_domains_registry() -> dict[str, dict[str, Any]]:
    import json
    from pathlib import Path

    path = Path(__file__).with_name("known_domains.json")
    with path.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


KNOWN_DOMAINS_REGISTRY: dict[str, dict[str, Any]] = _load_known_domains_registry()


def _shopping_retailers_for_region(region: str) -> list[dict[str, Any]]:
    """Registry retailers that serve ``region`` directly.

    A retailer qualifies only when its registry entry carries an
    explicit regional URL for that region — a GLOBAL fallback does
    not count, so an Indian shopping task never gets US retailers
    (bestbuy.com, walmart.com) and vice versa. Returns the entry
    plus its regional canonical URL and domain. The URL is the
    retailer's homepage, never a per-retailer search URL: the agent
    navigates there and searches on the site itself (Task 8 — no
    baked-in search URLs). This is the shopping analogue of the
    research hubs (nature.com / arxiv.org / science.org): for a
    price comparison the retailer IS the primary source, and
    discovery without it can never return a retailer.
    """
    reg = (region or "IN").upper()
    out: list[dict[str, Any]] = []
    for key, entry in KNOWN_DOMAINS_REGISTRY.items():
        if entry.get("category") != "retail":
            continue
        regional_urls = entry.get("regional_urls") or {}
        url = regional_urls.get(reg) or regional_urls.get(reg.lower())
        if not url:
            continue
        try:
            domain = urllib.parse.urlsplit(url).netloc.lower().replace("www.", "")
        except Exception:
            continue
        if not domain:
            continue
        out.append({"key": key, "entry": entry, "url": url, "domain": domain})
    return out


# ---------------------------------------------------------------------------
# Ambiguous Entity Registry & Disambiguation Engine
# ---------------------------------------------------------------------------

AMBIGUOUS_ENTITIES_REGISTRY: dict[str, dict[str, Any]] = {
    "mercury": {
        "term": "Mercury",
        "description": "Multi-entity ambiguity (banking, marine propulsion, insurance, planetary astronomy)",
        "qualifier_keywords": {"bank", "banking", "fintech", "startup", "marine", "boat", "motor", "propulsion", "insurance", "auto", "planet", "astronomy", "nasa", "element"},
        "candidates": [
            {
                "name": "Mercury (Banking & Fintech)",
                "url": "https://mercury.com",
                "domain": "mercury.com",
                "category": "fintech",
                "description": "Banking platform and financial services for startups and enterprises.",
            },
            {
                "name": "Mercury Marine",
                "url": "https://www.mercurymarine.com",
                "domain": "mercurymarine.com",
                "category": "marine",
                "description": "Outboard motors, commercial marine propulsion, and boats.",
            },
            {
                "name": "Mercury Insurance",
                "url": "https://www.mercurydirect.com",
                "domain": "mercurydirect.com",
                "category": "insurance",
                "description": "Auto, home, and business insurance coverage.",
            },
            {
                "name": "Planet Mercury (NASA Science)",
                "url": "https://science.nasa.gov/mercury",
                "domain": "science.nasa.gov",
                "category": "science",
                "description": "NASA solar system exploration and scientific planetary data.",
            },
        ],
    },
    "apollo": {
        "term": "Apollo",
        "description": "Multi-entity ambiguity (sales intelligence, GraphQL tooling, healthcare, automotive tyres)",
        "qualifier_keywords": {"sales", "lead", "b2b", "graphql", "developer", "schema", "hospital", "healthcare", "doctor", "tyre", "tire", "car"},
        "candidates": [
            {
                "name": "Apollo.io",
                "url": "https://www.apollo.io",
                "domain": "apollo.io",
                "category": "software",
                "description": "B2B lead generation, enrichment, and sales engagement platform.",
            },
            {
                "name": "Apollo GraphQL",
                "url": "https://www.apollographql.com",
                "domain": "apollographql.com",
                "category": "developer",
                "description": "GraphQL Federation, Apollo Client, and developer schema tooling.",
            },
            {
                "name": "Apollo Hospitals",
                "url": "https://www.apollohospitals.com",
                "domain": "apollohospitals.com",
                "category": "healthcare",
                "description": "Hospital network, clinical consultations, and medical healthcare.",
            },
            {
                "name": "Apollo Tyres",
                "url": "https://www.apollotyres.com",
                "domain": "apollotyres.com",
                "category": "automotive",
                "description": "Automotive and industrial tyre manufacturer.",
            },
        ],
    },
    "target": {
        "term": "Target",
        "description": "Ambiguity between US department store retail brand and generic noun",
        "qualifier_keywords": {"store", "shop", "retail", "buy", "deals", "order", "groceries", "supermarket"},
        "candidates": [
            {
                "name": "Target (Retail)",
                "url": "https://www.target.com",
                "domain": "target.com",
                "category": "retail",
                "description": "Major US department store chain and online retail portal.",
            },
        ],
    },
    "jordan": {
        "term": "Jordan",
        "description": "Multi-entity ambiguity (athletic apparel brand vs sovereign nation)",
        "qualifier_keywords": {"shoes", "sneakers", "nike", "air", "basketball", "country", "travel", "tourism", "amman", "visit"},
        "candidates": [
            {
                "name": "Jordan (Nike Brand)",
                "url": "https://www.nike.com/jordan",
                "domain": "nike.com",
                "category": "retail",
                "description": "Air Jordan athletic footwear and apparel by Nike.",
            },
            {
                "name": "Jordan Tourism Portal",
                "url": "https://visitjordan.com",
                "domain": "visitjordan.com",
                "category": "travel",
                "description": "Official tourism portal for the Hashemite Kingdom of Jordan.",
            },
        ],
    },
}


def is_ambiguous_entity(text: str) -> tuple[bool, Optional[str], list[dict[str, Any]]]:
    """Check if an entity name is ambiguous without disambiguating qualifiers."""
    if not text:
        return False, None, []
    clean = re.sub(r"^(?:open|visit|go\s+to|navigate\s+to|browse\s+to)\s+", "", text.strip(), flags=re.IGNORECASE).strip(". ").lower()
    words = set(re.findall(r"\b[a-z0-9]+\b", clean))

    for key, item in AMBIGUOUS_ENTITIES_REGISTRY.items():
        if key in words:
            # Check if user provided disambiguating qualifier keywords
            qualifiers = item.get("qualifier_keywords", set())
            has_qualifier = bool(words & qualifiers)
            # If bare or missing qualifier, treat as ambiguous
            if not has_qualifier and len(words) <= 3:
                return True, item["term"], list(item["candidates"])

    return False, None, []


def format_ambiguity_clarification(entity_name: str, candidates: list[dict[str, Any]]) -> str:
    """Format safe refusal/clarification response presenting ambiguous candidates."""
    lines = [
        f"### ⚠️ Ambiguous Destination: \"{entity_name}\"\n",
        f"The requested destination **\"{entity_name}\"** is ambiguous and could refer to several distinct official websites:\n",
    ]
    for idx, c in enumerate(candidates, 1):
        lines.append(f"{idx}. **{c['name']}** — `{c['url']}` ({c['description']})")
    lines.append(
        "\nPlease clarify your intended destination so PrivAgent can navigate safely without guessing."
    )
    return "\n".join(lines)

LOOKALIKE_PATTERNS = [
    # Digit-letter substitutions for major brands
    re.compile(r"\b(?:amaz[0o]n|g[0o]{2}gle|paypa[1l]|fl[1i]pkart|m[1i]cr[0o]s[0o]ft|y[0o]utube|faceb[0o]{2}k)\b", re.IGNORECASE),
    # Hyphenated fake brand combos (e.g. amazon-support.xyz, flipkart-deals.top)
    re.compile(r"^(?:amazon|flipkart|google|paypal|apple|microsoft|samsung)-[a-z0-9\-]+\.(?:xyz|top|biz|info|live|club|click|work)$", re.IGNORECASE),
]

SUSPICIOUS_TLDS = {".xyz", ".top", ".biz", ".work", ".click", ".link", ".live", ".gq", ".cf", ".tk", ".ml"}
CYRILLIC_OR_CONFUSABLE_RE = re.compile(r"[\u0400-\u04FF\u0370-\u03FF\u200B-\u200D\uFEFF]")

INTERNAL_HOSTNAMES = {
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "metadata.google.internal",
    "instance-data",
    "metadata",
}

DISALLOWED_SCHEMES = {"file", "data", "javascript", "vbscript", "about", "ftp", "gopher", "blob"}

_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")
_V4_MAPPED_PREFIX = ipaddress.ip_network("::ffff:0:0/96")


def _is_ip_safe(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> tuple[bool, Optional[str]]:
    """Verify an IP address is a safe public endpoint, properly inspecting NAT64 / IPv4-mapped IPv6."""
    if isinstance(ip, ipaddress.IPv6Address) and (ip in _NAT64_PREFIX or ip in _V4_MAPPED_PREFIX):
        embedded_v4 = ipaddress.ip_address(ip.packed[-4:])
        if (
            embedded_v4.is_loopback
            or embedded_v4.is_private
            or embedded_v4.is_link_local
            or embedded_v4.is_reserved
            or str(embedded_v4) == "169.254.169.254"
        ):
            return False, f"Embedded IPv4 '{embedded_v4}' in IPv6 translation prefix is private/loopback."
        return True, None

    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or str(ip) == "169.254.169.254"
    ):
        return False, f"Address '{ip}' is private, loopback, or cloud metadata."
    return True, None


def is_safe_public_url(url_or_domain: str, resolver=None) -> tuple[bool, Optional[str]]:
    """Determine whether a URL or domain target is a safe, publicly reachable destination.

    Rejects:
    - Schemes other than http/https (e.g. file://, data:, javascript:)
    - Embedded credentials in the authority (user:pass@host)
    - Private, loopback, link-local, multicast, or reserved IP addresses
    - Known cloud metadata and internal hostnames (AWS/GCP/Azure metadata)

    ``resolver`` is an injectable DNS lookup with the same call shape
    as ``socket.getaddrinfo`` (``resolver(host, port, proto=...)``).
    It defaults to the real resolver, so production SSRF/rebinding
    behaviour is unchanged. Tests pass a stub that returns a public
    IP, which makes discovery testable in sandboxes whose DNS returns
    non-public addresses (e.g. the RFC 2544 benchmarking range
    198.18.0.0/15) for every hostname — without weakening the check
    itself. It also makes the rebinding defense testable: a resolver
    returning 127.0.0.1 must still be rejected.
    """
    if not url_or_domain:
        return False, "Target URL or domain is empty."
    resolve = resolver or socket.getaddrinfo

    target = url_or_domain.strip()
    m_scheme = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):", target)
    if m_scheme:
        scheme = m_scheme.group(1).lower()
        if scheme not in ("http", "https"):
            return False, f"Disallowed scheme '{scheme}': only http and https are permitted."
        try:
            parts = urllib.parse.urlsplit(target)
        except Exception as e:
            return False, f"Malformed URL: {e}"
        if parts.username or parts.password:
            return False, "Embedded credentials in URL authority are prohibited."
        hostname = parts.hostname or ""
    elif target.startswith("//"):
        try:
            parts = urllib.parse.urlsplit("http:" + target)
            hostname = parts.hostname or ""
        except Exception as e:
            return False, f"Malformed URL: {e}"
    else:
        clean = target.split("/")[0].split("?")[0].split("#")[0]
        if "@" in clean:
            return False, "Embedded credentials in domain are prohibited."
        hostname = clean.split(":")[0]

    hostname = hostname.lower().strip()
    if not hostname:
        return False, "Empty hostname."

    if (
        hostname in INTERNAL_HOSTNAMES
        or hostname.endswith(".internal")
        or hostname.endswith(".local")
        or hostname.endswith(".localhost")
    ):
        return False, f"Access to private/internal host '{hostname}' is blocked."

    try:
        ip = ipaddress.ip_address(hostname)
        safe, err = _is_ip_safe(ip)
        if not safe:
            return False, err
    except ValueError:
        # Hostname is not an IP literal. Check for embedded private IPs in domain patterns (e.g. nip.io, sslip.io)
        embedded_ip = re.search(
            r"(?:127(?:\.\d+){3}|10(?:\.\d+){3}|192\.168(?:\.\d+){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d+){2}|169\.254(?:\.\d+){2})",
            hostname,
        )
        if embedded_ip:
            return False, f"Embedded private IP pattern '{embedded_ip.group(0)}' in hostname '{hostname}' is blocked."

        # DNS resolution and rebinding defense
        try:
            addr_info = resolve(hostname, None, proto=socket.IPPROTO_TCP)
            for _, _, _, _, sockaddr in addr_info:
                resolved_ip_str = sockaddr[0]
                resolved_ip = ipaddress.ip_address(resolved_ip_str)
                safe, err = _is_ip_safe(resolved_ip)
                if not safe:
                    return False, f"Hostname '{hostname}' resolves to private/internal IP '{resolved_ip_str}' ({err})."
        except (socket.gaierror, socket.herror, TimeoutError, OSError):
            # Target does not resolve during offline test or unreachable network
            pass

    return True, None


def validate_redirect_chain(initial_url: str, redirect_urls: list[str], resolver=None) -> tuple[bool, Optional[str]]:
    """Validate an entire HTTP redirect chain for SSRF, internal IPs, and scheme downgrades.

    Every single hop in the redirect chain must satisfy is_safe_public_url.
    Rejects any hop that attempts to redirect into loopback, private networks, cloud metadata,
    or dangerous protocols.
    """
    safe_init, init_err = is_safe_public_url(initial_url, resolver=resolver)
    if not safe_init:
        return False, f"Initial target rejected: {init_err}"

    if len(redirect_urls) > 10:
        return False, f"Redirect chain exceeds maximum permitted hops ({len(redirect_urls)} > 10)."

    for idx, hop_url in enumerate(redirect_urls, start=1):
        safe_hop, hop_err = is_safe_public_url(hop_url, resolver=resolver)
        if not safe_hop:
            return False, f"Redirect hop {idx} to '{hop_url}' rejected: {hop_err}"

    return True, None


def is_lookalike_or_suspicious(domain: str) -> tuple[bool, Optional[str]]:
    """Detect whether a domain presents a lookalike, typosquat, or spoofing risk."""
    if not domain:
        return False, None
    dom = domain.lower().replace("www.", "").strip()

    # 1. Punycode / Internationalized Domain Name homograph
    if "xn--" in dom:
        return True, "Punycode domain detected (potential homograph impersonation risk)."

    # 2. Confusable or mixed-script Unicode characters
    if CYRILLIC_OR_CONFUSABLE_RE.search(dom):
        return True, "Confusable or mixed-script Unicode character detected (potential homograph attack)."

    # 3. Known brand in subdomain of a suspicious or unrelated domain
    # e.g. "amazon.in.deals-online.xyz" or "amazon.com.fake-deals.xyz"
    parts = dom.split(".")
    if len(parts) >= 3:
        for brand in ("amazon", "flipkart", "google", "paypal", "apple", "samsung", "netflix"):
            if brand in parts[:-2]:
                real_root = ".".join(parts[-2:])
                is_genuine_root = any(real_root == kd["primary_domain"] or real_root in kd.get("allowed_domains", []) for kd in KNOWN_DOMAINS_REGISTRY.values())
                if not is_genuine_root:
                    return True, f"Deceptive brand subdomain '{brand}' hosted on third-party root '{real_root}'."

    # 4. Digit substitution in known brand names
    for pat in LOOKALIKE_PATTERNS:
        # Check if domain has lookalike pattern BUT is not the genuine domain
        m = pat.search(dom)
        if m:
            # Genuine domains allowed
            is_genuine = any(dom == kd["primary_domain"] or dom in kd.get("allowed_domains", []) for kd in KNOWN_DOMAINS_REGISTRY.values())
            if not is_genuine:
                return True, f"Suspicious lookalike pattern detected: '{m.group(0)}' in domain '{dom}'."

    return False, None


# ---------------------------------------------------------------------------
# Reusable Website Resolver Class
# ---------------------------------------------------------------------------

class WebsiteResolver:
    """Enterprise domain resolver, official verifier, and resolution cache."""

    def __init__(self, default_region: str = "IN", cache_ttl: float = 3600.0, resolver=None) -> None:
        self.default_region = default_region
        self.cache_ttl = cache_ttl
        # Injectable DNS lookup (same call shape as socket.getaddrinfo),
        # threaded into is_safe_public_url. None = real DNS (production
        # behaviour unchanged). Tests pass a stub returning public IPs.
        self._resolver = resolver
        self._memory_cache: dict[tuple[str, str], tuple[ResolvedWebsite, float]] = {}

    def resolve(
        self,
        query_or_entity: str,
        region: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> ResolvedWebsite:
        """Resolve a brand, query, or URL into a verified ResolvedWebsite.

        Priority:
        1. Context cache hit (within mission memory, respecting TTL).
        2. Direct canonical URL parsing (with SSRF and private-network checks).
        3. Authoritative known-domain registry with regional domain selection.
        4. Safe search-engine fallback URL for unknown entities.
        """
        raw = (query_or_entity or "").strip()
        reg = (region or self.default_region).lower()
        now = time.time()

        # 1. Context Cache Check with TTL validation
        ctx_cache = (context or {}).get("website_resolution_cache")
        cache_key = f"{raw.lower()}::{reg}"
        if ctx_cache and isinstance(ctx_cache, dict) and cache_key in ctx_cache:
            entry = ctx_cache[cache_key]
            if isinstance(entry, dict):
                cached_at = entry.get("_cached_at", 0)
                if cached_at == 0 or (now - cached_at < self.cache_ttl):
                    return ResolvedWebsite.from_dict(entry)
                else:
                    del ctx_cache[cache_key]
            elif hasattr(entry, "canonical_url"):
                return entry

        if (raw.lower(), reg) in self._memory_cache:
            cached_obj, cached_at = self._memory_cache[(raw.lower(), reg)]
            if now - cached_at < self.cache_ttl:
                return cached_obj
            else:
                del self._memory_cache[(raw.lower(), reg)]

        # 2. Direct URL handling
        if raw.startswith("http://") or raw.startswith("https://") or "://" in raw:
            is_safe, safe_err = is_safe_public_url(raw, resolver=self._resolver)
            if not is_safe:
                resolved = ResolvedWebsite(
                    target_name=raw,
                    canonical_url="",
                    primary_domain="",
                    allowed_domains=[],
                    is_verified_official=False,
                    confidence=0.0,
                    region=reg,
                    resolution_method="security_rejection",
                    is_lookalike_risk=True,
                    warning=f"SSRF / Unsafe destination rejected: {safe_err}",
                )
                return resolved

            try:
                parts = urllib.parse.urlsplit(raw)
                domain = parts.netloc.lower().replace("www.", "")
                is_risk, risk_warn = is_lookalike_or_suspicious(domain)

                # Check if it matches a known official registry
                matched_known = None
                for kd in KNOWN_DOMAINS_REGISTRY.values():
                    if domain == kd["primary_domain"] or domain in kd["allowed_domains"]:
                        matched_known = kd
                        break

                resolved = ResolvedWebsite(
                    target_name=matched_known["name"] if matched_known else domain,
                    canonical_url=raw,
                    primary_domain=domain,
                    allowed_domains=matched_known["allowed_domains"] if matched_known else [domain],
                    is_verified_official=bool(matched_known) and not is_risk,
                    confidence=0.99 if matched_known else 0.85,
                    region=reg,
                    resolution_method="direct_url",
                    is_lookalike_risk=is_risk,
                    warning=risk_warn,
                )
                self._record_cache(raw, reg, resolved, context)
                return resolved
            except Exception:
                pass

        # 3. Ambiguity Check for bare entity queries (e.g. "Mercury", "Apollo")
        is_ambig, ambig_term, ambig_cands = is_ambiguous_entity(raw)
        if is_ambig:
            resolved = ResolvedWebsite(
                target_name=ambig_term or raw,
                canonical_url="",
                primary_domain="",
                allowed_domains=[],
                is_verified_official=False,
                confidence=0.5,
                region=reg,
                resolution_method="ambiguity_refusal",
                is_ambiguous=True,
                ambiguity_candidates=ambig_cands,
                warning=f"Destination '{raw}' is ambiguous. Multiple authoritative entities exist across different domains.",
            )
            self._record_cache(raw, reg, resolved, context)
            return resolved

        # 4. Authoritative Registry Resolution
        raw_clean = raw.lower()
        for key, entry in KNOWN_DOMAINS_REGISTRY.items():
            matched = False
            for pat in entry["patterns"]:
                if re.search(pat, raw_clean):
                    matched = True
                    break

            if matched:
                # Regional domain selection
                reg_urls = entry.get("regional_urls", {})
                canonical_url = reg_urls.get(reg.upper()) or reg_urls.get(reg) or reg_urls.get("GLOBAL") or f"https://{entry['primary_domain']}"
                resolved = ResolvedWebsite(
                    target_name=entry["name"],
                    canonical_url=canonical_url,
                    primary_domain=entry["primary_domain"],
                    allowed_domains=list(entry["allowed_domains"]),
                    is_verified_official=True,
                    confidence=1.0,
                    region=reg,
                    resolution_method="registry",
                )
                self._record_cache(raw, reg, resolved, context)
                return resolved

        # 4. Search-based Fallback for Unknown Websites
        # If the target is an unknown website or query, produce a safe search navigation URL
        search_query = urllib.parse.quote_plus(raw)
        search_url = f"https://www.google.com/search?q={search_query}"
        resolved = ResolvedWebsite(
            target_name=raw,
            canonical_url=search_url,
            primary_domain="google.com",
            allowed_domains=["google.com", "google.co.in"],
            is_verified_official=False,
            confidence=0.70,
            region=reg,
            resolution_method="search_fallback",
        )
        self._record_cache(raw, reg, resolved, context)
        return resolved

    def extract_target_entity(self, instruction: str) -> tuple[Optional[str], Optional[str]]:
        """Determine whether the user specified an explicit website, and extract the objective.

        Returns
        -------
        (target_entity, objective)
        - Scenario A (Explicit site): ("Amazon", "find the cheapest laptop")
        - Scenario B (No site specified): (None, "find the cheapest laptop with an RTX 4060")
        """
        if not instruction:
            return None, None
        inst = instruction.strip()

        # 1. Direct site / specific page opening patterns: "open <site>", "visit <site>", "open the Wikipedia page about X"
        wiki_m = re.match(
            r"^(?:open|visit|go\s+to|navigate\s+to)\s+(?:the\s+)?([a-zA-Z0-9_\-\.]+)\s+(?:page|article|site|website)?\s*(?:about|on|for)\s+(.+?)(?:\.|\s+(?:and|,)\s+find|\s+(?:and|,)\s+search|\s+(?:and|,)\s+extract|$)",
            inst,
            re.IGNORECASE,
        )
        if wiki_m:
            cand = wiki_m.group(1).strip()
            topic_hint = wiki_m.group(2).strip()
            cand_lower = cand.lower()
            if cand_lower in KNOWN_DOMAINS_REGISTRY or any(re.search(kd["patterns"][0], cand_lower) for kd in KNOWN_DOMAINS_REGISTRY.values()):
                rest = inst[wiki_m.end():].strip()
                obj = f"{topic_hint}. {rest}".strip(". ") if rest else topic_hint
                return cand, obj or topic_hint

        direct_m = re.match(r"^(?:open|visit|go\s+to|navigate\s+to|browse\s+to)\s+([a-zA-Z0-9_\-\.\:\/]+(?:\s+[a-zA-Z0-9_\-\.]+)?)(?:\s+(?:and|,)\s+(.+))?$", inst, re.IGNORECASE)
        if direct_m:
            candidate_entity = direct_m.group(1).strip()
            objective = direct_m.group(2).strip() if direct_m.group(2) else None
            # Exclude generic non-entity commands like "open a tab", "open search"
            if candidate_entity.lower() not in {"a tab", "the tab", "new tab", "tab", "search", "browser"}:
                return candidate_entity, objective

        # 2. Prepositional target patterns: "... on Amazon", "... from Flipkart", "... using GitHub"
        prep_m = re.search(r"\b(?:on|from|via|using|at)\s+([a-zA-Z0-9_\-\.]+)(?:\s+(?:website|store|site|portal|page))?\b", inst, re.IGNORECASE)
        if prep_m:
            cand = prep_m.group(1).strip()
            cand_lower = cand.lower()
            # Verify if cand matches a known brand, TLD, or registry entry
            is_known = cand_lower in KNOWN_DOMAINS_REGISTRY or any(re.search(kd["patterns"][0], cand_lower) for kd in KNOWN_DOMAINS_REGISTRY.values()) or "." in cand
            if is_known:
                # Remove the "on <brand>" from objective
                obj = re.sub(r"\b(?:on|from|via|using|at)\s+" + re.escape(cand) + r"(?:\s+(?:website|store|site|portal|page))?\b", "", inst, flags=re.IGNORECASE).strip()
                return cand, obj

        # Scenario B: No website specified
        return None, inst

    def decompose_search_queries(
        self,
        objective: str,
        category: str = "general",
        context: Optional[dict[str, Any]] = None,
    ) -> list[str]:
        """Decompose a user request into 2-3 focused search queries for broad discovery."""
        if not objective:
            return []
        clean = objective.strip()

        # Resolve prior topic early
        prior_topic = None
        if context and isinstance(context, dict):
            prior_topic = context.get("research_topic") or (context.get("session_memory") or {}).get("research_topic")
            if not prior_topic:
                pkg_data = context.get("research_knowledge_package")
                if pkg_data:
                    top = pkg_data.get("topic") if isinstance(pkg_data, dict) else getattr(pkg_data, "topic", None)
                    prior_topic = top.get("title") if isinstance(top, dict) else getattr(top, "title", None)

        # If pure export/deliverable directive, fall back to prior topic or empty
        from .intent_router import is_export_deliverable_directive, is_prior_research_reference
        is_exp, _, _ = is_export_deliverable_directive(clean, context=context)
        if is_exp:
            clean = prior_topic or ""
            if not clean:
                return []

        # A bare "based on till now / so far" names no new subject — it
        # is a reference to the prior research. Search the prior topic,
        # never the literal reference phrase (which produced the garbage
        # query "based on till now overview technical principles").
        if is_prior_research_reference(clean):
            clean = prior_topic or ""
            if not clean:
                return []

        # An anaphoric follow-up ("yeah research about the topic
        # first", "research about the topic first") names no new
        # subject either — the topic extractor resolves it to the
        # prior topic. Without this the raw instruction became the
        # topic and decomposed into garbage queries ("yeah first
        # overview technical principles"). Only the anaphoric case
        # rewrites the query text: an explicit new subject keeps its
        # own wording (the extractor returns it, not the prior topic).
        if prior_topic:
            from .intent_router import extract_research_topic

            resolved = extract_research_topic(clean, context)
            if resolved == prior_topic:
                clean = prior_topic
            elif resolved and resolved.startswith(prior_topic + " - "):
                # Mixed reference + modifier ("based on till now, and
                # also compare with biology") resolves in extract to
                # "<prior> - <modifier>": use that as the query text.
                # Without this the literal reference phrase became
                # the query and the modifier ("biology") was dropped.
                # The " - " separator is flattened to a space so the
                # query reads as a topic, not a code artifact.
                clean = resolved.replace(" - ", " ", 1)

        # A compound instruction's delivery / messaging / shopping
        # clauses are not search terms (live 2026-11-19: "research
        # about black holes and send it to prateek in whatsapp"
        # became the query "google about black holes and send to
        # prateek"). Cut them before any cleaning runs; the remainder
        # is kept by the compound splitter, never silently dropped.
        from .intent_router import BARE_DELIVERABLE_PREAMBLE_RE, cut_delivery_clauses

        clean = cut_delivery_clauses(clean)
        # A bare leading deliverable preamble ("ppt report on topic X")
        # is not the topic — same strip as extract_research_topic.
        clean = BARE_DELIVERABLE_PREAMBLE_RE.sub("", clean, count=1).strip()

        # Multi-sentence / structured decomposition:
        # Separate main topic from subtopic and deliverable clauses
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", clean) if s.strip()]
        topic_sentence = sentences[0] if sentences else clean

        # Extract explicit subtopics from subsequent clauses or including clauses
        explicit_subtopics = []
        for s in sentences[1:]:
            # Skip pure deliverable instructions ("Generate a PDF", "Create DOCX", "Include formulas and images")
            if re.search(r"\b(generate|create|prepare|export|write|make|output|produce)\s+.*?\b(pdf|docx|pptx|xlsx|csv|html|markdown|report|spreadsheet)\b", s, re.IGNORECASE):
                continue
            if re.search(r"^(?:include|with)\s+.*?\b(formulas?|equations?|images?|diagrams?|tables?|figures?|charts?|references?|citations?)\b", s, re.IGNORECASE):
                continue
            sub_m = re.search(r"^(?:explain|find|compare|cover|examine|details on|breakdown of|focusing on|specifically)\s+(?:their\s+)?(.+)$", s, re.IGNORECASE)
            raw_sub = sub_m.group(1).strip() if sub_m else s
            raw_sub = re.sub(r"\s+(?:using\s+.*|with\s+.*|and\s+create\s+.*|and\s+generate\s+.*)$", "", raw_sub, flags=re.IGNORECASE).strip()
            parts = [re.sub(r"\b(?:formulas|equations|diagrams|figures|charts|tables|references|citations|examples|source\s+links|options)\b", "", p, flags=re.IGNORECASE).strip() for p in re.split(r"[,;]|\band\b", raw_sub)]
            for p in parts:
                p_clean = p.strip(". ")
                if len(p_clean) >= 3 and p_clean.lower() not in ("their", "its", "each", "briefly", "retailer sources"):
                    explicit_subtopics.append(p_clean)

        # Also extract inline "including ...", "such as ..."
        if not explicit_subtopics:
            sub_m = re.search(
                r"\b(?:including|such as|covering|specifically|focusing on|details on|breakdown of|explain|examine|cover)\s+(.+?)(?:\.|\band\s+generate|\band\s+compare|\bcompare|\bgenerate|\bcreate|\bproduce|\bexport|\binclude|$)",
                objective,
                re.IGNORECASE,
            )
            if sub_m:
                raw_sub = sub_m.group(1).strip()
                parts = [re.sub(r"\b(?:formulas|equations|diagrams|figures|charts|tables|references|citations|examples)\b", "", p, flags=re.IGNORECASE).strip() for p in re.split(r"[,;]|\band\b", raw_sub)]
                explicit_subtopics = [p.strip(". ") for p in parts if len(p.strip(". ")) >= 3 and p.lower() not in ("their", "its", "each", "briefly", "retailer sources")]

        # Use topic_sentence as basis for clean
        clean = topic_sentence

        # Prefer an explicitly quoted topic, then strip submission
        # scaffolding and UI glyphs — BEFORE the 8-word cap below.
        # The cap is applied to the cleaned topic; if the scaffolding
        # ("i have to submit a research paper on topic") is still in
        # place it eats the cap and the real topic is truncated away.
        # Live case: "so i have to submit a research paper on topic :
        # \"Quantum Mechanics similarities with life?\"" -> topic
        # "Quantum Mechanics similarities with life".
        quoted = _extract_quoted_topic(clean)
        if quoted:
            clean = quoted
        else:
            clean = _strip_submission_preamble(clean)
        clean = _strip_ui_glyphs(clean)

        # 1. Strip conversational openers and slang
        clean = re.sub(
            r"^(?:(?:hey|hi|yo|hello|ok|okay|bro|dude|man|please|pls|buddy|pal|friend|so|well)\s+)+",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip()
        clean = re.sub(
            r"^(?:could|can|would)\s+(?:you|u)\s+(?:please\s+|pls\s+)?(?:give|make|create|prepare|generate|provide|write|add|show|include|expand|elaborate|do|research|investigate)?\s*(?:me\s+)?",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip()
        clean = re.sub(
            r"^(?:please\s+|pls\s+)?(?:give|make|create|prepare|generate|provide|write|add|show|include|expand|elaborate|do)\s+(?:me\s+)?",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip()
        clean = re.sub(
            r"^(?:make\s+it|turn\s+it\s+into|convert\s+it\s+to|change\s+(?:it\s+)?to|switch\s+(?:it\s+)?to)\s+",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip()
        clean = re.sub(r"^(?:now|then|next|also)\s+", "", clean, flags=re.IGNORECASE).strip()

        # 2. Clean leading command verbs and articles
        clean = re.sub(
            r"^(?:find|search(?:\s+for)?|look\s+up|research|compare(?:\s+the)?|get|show|check|discover|explain|describe|analyze|summarize|open(?:\s+the)?(?:\s+(?:google|bing|duckduckgo|wikipedia\s+page\s+about))?)\s*,?\s*",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip()
        clean = re.sub(r"^(?:the|a|an)\s+", "", clean, flags=re.IGNORECASE).strip()

        # 3. Strip deliverable artifacts & slide/page counts (numbers or words)
        clean = re.sub(
            r"\b(?:a\s+|an\s+)?(?:more\s+)?(?:detailed\s+)?(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|12)[- ](?:page|slide)s?\s+)?(?:research\s+)?(?:report|presentation|slides?|slide\s*deck|excel(?:\s+comparison)?|spreadsheet|workbook|document|whitepaper|dossier|pdf|docx|pptx|ppt|powerpoint|xlsx|csv|html|comparison)(?:\s+(?:of|with)\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|12)\s+(?:slides?|pages?))?\b",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip()
        clean = re.sub(r"\b(?:of\s+)?(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|12)\s*[- ]?(?:slides?|pages?)\b", "", clean, flags=re.IGNORECASE).strip()
        clean = re.sub(
            r"^(?:add|include|expand\s+(?:on\s+)?|elaborate\s+(?:on\s+)?|give|show|provide)\s+(?:more|further|additional|better|technical)\s*",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip()

        # 4. Strip inclusion clauses for visuals / formulas
        clean = re.sub(
            r"\b(?:it\s+should\s+include|including|with)\b(?:(?!\b(?:and\s+also|and\s+research|also\s+research|research)\b).)*\b(?:formulas|equations|diagrams|figures|charts|tables|citations|references)(?:\s+(?:too|also|as\s+well))?",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip()
        # Trailing style clauses ("... use images and make it nice
        # looking ppt") are presentation wishes, not the topic. The
        # (.+?) guard keeps a topic that legitimately STARTS with
        # "use images" intact.
        clean = re.sub(
            r"^(.+?)\s+use\s+(?:relevant\s+)?(?:images?|diagrams?|figures?|charts?)\b.*$",
            r"\1",
            clean,
            flags=re.IGNORECASE,
        ).strip()
        clean = re.sub(
            r"^(.+?)\s+make\s+it\s+(?:nice(?:[-\s]looking)?|better|good|great|beautiful|presentable)\b.*$",
            r"\1",
            clean,
            flags=re.IGNORECASE,
        ).strip()

        # 5. Strip trailing deliverable clauses: e.g. "... and prepare a detailed report"
        clean = re.sub(
            r"\s+(?:and\s+)?(?:prepare|create|generate|write|make|build|export|output)\s+(?:a|an)?\s+(?:detailed\s+)?(?:research\s+)?(?:report|presentation|slides?|slide\s*deck|excel(?:\s+comparison)?|spreadsheet|workbook|document|whitepaper|dossier|pdf|docx|pptx|xlsx|csv|html).*$",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip()

        # 6. Strip trailing format or illustration clauses: e.g. "... with relevant diagrams and references", "... in PDF"
        clean = re.sub(
            r"\s+(?:with\s+(?:relevant\s+)?(?:diagrams|references|equations|formulas|examples|figures|citations|charts|tables).*)$",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip()
        clean = re.sub(
            r"\s+(?:with\s+diagrams\s+)?(?:in|as)\s+(?:a\s+)?(?:pdf|word|docx|excel|spreadsheet|presentation|pptx|markdown|html).*$",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip()

        # 7. Strip connectors, research verbs, comparison fluff, trailing slang, and punctuation
        clean = re.sub(
            r"\b(?:and\s+)?(?:also\s+)?(?:research(?:ing)?|conduct\s+research\s+on|investigate|study|look\s+into|search\s+for)\s+(?:different\s+)?",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip()
        clean = re.sub(r"\s+(?:and\s+)?compare(?:\s+(?:them|findings|prices|specs|options|features|results))?(?:\s+(?:from|across)\s+.*)?$", "", clean, flags=re.IGNORECASE).strip()
        clean = re.sub(r"\s+(?:bro|man|dude|buddy|pal|friend|pls|please|thanks|thank\s+you|for\s+me|as\s+well|too)$", "", clean, flags=re.IGNORECASE).strip()
        clean = re.sub(r"^(?:and|also|too|with|in|for|about)\s+", "", clean, flags=re.IGNORECASE).strip()
        clean = re.sub(r"\s+(?:too|also|as\s+well|for\s+it|about\s+it|instead)$", "", clean, flags=re.IGNORECASE).strip()
        clean = re.sub(r"\s+(?:in|for)\s+(?:India|US|UK)$", "", clean, flags=re.IGNORECASE).strip()
        clean = re.sub(r"[,;.!?]+$", "", clean).strip()
        clean = re.sub(r"[,;]+", " ", clean).strip()
        clean = re.sub(r"\s+", " ", clean).strip()
        clean = re.sub(r"\s+(?:bro|man|dude|buddy|pal|friend|pls|please|thanks|thank\s+you|for\s+me|as\s+well|too)$", "", clean, flags=re.IGNORECASE).strip()

        # 7b. Strip domain names and topic-meta filler: a domain is
        # never a search keyword and "this topic" refers to the
        # request, not its subject (see _strip_query_filler).
        clean = _strip_query_filler(clean)

        # 8. Check context for prior research topic if clean is empty or generic
        prior_topic = None
        if context and isinstance(context, dict):
            prior_topic = context.get("research_topic") or (context.get("session_memory") or {}).get("research_topic")
            if not prior_topic:
                pkg_data = context.get("research_knowledge_package")
                if pkg_data:
                    top = pkg_data.get("topic") if isinstance(pkg_data, dict) else getattr(pkg_data, "topic", None)
                    prior_topic = top.get("title") if isinstance(top, dict) else getattr(top, "title", None)

        generic_terms = {
            "platforms", "different platforms", "frameworks", "implementations",
            "details", "more details", "technical details", "more technical details",
            "overview", "comparison", "it", "this", "that", "better", "it better", "elaborate",
            "bro", "man", "dude", "generate", "create", "export", "save", "download",
            "pdf", "docx", "pptx", "xlsx", "report", "file", "presentation", "slide", "slides",
        }
        words = [w for w in clean.split() if w.lower() not in generic_terms]
        if not words or clean.lower() in generic_terms:
            if prior_topic:
                if clean and clean.lower() in ("platforms", "different platforms", "frameworks", "implementations"):
                    clean = f"{prior_topic} {clean}"
                else:
                    clean = prior_topic
            else:
                clean = ""
        elif len(words) != len(clean.split()):
            # Deictic words ("this", "that", "it") survived mid-topic:
            # the filter above only tested emptiness and never rebuilt
            # clean, so they leaked into the query verbatim. Only
            # deictics are removed here — content words that happen
            # to be in generic_terms ("frameworks", "details",
            # "overview") stay, or "node.js frameworks" would lose
            # its subject.
            deictic = {"it", "this", "that", "its", "their", "each", "bro", "man", "dude"}
            rebuilt = [w for w in clean.split() if w.lower() not in deictic]
            if rebuilt:
                clean = " ".join(rebuilt)

        if not clean:
            return []

        # Cap length to focused keywords (max 8 words)
        words = clean.split()
        if len(words) > 8:
            clean = " ".join(words[:8])
            # The cap itself can strand a dangling token ("... and").
            clean = _strip_query_filler(clean)

        queries: list[str] = []

        if category == "shopping":
            is_india = "india" in objective.lower() or (context or {}).get("user_region") == "IN"
            suffix = " India" if is_india else ""
            if explicit_subtopics:
                queries.append(f"{clean} price{suffix}")
                queries.append(f"{clean} {' '.join(explicit_subtopics[:3])}")
                queries.append(f"best {clean} buy online deals{suffix}")
            else:
                queries.append(f"{clean} price{suffix}")
                queries.append(f"{clean} buy online best deals{suffix}")
                if "best" in clean.lower() or "cheapest" in clean.lower():
                    queries.append(f"{clean} reviews specs comparison")
                else:
                    queries.append(f"best {clean} reviews specs comparison")
        elif category in ("software_architecture", "cloud_services"):
            if explicit_subtopics:
                queries.append(f"{clean} comparison overview")
                queries.append(f"{clean} {' '.join(explicit_subtopics[:3])}")
                queries.append(f"{clean} {' '.join(explicit_subtopics[3:6])}" if len(explicit_subtopics) > 3 else f"{clean} trade-offs architecture")
            else:
                queries.append(f"{clean} comparison architecture")
                queries.append(f"{clean} scalability performance trade-offs")
                queries.append(f"{clean} technical documentation guide")
        elif category in ("agriculture_science", "research"):
            if explicit_subtopics:
                queries.append(f"{clean} {' '.join(explicit_subtopics[:2])}")
                queries.append(f"{clean} {' '.join(explicit_subtopics[2:4])}" if len(explicit_subtopics) > 2 else f"{clean} technical overview")
                queries.append(f"{clean} {' '.join(explicit_subtopics[4:6])}" if len(explicit_subtopics) > 4 else f"{clean} research findings")
            elif "transformer" in clean.lower() or "attention" in clean.lower():
                queries.append(f"{clean} architecture technical breakdown")
                queries.append(f"{clean} self-attention and multi-head attention")
                queries.append(f"{clean} positional encoding formula")
            elif any(w in clean.lower() for w in ("platform", "framework", "implementation", "library")):
                queries.append(f"{clean} frameworks libraries python")
                queries.append(f"{clean} models benchmarks comparison")
                queries.append(f"{clean} architecture technical documentation")
            else:
                # The topic itself may already carry the suffix words
                # ("Quantum overview technical principles"): appending
                # them again duplicates the tail. Skip a suffix the
                # topic already ends with.
                def _with_suffix(base: str, suffix: str) -> str:
                    return base if base.lower().endswith(suffix.lower()) else f"{base} {suffix}"

                queries.append(_with_suffix(clean, "overview technical principles"))
                queries.append(_with_suffix(clean, "key components methodologies"))
                queries.append(_with_suffix(clean, "authoritative research overview"))
        elif category == "travel":
            queries.append(f"{clean} booking prices")
            queries.append(f"{clean} schedule rates comparison")
        else:
            queries.append(clean)
            queries.append(f"{clean} official overview")
            queries.append(f"{clean} guide specifications")

        # Deduplicate while preserving order
        seen = set()
        out = []
        for q in queries:
            # Defence in depth: a subtopic clause can carry the same
            # filler ("... on nature.com") even when the main topic
            # was cleaned above.
            q = _strip_query_filler(q)
            q_norm = q.lower().strip()
            if q_norm and q_norm not in seen:
                seen.add(q_norm)
                out.append(q.strip())
        return out[:3]

    def discover_candidate_sources(
        self,
        queries: list[str],
        category: str = "general",
        region: str = "IN",
        context: Optional[dict[str, Any]] = None,
        max_sources: int = 5,
        provenance_log: Optional[list[dict[str, Any]]] = None,
        allowed_domains: Optional[list[str]] = None,
    ) -> list[DiscoveredSource]:
        """Discover, filter, score, and rank candidate sources for autonomous website discovery.

        ``allowed_domains`` (None = no allow-list configured, keep every
        hub) restricts the hubs to domains the operator actually allows
        navigation to. A hub outside the allow-list is never offered:
        offering it builds a deterministic plan that the allow-list
        gate then rejects with PLAN_INVALID, so the task fails before
        the LLM planner ever runs. Live case: research picked
        nature.com (highest suitability) although nature.com was not
        in PRIVAGENT_ALLOWED_DOMAINS, and the research task died on
        the first step.
        """
        candidates: list[DiscoveredSource] = []
        visited_domains = set((context or {}).get("visited_domains", []))
        reg_clean = (region or self.default_region).upper()

        # Build candidate pools by category.
        #
        # Task 8: hubs are GENERIC search endpoints, never per-retailer
        # search URLs. Shopping used to build one search URL per retailer;
        # that baked a site list into discovery. A shopping query is now
        # answered by general web search, and the agent opens whichever
        # retailer the results point at — or the one the user named, via
        # resolve().
        primary_hubs: list[dict[str, Any]] = []
        if category == "shopping":
            primary_hubs = [
                {
                    "domain": "google.com",
                    "url_template": "https://www.google.com/search?q={query}",
                    "name": "Web Search (Google)",
                    "credibility": 0.95,
                    "suitability": 0.98,
                    "is_primary": True,
                },
                {
                    "domain": "duckduckgo.com",
                    "url_template": "https://duckduckgo.com/?q={query}",
                    "name": "Web Search (DuckDuckGo)",
                    "credibility": 0.90,
                    "suitability": 0.90,
                    "is_primary": False,
                },
                {
                    "domain": "en.wikipedia.org",
                    "url_template": "https://en.wikipedia.org/wiki/Special:Search?search={query}",
                    "name": "Wikipedia",
                    "credibility": 0.92,
                    "suitability": 0.85,
                    "is_primary": False,
                },
            ]
            # Retailers are candidates too, from the registry: for a
            # price comparison the retailer is the primary source, the
            # search engine only a starting point. Their URL is the
            # regional homepage (never a per-retailer search URL —
            # Task 8), and the region filter keeps Indian tasks on
            # Indian retailers and US tasks on US ones.
            for ret in _shopping_retailers_for_region(reg_clean):
                primary_hubs.append({
                    "domain": ret["domain"],
                    "url_template": ret["url"],
                    "name": ret["entry"].get("name") or ret["key"],
                    "credibility": 0.93,
                    "suitability": 0.97,
                    "is_primary": True,
                })
        elif category == "software_architecture":
            primary_hubs = [
                {
                    "domain": "google.com",
                    "url_template": "https://www.google.com/search?q={query}",
                    "name": "Google Technical Documentation",
                    "credibility": 0.95,
                    "suitability": 0.98,
                    "is_primary": True,
                },
                {
                    "domain": "en.wikipedia.org",
                    "url_template": "https://en.wikipedia.org/wiki/Special:Search?search={query}",
                    "name": "Wikipedia",
                    "credibility": 0.92,
                    "suitability": 0.95,
                    "is_primary": True,
                },
                {
                    "domain": "arxiv.org",
                    "url_template": "https://arxiv.org/search/?query={query}&searchtype=all",
                    "name": "arXiv",
                    "credibility": 0.90,
                    "suitability": 0.88,
                    "is_primary": False,
                },
            ]
        elif category == "cloud_services":
            primary_hubs = [
                {
                    "domain": "google.com",
                    "url_template": "https://www.google.com/search?q={query}",
                    "name": "Google Cloud & Enterprise Documentation Search",
                    "credibility": 0.95,
                    "suitability": 0.98,
                    "is_primary": True,
                },
                {
                    "domain": "en.wikipedia.org",
                    "url_template": "https://en.wikipedia.org/wiki/Special:Search?search={query}",
                    "name": "Wikipedia",
                    "credibility": 0.90,
                    "suitability": 0.92,
                    "is_primary": False,
                },
            ]
        elif category == "agriculture_science":
            primary_hubs = [
                {
                    "domain": "google.com",
                    "url_template": "https://www.google.com/search?q={query}",
                    "name": "Google Agriculture Science & Research",
                    "credibility": 0.95,
                    "suitability": 0.98,
                    "is_primary": True,
                },
                {
                    "domain": "en.wikipedia.org",
                    "url_template": "https://en.wikipedia.org/wiki/Special:Search?search={query}",
                    "name": "Wikipedia",
                    "credibility": 0.90,
                    "suitability": 0.95,
                    "is_primary": True,
                },
                {
                    "domain": "science.org",
                    "url_template": "https://www.science.org/action/doSearch?AllField={query}",
                    "name": "Science / AAAS",
                    "credibility": 0.98,
                    "suitability": 0.92,
                    "is_primary": True,
                },
                {
                    "domain": "nature.com",
                    "url_template": "https://www.nature.com/search?q={query}",
                    "name": "Nature Publishing",
                    "credibility": 0.99,
                    "suitability": 0.90,
                    "is_primary": False,
                },
            ]
        elif category == "research":
            # Check if query is software architecture or cloud related
            q_lower = (queries[0] if queries else "").lower()
            if any(w in q_lower for w in ("architecture", "microservices", "monolithic", "serverless", "distributed", "software", "cloud", "aws", "azure")):
                primary_hubs = [
                    {
                        "domain": "google.com",
                        "url_template": "https://www.google.com/search?q={query}",
                        "name": "Google Technical Documentation",
                        "credibility": 0.95,
                        "suitability": 0.98,
                        "is_primary": True,
                    },
                    {
                        "domain": "en.wikipedia.org",
                        "url_template": "https://en.wikipedia.org/wiki/Special:Search?search={query}",
                        "name": "Wikipedia",
                        "credibility": 0.92,
                        "suitability": 0.95,
                        "is_primary": True,
                    },
                    {
                        "domain": "arxiv.org",
                        "url_template": "https://arxiv.org/search/?query={query}&searchtype=all",
                        "name": "arXiv",
                        "credibility": 0.90,
                        "suitability": 0.88,
                        "is_primary": False,
                    },
                ]
            else:
                primary_hubs = [
                    {
                        "domain": "nature.com",
                        "url_template": "https://www.nature.com/search?q={query}",
                        "name": "Nature Publishing",
                        "credibility": 0.99,
                        "suitability": 0.98,
                        "is_primary": True,
                    },
                    {
                        "domain": "arxiv.org",
                        "url_template": "https://arxiv.org/search/?query={query}&searchtype=all",
                        "name": "arXiv",
                        "credibility": 0.99,
                        "suitability": 0.97,
                        "is_primary": True,
                    },
                    {
                        "domain": "science.org",
                        "url_template": "https://www.science.org/action/doSearch?AllField={query}",
                        "name": "Science / AAAS",
                        "credibility": 0.98,
                        "suitability": 0.95,
                        "is_primary": True,
                    },
                    {
                        "domain": "en.wikipedia.org",
                        "url_template": "https://en.wikipedia.org/wiki/Special:Search?search={query}",
                        "name": "Wikipedia",
                        "credibility": 0.85,
                        "suitability": 0.88,
                        "is_primary": False,
                    },
                ]
        elif category == "travel":
            # Task 8: no per-site travel URL template. Travel is answered
            # by general web search; the agent opens the travel site the
            # results (or the user) point at and works it via the AX tree.
            primary_hubs = [
                {
                    "domain": "google.com",
                    "url_template": "https://www.google.com/search?q={query}",
                    "name": "Web Search — travel",
                    "credibility": 0.95,
                    "suitability": 0.96,
                    "is_primary": True,
                },
                {
                    "domain": "duckduckgo.com",
                    "url_template": "https://duckduckgo.com/?q={query}",
                    "name": "Web Search (DuckDuckGo)",
                    "credibility": 0.90,
                    "suitability": 0.90,
                    "is_primary": False,
                },
            ]
        else:
            primary_hubs = [
                {
                    "domain": "google.com",
                    "url_template": "https://www.google.com/search?q={query}",
                    "name": "Google Search",
                    "credibility": 0.90,
                    "suitability": 0.85,
                    "is_primary": False,
                },
                {
                    "domain": "en.wikipedia.org",
                    "url_template": "https://en.wikipedia.org/wiki/Special:Search?search={query}",
                    "name": "Wikipedia",
                    "credibility": 0.85,
                    "suitability": 0.85,
                    "is_primary": False,
                },
            ]

        # Allow-list filter: only offer hubs the operator allows
        # navigation to. None/empty = no allow-list configured, keep
        # every hub (backwards compatible). A filtered-out hub is
        # logged so the provenance trail shows why it was skipped.
        if allowed_domains:
            from .allowlist import host_matches_allowlist

            kept: list[dict[str, Any]] = []
            for hub in primary_hubs:
                if host_matches_allowlist(hub["domain"], allowed_domains):
                    kept.append(hub)
                elif provenance_log is not None:
                    provenance_log.append({
                        "domain": hub["domain"],
                        "url": hub["url_template"],
                        "status": "rejected",
                        "reason": "Rejected: domain is not on the domain allow-list",
                    })
            primary_hubs = kept

        # Generate candidates from queries across candidate hubs
        seen_domains: set[str] = set()
        # Gate: a query that is only filler ("whole on this topic")
        # must never be templated into a hub search URL. Decomposition
        # normally strips it; this is the backstop for callers that
        # pass raw instruction words.
        primary_query = _strip_query_filler(queries[0]) if queries else ""
        if not primary_query:
            primary_query = "research"

        for hub in primary_hubs:
            dom = hub["domain"]
            q_encoded = urllib.parse.quote_plus(primary_query)
            target_url = hub["url_template"].format(query=q_encoded)

            # SSRF and private IP check
            is_safe, safe_err = is_safe_public_url(dom, resolver=self._resolver)
            if not is_safe:
                if provenance_log is not None:
                    provenance_log.append({
                        "domain": dom,
                        "url": target_url,
                        "status": "rejected",
                        "reason": f"Rejected: {safe_err}",
                    })
                continue

            if dom in seen_domains or dom in visited_domains:
                if provenance_log is not None:
                    provenance_log.append({
                        "domain": dom,
                        "url": target_url,
                        "status": "rejected",
                        "reason": "Rejected: duplicate domain or already visited",
                    })
                continue

            # Check lookalike and deceptive domain filter
            is_risk, risk_warn = is_lookalike_or_suspicious(dom)
            if is_risk:
                if provenance_log is not None:
                    provenance_log.append({
                        "domain": dom,
                        "url": target_url,
                        "status": "rejected",
                        "reason": f"Rejected: {risk_warn}",
                    })
                continue

            relevance = 0.92 if any(w in primary_query.lower() for w in ("laptop", "battery", "quantum", "flight")) else 0.85
            credibility = hub["credibility"]
            freshness = 0.90 if any(y in primary_query for y in ("2024", "2025", "2026", "latest")) else 0.80
            suitability = hub["suitability"]

            # Multi-factor rank: 35% relevance, 35% credibility, 15% freshness, 15% suitability
            rank = (0.35 * relevance) + (0.35 * credibility) + (0.15 * freshness) + (0.15 * suitability)

            rationale = (
                f"Selected authoritative {category} hub '{hub['name']}' ({dom}) "
                f"with high credibility ({credibility:.2f}) and domain alignment."
            )

            discovered = DiscoveredSource(
                url=target_url,
                domain=dom,
                title=f"{hub['name']} — {primary_query}",
                snippet=f"Search results and catalog listings for '{primary_query}' on {dom}.",
                discovered_by_query=primary_query,
                relevance_score=relevance,
                credibility_score=credibility,
                freshness_score=freshness,
                suitability_score=suitability,
                final_rank_score=round(rank, 4),
                is_primary=hub.get("is_primary", False),
                selection_rationale=rationale,
                is_spam_or_ad=False,
            )
            candidates.append(discovered)
            seen_domains.add(dom)

            if provenance_log is not None:
                provenance_log.append({
                    "domain": dom,
                    "url": target_url,
                    "status": "candidate",
                    "reason": rationale,
                    "scores": {
                        "relevance": relevance,
                        "credibility": credibility,
                        "freshness": freshness,
                        "suitability": suitability,
                        "final_rank": round(rank, 4),
                    },
                })

        # Sort candidates by final_rank_score descending
        candidates.sort(key=lambda c: c.final_rank_score, reverse=True)
        return candidates[:max_sources]

    def discover_websites_for_task(
        self,
        instruction: str,
        region: str = "IN",
        context: Optional[dict[str, Any]] = None,
        allowed_domains: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Autonomous Website Discovery coordinator for PrivAgent missions.

        ``allowed_domains`` (None = no allow-list configured) is
        threaded into candidate discovery so only navigable hubs are
        offered. The result carries ``discovery_blocked_reason`` when
        no hub survives: the caller must surface it instead of
        building a plan that the allow-list gate will reject.

        Handles:
        - Scenario A: User specifies a website -> resolves & verifies official domain.
        - Scenario B: User does NOT specify a website -> decomposes queries, discovers
          candidate sources, ranks them, and selects independent destinations.
        """
        entity, objective = self.extract_target_entity(instruction)
        provenance_log: list[dict[str, Any]] = []

        # SCENARIO A: User specified an explicit website
        if entity:
            resolved = self.resolve(entity, region=region, context=context)
            if resolved.is_ambiguous:
                provenance_log.append({
                    "target": entity,
                    "status": "ambiguous",
                    "reason": f"Destination '{entity}' is ambiguous with {len(resolved.ambiguity_candidates)} candidates.",
                })
            elif not resolved.is_verified_official and resolved.is_lookalike_risk:
                provenance_log.append({
                    "target": entity,
                    "status": "rejected",
                    "reason": f"Security risk / lookalike warning: {resolved.warning}",
                })
            else:
                provenance_log.append({
                    "target": entity,
                    "domain": resolved.primary_domain,
                    "url": resolved.canonical_url,
                    "status": "selected",
                    "reason": f"Explicitly requested destination '{entity}' resolved to official domain {resolved.primary_domain}.",
                })

            return {
                "scenario": "A",
                "specified_website": entity,
                "objective": objective or instruction,
                "is_ambiguous": resolved.is_ambiguous,
                "ambiguity_candidates": resolved.ambiguity_candidates,
                "resolved_website": resolved.to_dict(),
                "search_queries": [],
                "candidate_sources": [],
                "selected_sources": [
                    {
                        "domain": resolved.primary_domain,
                        "url": resolved.canonical_url,
                        "title": resolved.target_name,
                        "is_verified_official": resolved.is_verified_official,
                        "selection_rationale": f"Explicitly requested destination '{entity}' resolved to official domain {resolved.primary_domain}.",
                    }
                ] if not resolved.is_ambiguous else [],
                "primary_navigation_url": resolved.canonical_url if not resolved.is_ambiguous else None,
                "source_provenance": provenance_log,
            }

        # SCENARIO B: User did NOT specify a website -> Autonomous Discovery
        clean_inst = instruction.lower()
        if any(w in clean_inst for w in ("laptop", "iphone", "phone", "price", "buy", "shop", "cheapest", "under ₹", "under rs", "under $", "smartphone")):
            category = "shopping"
        elif any(w in clean_inst for w in ("flight", "hotel", "travel", "ticket", "train")):
            category = "travel"
        elif any(w in clean_inst for w in ("microservices", "monolithic", "serverless", "distributed systems", "software architecture")):
            category = "software_architecture"
        elif any(w in clean_inst for w in ("aws", "azure", "google cloud", "oracle cloud", "cloud provider", "cloud services")):
            category = "cloud_services"
        elif any(w in clean_inst for w in ("agriculture", "farming", "crop", "irrigation", "soil")):
            category = "agriculture_science"
        elif any(w in clean_inst for w in ("research", "study", "scientific", "battery", "architecture", "developments", "quantum", "formula", "academic")):
            category = "research"
        else:
            category = "general"

        queries = self.decompose_search_queries(objective or instruction, category=category, context=context)
        candidates = self.discover_candidate_sources(
            queries,
            category=category,
            region=region,
            context=context,
            provenance_log=provenance_log,
            allowed_domains=allowed_domains,
        )

        # Select top sources (multi-source for comparison/corroboration)
        selected = candidates[:3] if len(candidates) >= 3 else candidates
        selected_domains = {s.domain for s in selected}

        # Update provenance statuses
        for entry in provenance_log:
            if entry.get("status") == "candidate":
                if entry.get("domain") in selected_domains:
                    entry["status"] = "selected"
                else:
                    entry["status"] = "fallback"

        # No candidate survived (SSRF rejection, or every hub filtered
        # out by the domain allow-list). Never fall back to a
        # raw-instruction Google URL: the instruction is not a query
        # (it carries the assignment scaffolding and the deliverable),
        # and a Google URL is rejected by the same allow-list that
        # filtered the hubs. Fall back to Google only with the CLEANED
        # query and only when Google itself is allow-listed (or no
        # allow-list is configured); otherwise report the block so the
        # caller can tell the user which domain to add.
        blocked_reason: Optional[str] = None
        if selected:
            primary_url = selected[0].url
        elif queries and (not allowed_domains or _host_on_allowlist("google.com", allowed_domains)):
            primary_url = f"https://www.google.com/search?q={urllib.parse.quote_plus(queries[0])}"
        else:
            primary_url = None
            blocked_reason = (
                "No research source is on the domain allow-list, so I cannot open a page to research this. "
                "Add one of google.com, en.wikipedia.org, arxiv.org, or nature.com to "
                "PRIVAGENT_ALLOWED_DOMAINS in backend/.env and restart the backend, then ask again."
            )

        # Log discovery decisions in task context
        if context is not None and isinstance(context, dict):
            if "discovery_log" not in context:
                context["discovery_log"] = []
            context["discovery_log"].append({
                "instruction": instruction,
                "category": category,
                "queries": queries,
                "selected_domains": [s.domain for s in selected],
                "primary_url": primary_url,
                "provenance": provenance_log,
            })

        return {
            "scenario": "B",
            "specified_website": None,
            "objective": objective or instruction,
            "category": category,
            "is_ambiguous": False,
            "ambiguity_candidates": [],
            "search_queries": queries,
            "candidate_sources": [c.to_dict() for c in candidates],
            "selected_sources": [s.to_dict() for s in selected],
            "primary_navigation_url": primary_url,
            "discovery_blocked_reason": blocked_reason,
            "source_provenance": provenance_log,
        }

    def is_official_domain(self, domain: str, target_name: str) -> bool:
        """Check whether a domain is the verified official host for target_name."""
        if not domain or not target_name:
            return False
        clean_dom = domain.lower().replace("www.", "").strip()
        clean_target = target_name.lower().strip()

        # Check known registry
        for kd in KNOWN_DOMAINS_REGISTRY.values():
            if kd["name"].lower() == clean_target or any(re.search(p, clean_target) for p in kd["patterns"]):
                return clean_dom == kd["primary_domain"] or clean_dom in kd.get("allowed_domains", [])

        # Direct domain match check
        target_root = clean_target.replace(" ", "").replace("-", "")
        dom_root = clean_dom.split(".")[0].replace("-", "")
        return dom_root == target_root

    def _record_cache(
        self,
        raw: str,
        region: str,
        resolved: ResolvedWebsite,
        context: Optional[dict[str, Any]],
    ) -> None:
        reg_clean = (region or self.default_region).lower()
        key = (raw.lower(), reg_clean)
        now = time.time()
        self._memory_cache[key] = (resolved, now)
        if context is not None and isinstance(context, dict):
            if "website_resolution_cache" not in context:
                context["website_resolution_cache"] = {}
            cached_data = resolved.to_dict()
            cached_data["_cached_at"] = now
            context["website_resolution_cache"][f"{raw.lower()}::{reg_clean}"] = cached_data


# Global Singleton Instance for shared use across backend modules
website_resolver = WebsiteResolver()
