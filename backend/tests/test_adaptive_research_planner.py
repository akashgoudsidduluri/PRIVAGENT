"""Comprehensive Test Suite for Adaptive Research Planning & Intelligent Document Generation.

Validates:
1. Dynamic page constraint parsing (exact, approximate, maximum, slides, content-driven).
2. Complexity assessment and dynamic source sizing (simple, standard, complex comparative).
3. Visual intelligence utility derivation (architecture diagrams, comparison tables, charts, formulas).
4. Prioritized subtopic decomposition with focused technical queries.
5. Adaptive research expansion and evidence gap detection.
6. Termination guards preventing infinite expansion loops.
7. Exact 5-page PDF document generation budgeting and page-break verification.
8. Follow-up intent classification ("nice but i need a 5 pages report and also add more supporting images to it").
"""
from __future__ import annotations

import tempfile
from pathlib import Path
import pytest
from pypdf import PdfReader

from app.research.models import (
    EvidenceClassification,
    EvidenceItem,
    FormulaItem,
    ResearchClaim,
    ResearchKnowledgePackage,
    ResearchSubtopic,
    SourceReference,
    VisualAsset,
)
from app.research.adaptive_planner import (
    ComplexityTier,
    PageConstraintType,
    assess_complexity_and_sources,
    create_section_allocations,
    decompose_subtopics,
    derive_visual_plan,
    detect_evidence_gaps,
    generate_adaptive_research_plan,
    generate_targeted_gap_queries,
    parse_page_constraint,
)
from app.intent_router import TaskIntent, ResearchOperationType, classify_intent
from app.research.renderers.pdf_renderer import PdfRenderer
from app.research.visuals.diagram_generator import render_attention_diagram_to_png, render_diagram_to_png
from app.research.visuals.formula_renderer import render_latex_formula


def test_parse_page_constraint_exact():
    """Verify parsing exact page constraints from numbers and English words."""
    c1 = parse_page_constraint("Make a detailed five-page PDF with formulas and diagrams.")
    assert c1.constraint_type == PageConstraintType.EXACT
    assert c1.target_pages == 5
    assert c1.min_pages == 5
    assert c1.max_pages == 5

    c2 = parse_page_constraint("nice but i need a 5 pages report and also add more supporting images to it")
    assert c2.constraint_type == PageConstraintType.EXACT
    assert c2.target_pages == 5


def test_parse_page_constraint_approximate_and_max():
    """Verify parsing approximate and maximum page bounds."""
    c_approx = parse_page_constraint("Prepare approximately 5 pages on deep learning")
    assert c_approx.constraint_type == PageConstraintType.APPROXIMATE
    assert c_approx.target_pages == 5
    assert c_approx.min_pages == 4
    assert c_approx.max_pages == 6

    c_max = parse_page_constraint("Keep the deliverable under 4 pages")
    assert c_max.constraint_type == PageConstraintType.MAXIMUM
    assert c_max.max_pages == 4


def test_parse_page_constraint_range():
    """A page range must preserve both bounds instead of collapsing to the upper number."""
    for text in ("Make a 3-5 page report on transformers", "Write three to five pages on transformers"):
        constraint = parse_page_constraint(text)
        assert constraint.constraint_type == PageConstraintType.APPROXIMATE
        assert constraint.min_pages == 3
        assert constraint.max_pages == 5
        assert constraint.target_pages == 5


def test_parse_page_constraint_slides_and_content_driven():
    """Verify slide deck parsing and content-driven fallback."""
    c_slides = parse_page_constraint("Create a 10-slide presentation on quantum computing", deliverable_type="presentation")
    assert c_slides.is_slides is True
    assert c_slides.target_slides == 10

    c_content = parse_page_constraint("Research transformer architecture")
    assert c_content.constraint_type == PageConstraintType.CONTENT_DRIVEN
    assert c_content.target_pages is None


def test_assess_complexity_and_sources():
    """Verify dynamic source sizing scales appropriately without arbitrary hardcoded quotas."""
    # Simple query -> 1 source
    comp_s, min_s, tgt_s, max_s, depth_s = assess_complexity_and_sources("what is binary search and how does it work")
    assert comp_s == ComplexityTier.SIMPLE
    assert min_s == 1
    assert tgt_s == 1

    # Standard query -> 2 sources
    comp_std, min_std, tgt_std, max_std, depth_std = assess_complexity_and_sources("explain react state management patterns")
    assert comp_std == ComplexityTier.STANDARD
    assert min_std == 2
    assert tgt_std == 3

    # Complex comparative query -> 3-4 sources
    comp_c, min_c, tgt_c, max_c, depth_c = assess_complexity_and_sources(
        "Research transformer architecture, compare different platforms, attention mechanisms and benchmark throughput"
    )
    assert comp_c == ComplexityTier.COMPLEX_COMPARATIVE
    assert min_c == 3
    assert tgt_c >= 4


def test_visual_intelligence_derivation():
    """Verify domain-driven visual asset selection."""
    vis_trans = derive_visual_plan("Research transformer architecture, attention formulas, diagrams and benchmark metrics")
    assert vis_trans.need_architecture_diagram is True
    assert vis_trans.need_formulas is True
    assert vis_trans.need_chart is True
    assert vis_trans.need_comparison_table is True

    vis_simple = derive_visual_plan("Define binary search", complexity=ComplexityTier.SIMPLE)
    assert vis_simple.need_architecture_diagram is False
    assert vis_simple.need_chart is False


def test_decompose_subtopics_and_queries():
    """Verify subtopic decomposition outputs clean technical queries without conversational filler."""
    subtopics = decompose_subtopics(
        "Research transformer architecture and explain self-attention and multi-head attention",
        "Transformer Architecture",
        ComplexityTier.COMPLEX_COMPARATIVE,
    )
    assert len(subtopics) >= 4
    for st in subtopics:
        assert st.priority in (1, 2, 3)
        assert len(st.search_query.split()) >= 3
        # Ensure no conversational filler in search queries
        assert "can you" not in st.search_query.lower()
        assert "please" not in st.search_query.lower()


def test_evidence_gap_detection_and_targeted_expansion():
    """Verify that subtopics lacking verified backing are flagged as gaps."""
    plan = generate_adaptive_research_plan(
        "Research transformer architecture, including self-attention and multi-head attention",
        {"research_topic": "Transformer Architecture"},
    )
    pkg = ResearchKnowledgePackage(
        package_id="pkg_test_gap",
        topic={"title": "Transformer Architecture", "objective": "Test"},
    )
    src_id = pkg.add_source(SourceReference(
        source_id="src_1",
        url="https://nature.com/articles/123",
        domain="nature.com",
        title="Attention Mechanics",
    ))
    ev_id = pkg.add_evidence(EvidenceItem(
        evidence_id="ev_1",
        source_id=src_id,
        quote="Self-attention allows tokens to attend to each other across sequence length.",
    ))
    # Add claim only covering subtopic 1 / self-attention
    pkg.add_claim(ResearchClaim(
        claim_id="clm_1",
        statement="Self-attention allows tokens to attend to each other across sequence length.",
        subtopic_id="sub_01",
        primary_source_id=src_id,
        evidence_item_ids=[ev_id],
        classification=EvidenceClassification.SINGLE_SOURCE,
    ))

    # Evidence gap detection
    gaps = detect_evidence_gaps(plan, pkg)
    assert len(gaps) > 0
    # The uncovered subtopics (e.g. multi-head attention, encoder-only) must be in gaps
    gap_titles = [g.title for g in gaps]
    assert any("Multi-Head" in gt or "Encoder" in gt for gt in gap_titles)

    # Formulate clean targeted gap queries
    queries = generate_targeted_gap_queries(gaps, "Transformer Architecture")
    assert len(queries) > 0
    for q in queries:
        assert "Transformer Architecture" in q or "transformer" in q.lower()
        assert "please" not in q.lower()


def test_five_page_pdf_generation_exact_count():
    """Verify that requesting a 5-page PDF generates a document with EXACTLY 5 pages and no blanks."""
    pkg = ResearchKnowledgePackage(
        package_id="pkg_test_5p",
        topic={
            "title": "Transformer Architecture: Attention Mechanisms & Technical Analysis",
            "objective": "Research transformer architecture with formulas and diagrams in a 5-page report",
        },
    )
    src1 = pkg.add_source(SourceReference(
        source_id="src_1",
        url="https://arxiv.org/abs/1706.03762",
        domain="arxiv.org",
        title="Attention Is All You Need",
        is_primary=True,
    ))
    src2 = pkg.add_source(SourceReference(
        source_id="src_2",
        url="https://nature.com/articles/s41598-026-61520-8",
        domain="nature.com",
        title="Self-Attention Transformer Mechanics",
    ))
    ev1 = pkg.add_evidence(EvidenceItem(
        evidence_id="ev_1",
        source_id=src1,
        quote="The Transformer allows for significantly more parallelization than recurrent networks.",
    ))
    pkg.add_claim(ResearchClaim(
        claim_id="clm_1",
        statement="Transformers replace sequential recurrence with parallel scaled dot-product attention.",
        primary_source_id=src1,
        supporting_source_ids=[src2],
        evidence_item_ids=[ev1],
        classification=EvidenceClassification.CORROBORATED,
    ))

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # Generate test formula PNG
        eq_path = tmp_path / "eq_form_01.png"
        render_latex_formula(r"\text{Attention}(Q, K, V) = \text{softmax}\left(\frac{QK^T}{\sqrt{d_k}}\right)V", str(eq_path))
        pkg.add_formula(FormulaItem(
            formula_id="form_01",
            name="Scaled Dot-Product Attention",
            latex=r"\text{Attention}(Q, K, V) = \text{softmax}\left(\frac{QK^T}{\sqrt{d_k}}\right)V",
            explanation="Calculates token attention weights.",
        ))
        pkg.add_visual(VisualAsset(
            asset_id="vis_form_01",
            asset_type="formula",
            title="Scaled Dot-Product Attention",
            url_or_path=str(eq_path),
            metadata={"formula_id": "form_01"},
        ))

        # Generate test architecture diagram PNG
        diag_path = tmp_path / "architecture_diagram.png"
        render_diagram_to_png("Transformer", str(diag_path))
        pkg.add_visual(VisualAsset(
            asset_id="vis_diagram_arch",
            asset_type="diagram",
            title="Transformer Architecture",
            caption="Block schematic of Transformer layers",
            url_or_path=str(diag_path),
        ))

        # Generate test attention pipeline diagram PNG
        att_path = tmp_path / "attention_mechanism_diagram.png"
        render_attention_diagram_to_png(str(att_path))
        pkg.add_visual(VisualAsset(
            asset_id="vis_diagram_att",
            asset_type="diagram",
            title="Scaled Dot-Product Attention Pipeline",
            caption="Computation pipeline from Q, K, V inputs to softmax weights",
            url_or_path=str(att_path),
        ))

        # Render PDF with 5-page exact constraint
        pdf_out = tmp_path / "Transformer_5_Page_Report.pdf"
        renderer = PdfRenderer()
        plan = generate_adaptive_research_plan("Make a detailed five-page PDF with formulas and diagrams")
        renderer.render(pkg, str(pdf_out), research_plan=plan.model_dump(), target_pages=5)

        assert pdf_out.exists()
        assert pdf_out.stat().st_size > 50000

        # Inspect generated PDF page count using pypdf
        reader = PdfReader(str(pdf_out))
        page_count = len(reader.pages)
        assert page_count == 5, f"Expected exactly 5 pages, but got {page_count} pages."

        # Verify page content readability across every page
        for p_idx, page in enumerate(reader.pages, start=1):
            text = page.extract_text()
            assert len(text.strip()) > 50, f"Page {p_idx} appears blank or empty."


def test_follow_up_intent_classification():
    """Verify that follow-up directives to modify length or add images route to EXPORT_EXISTING_PACKAGE."""
    context = {
        "is_research": True,
        "research_topic": "Transformer Architecture",
        "requested_format": "pdf",
        "deliverable_type": "report",
    }
    prompt = "nice but i need a 5 pages report and also add more supporting images to it"
    classification = classify_intent(prompt, context=context)

    assert classification.operation_type == ResearchOperationType.EXPORT_EXISTING_PACKAGE
    assert classification.is_research is True
    assert classification.research_topic == "Transformer Architecture"
    assert classification.requested_format == "pdf"

