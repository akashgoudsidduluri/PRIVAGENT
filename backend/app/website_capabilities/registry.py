"""Website Capability Registry.

Central registry mapping domains, services, and tasks to specialized capabilities.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

from .base import BaseWebsiteCapability
from .generic import GenericWebsiteCapability
from .irctc import IrctcCapability
from .leetcode import LeetCodeCapability

_CAPABILITIES: dict[str, BaseWebsiteCapability] = {
    "leetcode": LeetCodeCapability(),
    "irctc": IrctcCapability(),
}

_DOMAIN_MAPPINGS: dict[str, str] = {
    "leetcode.com": "leetcode",
    "irctc.co.in": "irctc",
    "www.irctc.co.in": "irctc",
}


def get_capability_for_service(service_id: str) -> Optional[BaseWebsiteCapability]:
    """Retrieve capability by service id."""
    return _CAPABILITIES.get(service_id.strip().lower())


def get_capability_for_domain(domain_or_url: str) -> BaseWebsiteCapability:
    """Retrieve capability by domain or URL, defaulting to GenericWebsiteCapability."""
    raw = domain_or_url.strip().lower()
    if "://" in raw:
        try:
            domain = urlsplit(raw).netloc.split(":")[0]
        except Exception:
            domain = raw
    else:
        domain = raw.split("/")[0].split(":")[0]

    # Clean domain
    for d_key, s_key in _DOMAIN_MAPPINGS.items():
        if d_key in domain or domain in d_key:
            return _CAPABILITIES[s_key]

    for s_key, cap in _CAPABILITIES.items():
        if s_key in domain or domain in cap.primary_domain:
            return cap

    return GenericWebsiteCapability(domain=domain or "generic")


def resolve_capability(query_or_instruction: str) -> Optional[BaseWebsiteCapability]:
    """Infer the primary capability from a natural language directive or URL."""
    text = (query_or_instruction or "").lower()

    if any(k in text for k in ("leetcode", "two sum", "hackerrank")):
        return _CAPABILITIES["leetcode"]

    if any(k in text for k in ("irctc", "train ticket", "trains from", "trains to", "railway ticket")):
        return _CAPABILITIES["irctc"]

    # Check for URLs
    for d_key, s_key in _DOMAIN_MAPPINGS.items():
        if d_key in text:
            return _CAPABILITIES[s_key]

    return None
