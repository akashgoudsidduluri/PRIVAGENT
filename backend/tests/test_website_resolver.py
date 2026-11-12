"""Regression test suite for Reusable WebsiteResolver."""
from __future__ import annotations

import pytest
from app.website_resolver import (
    ResolvedWebsite,
    WebsiteResolver,
    is_lookalike_or_suspicious,
    website_resolver,
)


def test_known_domain_registry_resolution():
    """Verify registry resolution for major e-commerce, knowledge, and academic services."""
    r_amz = website_resolver.resolve("Amazon", region="IN")
    assert r_amz.canonical_url == "https://www.amazon.in"
    assert r_amz.primary_domain == "amazon.in"
    assert r_amz.is_verified_official is True
    assert "amazon.com" in r_amz.allowed_domains

    r_fk = website_resolver.resolve("Flipkart")
    assert r_fk.canonical_url == "https://www.flipkart.com"
    assert r_fk.primary_domain == "flipkart.com"
    assert r_fk.is_verified_official is True

    r_wiki = website_resolver.resolve("Wikipedia")
    assert r_wiki.primary_domain == "wikipedia.org"
    assert r_wiki.is_verified_official is True

    r_arxiv = website_resolver.resolve("arXiv")
    assert r_arxiv.primary_domain == "arxiv.org"
    assert r_arxiv.is_verified_official is True

    r_nature = website_resolver.resolve("Nature journal")
    assert r_nature.primary_domain == "nature.com"
    assert r_nature.is_verified_official is True

    r_gmail = website_resolver.resolve("gmail")
    assert r_gmail.primary_domain == "mail.google.com"
    assert r_gmail.is_verified_official is True


def test_regional_domain_selection():
    """Verify regional selection (e.g., India vs US regional domains)."""
    r_in = website_resolver.resolve("Amazon", region="IN")
    assert r_in.canonical_url == "https://www.amazon.in"

    r_us = website_resolver.resolve("Amazon", region="US")
    assert r_us.canonical_url == "https://www.amazon.com"

    r_samsung_in = website_resolver.resolve("Samsung store", region="IN")
    assert "samsung.com/in/" in r_samsung_in.canonical_url


def test_search_based_resolution_for_unknown_websites():
    """Unknown entities must fall back safely to search navigation without failing."""
    r_unknown = website_resolver.resolve("Quantum Innovations Lab Zurich")
    assert r_unknown.resolution_method == "search_fallback"
    assert "google.com/search?q=" in r_unknown.canonical_url
    assert "Quantum+Innovations" in r_unknown.canonical_url
    assert r_unknown.is_verified_official is False


def test_official_domain_verification():
    """Verify domain verification check against known entities."""
    assert website_resolver.is_official_domain("amazon.in", "Amazon") is True
    assert website_resolver.is_official_domain("amazon.com", "Amazon") is True
    assert website_resolver.is_official_domain("flipkart.com", "Flipkart") is True
    assert website_resolver.is_official_domain("google.com", "Google") is True
    assert website_resolver.is_official_domain("wikipedia.org", "Wikipedia") is True

    # Third party / lookalike domains must not be verified as official
    assert website_resolver.is_official_domain("amazon-deals.xyz", "Amazon") is False
    assert website_resolver.is_official_domain("flipkart-scam.net", "Flipkart") is False
    assert website_resolver.is_official_domain("randomblog.com", "Amazon") is False


def test_lookalike_and_deceptive_domain_detection():
    """Detect typosquats, digit substitution, punycode, and subdomain spoofing."""
    # Digit substitution
    is_risk, reason = is_lookalike_or_suspicious("amaz0n.com")
    assert is_risk is True
    assert "lookalike" in reason.lower()

    is_risk_fk, _ = is_lookalike_or_suspicious("fl1pkart.com")
    assert is_risk_fk is True

    # Punycode
    is_risk_puny, reason_puny = is_lookalike_or_suspicious("xn--amazn-7ra.com")
    assert is_risk_puny is True
    assert "punycode" in reason_puny.lower()

    # Subdomain spoofing
    is_risk_sub, reason_sub = is_lookalike_or_suspicious("amazon.com.fake-deals.xyz")
    assert is_risk_sub is True
    assert "subdomain" in reason_sub.lower()

    # Genuine domains are clean
    is_clean, _ = is_lookalike_or_suspicious("amazon.in")
    assert is_clean is False

    is_clean_wiki, _ = is_lookalike_or_suspicious("en.wikipedia.org")
    assert is_clean_wiki is False


def test_resolution_caching_in_task_context():
    """Cached resolutions within task memory must be reused without repeated lookups."""
    resolver = WebsiteResolver()
    ctx: dict = {}

    r1 = resolver.resolve("Flipkart", context=ctx)
    assert "website_resolution_cache" in ctx
    assert "flipkart::in" in ctx["website_resolution_cache"]

    # Second lookup must resolve from context cache
    r2 = resolver.resolve("Flipkart", context=ctx)
    assert r2.canonical_url == r1.canonical_url
    assert r2.primary_domain == r1.primary_domain


def test_ssrf_and_private_network_rejection():
    """Verify that private IPs, loopback, cloud metadata, and internal hostnames are blocked."""
    from app.website_resolver import is_safe_public_url

    # Loopback
    safe, err = is_safe_public_url("http://127.0.0.1:8000/secret")
    assert safe is False
    assert "loopback" in err.lower() or "blocked" in err.lower()

    safe_lh, err_lh = is_safe_public_url("http://localhost:3000")
    assert safe_lh is False
    assert "private/internal" in err_lh.lower() or "blocked" in err_lh.lower()

    # Private network ranges (RFC 1918)
    safe_priv1, _ = is_safe_public_url("http://192.168.1.1/admin")
    assert safe_priv1 is False

    safe_priv2, _ = is_safe_public_url("http://10.0.0.5/api")
    assert safe_priv2 is False

    # Cloud metadata (AWS/GCP)
    safe_meta, _ = is_safe_public_url("http://169.254.169.254/latest/meta-data/")
    assert safe_meta is False

    safe_gcp, _ = is_safe_public_url("http://metadata.google.internal/computeMetadata/v1/")
    assert safe_gcp is False

    # Disallowed non-http schemes
    safe_file, err_file = is_safe_public_url("file:///C:/Windows/System32/drivers/etc/hosts")
    assert safe_file is False
    assert "disallowed scheme" in err_file.lower()

    safe_js, _ = is_safe_public_url("javascript:alert(1)")
    assert safe_js is False

    # Embedded credentials
    safe_cred, err_cred = is_safe_public_url("http://admin:secret@example.com/")
    assert safe_cred is False
    assert "credentials" in err_cred.lower()

    # Direct URL resolve must reject SSRF
    r_bad = website_resolver.resolve("http://127.0.0.1:8080/debug")
    assert r_bad.resolution_method == "security_rejection"
    assert r_bad.is_lookalike_risk is True
    assert "SSRF" in r_bad.warning


def test_unicode_homograph_detection():
    """Detect mixed-script / Cyrillic confusable homograph characters in domains."""
    # Domain using Cyrillic small letter 'a' (U+0430) instead of ASCII 'a'
    cyrillic_domain = "\u0430mazon.com"
    is_risk, reason = is_lookalike_or_suspicious(cyrillic_domain)
    assert is_risk is True
    assert "homograph" in reason.lower() or "unicode" in reason.lower()


def test_cache_ttl_expiration():
    """Resolution cache must evict entries after TTL expiration."""
    resolver = WebsiteResolver(cache_ttl=0.05)  # 50ms TTL for test
    ctx: dict = {}

    r1 = resolver.resolve("Flipkart", context=ctx)
    assert r1.primary_domain == "flipkart.com"
    assert ("flipkart", "in") in resolver._memory_cache

    # Wait for TTL expiration
    import time
    time.sleep(0.06)

    # Next lookup evicts expired cache and re-resolves
    r2 = resolver.resolve("Flipkart", context=ctx)
    assert r2.primary_domain == "flipkart.com"


def test_source_provenance_tracking():
    """Autonomous website discovery must record detailed source provenance."""
    instruction = "Find the best gaming monitor under 20000"
    res = website_resolver.discover_websites_for_task(instruction)

    assert "source_provenance" in res
    provenance = res["source_provenance"]
    assert len(provenance) >= 2

    # Statuses must be tracked
    statuses = [p["status"] for p in provenance]
    assert "selected" in statuses
    for p in provenance:
        assert "reason" in p
        assert len(p["reason"]) > 5

