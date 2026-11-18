"""Per-page customizable PDF artifact generation.

Covers the parser (per-page specs from the user's instruction), the
per-page plan builder (one section per user page, evidence and browser
images matched to the right page, nothing invented), and the PDF
renderer (exactly N pages, each user page on its own PDF page, honest
overflow reporting). Every test is proven to fail pre-fix via git stash.
"""
from __future__ import annotations

import io
import tempfile
from pathlib import Path

from pypdf import PdfReader
from PIL import Image as PILImage

from app.research.adaptive_planner import parse_page_specs, reconcile_page_count
from app.research.document_plan import build_per_page_plan, build_structured_document_plan
from app.research.models import (
    EvidenceClassification,
    ResearchClaim,
    ResearchKnowledgePackage,
    ResearchSubtopic,
    ResearchTopic,
    SourceReference,
    VisualAsset,
)
from app.research.renderers.pdf_renderer import PdfRenderer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _build_pkg() -> ResearchKnowledgePackage:
    """Package with claims about conductivity, energy density, and cost,
    plus one browser-extracted image about conductivity."""
    topic = ResearchTopic(
        title="Solid-State Battery Breakthroughs",
        objective="Analyze energy density, ionic conductivity, and commercial viability",
        target_format="pdf",
        subtopics=[
            ResearchSubtopic(
                subtopic_id="sub_01",
                title="Sulfide Electrolytes",
                status="completed",
                findings_summary="Sulfide solid electrolytes exhibit ionic conductivity exceeding 20 mS/cm.",
                claim_ids=["clm_01"],
            ),
            ResearchSubtopic(
                subtopic_id="sub_02",
                title="Energy Density",
                status="completed",
                findings_summary="Prototype pouch cells reached 450 Wh/kg.",
                claim_ids=["clm_02"],
            ),
        ],
    )
    pkg = ResearchKnowledgePackage(package_id="pkg_per_page", topic=topic)
    pkg.add_source(SourceReference(
        source_id="src_01", url="https://nature.com/articles/battery",
        domain="nature.com", title="Solid Electrolyte Ionic Transport",
        is_primary=True,
    ))
    pkg.add_source(SourceReference(
        source_id="src_02", url="https://arxiv.org/abs/2403.99999",
        domain="arxiv.org", title="Energy Density Scaling in Lithium Metal Cells",
        is_primary=True,
    ))
    pkg.add_claim(ResearchClaim(
        claim_id="clm_01",
        statement="Sulfide solid electrolytes achieved 25 mS/cm ionic conductivity at 25C.",
        classification=EvidenceClassification.SINGLE_SOURCE,
        claim_type="statistic", subtopic_id="sub_01",
        primary_source_id="src_01", confidence=0.94,
    ))
    pkg.add_claim(ResearchClaim(
        claim_id="clm_02",
        statement="Full cell energy density reached 450 Wh/kg in prototype pouch cells.",
        classification=EvidenceClassification.CORROBORATED,
        claim_type="advantage", subtopic_id="sub_02",
        primary_source_id="src_02", confidence=0.91,
    ))
    pkg.add_claim(ResearchClaim(
        claim_id="clm_03",
        statement="Manufacturing cost per kWh fell 18 percent year over year.",
        classification=EvidenceClassification.SINGLE_SOURCE,
        claim_type="statistic", primary_source_id="src_02",
        confidence=0.88,
    ))
    return pkg


def _local_image(tmp_dir: Path, name: str = "img.png") -> str:
    """A small real PNG on disk (no network) for the visual tests."""
    path = tmp_dir / name
    PILImage.new("RGB", (400, 200), color=(30, 58, 138)).save(path)
    return str(path)


def _page_texts(pdf_path: str) -> list[str]:
    reader = PdfReader(pdf_path)
    return [(p.extract_text() or "") for p in reader.pages]


# ---------------------------------------------------------------------------
# 1. Parser
# ---------------------------------------------------------------------------

def test_parse_page_specs_comma_separated():
    specs = parse_page_specs(
        "I need 5 pages: page 1 intro and overview, page 2 specs and prices, "
        "page 3 comparison table, page 4 pros and cons, page 5 verdict"
    )
    assert len(specs) == 5
    assert [s.page_number for s in specs] == [1, 2, 3, 4, 5]
    assert specs[0].title == "intro and overview"
    assert specs[1].title == "specs and prices"
    assert specs[4].title == "verdict"
    assert "specs and prices" in specs[1].brief


def test_parse_page_specs_semicolon_and_dash():
    specs = parse_page_specs(
        "Generate a 5-page PDF. page 1 - intro; page 2 - specs; page 3 - comparison; "
        "page 4 - pros and cons; page 5 - verdict"
    )
    assert len(specs) == 5
    assert specs[0].title == "intro"
    assert specs[3].title == "pros and cons"


def test_parse_page_specs_newlines():
    specs = parse_page_specs(
        "Research transformers.\npage 1: what is attention\n"
        "page 2: multi-head attention\npage 3: positional encoding"
    )
    assert len(specs) == 3
    assert specs[1].title == "multi-head attention"


def test_parse_page_specs_bare_numbered_list():
    specs = parse_page_specs("I need 3 pages: 1. intro 2. specs 3. verdict")
    assert len(specs) == 3
    assert [s.title for s in specs] == ["intro", "specs", "verdict"]


def test_parse_page_specs_bare_numbered_list_parens():
    specs = parse_page_specs("I need 3 pages: 1) intro 2) specs 3) verdict")
    assert len(specs) == 3
    assert specs[2].title == "verdict"


def test_parse_page_specs_no_specs():
    assert parse_page_specs("Make a 5-page report on transformers") == []
    assert parse_page_specs("") == []


def test_parse_page_specs_numbered_list_without_page_count():
    # A plain numbered list is NOT a page spec — no false positive.
    assert parse_page_specs("Do these 3 things: 1. search 2. compare 3. summarise") == []


def test_reconcile_page_count_mismatch_reported():
    n, specs, msg = reconcile_page_count(
        "I need 4 pages but only list page 1 intro, page 2 specs"
    )
    assert n == 2
    assert len(specs) == 2
    assert msg is not None and "4 pages" in msg and "2 per-page briefs" in msg


def test_reconcile_page_count_agrees():
    n, specs, msg = reconcile_page_count(
        "I need 5 pages: page 1 intro, page 2 specs, page 3 comparison, "
        "page 4 pros and cons, page 5 verdict"
    )
    assert n == 5
    assert len(specs) == 5
    assert msg is None


def test_parse_page_specs_ordinal_first_last():
    specs = parse_page_specs(
        "first page: intro and overview, last page: verdict and recommendation"
    )
    assert len(specs) == 2
    assert specs[0].title == "intro and overview"
    assert specs[1].title == "verdict and recommendation"


def test_parse_page_specs_duplicate_number_keeps_last():
    # A brief that references "page 2" must not steal the real page 2.
    specs = parse_page_specs(
        "page 1: intro, see page 2 for the chart, page 2: specs and prices"
    )
    assert len(specs) == 2
    assert specs[1].title == "specs and prices"


# ---------------------------------------------------------------------------
# 2. Plan builder
# ---------------------------------------------------------------------------

def test_per_page_plan_exact_sections():
    pkg = _build_pkg()
    specs = parse_page_specs(
        "page 1 ionic conductivity, page 2 energy density, page 3 manufacturing cost"
    )
    plan = build_per_page_plan(pkg, specs)
    assert plan.is_per_page is True
    assert plan.target_pages == 3
    assert plan.total_allocated_pages == 3
    assert len(plan.sections) == 3
    assert [s.page_number for s in plan.sections] == [1, 2, 3]
    assert [s.title for s in plan.sections] == [
        "1. ionic conductivity", "2. energy density", "3. manufacturing cost",
    ]
    assert [s.page_break_before for s in plan.sections] == [False, True, True]


def test_per_page_plan_evidence_on_right_page():
    pkg = _build_pkg()
    specs = parse_page_specs(
        "page 1 ionic conductivity, page 2 energy density, page 3 manufacturing cost"
    )
    plan = build_per_page_plan(pkg, specs)
    texts = [" ".join((b.text or "") for b in s.blocks) for s in plan.sections]
    assert "ionic conductivity" in texts[0]
    assert "450 Wh/kg" in texts[1]
    assert "18 percent" in texts[2]
    # No claim duplicated across pages: each statement appears once.
    assert texts[0].count("ionic conductivity") >= 1
    assert "450 Wh/kg" not in texts[0]
    assert "ionic conductivity" not in texts[1]


def test_per_page_plan_no_evidence_page_says_so():
    pkg = _build_pkg()
    specs = parse_page_specs("page 1 ionic conductivity, page 2 recycling policy")
    plan = build_per_page_plan(pkg, specs)
    page2_text = " ".join((b.text or "") for b in plan.sections[1].blocks)
    assert "No evidence" in page2_text
    # Nothing invented: no claim statement appears on the empty page.
    assert "25 mS/cm" not in page2_text
    assert "450 Wh/kg" not in page2_text
    assert "18 percent" not in page2_text


def test_per_page_plan_image_on_matching_page_only():
    pkg = _build_pkg()
    with tempfile.TemporaryDirectory() as tmp:
        img_path = _local_image(Path(tmp))
        vis = VisualAsset(
            asset_id="vis_01", asset_type="photograph",
            title="Sulfide electrolyte conductivity chart",
            caption="Ionic conductivity of sulfide electrolytes at room temperature",
            url_or_path=img_path,
            source_url="https://nature.com/articles/battery",
            source_id="src_01", is_generated=False,
        )
        pkg.add_visual(vis)
        specs = parse_page_specs("page 1 ionic conductivity, page 2 energy density")
        plan = build_per_page_plan(pkg, specs)
        imgs_p1 = [b for b in plan.sections[0].blocks if b.block_type.value == "image"]
        imgs_p2 = [b for b in plan.sections[1].blocks if b.block_type.value == "image"]
        assert len(imgs_p1) == 1
        assert imgs_p1[0].visual_asset.asset_id == "vis_01"
        assert imgs_p2 == []


def test_per_page_plan_visual_matching_no_page_attached_nowhere():
    pkg = _build_pkg()
    with tempfile.TemporaryDirectory() as tmp:
        img_path = _local_image(Path(tmp))
        vis = VisualAsset(
            asset_id="vis_99", asset_type="photograph",
            title="Unrelated satellite photo",
            caption="A photo of a desert landscape",
            url_or_path=img_path,
            source_url="https://example.com/desert",
            source_id=None, is_generated=False,
        )
        pkg.add_visual(vis)
        specs = parse_page_specs("page 1 ionic conductivity, page 2 energy density")
        plan = build_per_page_plan(pkg, specs)
        all_imgs = [b for s in plan.sections for b in s.blocks if b.block_type.value == "image"]
        assert all_imgs == []


def test_per_page_plan_no_bibliography_section():
    pkg = _build_pkg()
    specs = parse_page_specs("page 1 ionic conductivity, page 2 energy density")
    plan = build_per_page_plan(pkg, specs)
    assert all(
        b.block_type.value != "bibliography"
        for s in plan.sections for b in s.blocks
    )


def test_build_structured_document_plan_delegates_to_per_page():
    pkg = _build_pkg()
    plan = build_structured_document_plan(
        pkg, target_format="pdf",
        instruction="I need 3 pages: page 1 ionic conductivity, "
        "page 2 energy density, page 3 manufacturing cost",
    )
    assert plan.is_per_page is True
    assert len(plan.sections) == 3
    assert plan.target_pages == 3


def test_build_structured_document_plan_unchanged_without_specs():
    pkg = _build_pkg()
    plan = build_structured_document_plan(
        pkg, target_format="pdf",
        instruction="Make a 5-page report on batteries",
    )
    assert plan.is_per_page is False
    # Fixed sections still apply (executive summary etc.), not per-page.
    assert len(plan.sections) >= 1
    assert plan.target_pages == 5


def test_per_page_mismatch_recorded_on_plan():
    pkg = _build_pkg()
    plan = build_structured_document_plan(
        pkg, target_format="pdf",
        instruction="I need 4 pages: page 1 ionic conductivity, page 2 energy density",
    )
    assert plan.is_per_page is True
    assert len(plan.sections) == 2
    assert plan.per_page_mismatch is not None
    assert "4 pages" in plan.per_page_mismatch
    assert "2 per-page briefs" in plan.per_page_mismatch


def test_per_page_plan_sources_paragraph_on_last_page():
    pkg = _build_pkg()
    specs = parse_page_specs("page 1 ionic conductivity, page 2 energy density")
    plan = build_per_page_plan(pkg, specs)
    # The last section carries a compact Sources paragraph resolving
    # the inline citation brackets to titles + URLs.
    last_text = " ".join((b.text or "") for b in plan.sections[-1].blocks)
    assert "Sources:" in last_text
    assert "nature.com" in last_text or "arxiv.org" in last_text
    # No bibliography section was added (exact page count preserved).
    assert all(
        b.block_type.value != "bibliography"
        for s in plan.sections for b in s.blocks
    )


# ---------------------------------------------------------------------------
# 3. PDF renderer: exact pages, each user page on its own PDF page
# ---------------------------------------------------------------------------

def test_per_page_pdf_exact_pages_and_mapping():
    pkg = _build_pkg()
    with tempfile.TemporaryDirectory() as tmp:
        img_path = _local_image(Path(tmp))
        pkg.add_visual(VisualAsset(
            asset_id="vis_01", asset_type="photograph",
            title="Sulfide electrolyte conductivity chart",
            caption="Ionic conductivity of sulfide electrolytes",
            url_or_path=img_path,
            source_url="https://nature.com/articles/battery",
            source_id="src_01", is_generated=False,
        ))
        out = str(Path(tmp) / "per_page.pdf")
        renderer = PdfRenderer()
        result = renderer.render(
            pkg, out,
            instruction="I need 3 pages: page 1 ionic conductivity, "
            "page 2 energy density, page 3 manufacturing cost",
        )
        assert Path(result).exists() and Path(result).stat().st_size > 0
        texts = _page_texts(result)
        assert len(texts) == 3, f"expected exactly 3 pages, got {len(texts)}"
        # Each user page lands on its own PDF page.
        assert "ionic conductivity" in texts[0].lower()
        assert "energy density" in texts[1].lower()
        assert "manufacturing cost" in texts[2].lower()
        # The renderer recorded no overflow.
        assert renderer.last_doc_plan.is_per_page is True
        assert renderer.last_doc_plan.per_page_overflow == []


def test_per_page_pdf_image_rendered_on_its_page():
    pkg = _build_pkg()
    with tempfile.TemporaryDirectory() as tmp:
        img_path = _local_image(Path(tmp))
        pkg.add_visual(VisualAsset(
            asset_id="vis_01", asset_type="photograph",
            title="Sulfide electrolyte conductivity chart",
            caption="Ionic conductivity of sulfide electrolytes",
            url_or_path=img_path,
            source_url="https://nature.com/articles/battery",
            source_id="src_01", is_generated=False,
        ))
        out = str(Path(tmp) / "per_page_img.pdf")
        renderer = PdfRenderer()
        renderer.render(
            pkg, out,
            instruction="I need 2 pages: page 1 ionic conductivity, page 2 energy density",
        )
        texts = _page_texts(out)
        assert len(texts) == 2
        # The image caption appears on page 1 (the matching page), not page 2.
        assert "ionic conductivity of sulfide" in texts[0].lower()
        assert "ionic conductivity of sulfide" not in texts[1].lower()


def test_per_page_pdf_overflow_recorded_honestly():
    pkg = _build_pkg()
    # A single page whose brief matches a claim with an enormous
    # statement cannot fit on one PDF page even at the deepest trim.
    long_statement = " ".join(["ionic conductivity measurement"] * 400)
    pkg.add_claim(ResearchClaim(
        claim_id="clm_long",
        statement=long_statement,
        classification=EvidenceClassification.SINGLE_SOURCE,
        claim_type="statistic", primary_source_id="src_01",
        confidence=0.9,
    ))
    with tempfile.TemporaryDirectory() as tmp:
        out = str(Path(tmp) / "per_page_overflow.pdf")
        renderer = PdfRenderer()
        renderer.render(
            pkg, out,
            instruction="I need 2 pages: page 1 ionic conductivity, page 2 energy density",
        )
        texts = _page_texts(out)
        # Honest best effort: the doc is longer than asked, and the
        # overflow is recorded on the plan instead of being hidden.
        assert len(texts) > 2
        assert renderer.last_doc_plan.per_page_overflow
        assert 1 in renderer.last_doc_plan.per_page_overflow


def test_per_page_pdf_no_false_overflow_when_body_mentions_next_title():
    # Review finding: a title phrase appearing in an earlier page's
    # body must not false-positive as overflow. Page 1's claim
    # mentions "energy density" (page 2's title), but the doc is
    # still exactly 2 pages with each section on its own page.
    pkg = _build_pkg()
    pkg.add_claim(ResearchClaim(
        claim_id="clm_x",
        statement="Ionic conductivity directly limits the energy density achievable in solid-state cells.",
        classification=EvidenceClassification.SINGLE_SOURCE,
        claim_type="general", primary_source_id="src_01",
        confidence=0.9,
    ))
    with tempfile.TemporaryDirectory() as tmp:
        out = str(Path(tmp) / "per_page_no_false_overflow.pdf")
        renderer = PdfRenderer()
        renderer.render(
            pkg, out,
            instruction="I need 2 pages: page 1 ionic conductivity, page 2 energy density",
        )
        texts = _page_texts(out)
        assert len(texts) == 2
        assert renderer.last_doc_plan.per_page_overflow == []
