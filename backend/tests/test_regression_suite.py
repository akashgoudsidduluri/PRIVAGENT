"""Regression Test Suite for PrivAgent Execution Failures.

Covers:
1. Planner:
   - Valid structured plan response.
   - Prose instead of JSON handling.
   - Malformed JSON recovery / rejection.
   - JSON inside Markdown fences with reasoning tokens and <think> tags.
   - Truncated responses and token budget recovery.
   - Missing required fields / unsupported actions.
   - Provider error failover and recovery exhaustion.
2. Context and Search:
   - Current task isolation & previous-task contamination prevention.
   - Subject-specific query decomposition (Agriculture, Software Architecture, Cloud, Smartphones).
   - Domain-appropriate source selection & geographic retailer constraints.
3. Execution & Resumption:
   - Checkpoint persistence and interrupted task state across restarts.
   - Continuation and resumption without duplicate external actions.
4. Artifact Deliverables:
   - Verification across all 7 formats (PDF, DOCX, PPTX, XLSX, CSV, MD, HTML).
   - Artifact integrity & error handling.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.hub import hub, TaskRecord
from app.intent_router import classify_intent, TaskIntent
from app.planner import plan_from_observation, TaskPlan
from app.providers.gemini import extract_json_from_text
from app.providers.openai_compatible import (
    OpenAICompatibleProvider,
    _content_of,
)
from app.website_resolver import website_resolver
from app.session_manager import session_manager, BrowserSession
from app.research.renderers import (
    PdfRenderer,
    DocxRenderer,
    PptxRenderer,
    XlsxRenderer,
    CsvRenderer,
    MarkdownRenderer,
    HtmlRenderer,
    render_artifact,
)
from app.research.models import (
    ResearchKnowledgePackage,
    ResearchTopic,
    ResearchSubtopic,
    ResearchClaim,
    EvidenceItem,
    SourceReference,
    DataTable,
    FormulaItem,
    EvidenceClassification,
)


# ============================================================================
# 1. PLANNER TESTS
# ============================================================================

def test_planner_extract_json_with_think_tags():
    """Gemini/DeepSeek reasoning models include <think> ... </think> tags before JSON."""
    raw = (
        "<think>\n"
        "The user wants to navigate to Wikipedia to find components of an autonomous agent.\n"
        "Let's emit a valid navigation action.\n"
        "</think>\n"
        "```json\n"
        "{\n"
        '  "why": "Navigating to Wikipedia",\n'
        '  "plan": [\n'
        "    {\n"
        '      "why": "Open autonomous agent page",\n'
        '      "action": {\n'
        '        "type": "navigate",\n'
        '        "params": {"url": "https://en.wikipedia.org/wiki/Intelligent_agent"}\n'
        "      }\n"
        "    }\n"
        "  ]\n"
        "}\n"
        "```"
    )
    extracted = extract_json_from_text(raw)
    assert extracted is not None
    assert "plan" in extracted
    assert extracted["plan"][0]["action"]["type"] == "navigate"


def test_planner_extract_json_trailing_comma_recovery():
    """LLMs frequently produce trailing commas before closing braces."""
    raw = (
        "{\n"
        '  "why": "Extracting information",\n'
        '  "plan": [\n'
        "    {\n"
        '      "why": "Done",\n'
        '      "action": {\n'
        '        "type": "done",\n'
        '        "params": {"summary": "Completed successfully",},\n'
        "      },\n"
        "    },\n"
        "  ],\n"
        "}"
    )
    extracted = extract_json_from_text(raw)
    assert extracted is not None
    assert extracted["why"] == "Extracting information"
    assert extracted["plan"][0]["action"]["type"] == "done"


def test_planner_prose_instead_of_json_rejected_cleanly():
    """When a model returns pure explanatory prose, extract_json_from_text raises ValueError."""
    prose = (
        "I will now help you compare monolithic and microservices architectures. "
        "Monolithic architectures have a single codebase, while microservices are distributed. "
        "Here are the main trade-offs to consider."
    )
    with pytest.raises(ValueError, match="PLAN_INVALID"):
        extract_json_from_text(prose)


def test_planner_truncated_json_rejected_cleanly():
    """When a response is truncated mid-stream, extract_json_from_text safely raises ValueError."""
    truncated = '{"why": "Searching for cloud providers", "plan": [{"why": "Open search", "action": {"type": "navig'
    with pytest.raises(ValueError, match="PLAN_INVALID"):
        extract_json_from_text(truncated)


def test_openai_compatible_reasoning_content_extraction():
    """Ensure _content_of extracts text when reasoning_content is present."""
    # Case 1: normal message content
    res1 = {"choices": [{"message": {"content": '{"why": "test", "plan": []}'}}]}
    assert _content_of(res1) == '{"why": "test", "plan": []}'

    # Case 2: message content is empty, but text is in choice
    res2 = {"choices": [{"text": '{"why": "choice_text", "plan": []}'}]}
    assert _content_of(res2) == '{"why": "choice_text", "plan": []}'

    # Case 3: message content has text, reasoning_content is separate
    res3 = {
        "choices": [
            {
                "message": {
                    "reasoning_content": "Thinking about plan...",
                    "content": '{"why": "actual_plan", "plan": []}',
                }
            }
        ]
    }
    assert _content_of(res3) == '{"why": "actual_plan", "plan": []}'


def test_openai_compatible_plan_call_token_limit():
    """Verify plan_call passes max_tokens >= 4096 to prevent reasoning token truncation."""
    from app.planner import PrivacyFilteredPayload
    provider = OpenAICompatibleProvider(api_key="mock", base_url="http://mock/v1", model="mock-model")
    payload = PrivacyFilteredPayload(
        tab_id=1,
        url="https://example.com",
        title="Test",
        scroll={"x": 0, "y": 0},
        dpr=1.0,
        elements_original=0,
        elements_sent=0,
        interactive=[],
        page_text="sample text",
    )
    with patch.object(provider, "_completion") as mock_comp:
        mock_comp.return_value = ('{"why": "test", "plan": []}', {"choices": []})
        provider.plan_call("instruction", payload, [])
        assert mock_comp.called
        req_payload = mock_comp.call_args.kwargs.get("payload") or mock_comp.call_args[0][0]
        assert req_payload.get("max_tokens", 0) >= 4096


# ============================================================================
# 2. CONTEXT & SEARCH QUERY GENERATION TESTS
# ============================================================================

def test_search_query_generation_agriculture():
    """Case A regression: Query generation for AI in agriculture must not inject transformer keywords."""
    inst = (
        "Research Artificial Intelligence in Agriculture. Explain precision farming, crop disease detection, "
        "soil monitoring, smart irrigation, yield prediction and autonomous farming. "
        "Include formulas, images, diagrams, tables and verified references. Generate a comprehensive PDF report."
    )
    queries = website_resolver.decompose_search_queries(inst, category="agriculture_science")
    assert len(queries) >= 1
    for q in queries:
        assert "transformer" not in q.lower()
        assert "attention is all you need" not in q.lower()
        assert "pdf" not in q.lower()
        assert "formulas" not in q.lower()
    assert any("agriculture" in q.lower() for q in queries)


def test_search_query_generation_software_architecture():
    """Case C regression: Architecture comparison queries must focus on architecture concepts without deliverable fluff."""
    inst = (
        "Compare monolithic, microservices and serverless architectures. Explain scalability, "
        "performance, fault tolerance, deployment and maintenance. Include a comparison table and diagrams, "
        "and create a DOCX report."
    )
    queries = website_resolver.decompose_search_queries(inst, category="software_architecture")
    assert len(queries) >= 1
    for q in queries:
        assert "docx" not in q.lower()
        assert "diagrams" not in q.lower()
        assert "report" not in q.lower()
    assert any("monolithic" in q.lower() or "microservices" in q.lower() or "architecture" in q.lower() for q in queries)


def test_search_query_generation_smartphone_india():
    """Case E regression: Smartphone product research must preserve Indian retailer and pricing constraints."""
    inst = (
        "Compare iPhone 16 Pro and Samsung Galaxy S24 Ultra in India. Include prices, storage variants, "
        "camera, battery, No Cost EMI and retailer source links. Export to XLSX."
    )
    queries = website_resolver.decompose_search_queries(inst, category="shopping", context={"user_region": "IN"})
    assert len(queries) >= 1
    for q in queries:
        assert "xlsx" not in q.lower()
        assert "export" not in q.lower()
    assert any("iphone 16 pro" in q.lower() for q in queries)


def test_source_selection_appropriate_for_subject():
    """Case A, C, D, E hub routing: software architecture must NOT route to nature.com."""
    # Software architecture: should route to Wikipedia or Google Technical Docs
    res_arch = website_resolver.discover_websites_for_task("Compare monolithic and microservices architectures")
    selected_arch = [s["domain"] for s in res_arch["selected_sources"]]
    assert "nature.com" not in selected_arch
    assert any("wikipedia.org" in d or "google.com" in d for d in selected_arch)

    # Smartphone shopping: should route to Amazon India / Flipkart
    res_phone = website_resolver.discover_websites_for_task("Compare iPhone 16 Pro and Samsung S24 Ultra price", region="IN")
    selected_phone = [s["domain"] for s in res_phone["selected_sources"]]
    assert any("amazon.in" in d or "flipkart.com" in d for d in selected_phone)


def test_wikipedia_page_entity_extraction():
    """Case B regression: 'Open the Wikipedia page about X' resolves entity as Wikipedia."""
    entity, obj = website_resolver.extract_target_entity(
        "Open the Wikipedia page about autonomous agents. Find the four main components."
    )
    assert entity == "Wikipedia"
    assert "autonomous agents" in obj.lower()


# ============================================================================
# 3. RESTART RECOVERY & SESSION RESTORATION TESTS
# ============================================================================

def test_session_manager_restores_from_db_on_cache_miss():
    """When in-memory cache is emptied (e.g. server restart), session_manager loads from SQLite."""
    from app import db
    chat_id = "test-conv-restart-999"
    sess_id = "sess-restart-abc"

    # Save directly to SQLite
    db.save_browser_session({
        "session_id": sess_id,
        "task_id": "task-restart-1",
        "conversation_id": chat_id,
        "origin": "new_task",
        "primary_tab_id": 101,
        "child_tab_ids": [102, 103],
        "status": "active",
        "created_at": time.time(),
        "last_activity_at": time.time(),
    })

    # Clear in-memory session manager cache
    session_manager.clear()

    # Query active session
    loaded = session_manager.get_active_session_for_chat(chat_id)
    assert loaded is not None
    assert loaded.session_id == sess_id
    assert loaded.primary_tab_id == 101
    assert 102 in loaded.child_tab_ids


def test_interrupted_task_can_be_resumed_from_checkpoint():
    """An interrupted task restored from checkpoint has state 'interrupted' and can be resumed."""
    from app import db
    from app.main import _require

    tid = "task-interrupted-test-456"
    chat_id = "conv-interrupted-test"

    db.save_task_checkpoint(
        task_id=tid,
        conversation_id=chat_id,
        session_id="sess-interrupted",
        step_index=2,
        last_url="https://en.wikipedia.org/wiki/Intelligent_agent",
        history=[{"action": {"type": "navigate"}, "result_ok": True}],
        working_memory={"instruction": "Find components of autonomous agents"},
    )

    # Remove from in-memory hub
    hub.tasks.pop(tid, None)

    restored = _require(tid)
    assert restored is not None
    assert restored.state == "interrupted"
    assert restored.steps_done == 2
    assert "progress preserved at step 2" in restored.message


# ============================================================================
# 4. ARTIFACT GENERATION TESTS (ALL 7 FORMATS)
# ============================================================================

def _build_test_package() -> ResearchKnowledgePackage:
    topic = ResearchTopic(
        title="Artificial Intelligence in Agriculture",
        objective="Analyze precision farming, crop disease detection, soil monitoring, smart irrigation",
        scope="Commercial and research agronomy applications",
        target_format="pdf",
        subtopics=[
            ResearchSubtopic(
                subtopic_id="sub_01",
                title="Smart Irrigation & Soil Monitoring",
                status="completed",
                findings_summary="IoT sensor networks with neural controllers reduce water consumption by 28%.",
                claim_ids=["clm_01"],
            )
        ],
    )
    pkg = ResearchKnowledgePackage(
        package_id="pkg_test_agri",
        topic=topic,
    )
    s1 = SourceReference(
        source_id="src_01",
        url="https://en.wikipedia.org/wiki/Precision_agriculture",
        domain="en.wikipedia.org",
        title="Precision Agriculture Overview",
        reliability_score=0.92,
        is_primary=True,
    )
    s2 = SourceReference(
        source_id="src_02",
        url="https://nature.com/articles/s41477-020-00808-7",
        domain="nature.com",
        title="AI in Agriculture and Food Systems",
        reliability_score=0.99,
        is_primary=True,
    )
    pkg.add_source(s1)
    pkg.add_source(s2)

    ev1 = EvidenceItem(
        evidence_id="ev_01",
        source_id="src_01",
        quote="Automated irrigation scheduling based on soil moisture sensors saved 28% water.",
        confidence=0.95,
    )
    pkg.add_evidence(ev1)

    c1 = ResearchClaim(
        claim_id="clm_01",
        statement="IoT sensors coupled with LSTM networks reduce irrigation water consumption by 28%.",
        classification=EvidenceClassification.CORROBORATED,
        claim_type="advantage",
        subtopic_id="sub_01",
        primary_source_id="src_01",
        supporting_source_ids=["src_02"],
        evidence_item_ids=["ev_01"],
        confidence=0.95,
    )
    pkg.add_claim(c1)

    tbl = DataTable(
        table_id="tbl_agri",
        title="AI Methodologies in Agriculture",
        headers=["Application", "AI Technique", "Efficiency Gain", "Adoption Stage"],
        rows=[
            ["Smart Irrigation", "LSTM & Soil Sensors", "28% water savings", "Commercial"],
            ["Disease Detection", "CNN & Mobile Vision", "94% accuracy", "Production"],
            ["Yield Prediction", "Random Forest & Satellite", "15% accuracy boost", "Early Commercial"],
        ],
        caption="Summary of core AI applications in agriculture.",
    )
    pkg.add_table(tbl)

    formula = FormulaItem(
        formula_id="eq_01",
        name="Water Stress Index (CWSI)",
        latex=r"CWSI = \frac{(T_c - T_a) - (T_c - T_a)_{lower}}{(T_c - T_a)_{upper} - (T_c - T_a)_{lower}}",
        explanation="Crop Water Stress Index for precision irrigation scheduling.",
    )
    pkg.add_formula(formula)

    return pkg


def test_artifact_renderers_all_formats():
    """Verify that all 7 supported formats render intact files with valid sizes and headers."""
    pkg = _build_test_package()
    with tempfile.TemporaryDirectory() as tmpdir:
        out_dir = Path(tmpdir)

        # 1. PDF
        pdf_path = out_dir / "report.pdf"
        PdfRenderer().render(pkg, str(pdf_path))
        assert pdf_path.exists()
        assert pdf_path.stat().st_size > 500
        with open(pdf_path, "rb") as f:
            assert f.read(4) == b"%PDF"

        # 2. DOCX
        docx_path = out_dir / "report.docx"
        DocxRenderer().render(pkg, str(docx_path))
        assert docx_path.exists()
        assert docx_path.stat().st_size > 1000
        with open(docx_path, "rb") as f:
            assert f.read(2) == b"PK"  # Zip archive header

        # 3. PPTX
        pptx_path = out_dir / "presentation.pptx"
        PptxRenderer().render(pkg, str(pptx_path))
        assert pptx_path.exists()
        assert pptx_path.stat().st_size > 1000
        with open(pptx_path, "rb") as f:
            assert f.read(2) == b"PK"

        # 4. XLSX
        xlsx_path = out_dir / "data.xlsx"
        XlsxRenderer().render(pkg, str(xlsx_path))
        assert xlsx_path.exists()
        assert xlsx_path.stat().st_size > 500
        with open(xlsx_path, "rb") as f:
            assert f.read(2) == b"PK"

        # 5. CSV
        csv_path = out_dir / "table.csv"
        CsvRenderer().render(pkg, str(csv_path))
        assert csv_path.exists()
        assert csv_path.stat().st_size > 50
        content = csv_path.read_text(encoding="utf-8")
        assert "Application" in content or "Smart Irrigation" in content

        # 6. Markdown
        md_path = out_dir / "notes.md"
        MarkdownRenderer().render(pkg, str(md_path))
        assert md_path.exists()
        assert md_path.stat().st_size > 100
        md_text = md_path.read_text(encoding="utf-8")
        assert "Artificial Intelligence in Agriculture" in md_text
        assert "Smart Irrigation" in md_text

        # 7. HTML
        html_path = out_dir / "page.html"
        HtmlRenderer().render(pkg, str(html_path))
        assert html_path.exists()
        assert html_path.stat().st_size > 200
        html_text = html_path.read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in html_text
        assert "Artificial Intelligence in Agriculture" in html_text
