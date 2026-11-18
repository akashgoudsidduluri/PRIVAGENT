"""Fix tests for the research + multi-tab + doc-generation focus (2026-11-18).

Covers the three fixes from the team audit:
1. Injectable DNS resolver — the SSRF/rebinding defense is testable
   and intact; the sandbox DNS sinkhole no longer zeroes discovery.
2. Retailer candidates — shopping discovery returns the region's
   retailers from the known-domains registry (canonical homepages,
   never per-retailer search URLs).
3. Multi-tab prune — a closed tab leaves the registry (helper-level
   tests; the loop-level behaviour is in test_agentLoop.py).
"""
from __future__ import annotations

import socket

from app.loop_checkpoints import _prune_tabs_not_in_observation
from app.website_resolver import (
    WebsiteResolver,
    _shopping_retailers_for_region,
    is_safe_public_url,
)


def _public_resolver(hostname, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


def _rebinding_resolver(hostname, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]


# ---------------------------------------------------------------------------
# 1. Injectable DNS resolver: defense intact, sandbox testable
# ---------------------------------------------------------------------------

def test_stub_resolver_public_ip_is_safe():
    safe, err = is_safe_public_url("google.com", resolver=_public_resolver)
    assert safe is True
    assert err is None


def test_rebinding_resolver_loopback_is_rejected():
    """A resolver returning loopback must still be rejected — the
    injectable resolver must not weaken the rebinding defense."""
    safe, err = is_safe_public_url("google.com", resolver=_rebinding_resolver)
    assert safe is False
    assert "127.0.0.1" in (err or "")


def test_rebinding_resolver_zeroes_discovery():
    """With a rebinding resolver, discovery returns no candidates
    (every hub is rejected) — the defense is testable end to end."""
    r = WebsiteResolver(resolver=_rebinding_resolver)
    res = r.discover_websites_for_task("Find the cheapest laptop with an RTX 4060.")
    assert res["candidate_sources"] == []
    assert res["selected_sources"] == []


def test_default_resolver_is_real_dns():
    """The global resolver uses real DNS by default (production
    behaviour unchanged) — it just cannot be relied on in this
    sandbox, where DNS returns RFC 2544 addresses."""
    from app.website_resolver import website_resolver

    assert website_resolver._resolver is None


# ---------------------------------------------------------------------------
# 2. Retailer candidates are regional and never search URLs
# ---------------------------------------------------------------------------

def test_shopping_retailers_india():
    rets = _shopping_retailers_for_region("IN")
    domains = {r["domain"] for r in rets}
    assert {"amazon.in", "flipkart.com", "croma.com", "reliancedigital.in"} <= domains
    assert "bestbuy.com" not in domains
    assert "walmart.com" not in domains
    # Canonical homepages only — no per-retailer search URL.
    for r in rets:
        assert "?" not in r["url"]
        assert r["url"].startswith("https://")


def test_shopping_retailers_us():
    rets = _shopping_retailers_for_region("US")
    domains = {r["domain"] for r in rets}
    assert {"amazon.com", "bestbuy.com", "walmart.com"} <= domains
    assert "flipkart.com" not in domains
    assert "croma.com" not in domains


def test_shopping_discovery_includes_retailers(sandbox_resolver):
    res = sandbox_resolver.discover_websites_for_task(
        "Find three good programming laptops under 80000 in India.", region="IN"
    )
    domains = [c["domain"] for c in res["candidate_sources"]]
    assert any(d in domains for d in ("amazon.in", "flipkart.com", "croma.com", "reliancedigital.in"))
    assert "bestbuy.com" not in domains
    # No per-retailer search URL anywhere in the candidates.
    for c in res["candidate_sources"]:
        if "?" in c["url"]:
            assert any(h in c["url"] for h in ("google.com", "duckduckgo.com", "wikipedia.org"))


# ---------------------------------------------------------------------------
# 3. Multi-tab prune helper
# ---------------------------------------------------------------------------

def _memory_with_tabs():
    return {
        "primary_tab_id": 1,
        "tabs": [
            {"tab_id": 1, "url": "https://example.com/", "title": "Home", "active": False},
            {"tab_id": 2, "url": "https://example.com/docs", "title": "Docs", "active": True},
            {"tab_id": 3, "url": "https://example.com/about", "title": "About", "active": False},
        ],
    }


def test_prune_removes_closed_tab_and_reassigns_active():
    wm = _memory_with_tabs()
    pruned = _prune_tabs_not_in_observation(wm, [1, 2], observed_tab_id=1, primary_tab_id=1)
    assert pruned == [3]
    assert [t["tab_id"] for t in wm["tabs"]] == [1, 2]
    # Tab 2 was active and was NOT pruned, so it stays active.
    assert [t["tab_id"] for t in wm["tabs"] if t["active"]] == [2]


def test_prune_active_closed_tab_moves_active_to_observed():
    wm = _memory_with_tabs()
    pruned = _prune_tabs_not_in_observation(wm, [1], observed_tab_id=1, primary_tab_id=1)
    assert pruned == [2, 3]
    assert [t["tab_id"] for t in wm["tabs"]] == [1]
    assert wm["tabs"][0]["active"] is True


def test_prune_never_removes_primary():
    wm = _memory_with_tabs()
    pruned = _prune_tabs_not_in_observation(wm, [2], observed_tab_id=2, primary_tab_id=1)
    assert pruned == [3]
    assert 1 in [t["tab_id"] for t in wm["tabs"]]


def test_prune_empty_list_prunes_nothing():
    wm = _memory_with_tabs()
    assert _prune_tabs_not_in_observation(wm, [], observed_tab_id=1, primary_tab_id=1) == []
    assert len(wm["tabs"]) == 3


def test_prune_list_missing_observed_tab_prunes_nothing():
    wm = _memory_with_tabs()
    # The list does not contain the observed tab: not a fresh
    # snapshot for this task, so prune nothing.
    assert _prune_tabs_not_in_observation(wm, [2, 3], observed_tab_id=1, primary_tab_id=1) == []
    assert len(wm["tabs"]) == 3


# ---------------------------------------------------------------------------
# 4. PDF fit loop: a filler-only section keeps its text (no blank page)
# ---------------------------------------------------------------------------

def test_filler_only_section_keeps_text_when_trimming(tmp_path):
    """Cross-check fix (deepseek-v4.1 #3, qwen): dropping filler text
    from a section whose ONLY blocks are filler text leaves a page
    with just a heading. The renderer must keep the filler text when
    the section has no other surviving content, so no page goes blank.

    The filler section is given enough text to span two pages, so
    dropping it is what would bring the document to the 5-page
    target: without the guard the fit loop chooses that 5-page
    result (with a bare "Notes" page); with the guard it keeps the
    text and returns the smallest non-blank result instead.

    Proven real: fails pre-fix (the "Notes" page is near-blank and
    the filler text is gone from the document).
    """
    from unittest.mock import patch

    from pypdf import PdfReader

    from app.research.document_plan import build_structured_document_plan
    from app.research.models import (
        EvidenceClassification,
        EvidenceItem,
        ResearchClaim,
        ResearchKnowledgePackage,
        SourceReference,
    )
    from app.research.renderers.pdf_renderer import PdfRenderer

    pkg = ResearchKnowledgePackage(
        package_id="pkg_guard_test",
        topic={"title": "Transformer Architecture", "objective": "Research transformer architecture in a 5-page report"},
    )
    s1 = pkg.add_source(SourceReference(source_id="src_1", url="https://arxiv.org/abs/1706.03762", domain="arxiv.org", title="Attention Is All You Need", is_primary=True))
    s2 = pkg.add_source(SourceReference(source_id="src_2", url="https://nature.com/articles/s41598-026-61520-8", domain="nature.com", title="Self-Attention Transformer Mechanics"))
    ev1 = pkg.add_evidence(EvidenceItem(evidence_id="ev_1", source_id=s1, quote="The Transformer allows for significantly more parallelization than recurrent networks."))
    pkg.add_claim(ResearchClaim(claim_id="clm_1", statement="Transformers replace sequential recurrence with parallel scaled dot-product attention.", primary_source_id=s1, supporting_source_ids=[s2], evidence_item_ids=[ev1], classification=EvidenceClassification.CORROBORATED))

    plan = build_structured_document_plan(pkg, target_format="pdf", target_pages=5)
    filler_secs = [s for s in plan.sections if s.metadata.get("filler")]
    assert filler_secs, "plan must contain a filler section for this test"
    target_sec = filler_secs[0]
    assert target_sec.page_break_before, "filler section must start its own page"
    assert all(b.block_type.value == "text" for b in target_sec.blocks), "filler section must be text-only"
    # A short heading: without the guard this page would be just "Notes".
    target_sec.title = "Notes"
    # Enough filler text to span two pages, so dropping it is what
    # would bring the document to the target. The marker makes the
    # text findable in the extracted output.
    marker = "FILLER_KEEPER_MARKER "
    long_text = marker + ("The filler section carries operational guidance that spans more than one page. " * 120)
    target_sec.add_text(long_text)

    out = tmp_path / "guard_test.pdf"
    with patch(
        "app.research.renderers.pdf_renderer.build_structured_document_plan",
        return_value=plan,
    ):
        PdfRenderer().render(pkg, str(out), target_pages=5)

    assert out.exists()
    reader = PdfReader(str(out))
    full_text = ""
    for p_idx, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        full_text += text + "\n"
        assert len(text) > 50, f"Page {p_idx} is blank or near-blank ({len(text)} chars)"
    assert marker.strip() in full_text, "filler-only section text was dropped, leaving a bare heading"
