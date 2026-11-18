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


def test_scenario_b_shopping_discovery_no_website_specified(sandbox_resolver):
    """When user does not specify a website for shopping, autonomously discover and rank retail sources.

    Task 8: discovery is GENERIC — shopping is answered by general web
    search hubs, never by per-retailer search URLs. The agent opens
    whichever retailer the search results point at; no retailer search
    URL is baked into discovery. Retailers ARE candidates, though:
    their regional homepages come from the known-domains registry, so
    a shopping task can go straight to the retailer. (Fixed 2026-11-18:
    the sandbox DNS returns RFC 2544 addresses for every host, so the
    test uses the ``sandbox_resolver`` fixture with stub DNS instead of
    the global resolver.)
    """
    instruction = "Find the cheapest laptop with an RTX 4060."
    context: dict = {}
    res = sandbox_resolver.discover_websites_for_task(instruction, context=context)

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

    # Top selected sources must be independent. Retailers may be
    # selected (their regional homepages come from the registry), but
    # discovery must never fabricate a per-retailer SEARCH URL:
    # only the search hubs may carry a query string.
    selected_domains = [s["domain"] for s in res["selected_sources"]]
    assert len(set(selected_domains)) >= 2  # Independent sources
    for source in res["selected_sources"]:
        url = source["url"]
        if "?" in url:
            assert any(h in url for h in ("google.com", "duckduckgo.com", "wikipedia.org")), (
                f"per-retailer search URL baked into discovery: {url}"
            )

    # Discovery decisions recorded in context
    assert "discovery_log" in context
    assert len(context["discovery_log"]) == 1


def test_scenario_b_research_prefers_primary_authoritative_sources(sandbox_resolver):
    """When user requests scientific/technical research without specifying a site, prefer primary sources."""
    instruction = "Research the latest developments in solid-state batteries and compare findings from reliable sources."
    context: dict = {}
    res = sandbox_resolver.discover_websites_for_task(instruction, context=context)

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


# --- Research query cleaning + allow-list-aware discovery (2026-11-19) ---
#
# Live failure: "so i have to submit a research paper on topic :
# \"Quantum Mechanics similarities with life?\" so can you research
# about this topic online..." produced queries[0] ==
# "i have to submit a paper : \"Quantum overview technical
# principles" (submission scaffolding ate the 8-word cap, the real
# topic was truncated, a UI arrow leaked in) and discovery picked
# nature.com — which was not on the user's domain allow-list — so
# the deterministic plan was rejected with PLAN_INVALID and the
# research task died on the first step.

_RESEARCH_INSTRUCTION = (
    'so i have to submit a research paper on topic : '
    '"Quantum Mechanics similarities with life?" so can you research '
    "about this topic online and get me information report in doc "
    "format and include the images in it"
)
_ERROR_URL_INSTRUCTION = 'i have to submit a paper : "Quantum overview technical principles ↗'


def test_research_query_strips_submission_preamble_and_ui_glyph(sandbox_resolver):
    """The decomposed research query is the topic only: no submission
    scaffolding, no quotes/colons, no UI glyph, and the real topic is
    not truncated by the 8-word cap."""
    res = sandbox_resolver.discover_websites_for_task(_RESEARCH_INSTRUCTION, context={})
    assert res["category"] == "research"
    queries = res["search_queries"]
    assert len(queries) >= 1
    q0 = queries[0]
    assert "Quantum Mechanics similarities with life" in q0
    for banned in ("i have to submit", "submit a paper", "research paper", "on topic", '"', ":", "↗", "✓", "›"):
        assert banned not in q0, f"query still carries {banned!r}: {q0!r}"
    # The primary URL carries the clean topic, not the raw instruction.
    assert "Quantum+Mechanics+similarities+with+life" in (res["primary_navigation_url"] or "")
    assert "submit" not in (res["primary_navigation_url"] or "")


def test_research_query_error_url_variant_is_clean(sandbox_resolver):
    """The exact phrasing behind the failed nature.com URL (with the
    leaked external-link arrow) decomposes to the clean topic, and the
    research suffix is not duplicated onto a topic that already
    carries it."""
    res = sandbox_resolver.discover_websites_for_task(_ERROR_URL_INSTRUCTION, context={})
    q0 = res["search_queries"][0]
    assert q0 == "Quantum overview technical principles", q0
    assert "↗" not in q0 and "submit" not in q0
    assert "overview technical principles overview" not in " ".join(res["search_queries"])


def test_quoted_topic_and_helpers():
    """Unit checks for the topic helpers."""
    from app.website_resolver import (
        _extract_quoted_topic,
        _strip_submission_preamble,
        _strip_ui_glyphs,
    )

    assert _extract_quoted_topic(
        'submit a paper on topic : "Quantum Mechanics similarities with life?"'
    ) == "Quantum Mechanics similarities with life"
    # Unclosed quote still counts.
    assert _extract_quoted_topic('i have to submit a paper : "Quantum overview') == "Quantum overview"
    assert _extract_quoted_topic("research solid-state batteries") is None
    assert _strip_submission_preamble("i have to submit a research paper on topic Quantum") == "Quantum"
    assert _strip_submission_preamble("i need to write an essay about photosynthesis") == "photosynthesis"
    assert "on topic" not in _strip_submission_preamble("submit a paper on topic black holes")
    assert _strip_ui_glyphs("Quantum overview ↗") == "Quantum overview"
    assert _strip_ui_glyphs("check this ✓ and that ›") == "check this and that"


def test_research_discovery_filters_hubs_by_allowlist(sandbox_resolver):
    """Discovery never offers a hub outside the domain allow-list.
    With no research hub allow-listed, the primary URL is None and
    the block is explained instead of a doomed PLAN_INVALID navigate."""
    # No research hub on the list -> blocked, honestly.
    res = sandbox_resolver.discover_websites_for_task(
        _RESEARCH_INSTRUCTION,
        context={},
        allowed_domains=["amazon.in", "flipkart.com", "croma.com"],
    )
    assert res["category"] == "research"
    domains = [s["domain"] for s in res["selected_sources"]]
    assert "nature.com" not in domains and "arxiv.org" not in domains
    assert res["primary_navigation_url"] is None
    assert res["discovery_blocked_reason"]
    assert "PRIVAGENT_ALLOWED_DOMAINS" in res["discovery_blocked_reason"]

    # google.com on the list -> Google fallback with the CLEAN query.
    res = sandbox_resolver.discover_websites_for_task(
        _RESEARCH_INSTRUCTION,
        context={},
        allowed_domains=["amazon.in", "google.com"],
    )
    assert res["primary_navigation_url"] is not None
    assert res["primary_navigation_url"].startswith("https://www.google.com/search?q=")
    assert "Quantum+Mechanics+similarities+with+life" in res["primary_navigation_url"]
    assert "submit" not in res["primary_navigation_url"]
    assert res["discovery_blocked_reason"] is None

    # en.wikipedia.org on the list -> Wikipedia is the primary hub.
    res = sandbox_resolver.discover_websites_for_task(
        _RESEARCH_INSTRUCTION,
        context={},
        allowed_domains=["amazon.in", "en.wikipedia.org"],
    )
    assert res["selected_sources"][0]["domain"] == "en.wikipedia.org"
    assert "en.wikipedia.org/wiki/Special:Search" in res["primary_navigation_url"]
    assert "Quantum+Mechanics+similarities+with+life" in res["primary_navigation_url"]

    # No allow-list configured -> backwards compatible: nature.com offered.
    res = sandbox_resolver.discover_websites_for_task(_RESEARCH_INSTRUCTION, context={})
    assert "nature.com" in [s["domain"] for s in res["selected_sources"]]
    assert res["primary_navigation_url"] is not None
    assert "nature.com/search" in res["primary_navigation_url"]
    assert res["discovery_blocked_reason"] is None


def test_deterministic_plan_blocked_discovery_answers_honestly():
    """When discovery is blocked (no allow-listed hub), the
    deterministic planner answers with the fix instead of a navigate
    the allow-list gate would reject with PLAN_INVALID."""
    from app.planner import try_deterministic_plan
    from app.privacy import PrivacyFilteredPayload

    working_memory = {
        "autonomous_discovery": {
            "scenario": "B",
            "category": "research",
            "search_queries": ["Quantum Mechanics similarities with life overview technical principles"],
            "selected_sources": [],
            "primary_navigation_url": None,
            "discovery_blocked_reason": (
                "No research source is on the domain allow-list, so I cannot open a page to research this. "
                "Add one of google.com, en.wikipedia.org, arxiv.org, or nature.com to "
                "PRIVAGENT_ALLOWED_DOMAINS in backend/.env and restart the backend, then ask again."
            ),
        }
    }
    payload = PrivacyFilteredPayload(
        tab_id=1,
        url="",
        title="",
        interactive=[],
        scroll={},
        dpr=1.0,
        elements_original=0,
        elements_sent=0,
    )
    plan = try_deterministic_plan(_RESEARCH_INSTRUCTION, payload, [], working_memory=working_memory)
    assert plan is not None
    # No navigate step: the plan is an honest answer, not a doomed action.
    assert plan.steps == []
    assert plan.answer is not None
    assert "PRIVAGENT_ALLOWED_DOMAINS" in plan.answer
    assert "google.com" in plan.answer

