"""Tests for Chat Memory, Anaphora Resolution, and Clean Search Query Decomposition.

Verifies:
1. Conversational follow-ups inherit topic and context across turns.
2. Search query decomposition strips conversational fluff, deliverable formats,
   formulas/diagrams clauses, and produces concise, high-precision search queries.
3. Chat memory extraction and persistence stores research topics and deliverable types.
4. 5-slide PDF presentation formatting renders clean slide sections.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from app.intent_router import (
    IntentClassification,
    TaskIntent,
    classify_intent,
    detect_deliverable_type,
    detect_requested_format,
    extract_research_topic,
    resolve_topic_from_context,
)
from app.research.models import (
    EvidenceClassification,
    EvidenceItem,
    FormulaItem,
    ResearchClaim,
    ResearchKnowledgePackage,
    ResearchSubtopic,
    ResearchTopic,
    SourceReference,
)
from app.research.renderers.pdf_renderer import PdfRenderer
from app.website_resolver import website_resolver


def test_follow_up_topic_resolution_and_inheritance():
    """Verify follow-up turns inherit and augment the research topic from context."""
    prior_topic = "transformer architecture"
    ctx = {
        "research_topic": prior_topic,
        "is_research": True,
        "requested_format": "pdf",
        "deliverable_type": "report",
        "conversation_history": [
            {
                "role": "user",
                "content": "Research transformer architecture, explain self-attention, multi-head attention, encoder-only and decoder-only architectures with equations and diagrams, and generate a detailed PDF report.",
            },
            {
                "role": "agent",
                "content": "Autonomous research on transformer architecture completed. Deliverable Ready: [transformer_report.pdf]",
            },
        ],
    }

    # Turn 2: User prompt with conversational fluff and no explicit topic
    prompt_turn2 = "could u give more detailed pdf of 5 slides it should include formulas and diagrams too and also research different platforms"
    cl = classify_intent(prompt_turn2, context=ctx)

    assert cl.intent == TaskIntent.RESEARCH_TASK
    assert cl.is_research is True
    assert cl.requested_format == "pdf"
    assert cl.deliverable_type == "presentation"
    # Topic should inherit prior_topic and append platforms
    assert "transformer architecture" in cl.research_topic.lower()
    assert "platform" in cl.research_topic.lower()
    assert "could u give" not in cl.research_topic.lower()
    assert "5 slides" not in cl.research_topic.lower()


def test_pure_follow_up_formatting_without_new_subdomain():
    """Verify follow-up requesting only format changes inherits exact prior topic."""
    ctx = {
        "research_topic": "solid-state batteries",
        "is_research": True,
        "requested_format": "docx",
        "deliverable_type": "report",
    }

    prompt = "could you please prepare a presentation of 5 slides on this instead"
    cl = classify_intent(prompt, context=ctx)

    assert cl.intent == TaskIntent.RESEARCH_TASK
    assert cl.requested_format == "pptx"
    assert cl.deliverable_type == "presentation"
    assert cl.research_topic == "solid-state batteries"


def test_search_query_decomposition_strips_conversational_fluff():
    """Verify decompose_search_queries produces concise, high-precision search queries."""
    # Turn 1 verbose prompt
    p1 = "Research transformer architecture, explain self-attention, multi-head attention, encoder-only and decoder-only architectures with equations and diagrams, and generate a detailed PDF report."
    queries1 = website_resolver.decompose_search_queries(p1, category="research")
    assert len(queries1) >= 1
    for q in queries1:
        # Search query must NOT contain formatting requests or deliverable demands
        assert "generate a detailed pdf report" not in q.lower()
        assert "with equations and diagrams" not in q.lower()
        assert len(q.split()) <= 14

    # Turn 2 follow-up prompt with context
    p2 = "could u give more detailed pdf of 5 slides it should include formulas and diagrams too and also research different platforms"
    ctx = {"research_topic": "transformer architecture"}
    queries2 = website_resolver.decompose_search_queries(p2, category="research", context=ctx)
    assert len(queries2) >= 1
    for q in queries2:
        assert "could u give" not in q.lower()
        assert "5 slides" not in q.lower()
        assert "diagrams too" not in q.lower()
        assert "transformer architecture" in q.lower()
        assert "platform" in q.lower()


def test_chat_memory_extraction_and_persistence(tmp_path):
    """Verify extract_mission_facts and db chat_memory persist research metadata."""
    from app import db
    from app.agentLoop import extract_mission_facts

    db_file = str(tmp_path / "test_chat_mem.db")
    db.init_db(db_file)
    chat = db.create_chat(title="Research Test", db_path=db_file)
    chat_id = chat["id"]

    instruction = "Research transformer architecture with diagrams in PDF"
    summary = "Research completed. 📄 **Deliverable Ready:** [transformer_arch.pdf] (PDF, 89.2 KB). Available for download."

    facts = extract_mission_facts(instruction, summary)
    assert "research_topic" in facts or "transformer" in facts.get("target_item", "").lower()
    assert facts.get("requested_format") == "pdf"
    assert facts.get("last_deliverable") == "transformer_arch.pdf"

    for k, v in facts.items():
        db.set_chat_memory(chat_id, k, v, db_path=db_file)

    loaded_mem = db.get_chat_memory(chat_id, db_path=db_file)
    assert loaded_mem.get("requested_format") == "pdf"
    assert loaded_mem.get("last_deliverable") == "transformer_arch.pdf"


def test_pdf_renderer_presentation_5_slides(tmp_path):
    """Verify PDF renderer produces multi-slide presentation layout when requested."""
    pkg = ResearchKnowledgePackage(
        package_id="pkg_slides_test",
        topic=ResearchTopic(
            title="Transformer Architecture & Frameworks",
            objective="Provide a detailed 5-slide technical presentation with formulas and diagrams.",
            scope="Deep learning sequence-to-sequence and decoder-only models.",
            target_format="pdf",
        ),
    )

    src = SourceReference(
        source_id="src_vaswani",
        url="https://arxiv.org/abs/1706.03762",
        title="Attention Is All You Need",
        domain="arxiv.org",
        reliability_score=0.99,
    )
    src2 = SourceReference(
        source_id="src_hf",
        url="https://huggingface.co/docs/transformers",
        title="Hugging Face Transformers Documentation",
        domain="huggingface.co",
        reliability_score=0.96,
    )
    pkg.add_source(src)
    pkg.add_source(src2)

    c1 = ResearchClaim(
        claim_id="cl_attn",
        statement="Scaled dot-product self-attention maps queries and key-value pairs with O(N^2) complexity.",
        claim_type="architecture",
        confidence=0.95,
        primary_source_id="src_vaswani",
        supporting_source_ids=["src_hf"],
        classification=EvidenceClassification.CORROBORATED,
    )
    pkg.add_claim(c1)

    f1 = FormulaItem(
        formula_id="eq_attn",
        name="Scaled Dot-Product Attention",
        latex=r"\text{Attention}(Q, K, V) = \text{softmax}\left(\frac{QK^T}{\sqrt{d_k}}\right)V",
        explanation="Attention calculation using query and key scaling.",
    )
    pkg.add_formula(f1)

    renderer = PdfRenderer()
    pdf_out = tmp_path / "presentation_5_slides.pdf"
    res_path = renderer.render(pkg, str(pdf_out), deliverable_type="presentation", instruction="5 slides in pdf")

    assert Path(res_path).exists()
    assert os.path.getsize(res_path) > 3000
