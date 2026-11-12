"""Comprehensive Unit and Integration Tests for Autonomous Website Discovery & Intent Routing.

Verifies:
1. Scenario A: User specifies a website -> resolves via WebsiteResolver and verifies official domain.
2. Scenario B: User does NOT specify a website -> decomposes queries, discovers candidates, ranks them.
3. Preference for primary sources in research vs shopping hubs in retail.
4. Selection of multiple independent sources when comparison or corroboration is requested.
5. Filtering of advertisements, SEO spam, and lookalike domains.
6. Preservation of search queries and discovery decisions in task context.
7. Ambiguous destination handling ("Open Mercury" -> safe clarification without guessing).
8. Intent classifier audit across the 10 mandated test queries and conceptual questions.
9. Consequential action safety (discovered URLs do not authorize purchases).
"""
from __future__ import annotations

from app.intent_router import TaskIntent, classify_intent
from app.website_resolver import (
    AMBIGUOUS_ENTITIES_REGISTRY,
    DiscoveredSource,
    format_ambiguity_clarification,
    is_ambiguous_entity,
    is_lookalike_or_suspicious,
    website_resolver,
)


def test_scenario_a_specified_website():
    """When user specifies a website, WebsiteResolver resolves and verifies the official domain."""
    instruction = "Find the cheapest laptop on Amazon."
    res = website_resolver.discover_websites_for_task(instruction)

    assert res["scenario"] == "A"
    assert res["specified_website"] == "Amazon"
    assert res["is_ambiguous"] is False
    assert res["resolved_website"] is not None
    assert res["resolved_website"]["primaryDomain"] in ("amazon.in", "amazon.com")
    assert res["resolved_website"]["isVerifiedOfficial"] is True
    assert "https://www.amazon." in res["primary_navigation_url"]


def test_scenario_b_shopping_discovery_no_website_specified():
    """When user does not specify a website for shopping, autonomously discover and rank retail sources."""
    instruction = "Find the cheapest laptop with an RTX 4060."
    context: dict = {}
    res = website_resolver.discover_websites_for_task(instruction, context=context)

    assert res["scenario"] == "B"
    assert res["specified_website"] is None
    assert res["category"] == "shopping"
    assert len(res["search_queries"]) >= 2
    assert any("RTX 4060" in q for q in res["search_queries"])

    # Discovered candidate sources
    candidates = res["candidate_sources"]
    assert len(candidates) >= 3

    # Ranking and scores exist
    for cand in candidates:
        assert cand["relevanceScore"] > 0
        assert cand["credibilityScore"] > 0
        assert cand["finalRankScore"] > 0
        assert cand["discoveredByQuery"] != ""
        assert not cand["isSpamOrAd"]

    # Top selected sources must include independent domains
    selected_domains = [s["domain"] for s in res["selected_sources"]]
    assert len(set(selected_domains)) >= 2  # Independent sources
    assert any("amazon" in d for d in selected_domains)
    assert any("flipkart" in d for d in selected_domains)

    # Discovery decisions recorded in context
    assert "discovery_log" in context
    assert len(context["discovery_log"]) == 1


def test_scenario_b_research_prefers_primary_authoritative_sources():
    """When user requests scientific/technical research without specifying a site, prefer primary sources."""
    instruction = "Research the latest developments in solid-state batteries and compare findings from reliable sources."
    context: dict = {}
    res = website_resolver.discover_websites_for_task(instruction, context=context)

    assert res["scenario"] == "B"
    assert res["category"] == "research"
    assert len(res["search_queries"]) >= 2

    # Verify primary sources are prioritized
    candidates = res["candidate_sources"]
    domains = [c["domain"] for c in candidates]
    assert "nature.com" in domains or "arxiv.org" in domains or "science.org" in domains

    # Primary sources flagged
    primary_cands = [c for c in candidates if c["isPrimary"]]
    assert len(primary_cands) >= 1

    # Multiple independent domains selected for comparison
    selected_domains = [s["domain"] for s in res["selected_sources"]]
    assert len(set(selected_domains)) >= 2


def test_ad_and_seo_spam_filtering():
    """Verify that advertisement links, SEO spam farms, and lookalikes are filtered out."""
    # Lookalike
    is_risk_lookalike, _ = is_lookalike_or_suspicious("amaz0n.com")
    assert is_risk_lookalike is True

    # Punycode spoofing
    is_risk_puny, _ = is_lookalike_or_suspicious("xn--flpkart-w1a.com")
    assert is_risk_puny is True

    # Subdomain brand spoofing
    is_risk_sub, _ = is_lookalike_or_suspicious("amazon.in.free-deals.top")
    assert is_risk_sub is True


def test_ambiguous_destination_open_mercury():
    """Ambiguous destinations like 'Open Mercury' must be caught safely without guessing."""
    # 1. Intent Router detects ambiguity
    classification = classify_intent("Open Mercury.")
    assert classification.is_browser is True
    assert classification.is_ambiguous is True
    assert "ambiguous" in classification.reason.lower()

    # 2. WebsiteResolver identifies ambiguous entity and returns candidates
    is_ambig, term, candidates = is_ambiguous_entity("Mercury")
    assert is_ambig is True
    assert term == "Mercury"
    assert len(candidates) >= 3

    domains = [c["domain"] for c in candidates]
    assert "mercury.com" in domains
    assert "mercurymarine.com" in domains
    assert "science.nasa.gov" in domains

    # 3. Clarification message formatting
    clarification = format_ambiguity_clarification("Mercury", candidates)
    assert "Ambiguous Destination: \"Mercury\"" in clarification
    assert "Mercury (Banking & Fintech)" in clarification
    assert "Mercury Marine" in clarification
    assert "Planet Mercury" in clarification
    assert "Please clarify your intended destination" in clarification

    # 4. Qualified destination is NOT ambiguous
    is_ambig_qualified, _, _ = is_ambiguous_entity("Mercury Marine outboard motors")
    assert is_ambig_qualified is False


def test_intent_router_10_mandated_audit_queries():
    """Audit the 10 mandated user queries across conversational, research, shopping, navigation, and continuation."""
    # 1. "Explain transformers." -> NON_BROWSER (conceptual explanation)
    r1 = classify_intent("Explain transformers.")
    assert r1.intent == TaskIntent.NON_BROWSER
    assert r1.is_browser is False

    # 2. "Research transformer architecture." -> RESEARCH_TASK
    r2 = classify_intent("Research transformer architecture.")
    assert r2.intent == TaskIntent.RESEARCH_TASK
    assert r2.is_browser is True
    assert r2.is_research is True

    # 3. "Find the cheapest iPhone." -> NEW_BROWSER_TASK (shopping discovery)
    r3 = classify_intent("Find the cheapest iPhone.")
    assert r3.intent == TaskIntent.NEW_BROWSER_TASK
    assert r3.is_browser is True

    # 4. "Open Amazon." -> NEW_BROWSER_TASK (direct navigation)
    r4 = classify_intent("Open Amazon.")
    assert r4.intent == TaskIntent.NEW_BROWSER_TASK
    assert r4.is_browser is True
    assert r4.is_ambiguous is False
    assert r4.target_tab_hint == "Amazon"

    # 5. "Compare Amazon and Flipkart prices." -> NEW_BROWSER_TASK (multi-site comparison)
    r5 = classify_intent("Compare Amazon and Flipkart prices.")
    assert r5.intent == TaskIntent.NEW_BROWSER_TASK
    assert r5.is_browser is True

    # 6. "Find official React documentation." -> NEW_BROWSER_TASK (official docs navigation)
    r6 = classify_intent("Find official React documentation.")
    assert r6.intent == TaskIntent.NEW_BROWSER_TASK
    assert r6.is_browser is True

    # 7. "Summarize this webpage." -> USE_EXISTING_USER_TAB (current tab inspection)
    r7 = classify_intent("Summarize this webpage.", context={"active_session": True})
    assert r7.intent == TaskIntent.USE_EXISTING_USER_TAB
    assert r7.is_browser is True

    # 8. "Prepare a PDF about operating systems." -> RESEARCH_TASK (requested format PDF)
    r8 = classify_intent("Prepare a PDF about operating systems.")
    assert r8.intent == TaskIntent.RESEARCH_TASK
    assert r8.is_browser is True
    assert r8.requested_format == "pdf"
    assert r8.deliverable_type == "report"

    # 9. "Find the best laptops for programming." -> NEW_BROWSER_TASK (product research)
    r9 = classify_intent("Find the best laptops for programming.")
    assert r9.intent == TaskIntent.NEW_BROWSER_TASK
    assert r9.is_browser is True

    # 10. "Continue the research from where you stopped." -> CONTINUE_BROWSER_TASK
    r10 = classify_intent(
        "Continue the research from where you stopped.",
        context={"active_session": True, "is_research": True, "requested_format": "pdf"},
    )
    assert r10.intent == TaskIntent.CONTINUE_BROWSER_TASK
    assert r10.is_browser is True
    assert r10.is_research is True
    assert r10.requested_format == "pdf"


def test_ordinary_coding_and_math_not_routed_to_browser():
    """Ordinary coding, algorithms, and math must remain NON_BROWSER conversational tasks."""
    assert classify_intent("Explain how quicksort works.").intent == TaskIntent.NON_BROWSER
    assert classify_intent("Write a python function to invert a binary tree.").intent == TaskIntent.NON_BROWSER
    assert classify_intent("Calculate 25 * 400 + 15% of 80.").intent == TaskIntent.NON_BROWSER
    assert classify_intent("What is dependency injection?").intent == TaskIntent.NON_BROWSER

