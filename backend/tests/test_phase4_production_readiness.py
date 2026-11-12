"""Comprehensive Production Readiness & End-to-End Integration Verification.

Audits:
1. Genuine autonomous research:
   - "Research transformer architecture, explain self-attention, multi-head attention, encoder-only, decoder-only and encoder-decoder architectures. Include reliable references and generate a detailed PDF with diagrams."
   - Intent classification, query decomposition, candidate source ranking, browser navigation, evidence harvesting, PDF generation, and download.
2. Genuine Indian product comparison:
   - "Find three good programming laptops under ₹80,000 in India. Compare their CPU, GPU, RAM, storage, display, prices and advantages. Use multiple Indian retailers and generate an XLSX comparison."
   - Indian retailer discovery (Amazon.in, Flipkart, Croma), INR prices (₹), exact variants, multi-sheet XLSX with formulas (AVERAGE, MIN, MAX).
3. Research interruption and recovery:
   - Checkpoint persistence, backend restart simulation, restored state validation, duplicate evidence avoidance, successful resumption to final deliverable.
4. Artifact integrity across all 7 formats:
   - Independent file parsing for DOCX, PDF, PPTX, XLSX, CSV, Markdown, and HTML.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import docx
import openpyxl
import pptx
import pytest
from fastapi.testclient import TestClient

from app import agentLoop, db
from app.contracts import BrowserObservation
from app.intent_router import classify_intent
from app.main import ARTIFACTS_DIR, app
from app.privacy import classify_and_redact
from app.research.collector import harvest_page_evidence
from app.research.models import (
    DataTable,
    EvidenceClassification,
    FormulaItem,
    ResearchClaim,
    ResearchKnowledgePackage,
    ResearchSubtopic,
    ResearchTopic,
    SourceReference,
    VisualAsset,
)
from app.research.product_extractor import (
    compare_product_listings,
    extract_product_listings,
    parse_price,
)
from app.research.renderers import render_artifact
from app.website_resolver import website_resolver


client = TestClient(app)


# ---------------------------------------------------------------------------
# Helper: Simulated observation generator
# ---------------------------------------------------------------------------

def _make_obs(url: str, title: str, text: str) -> BrowserObservation:
    return BrowserObservation.model_validate({
        "tabId": 101,
        "url": url,
        "title": title,
        "page_text": text,
        "interactive": [],
        "scroll": {"y": 0, "viewport": {"w": 1280, "h": 800}},
        "dpr": 1.0,
    })


# ---------------------------------------------------------------------------
# 1. Genuine Autonomous Research: Transformer Architecture in PDF
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audit_1_autonomous_transformer_research_pdf(tmp_path: Path):
    """Verify autonomous intent routing, query decomposition, discovery, harvesting, and PDF generation."""
    instruction = (
        "Research transformer architecture, explain self-attention, multi-head attention, "
        "encoder-only, decoder-only and encoder-decoder architectures. Include reliable references "
        "and generate a detailed PDF with diagrams."
    )

    # 1. Intent classification
    cl = classify_intent(instruction)
    assert cl.is_research is True
    assert cl.requested_format == "pdf"
    assert cl.deliverable_type in ("report", "pdf")
    assert "transformer" in (cl.research_topic or "").lower()

    # 2. Autonomous search query decomposition & discovery
    disc = website_resolver.discover_websites_for_task(instruction)
    assert disc["scenario"] == "B"  # Autonomous discovery without hardcoded destination
    assert disc["category"] == "research"
    assert len(disc["search_queries"]) >= 2
    assert any("transformer" in q.lower() or "attention" in q.lower() for q in disc["search_queries"])

    candidates = disc["candidate_sources"]
    assert len(candidates) >= 2
    # Verify primary academic sources are ranked top
    top_domains = [c["domain"] for c in candidates[:3]]
    assert any(d in top_domains for d in ("arxiv.org", "nature.com", "science.org", "wikipedia.org"))

    # 3. Simulate multi-step browser execution loop
    task_id = "task_audit_transformer_01"
    pkg = ResearchKnowledgePackage(
        package_id=f"pkg_{task_id}",
        topic=ResearchTopic(
            title="Transformer Architecture: Foundations, Self-Attention & Implementation",
            objective=instruction,
            scope="Self-attention, Multi-head attention, and Architecture Families",
            target_format="pdf",
            subtopics=[
                ResearchSubtopic(
                    subtopic_id="sub_01",
                    title="Scaled Dot-Product Attention & Mathematical Foundations",
                    findings_summary="Attention mechanism computes weighted representation based on Query-Key compatibility.",
                ),
                ResearchSubtopic(
                    subtopic_id="sub_02",
                    title="Architectural Paradigms: Encoder-Only, Decoder-Only, and Seq2Seq",
                    findings_summary="BERT popularized bidirectional encoder-only models; GPT established autoregressive decoder-only hegemony.",
                ),
            ],
        ),
    )
    # Add formula
    pkg.add_formula(
        FormulaItem(
            formula_id="form_att",
            name="Scaled Dot-Product Attention",
            latex=r"\text{Attention}(Q, K, V) = \text{softmax}\left(\frac{Q K^T}{\sqrt{d_k}}\right) V",
            explanation="Computes queries and keys dot-product scaled by square root of key dimension.",
        )
    )

    # Page 1: arXiv canonical paper
    arxiv_text = """
    Attention Is All You Need
    By Ashish Vaswani, Noam Shazeer, Niki Parmar, Jakob Uszkoreit, Llion Jones, Aidan N. Gomez, Lukasz Kaiser, Illia Polosukhin. Published 2017-06-12.
    We propose the Transformer, a model architecture eschewing recurrence and relying entirely on an attention mechanism.
    The Transformer achieves 28.4 BLEU on the WMT 2014 English-to-German translation task, outperforming existing models.
    Scaled dot-product attention computes compatibility between queries and keys divided by the square root of key dimension dk.
    Multi-head attention allows the model to jointly attend to information from different representation subspaces.
    """
    res1 = harvest_page_evidence("https://arxiv.org/abs/1706.03762", "Attention Is All You Need - arXiv", arxiv_text, pkg)
    assert res1.status == "success"
    assert res1.claims_added >= 1

    # Page 2: Hugging Face / Model Paradigm Documentation
    hf_text = """
    Transformer Architecture Families: Encoder, Decoder, and Sequence-to-Sequence
    By Machine Learning Research Team. Published 2024-01-15.
    Encoder-only architectures such as BERT rely on bidirectional self-attention to generate contextual representations for classification.
    Decoder-only architectures like GPT-4 operate autoregressively with causal masking to generate text tokens sequentially.
    Encoder-decoder architectures such as T5 and BART excel at sequence transduction tasks including summarization and translation.
    FlashAttention achieves a 3x to 5x speedup and significantly outperforms standard attention memory footprints.
    """
    res2 = harvest_page_evidence("https://huggingface.co/blog/transformer-paradigms", "Transformer Model Families", hf_text, pkg)
    assert res2.status == "success"

    # Verify claim provenance
    assert len(pkg.claims) >= 2
    assert len(pkg.sources) == 2
    for clm in pkg.claims.values():
        assert clm.primary_source_id in pkg.sources

    # 4. Finalize artifacts in agentLoop
    wm = {
        "is_research": True,
        "requested_format": "pdf",
        "deliverable_type": "report",
        "instruction": instruction,
        "research_knowledge_package": pkg.to_dict(),
    }
    primary_art = agentLoop._finalize_research_artifacts(task_id, "conv_audit_01", wm)
    assert primary_art is not None
    assert primary_art["file_format"] == "pdf"
    assert Path(primary_art["filepath"]).exists()

    # 5. Verify PDF download and structure
    resp = client.get(f"/api/artifacts/{primary_art['artifact_id']}/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF-1.4")
    assert b"%%EOF" in resp.content


# ---------------------------------------------------------------------------
# 2. Genuine Indian Product Comparison in XLSX
# ---------------------------------------------------------------------------

def test_audit_2_indian_laptop_comparison_xlsx(tmp_path: Path):
    """Verify Indian retailer resolution, INR price extraction, specification parsing, and XLSX generation."""
    instruction = (
        "Find three good programming laptops under ₹80,000 in India. Compare their CPU, GPU, RAM, "
        "storage, display, prices and advantages. Use multiple Indian retailers and generate an XLSX comparison."
    )

    # 1. Routing
    cl = classify_intent(instruction)
    assert cl.is_research is True
    assert cl.requested_format == "xlsx"
    assert cl.deliverable_type == "dataset"

    # 2. Indian retailer discovery
    disc = website_resolver.discover_websites_for_task(instruction, region="IN")
    assert disc["scenario"] == "B"
    retailer_domains = [c["domain"] for c in disc["candidate_sources"]]
    # Indian retailers must be selected, NOT US retailers
    assert any(d in retailer_domains for d in ("amazon.in", "flipkart.com", "croma.com", "reliancedigital.in"))
    assert not any(d in retailer_domains for d in ("bestbuy.com", "newegg.com"))

    # 3. Simulate multi-retailer product page extractions in India
    amazon_in_page = """
    Lenovo IdeaPad Slim 5 14th Gen Intel Core Ultra 5 125H (16GB RAM / 1TB SSD / Intel Arc / 14-inch OLED WUXGA 400 nits)
    Deal Price: ₹74,990 (Inclusive of all taxes)
    M.R.P.: ₹98,990. Save ₹24,000 (24%).
    Ratings: 4.4 out of 5 stars.
    Key Features: 14" OLED 100% DCI-P3 display, Aluminum body, Backlit Keyboard, 16GB LPDDR5X RAM, 1TB SSD.
    """
    flipkart_page = """
    ASUS Vivobook Pro 15 OLED with AMD Ryzen 7 7735HS (16 GB / 512 GB SSD / NVIDIA GeForce RTX 3050 4GB / 15.6 inch FHD OLED)
    Special Price: ₹69,990
    Ratings: 4.5 out of 5 stars.
    Highlights: Dedicated RTX 3050 graphics, 15.6-inch OLED vibrant display, 16GB DDR5, 512GB NVMe SSD.
    """
    croma_page = """
    Acer Swift Go 14 AI Powered Intel Core Ultra 5 125H (16 GB RAM / 512 GB SSD / 14-inch 90Hz OLED)
    Offer Price: ₹66,990
    Ratings: 4.3 out of 5 stars.
    Description: Ultra-portable 1.32 kg, Killer Wi-Fi 7, Dual heat pipes, 16 GB LPDDR5X, 512 GB SSD.
    """

    listings_az = extract_product_listings(amazon_in_page, "amazon.in", "https://www.amazon.in/dp/B0D123")
    assert len(listings_az) >= 1
    item_az = listings_az[0]
    assert item_az["currency"] == "INR"
    assert item_az["price_num"] == 74990.0
    assert "Ultra 5" in item_az["cpu"]
    assert "16GB" in item_az["ram"]
    assert "1TB" in item_az["storage"]

    listings_fk = extract_product_listings(flipkart_page, "flipkart.com", "https://www.flipkart.com/p/item1")
    assert len(listings_fk) >= 1
    item_fk = listings_fk[0]
    assert item_fk["currency"] == "INR"
    assert item_fk["price_num"] == 69990.0
    assert "RTX 3050" in item_fk["gpu"]

    listings_cr = extract_product_listings(croma_page, "croma.com", "https://www.croma.com/p/item2")
    assert len(listings_cr) >= 1
    item_cr = listings_cr[0]
    assert item_cr["currency"] == "INR"
    assert item_cr["price_num"] == 66990.0

    all_listings = listings_az + listings_fk + listings_cr
    assert len(all_listings) == 3

    # Generate comparison table and claims
    table, claims = compare_product_listings(all_listings, topic_title="Programming Laptops under ₹80,000")
    assert table is not None
    assert len(table.rows) == 3
    assert any("Acer" in str(r[0]) or "66,990" in str(r[6]) for r in table.rows)

    # 4. Generate XLSX artifact
    pkg = ResearchKnowledgePackage(
        package_id="pkg_audit_laptops_inr",
        topic=ResearchTopic(
            title="Programming Laptops under ₹80,000 in India: Multi-Retailer Comparison",
            objective=instruction,
            target_format="xlsx",
        ),
        tables=[table],
        claims={c.claim_id: c for c in claims},
        sources={
            "src_az": SourceReference(source_id="src_az", url="https://amazon.in/item1", domain="amazon.in", title="Amazon.in Lenovo", reliability_score=0.95),
            "src_fk": SourceReference(source_id="src_fk", url="https://flipkart.com/item2", domain="flipkart.com", title="Flipkart ASUS", reliability_score=0.92),
            "src_cr": SourceReference(source_id="src_cr", url="https://croma.com/item3", domain="croma.com", title="Croma Acer", reliability_score=0.88),
        },
    )

    out_xlsx = tmp_path / "indian_laptops.xlsx"
    render_artifact(pkg, "xlsx", str(out_xlsx))
    assert out_xlsx.exists()

    # 5. Deep inspection of XLSX with openpyxl
    wb = openpyxl.load_workbook(out_xlsx)
    sheet_names = wb.sheetnames
    assert "Comparison & Metrics" in sheet_names
    assert "Sources & Provenance" in sheet_names

    ws = wb["Comparison & Metrics"]
    # Check that prices and formulas exist in the sheet
    cell_values = [str(cell.value) for row in ws.iter_rows() for cell in row if cell.value is not None]
    assert any("AVERAGE" in v for v in cell_values)
    assert any("MIN" in v for v in cell_values)
    assert any("MAX" in v for v in cell_values)
    assert any("66990" in v or "74990" in v for v in cell_values)


# ---------------------------------------------------------------------------
# 3. Research Interruption and Recovery
# ---------------------------------------------------------------------------

def test_audit_3_interruption_and_recovery(tmp_path: Path):
    """Verify research state persistence across simulated backend termination and clean resumption."""
    task_id = "task_audit_resume_99"
    instruction = "Research solid-state battery electrolytes and prepare report"

    # Step A: Partial research state before interruption
    pkg_initial = ResearchKnowledgePackage(
        package_id=f"pkg_{task_id}",
        topic=ResearchTopic(title="Solid-State Electrolytes", objective=instruction, target_format="docx"),
    )
    res_a = harvest_page_evidence(
        "https://nature.com/articles/battery-chem",
        "Nature Battery Chemistry",
        "Sulfide electrolytes demonstrate high room-temperature ionic conductivity of 25 mS/cm.",
        pkg_initial,
    )
    assert res_a.status == "success"
    assert len(pkg_initial.claims) == 1
    assert len(pkg_initial.sources) == 1

    # Save checkpoint to SQLite DB
    checkpoint_wm = {
        "is_research": True,
        "requested_format": "docx",
        "research_topic": "Solid-State Electrolytes",
        "research_knowledge_package": pkg_initial.to_dict(),
        "visited_domains": ["nature.com"],
        "prior_checkpoint_history": [{"action": {"type": "navigate", "params": {"url": "https://nature.com"}}, "result_ok": True}],
    }
    history_step = [{"action": {"type": "navigate", "params": {"url": "https://nature.com"}}, "result_ok": True}]
    db.save_task_checkpoint(
        task_id=task_id,
        conversation_id="conv_audit_resume",
        session_id="sess_audit_resume",
        step_index=1,
        last_url="https://nature.com",
        history=history_step,
        working_memory=checkpoint_wm,
    )

    # Step B: Simulate backend process termination and restart (reload from DB)
    loaded_checkpoint = db.get_task_checkpoint(task_id)
    assert loaded_checkpoint is not None
    assert loaded_checkpoint["step_index"] == 1
    restored_wm = loaded_checkpoint["working_memory"]

    # Rehydrate package from checkpoint
    restored_pkg = ResearchKnowledgePackage.from_dict(restored_wm["research_knowledge_package"])
    assert len(restored_pkg.sources) == 1
    assert "https://nature.com/articles/battery-chem" in [s.url for s in restored_pkg.sources.values()]
    assert len(restored_pkg.claims) == 1

    # Step C: Re-visiting the same URL does NOT create duplicate evidence or duplicate claims
    res_dup = harvest_page_evidence(
        "https://nature.com/articles/battery-chem",
        "Nature Battery Chemistry",
        "Sulfide electrolytes demonstrate high room-temperature ionic conductivity of 25 mS/cm.",
        restored_pkg,
    )
    assert res_dup.status == "duplicate"
    assert len(restored_pkg.sources) == 1
    assert len(restored_pkg.claims) == 1

    # Step D: Resume research by visiting second source
    res_b = harvest_page_evidence(
        "https://science.org/articles/oxide-cells",
        "Science Oxide Cells",
        "LLZO garnet-type solid electrolytes achieve critical current density of 10 mA/cm2.",
        restored_pkg,
    )
    assert res_b.status == "success"
    assert len(restored_pkg.sources) == 2
    assert len(restored_pkg.claims) == 2

    # Step E: Complete synthesis and artifact generation without re-doing step 1
    restored_wm["research_knowledge_package"] = restored_pkg.to_dict()
    art = agentLoop._finalize_research_artifacts(task_id, "conv_audit_resume", restored_wm)
    assert art is not None
    assert Path(art["filepath"]).exists()
    assert art["file_format"] == "docx"


# ---------------------------------------------------------------------------
# 4. Artifact Integrity Across All 7 Supported Formats
# ---------------------------------------------------------------------------

def test_audit_4_artifact_integrity_all_seven_formats(tmp_path: Path):
    """Generate and independently open/parse actual files in all 7 formats."""
    pkg = ResearchKnowledgePackage(
        package_id="pkg_all_formats",
        topic=ResearchTopic(
            title="Distributed Systems: Raft Consensus & Byzantine Fault Tolerance",
            objective="Comprehensive analysis of consensus algorithms with benchmarks and formulas",
            scope="Leader election, log replication, safety invariants, and throughput under network partitions",
            target_format="pdf",
            subtopics=[
                ResearchSubtopic(
                    subtopic_id="sub_raft",
                    title="Raft Algorithm Mechanics",
                    findings_summary="Raft decomposes consensus into leader election, log replication, and safety.",
                )
            ],
        ),
        sources={
            "src_usenix": SourceReference(
                source_id="src_usenix",
                url="https://usenix.org/conference/atc14/raft",
                domain="usenix.org",
                title="In Search of an Understandable Consensus Algorithm",
                author="Diego Ongaro & John Ousterhout",
                publication_date="2014-06-19",
                reliability_score=0.98,
                is_primary=True,
            ),
            "src_acm": SourceReference(
                source_id="src_acm",
                url="https://dl.acm.org/doi/10.1145/pbft",
                domain="acm.org",
                title="Practical Byzantine Fault Tolerance",
                author="Miguel Castro & Barbara Liskov",
                publication_date="1999-02-15",
                reliability_score=0.96,
                is_primary=True,
            ),
        },
        claims={
            "clm_01": ResearchClaim(
                claim_id="clm_01",
                statement="Raft guarantees safety if a leader has committed an entry in its term.",
                classification=EvidenceClassification.CORROBORATED,
                primary_source_id="src_usenix",
                supporting_source_ids=["src_acm"],
                evidence_item_ids=[],
                claim_type="mechanism",
            ),
            "clm_02": ResearchClaim(
                claim_id="clm_02",
                statement="PBFT tolerates up to F failures in an asynchronous environment with 3F + 1 replicas.",
                classification=EvidenceClassification.SINGLE_SOURCE,
                primary_source_id="src_acm",
                evidence_item_ids=[],
                claim_type="statistic",
            ),
        },
        tables=[
            DataTable(
                table_id="tbl_consensus",
                title="Consensus Algorithm Comparison",
                headers=["Algorithm", "Fault Model", "Min Nodes for F Faults", "Throughput (ops/s)"],
                rows=[
                    ["Raft", "Crash Fault-Tolerant (CFT)", "2F + 1", 12500],
                    ["Multi-Paxos", "Crash Fault-Tolerant (CFT)", "2F + 1", 13200],
                    ["PBFT", "Byzantine Fault-Tolerant (BFT)", "3F + 1", 3800],
                ],
            )
        ],
        formulas=[
            FormulaItem(
                formula_id="form_bft",
                name="BFT Quorum Resiliency",
                latex=r"N \ge 3F + 1",
                explanation="Minimum replicas required to guarantee safety and liveness under Byzantine faults.",
            )
        ],
    )

    # 1. DOCX
    docx_path = tmp_path / "distributed_systems.docx"
    render_artifact(pkg, "docx", str(docx_path))
    assert docx_path.exists()
    doc = docx.Document(docx_path)
    assert len(doc.paragraphs) > 5
    assert len(doc.tables) >= 1
    doc_text = " ".join(p.text for p in doc.paragraphs)
    assert "Raft Algorithm Mechanics" in doc_text

    # 2. PDF
    pdf_path = tmp_path / "distributed_systems.pdf"
    render_artifact(pkg, "pdf", str(pdf_path))
    assert pdf_path.exists()
    pdf_bytes = pdf_path.read_bytes()
    assert pdf_bytes.startswith(b"%PDF-1.4")
    assert b"%%EOF" in pdf_bytes

    # 3. PPTX
    pptx_path = tmp_path / "distributed_systems.pptx"
    render_artifact(pkg, "pptx", str(pptx_path))
    assert pptx_path.exists()
    prs = pptx.Presentation(pptx_path)
    assert len(prs.slides) >= 4
    assert prs.slide_width > 0

    # 4. XLSX
    xlsx_path = tmp_path / "distributed_systems.xlsx"
    render_artifact(pkg, "xlsx", str(xlsx_path))
    assert xlsx_path.exists()
    wb = openpyxl.load_workbook(xlsx_path)
    assert len(wb.sheetnames) >= 3

    # 5. CSV
    csv_path = tmp_path / "distributed_systems.csv"
    render_artifact(pkg, "csv", str(csv_path))
    assert csv_path.exists()
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        rows = list(reader)
        assert len(rows) >= 4
        assert "Algorithm" in rows[0]

    # 6. Markdown
    md_path = tmp_path / "distributed_systems.md"
    render_artifact(pkg, "markdown", str(md_path))
    assert md_path.exists()
    md_text = md_path.read_text(encoding="utf-8")
    assert md_text.startswith("---")
    assert "## 1. Executive Summary" in md_text
    assert "N \\ge 3F + 1" in md_text

    # 7. HTML
    html_path = tmp_path / "distributed_systems.html"
    render_artifact(pkg, "html", str(html_path))
    assert html_path.exists()
    html_text = html_path.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in html_text
    assert "<table" in html_text
    assert "Raft" in html_text
    assert "<script>" not in html_text  # Clean from dangerous script tags
