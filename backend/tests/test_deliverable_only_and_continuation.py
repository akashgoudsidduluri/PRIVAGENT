"""Regression test suite for deliverable-only execution, continuation handling,
multi-package context resolution, and research refinement.

Verifies:
1. Exact reproduced inputs:
   - "continue"
   - "bro generate a pdf man"
   - "export the previous analysis as a pdf"
   - "generate a pdf"
   - "cant you give this content in a docx file"
2. Tested across 6 chat states:
   - One completed research package
   - Multiple research packages (topic match, recency anchor, ambiguity clarification)
   - No research package
   - Interrupted research checkpoint
   - Failed research checkpoint
   - Existing artifact
3. Critical assertion: deliverable-only requests generate valid artifacts without browser navigation
4. Unrelated new research starts a fresh mission and does not inherit old completion notes
5. Research refinement preserves existing evidence and package content
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from app import db
from app.hub import TaskRecord
from app.intent_router import (
    IntentClassification,
    ResearchOperationType,
    TaskIntent,
    classify_intent,
)
from app.research.models import (
    DataTable,
    EvidenceClassification,
    EvidenceItem,
    FormulaItem,
    ResearchClaim,
    ResearchKnowledgePackage,
    ResearchTopic,
    SourceReference,
)
from app.research.package_resolver import resolve_target_package


def _build_test_package(package_id: str, topic_title: str) -> ResearchKnowledgePackage:
    pkg = ResearchKnowledgePackage(
        package_id=package_id,
        topic=ResearchTopic(
            title=topic_title,
            objective=f"Provide a comprehensive technical analysis on {topic_title}.",
            scope="Deep learning and foundation model architectures.",
            target_format="pdf",
        ),
    )
    src1 = SourceReference(
        source_id="src_vaswani",
        url="https://arxiv.org/abs/1706.03762",
        title="Attention Is All You Need",
        domain="arxiv.org",
        reliability_score=0.99,
    )
    src2 = SourceReference(
        source_id="src_hf",
        url="https://huggingface.co/docs/transformers",
        title="Transformers Documentation",
        domain="huggingface.co",
        reliability_score=0.96,
    )
    pkg.add_source(src1)
    pkg.add_source(src2)

    ev1 = EvidenceItem(
        evidence_id="ev_001",
        source_id="src_vaswani",
        quote="The Transformer allows for significantly more parallelization.",
        confidence=0.98,
    )
    pkg.add_evidence(ev1)

    clm1 = ResearchClaim(
        claim_id="clm_001",
        statement="Transformer self-attention replaces recurrence and enables O(1) sequential operations.",
        classification=EvidenceClassification.CORROBORATED,
        claim_type="architecture",
        primary_source_id="src_vaswani",
        supporting_source_ids=["src_hf"],
        confidence=0.96,
    )
    pkg.add_claim(clm1)

    tbl = DataTable(
        table_id="tbl_001",
        title="Architecture Comparison",
        headers=["Type", "Attention", "Primary Use Case"],
        rows=[
            ["Encoder-only", "Bidirectional", "Classification / Embeddings (BERT)"],
            ["Decoder-only", "Causal / Autoregressive", "Generation (GPT, Llama)"],
            ["Encoder-decoder", "Cross-attention", "Translation / Summarization (T5)"],
        ],
    )
    pkg.add_table(tbl)

    form = FormulaItem(
        formula_id="eq_001",
        name="Scaled Dot-Product Attention",
        latex=r"\text{Attention}(Q, K, V) = \text{softmax}\left(\frac{QK^T}{\sqrt{d_k}}\right)V",
        explanation="Attention calculation using query and key scaling.",
    )
    pkg.add_formula(form)
    return pkg


# ---------------------------------------------------------------------------
# Test 1: Single Completed Research Package -> Deliverable-Only Execution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reproduced_inputs_single_completed_package(tmp_path):
    """Verify exact reproduced inputs execute deliverable-only export with 0 browser navigation."""
    from app.agentLoop import run_task, real_hub

    temp_db = str(tmp_path / "test_single.db")
    db.init_db(db_path=temp_db)

    chat = db.create_chat(title="Transformer Mission", db_path=temp_db)
    chat_id = chat["id"]

    pkg = _build_test_package("pkg_transformer_01", "Transformer Architecture")
    db.save_research_package(pkg.to_dict(), chat_id, "task_initial", status="completed", db_path=temp_db)
    db.set_chat_memory(chat_id, "research_topic", "Transformer Architecture", db_path=temp_db)

    reproduced_inputs = [
        ("bro generate a pdf man", "pdf"),
        ("export the previous analysis as a pdf", "pdf"),
        ("generate a pdf", "pdf"),
        ("cant you give this content in a docx file", "docx"),
    ]

    for instruction, expected_fmt in reproduced_inputs:
        # Intent classification assertion
        ctx = {"is_research": True, "research_topic": "Transformer Architecture"}
        cl = classify_intent(instruction, context=ctx)
        assert cl.operation_type == ResearchOperationType.EXPORT_EXISTING_PACKAGE
        assert cl.is_browser is False
        assert cl.requested_format == expected_fmt

        # Package resolution assertion
        res = resolve_target_package(chat_id, instruction, context=ctx, requested_format=expected_fmt, db_path=temp_db)
        assert res.package is not None
        assert res.is_ambiguous is False
        assert res.package["package_id"] == "pkg_transformer_01"

        # Execution assertion (deliverable-only, zero browser sockets required)
        task_id = f"task_{abs(hash(instruction))}"
        record = TaskRecord(
            task_id=task_id,
            instruction=instruction,
            state="created",
            chat_id=chat_id,
        )
        real_hub.tasks[task_id] = record

        # Run task with mock DB path
        old_init_db = db.init_db
        try:
            db.init_db = lambda *args, **kwargs: None
            # Override connection db_path for test isolation
            original_get_conn = db.get_connection
            db.get_connection = lambda p=None: original_get_conn(temp_db)

            await run_task(task_id)

            assert record.state == "completed"
            assert "Deliverable Ready:" in record.message
            assert f".{expected_fmt}" in record.message.lower()

            # Check artifact was persisted in DB
            arts = db.list_task_artifacts(task_id, db_path=temp_db)
            assert len(arts) >= 1
            art = arts[0]
            assert art["file_format"] == expected_fmt
            assert Path(art["filepath"]).exists()
            assert art["file_size_bytes"] > 0
        finally:
            db.get_connection = original_get_conn
            db.init_db = old_init_db


# ---------------------------------------------------------------------------
# Test 2: Multiple Research Packages in One Chat (Disambiguation)
# ---------------------------------------------------------------------------

def test_multiple_packages_disambiguation(tmp_path):
    """Verify package resolution distinguishes specific topics, recency anchors, and ambiguous prompts."""
    temp_db = str(tmp_path / "test_multi.db")
    db.init_db(db_path=temp_db)

    chat = db.create_chat(title="Multi Research Mission", db_path=temp_db)
    chat_id = chat["id"]

    pkg1 = _build_test_package("pkg_battery_01", "Solid-State Batteries")
    pkg2 = _build_test_package("pkg_transformer_02", "Transformer Architecture")

    db.save_research_package(pkg1.to_dict(), chat_id, "task_01", status="completed", db_path=temp_db)
    import time
    time.sleep(0.05)
    db.save_research_package(pkg2.to_dict(), chat_id, "task_02", status="completed", db_path=temp_db)

    # 1. Topic-specific keyword: battery
    res_battery = resolve_target_package(chat_id, "export the battery analysis as a pdf", requested_format="pdf", db_path=temp_db)
    assert res_battery.is_ambiguous is False
    assert res_battery.package["package_id"] == "pkg_battery_01"

    # 2. Topic-specific keyword: transformer
    res_transformer = resolve_target_package(chat_id, "cant you give the transformer content in a docx file", requested_format="docx", db_path=temp_db)
    assert res_transformer.is_ambiguous is False
    assert res_transformer.package["package_id"] == "pkg_transformer_02"

    # 3. Recency anchor: "previous analysis" -> points to the most recent package (Transformer)
    res_recency = resolve_target_package(chat_id, "export the previous analysis as a pdf", requested_format="pdf", db_path=temp_db)
    assert res_recency.is_ambiguous is False
    assert res_recency.package["package_id"] == "pkg_transformer_02"

    # 4. Ambiguous generic request: "generate a pdf" -> asks clarification listing both packages
    res_ambig = resolve_target_package(chat_id, "generate a pdf", requested_format="pdf", db_path=temp_db)
    assert res_ambig.is_ambiguous is True
    assert res_ambig.clarification_message is not None
    assert "Solid-State Batteries" in res_ambig.clarification_message
    assert "Transformer Architecture" in res_ambig.clarification_message


# ---------------------------------------------------------------------------
# Test 3: Chat with No Research Package
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_export_request_with_no_package_in_chat(tmp_path):
    """Verify export requests in a fresh chat gracefully explain that no package exists without browser error."""
    from app.agentLoop import run_task, real_hub

    temp_db = str(tmp_path / "test_no_pkg.db")
    db.init_db(db_path=temp_db)

    chat = db.create_chat(title="Empty Chat", db_path=temp_db)
    chat_id = chat["id"]

    task_id = "task_empty_export"
    record = TaskRecord(
        task_id=task_id,
        instruction="bro generate a pdf man",
        state="created",
        chat_id=chat_id,
    )
    real_hub.tasks[task_id] = record

    original_get_conn = db.get_connection
    original_init_db = db.init_db
    try:
        db.init_db = lambda *args, **kwargs: None
        db.get_connection = lambda p=None: original_get_conn(temp_db)

        await run_task(task_id)

        assert record.state == "completed"
        assert "No prior research findings or package found" in record.message
        # Verify 0 artifacts were generated
        arts = db.list_task_artifacts(task_id, db_path=temp_db)
        assert len(arts) == 0
    finally:
        db.get_connection = original_get_conn
        db.init_db = original_init_db


# ---------------------------------------------------------------------------
# Test 4: Continuation on Interrupted Research Checkpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_continuation_on_interrupted_checkpoint(tmp_path):
    """Verify 'continue' detects an interrupted task checkpoint and resumes its stage."""
    from app.agentLoop import run_task, real_hub

    temp_db = str(tmp_path / "test_interrupted.db")
    db.init_db(db_path=temp_db)

    chat = db.create_chat(title="Interrupted Task Chat", db_path=temp_db)
    chat_id = chat["id"]

    # Save an in-flight checkpoint at step 2
    pkg = _build_test_package("pkg_in_flight", "Quantum Computing")
    db.save_task_checkpoint(
        task_id="task_prior_flight",
        conversation_id=chat_id,
        session_id="sess_flight",
        step_index=2,
        last_url="https://arxiv.org",
        history=[{"action": "navigate", "url": "https://arxiv.org"}],
        working_memory={
            "is_research": True,
            "research_stage": "harvesting",
            "research_topic": "Quantum Computing",
            "research_knowledge_package": pkg.to_dict(),
        },
        db_path=temp_db,
    )

    cl = classify_intent("continue", context={"is_research": True, "research_topic": "Quantum Computing"})
    assert cl.operation_type == ResearchOperationType.RESUME_EXISTING_MISSION


# ---------------------------------------------------------------------------
# Test 5: Continuation with Pending Export on Completed Package
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_continuation_completed_package_pending_export(tmp_path):
    """Verify 'continue' on a completed package with pending requested format renders the artifact."""
    from app.agentLoop import run_task, real_hub

    temp_db = str(tmp_path / "test_pending_exp.db")
    db.init_db(db_path=temp_db)

    chat = db.create_chat(title="Pending Export Chat", db_path=temp_db)
    chat_id = chat["id"]

    pkg = _build_test_package("pkg_pending_01", "Distributed Systems")
    db.save_research_package(pkg.to_dict(), chat_id, "task_done_research", status="completed", db_path=temp_db)
    db.set_chat_memory(chat_id, "requested_format", "pdf", db_path=temp_db)
    db.set_chat_memory(chat_id, "research_topic", "Distributed Systems", db_path=temp_db)

    task_id = "task_continue_pending"
    record = TaskRecord(
        task_id=task_id,
        instruction="continue",
        state="created",
        chat_id=chat_id,
    )
    real_hub.tasks[task_id] = record

    original_get_conn = db.get_connection
    original_init_db = db.init_db
    try:
        db.init_db = lambda *args, **kwargs: None
        db.get_connection = lambda p=None: original_get_conn(temp_db)

        await run_task(task_id)

        assert record.state == "completed"
        assert "Deliverable Ready:" in record.message
        assert ".pdf" in record.message.lower()

        arts = db.list_task_artifacts(task_id, db_path=temp_db)
        assert len(arts) >= 1
        assert arts[0]["file_format"] == "pdf"
    finally:
        db.get_connection = original_get_conn
        db.init_db = original_init_db


# ---------------------------------------------------------------------------
# Test 6: Continuation when Research AND Deliverable Already Completed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_continuation_when_already_completed(tmp_path):
    """Verify 'continue' does not start redundant mission when research and deliverable are finished."""
    from app.agentLoop import run_task, real_hub

    temp_db = str(tmp_path / "test_already_done.db")
    db.init_db(db_path=temp_db)

    chat = db.create_chat(title="Finished Chat", db_path=temp_db)
    chat_id = chat["id"]

    pkg = _build_test_package("pkg_finished_01", "Transformer Architecture")
    db.save_research_package(pkg.to_dict(), chat_id, "task_finished_res", status="completed", db_path=temp_db)
    db.save_artifact({
        "artifact_id": "art_done_01",
        "task_id": "task_finished_res",
        "conversation_id": chat_id,
        "filename": "transformer_architecture.pdf",
        "filepath": "/tmp/dummy.pdf",
        "file_format": "pdf",
        "mime_type": "application/pdf",
        "file_size_bytes": 1024,
        "title": "Transformer Report",
        "metadata": {},
    }, db_path=temp_db)
    db.set_chat_memory(chat_id, "last_deliverable", "transformer_architecture.pdf", db_path=temp_db)
    db.set_chat_memory(chat_id, "requested_format", "pdf", db_path=temp_db)
    db.set_chat_memory(chat_id, "research_topic", "Transformer Architecture", db_path=temp_db)

    task_id = "task_continue_done"
    record = TaskRecord(
        task_id=task_id,
        instruction="continue",
        state="created",
        chat_id=chat_id,
    )
    real_hub.tasks[task_id] = record

    original_get_conn = db.get_connection
    original_init_db = db.init_db
    try:
        db.init_db = lambda *args, **kwargs: None
        db.get_connection = lambda p=None: original_get_conn(temp_db)

        await run_task(task_id)

        assert record.state == "completed"
        assert "already completed" in record.message
        assert "Transformer Architecture" in record.message
    finally:
        db.get_connection = original_get_conn
        db.init_db = original_init_db


# ---------------------------------------------------------------------------
# Test 7: Research Refinement Preserves Existing Package Evidence
# ---------------------------------------------------------------------------

def test_research_refinement_preserves_package(tmp_path):
    """Verify refinement retains all prior evidence, sources, and claims while adding new focus."""
    temp_db = str(tmp_path / "test_refine.db")
    db.init_db(db_path=temp_db)

    chat = db.create_chat(title="Refine Chat", db_path=temp_db)
    chat_id = chat["id"]

    pkg = _build_test_package("pkg_base_01", "Transformer Architecture")
    db.save_research_package(pkg.to_dict(), chat_id, "task_base", status="completed", db_path=temp_db)

    instruction = "Add a comparison of JAX and PyTorch"
    cl = classify_intent(instruction, context={"is_research": True, "research_topic": "Transformer Architecture"})
    assert cl.operation_type == ResearchOperationType.REFINE_EXISTING_RESEARCH
    assert cl.is_browser is True

    res = resolve_target_package(chat_id, instruction, requested_format="pdf", db_path=temp_db)
    assert res.package is not None
    loaded_pkg = ResearchKnowledgePackage.from_dict(res.package["data"])
    assert len(loaded_pkg.sources) == 2
    assert len(loaded_pkg.claims) == 1
    assert "Transformer Architecture" in loaded_pkg.topic.title


# ---------------------------------------------------------------------------
# Test 8: Unrelated New Research Isolates Task State
# ---------------------------------------------------------------------------

def test_unrelated_new_research_isolates_state():
    """Verify an unrelated new research request starts clean and does not inherit previous notes."""
    ctx = {
        "is_research": True,
        "research_topic": "Transformer Architecture",
        "requested_format": "pdf",
        "last_deliverable": "transformer.pdf",
    }
    instruction = "Research the best backend frameworks for distributed systems"
    cl = classify_intent(instruction, context=ctx)
    assert cl.operation_type == ResearchOperationType.NEW_RESEARCH
    assert cl.is_browser is True
    assert "backend framework" in cl.research_topic.lower()
    assert "transformer" not in cl.research_topic.lower()


def test_reformat_pages_and_images_followup_routing(tmp_path):
    """Verify follow-up requests like 'Make the previous report five pages and add more images'
    are classified as EXPORT_EXISTING_PACKAGE without browser navigation, reusing existing package.
    """
    temp_db = str(tmp_path / "test_followup_reformat.db")
    db.init_db(db_path=temp_db)

    chat = db.create_chat(title="Transformer Research Chat", db_path=temp_db)
    chat_id = chat["id"]

    pkg = _build_test_package("pkg_trans_01", "Transformer Architecture & Attention Mechanisms")
    db.save_research_package(pkg.to_dict(), chat_id, "task_trans_01", status="completed", db_path=temp_db)

    ctx = {
        "is_research": True,
        "research_topic": "Transformer Architecture & Attention Mechanisms",
        "requested_format": "pdf",
        "chat_id": chat_id,
    }

    test_prompts = [
        "Make the previous report five pages and add more images",
        "Make the previous report 5 pages and add more images",
        "make the prior report five pages",
        "can you make the previous report 5 pages and include more diagrams",
        "turn the previous analysis into a 5-page report with more diagrams",
        "Format the previous research as a 5-page report with additional images",
    ]

    for prompt in test_prompts:
        cl = classify_intent(prompt, context=ctx)
        assert cl.operation_type == ResearchOperationType.EXPORT_EXISTING_PACKAGE, f"Failed for prompt: {prompt}"
        assert cl.intent == TaskIntent.NON_BROWSER, f"Failed non-browser check for: {prompt}"
        assert cl.is_browser is False, f"Failed is_browser check for: {prompt}"
        assert cl.requested_format == "pdf"
        assert "Transformer" in cl.research_topic

    # Verify package resolution finds the existing package
    res = resolve_target_package(chat_id, "Make the previous report five pages and add more images", requested_format="pdf", db_path=temp_db)
    assert res.package is not None
    loaded_pkg = ResearchKnowledgePackage.from_dict(res.package["data"])
    assert len(loaded_pkg.sources) == 2
    assert "Transformer Architecture" in loaded_pkg.topic.title

