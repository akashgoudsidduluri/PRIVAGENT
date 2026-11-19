"""Regression tests for research PDF/DOCX report quality defects.

Covers the six defects reported against generated research reports
(Quantum Mechanics run, 2026-11-19): duplicate synthesis, LLM
meta-apologies and raw markdown in rendered output, software-systems
boilerplate, Wikipedia maintenance banners, self-contradiction and
stopword noise, junk/unrelated images, and acronym title-casing.

Each test is written against the production-shaped failure first:
it fails on the unfixed code and passes after the fix.
"""
from __future__ import annotations

import io
import random
import tempfile
from pathlib import Path

import pytest
from PIL import Image

from app.research.adaptive_planner import ComplexityTier, decompose_subtopics
from app.research.collector import (
    canonical_source_key,
    detect_contradiction_between_claims,
    extract_evidence_snippets,
    extract_page_images,
    harvest_page_evidence,
)
from app.research.document_plan import (
    _clean_subtopic_title,
    _clean_title,
    build_structured_document_plan,
)
from app.research.models import (
    EvidenceClassification,
    EvidenceItem,
    ResearchClaim,
    ResearchKnowledgePackage,
    ResearchSubtopic,
    ResearchTopic,
    SourceReference,
    VisualAsset,
)
from app.research.visuals.image_pipeline import (
    average_hash,
    ensure_local_image_asset,
    is_blank_or_solid_image,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pkg_with_summary(summary: str, topic_title: str = "Quantum Mechanics Similarities With Life") -> ResearchKnowledgePackage:
    topic = ResearchTopic(
        title=topic_title,
        objective=f"Research {topic_title}",
        findings_summary=summary,
        subtopics=[
            ResearchSubtopic(
                subtopic_id="sub_01",
                title="Wave-Particle Duality and Observation",
                findings_summary="Wave-particle duality means particles show wave-like interference.",
                claim_ids=["clm_01"],
            ),
        ],
    )
    pkg = ResearchKnowledgePackage(package_id="pkg_quality", topic=topic)
    pkg.add_source(SourceReference(
        source_id="src_01",
        url="https://en.wikipedia.org/wiki/Quantum_mechanics",
        domain="en.wikipedia.org",
        title="Quantum mechanics",
        reliability_score=0.85,
    ))
    pkg.add_claim(ResearchClaim(
        claim_id="clm_01",
        statement="Entangled particles show correlated measurement outcomes.",
        classification=EvidenceClassification.SINGLE_SOURCE,
        primary_source_id="src_01",
        confidence=0.9,
    ))
    pkg.add_evidence(EvidenceItem(
        evidence_id="ev_01",
        source_id="src_01",
        quote="Quantum entanglement links particles across distance.",
        confidence=0.9,
    ))
    return pkg


def _plan_text(plan) -> str:
    parts: list[str] = []
    for sec in plan.sections:
        parts.append(sec.title)
        for block in sec.blocks:
            if block.title:
                parts.append(block.title)
            if block.text:
                parts.append(block.text)
            parts.extend(block.items or [])
            if block.data_table:
                parts.extend(block.data_table.headers)
                for row in block.data_table.rows:
                    parts.extend(str(c) for c in row)
    return "\n".join(parts)


def _solid_png(color=(255, 0, 0), size=(200, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def _noise_png(seed: int = 7, size=(200, 200)) -> bytes:
    rnd = random.Random(seed)
    img = Image.new("RGB", size)
    px = img.load()
    for x in range(size[0]):
        for y in range(size[1]):
            px[x, y] = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Defect 1: duplicate synthesis, meta-apologies, raw markdown
# ---------------------------------------------------------------------------

def test_findings_summary_rendered_only_once():
    """The topic findings_summary must not appear in both Section 1
    (Executive Synthesis) and Section 2 (Core Findings Synthesis)."""
    summary = "Quantum entanglement links particles across distance."
    pkg = _pkg_with_summary(summary)
    # A subtopic carrying the identical summary must not duplicate it either.
    pkg.topic.subtopics.append(ResearchSubtopic(
        subtopic_id="sub_02",
        title="Entanglement Overview",
        findings_summary=summary,
    ))
    plan = build_structured_document_plan(pkg, target_format="pdf")
    text = _plan_text(plan)
    assert text.count(summary) == 1
    assert "Core Findings Synthesis" not in text


def test_meta_apologies_and_markdown_stripped_from_synthesis():
    """LLM meta-disclaimers are dropped and markdown becomes styled
    blocks — no raw '##', '**' or '|' pipe characters in the plan."""
    summary = (
        "I cannot render a binary PDF, but here is the summary.\n\n"
        "## Key Points\n\n"
        "The **transformer** uses **self-attention**.\n\n"
        "| Model | Score |\n"
        "|---|---|\n"
        "| A | 90% |\n"
        "| B | 85% |\n\n"
        "Format note: this was generated as text."
    )
    pkg = _pkg_with_summary(summary)
    plan = build_structured_document_plan(pkg, target_format="pdf")
    text = _plan_text(plan)
    assert "I cannot render" not in text
    assert "Format note" not in text
    assert "##" not in text
    assert "**" not in text
    assert "|" not in text
    # The markdown heading became a block title and the table a real table.
    titles = [b.title for s in plan.sections for b in s.blocks]
    assert any(t == "Key Points" for t in titles)
    tables = [b.data_table for s in plan.sections for b in s.blocks if b.data_table]
    assert tables, "markdown pipe table must become a structured table block"
    assert tables[0].headers == ["Model", "Score"]
    assert tables[0].rows[0][0] == "A"


def test_pdf_render_has_no_raw_markdown_or_apology(tmp_path):
    """End to end: the rendered PDF contains no raw markdown markers
    and no LLM meta-apology."""
    from pypdf import PdfReader

    from app.research.renderers.pdf_renderer import PdfRenderer

    summary = (
        "I cannot render a binary PDF, but here is the summary.\n\n"
        "## Key Points\n\nThe **transformer** uses **self-attention**."
    )
    pkg = _pkg_with_summary(summary)
    out = Path(tmp_path) / "report.pdf"
    PdfRenderer().render(pkg, str(out))
    pdf_text = "".join((p.extract_text() or "") for p in PdfReader(str(out)).pages)
    assert "I cannot render" not in pdf_text
    assert "##" not in pdf_text
    assert "**" not in pdf_text
    assert "Key Points" in pdf_text


# ---------------------------------------------------------------------------
# Defect 2: domain-neutral headings, empty subsections omitted
# ---------------------------------------------------------------------------

def test_no_software_boilerplate_for_science_topic():
    """A Quantum Mechanics report must not contain software-systems
    boilerplate or invented generic claims."""
    pkg = _pkg_with_summary("Quantum entanglement links particles across distance.")
    plan = build_structured_document_plan(pkg, target_format="pdf")
    text = _plan_text(plan)
    for banned in (
        "service boundaries",
        "state transition pipelines",
        "Core Technical Mechanics",
        "Architectural Trade-off",
        "parameter sensitivity",
        "cascading degradation",
        "modular isolation",
    ):
        assert banned not in text


def test_empty_subsection_is_omitted():
    """A subtopic with no summary, description, claims, visuals or
    formulas produces no section at all."""
    pkg = ResearchKnowledgePackage(
        package_id="pkg_empty_sub",
        topic=ResearchTopic(
            title="Quantum Mechanics Similarities With Life",
            objective="Research quantum mechanics",
            subtopics=[
                ResearchSubtopic(subtopic_id="sub_01", title="Empty Subtopic With Nothing"),
            ],
        ),
    )
    plan = build_structured_document_plan(pkg, target_format="pdf")
    titles = [s.title for s in plan.sections]
    assert not any("Empty Subtopic With Nothing" in t for t in titles)


def test_adaptive_planner_subtopics_are_domain_neutral():
    """decompose_subtopics must not emit the software-systems
    'Core Technical Mechanics & Methodologies' title."""
    subs = decompose_subtopics(
        "research quantum mechanics similarities with life",
        "Quantum Mechanics Similarities With Life",
        ComplexityTier.STANDARD,
    )
    titles = [s.title for s in subs]
    assert not any("Core Technical Mechanics" in t for t in titles)


# ---------------------------------------------------------------------------
# Defect 3: Wikipedia maintenance banners, mirror-domain dedup
# ---------------------------------------------------------------------------

def test_wikipedia_maintenance_banners_filtered():
    """Sentences carrying Wikipedia maintenance templates are dropped
    from extracted evidence, even when they contain a factual signal."""
    text = (
        "The new battery achieved 98.5% capacity retention after 1000 cycles.\n"
        "Please help improve it by adding citations to reliable sources.\n"
        "Learn how and when to remove this message from the article.\n"
        "Independent tests measured 62.0% capacity retention after 1000 cycles, though citation needed.\n"
        "The cell delivered 450 Wh/kg energy density in laboratory conditions.\n"
        "This article may require cleanup to meet quality standards.\n"
    )
    snippets = extract_evidence_snippets(text)
    quotes = " ".join(s["quote"] for s in snippets)
    assert "Please help improve it" not in quotes
    assert "Learn how and when to remove this message" not in quotes
    assert "citation needed" not in quotes
    assert "may require cleanup" not in quotes
    assert "98.5% capacity retention" in quotes
    assert "450 Wh/kg" in quotes


def test_canonical_source_key_merges_mirror_domains():
    """bbc.co.uk and bbc.com with the identical path are the same source."""
    a = canonical_source_key("https://www.bbc.co.uk/news/articles/c123xyz")
    b = canonical_source_key("https://bbc.com/news/articles/c123xyz")
    c = canonical_source_key("https://www.bbc.com/news/articles/different9")
    assert a == b
    assert a != c


def test_mirror_domain_source_is_duplicate():
    """Harvesting the bbc.com mirror of an already-harvested bbc.co.uk
    article is a duplicate, not a second source."""
    pkg = ResearchKnowledgePackage(
        package_id="pkg_mirror",
        topic=ResearchTopic(title="Mirror Test", objective="test mirrors"),
    )
    text_a = (
        "<img src=\"https://example.com/fig.png\" alt=\"Test figure\">\n"
        "The reactor reached 40% efficiency in trials."
    )
    res_a = harvest_page_evidence(
        url="https://www.bbc.co.uk/news/articles/c123xyz",
        title="Reactor Trials",
        text=text_a,
        pkg=pkg,
    )
    assert res_a.status == "success"
    res_b = harvest_page_evidence(
        url="https://bbc.com/news/articles/c123xyz",
        title="Reactor Trials",
        text=text_a,
        pkg=pkg,
    )
    assert res_b.status == "duplicate"
    assert len(pkg.sources) == 1


# ---------------------------------------------------------------------------
# Defect 4: self-contradiction, stopword noise
# ---------------------------------------------------------------------------

def test_same_source_cannot_contradict_itself():
    """Two conflicting numbers on the SAME page are not a cross-source
    contradiction: no CONFLICTING claim, no recorded contradiction."""
    pkg = ResearchKnowledgePackage(
        package_id="pkg_self_contra",
        topic=ResearchTopic(title="Battery Longevity", objective="Compare battery metrics"),
    )
    text = (
        "<img src=\"https://example.com/fig.png\" alt=\"Test figure\">\n"
        "The new battery achieved 98.5% capacity retention after 1000 cycles.\n"
        "Under stress testing the battery capacity retention was only 62.0% after 1000 cycles."
    )
    res = harvest_page_evidence(
        url="https://science.org/battery-press",
        title="Novel Battery Longevity - Science",
        text=text,
        pkg=pkg,
    )
    assert res.status == "success"
    assert res.claims_conflicted == 0
    assert pkg.contradictions == []
    assert all(
        c.classification != EvidenceClassification.CONFLICTING
        for c in pkg.claims.values()
    )


def test_stopwords_do_not_create_contradiction():
    """Statements whose only shared tokens are stopwords ('from',
    'that', 'with', 'this') are not contradictions."""
    a = "The committee noted that the budget from the office was 40%."
    b = "The council said that the grant from the agency was 75%."
    is_contra, _ = detect_contradiction_between_claims(a, b)
    assert is_contra is False


def test_real_contradiction_still_detected():
    """The stopword fix must not hide genuine contradictions."""
    is_contra, reason = detect_contradiction_between_claims(
        "The new battery achieved 98.5% capacity retention after 1000 cycles.",
        "Under stress testing the battery capacity retention was only 62.0% after 1000 cycles.",
    )
    assert is_contra is True
    assert "percentage metrics" in (reason or "").lower()


# ---------------------------------------------------------------------------
# Defect 5: junk images, blank images, duplicate images, figure cap
# ---------------------------------------------------------------------------

def test_junk_web_images_rejected():
    """Promo banners, skeleton placeholders, generic og:image logos and
    GitHub opengraph cards are rejected; a real figure is kept."""
    html = """
    <html><head>
      <meta property="og:image" content="https://example.com/assets/site-logo.png">
      <meta property="og:image" content="https://opengraph.githubassets.com/abc123/repo">
    </head><body>
      <img src="https://example.com/ads/promo-banner.jpg" alt="Summer promo banner">
      <img src="https://example.com/img/skeleton-placeholder.png" alt="Loading placeholder">
      <img src="https://example.com/icons/32px-icon.png" alt="Small icon">
      <img src="https://example.com/photos/double-slit.png" alt="Quantum double-slit experiment diagram" width="800" height="600">
    </body></html>
    """
    visuals = extract_page_images(
        url="https://example.com/research/quantum",
        text_or_html=html,
        title="Quantum Research",
        domain="example.com",
        source_id="src_001",
    )
    urls = [v.url_or_path for v in visuals]
    assert urls == ["https://example.com/photos/double-slit.png"]
    assert all("logo" not in u and "promo" not in u and "placeholder" not in u for u in urls)


def test_blank_and_solid_images_detected():
    """Solid-colour and blank images have (near-)zero pixel variance."""
    assert is_blank_or_solid_image(_solid_png(color=(255, 0, 0))) is True
    assert is_blank_or_solid_image(_solid_png(color=(255, 255, 255))) is True
    assert is_blank_or_solid_image(_noise_png()) is False


def test_blank_remote_image_not_cached_for_report(tmp_path, monkeypatch):
    """A blank image fetched for a report is rejected at the pipeline,
    not embedded; a real image is cached normally."""
    from app.research.visuals import image_pipeline as ip

    solid = _solid_png()
    noise = _noise_png()

    def mock_fetch(url, dest_path, **kwargs):
        payload = solid if "blank" in url else noise
        Path(dest_path).write_bytes(payload)
        return True, None, {"format": "PNG", "width": 200, "height": 200}

    monkeypatch.setattr(ip, "fetch_and_validate_image_url", mock_fetch)

    vis_blank = VisualAsset(
        asset_id="vis_blank", asset_type="photograph", title="Blank",
        url_or_path="https://example.com/blank.png", is_generated=False,
    )
    assert ensure_local_image_asset(
        vis_blank, cache_dir=tmp_path / "cache", reject_blank=True) is None

    vis_real = VisualAsset(
        asset_id="vis_real", asset_type="photograph", title="Real",
        url_or_path="https://example.com/real.png", is_generated=False,
    )
    resolved = ensure_local_image_asset(
        vis_real, cache_dir=tmp_path / "cache", reject_blank=True)
    assert resolved is not None
    assert Path(resolved).exists()


def test_same_image_bytes_deduplicated_by_content_hash(tmp_path, monkeypatch):
    """The same image served under two different URLs is cached once
    and both visuals resolve to the same local path."""
    from app.research.visuals import image_pipeline as ip

    payload = _noise_png()

    def mock_fetch(url, dest_path, **kwargs):
        Path(dest_path).write_bytes(payload)
        return True, None, {"format": "PNG", "width": 200, "height": 200}

    monkeypatch.setattr(ip, "fetch_and_validate_image_url", mock_fetch)

    cache_dir = tmp_path / "cache"
    vis_a = VisualAsset(
        asset_id="vis_a", asset_type="photograph", title="A",
        url_or_path="https://cdn-a.example.com/fig.png", is_generated=False,
    )
    vis_b = VisualAsset(
        asset_id="vis_b", asset_type="photograph", title="B",
        url_or_path="https://cdn-b.example.com/fig.png?v=2", is_generated=False,
    )
    path_a = ensure_local_image_asset(vis_a, cache_dir=cache_dir)
    path_b = ensure_local_image_asset(vis_b, cache_dir=cache_dir)
    assert path_a is not None and path_b is not None
    assert path_a == path_b
    assert len(list(cache_dir.glob("*.png"))) == 1
    # Perceptual hashes of identical bytes are identical.
    assert average_hash(payload) == average_hash(payload)


def test_web_figure_cap_per_report():
    """At most 4 harvested (non-generated) figures are placed in one
    report; generated diagrams are not capped."""
    pkg = ResearchKnowledgePackage(
        package_id="pkg_cap",
        topic=ResearchTopic(title="Figure Cap", objective="cap test"),
    )
    for i in range(6):
        pkg.visuals.append(VisualAsset(
            asset_id=f"vis_web_{i}",
            asset_type="photograph",
            title=f"Quantum figure {i} wave interference",
            caption="A harvested figure.",
            url_or_path=f"https://example.com/fig{i}.png",
            source_id="src_01",
            is_generated=False,
        ))
    pkg.visuals.append(VisualAsset(
        asset_id="vis_gen_1",
        asset_type="diagram",
        title="Generated quantum diagram wave interference",
        caption="A generated diagram.",
        url_or_path="/tmp/gen.png",
        source_id="src_01",
        is_generated=True,
    ))
    plan = build_structured_document_plan(pkg, target_format="pdf")
    web_images = [
        b for s in plan.sections for b in s.blocks
        if b.block_type.value == "image" and b.visual_asset
        and not b.visual_asset.is_generated
    ]
    gen_images = [
        b for s in plan.sections for b in s.blocks
        if b.block_type.value == "image" and b.visual_asset
        and b.visual_asset.is_generated
    ]
    assert len(web_images) == 4
    assert len(gen_images) == 1


# ---------------------------------------------------------------------------
# Defect 6: acronym title-casing
# ---------------------------------------------------------------------------

def test_acronyms_stay_uppercase_in_titles():
    """AI / LLM / API / PDF must not be title-cased to Ai / Llm / Api / Pdf."""
    title = _clean_title("research on AI and LLM API for PDF generation")
    for acronym in ("AI", "LLM", "API", "PDF"):
        assert acronym in title
    for broken in ("Ai", "Llm", "Api", "Pdf"):
        assert broken not in title

    sub = _clean_subtopic_title("ai safety and llm alignment")
    assert "AI" in sub and "LLM" in sub

    all_caps = _clean_title("QUANTUM MECHANICS AND AI")
    assert all_caps == "Quantum Mechanics And AI"
