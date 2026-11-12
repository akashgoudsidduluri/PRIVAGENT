"""Comprehensive Acceptance Test Suite for Phase 4: Autonomous Research & Multi-Format Artifact Generation.

Verifies the 6 mandated end-to-end acceptance scenarios:
1. Transformer PDF: Architectural explanation with equations, diagrams, examples in PDF.
2. Laptop XLSX: Multi-retailer product research with specifications, variants, and Excel formulas.
3. Battery DOCX: Solid-state battery report with primary sources, claim types, contradictions in DOCX.
4. OS 12-Slide PPTX: 16:9 presentation on kernel architectures & scheduling with 12+ slides.
5. Computer Networks Multi-Format: Same canonical package exported to Markdown, HTML, and CSV.
6. Resumption & Recovery: Interruption at collection stage and restart without duplicate visits.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import docx
import openpyxl
import pptx
from fastapi.testclient import TestClient

from app import db
from app.intent_router import classify_intent
from app.main import ARTIFACTS_DIR, app
from app.research.collector import harvest_page_evidence
from app.research.models import (
    DataTable,
    EvidenceClassification,
    EvidenceItem,
    FormulaItem,
    ResearchClaim,
    ResearchKnowledgePackage,
    ResearchSubtopic,
    ResearchTopic,
    SourceReference,
    VisualAsset,
)
from app.research.product_extractor import compare_product_listings, extract_product_listings
from app.research.renderers import render_artifact
from app.research.visuals.chart_generator import generate_chart_from_table
from app.research.visuals.diagram_generator import generate_mermaid_diagram, render_diagram_to_png
from app.research.visuals.formula_renderer import render_latex_formula
from app.website_resolver import website_resolver


# ===========================================================================
# 1. TEST 1: Transformer PDF Deliverable
# ===========================================================================
def test_acceptance_1_transformer_pdf():
    """Explain transformer architecture with diagrams, equations and examples in a PDF."""
    instruction = "Explain transformer architecture with diagrams, equations and examples in a PDF."

    # 1. Intent routing
    cl = classify_intent(instruction)
    assert cl.is_research is True
    assert cl.requested_format == "pdf"
    assert cl.deliverable_type in ("report", "pdf")

    # 2. Setup canonical knowledge package
    pkg = ResearchKnowledgePackage(
        package_id="pkg_acc_transformer",
        topic=ResearchTopic(
            title="Transformer Architecture: Foundations, Self-Attention & Implementation",
            objective=instruction,
            scope="Deep dive into scaled dot-product attention, multi-head mechanisms, and FFNs",
            target_format="pdf",
            subtopics=[
                ResearchSubtopic(
                    subtopic_id="sub_trans_01",
                    title="Scaled Dot-Product & Multi-Head Self-Attention",
                    findings_summary="The attention mechanism maps query and key-value vectors to outputs via scaled softmax matrices.",
                    claim_ids=["clm_t_01"],
                )
            ],
        ),
    )

    src = SourceReference(
        source_id="src_vaswani_2017",
        url="https://arxiv.org/abs/1706.03762",
        domain="arxiv.org",
        title="Attention Is All You Need",
        author="Vaswani et al.",
        publication_date="2017-06-12",
        reliability_score=0.99,
        is_primary=True,
    )
    pkg.add_source(src)

    ev = EvidenceItem(
        evidence_id="ev_t_01",
        source_id="src_vaswani_2017",
        quote="An attention function can be described as mapping a query and a set of key-value pairs to an output.",
        confidence=0.98,
    )
    pkg.add_evidence(ev)

    clm = ResearchClaim(
        claim_id="clm_t_01",
        statement="Multi-head attention allows the model to jointly attend to information from different representation subspaces.",
        classification=EvidenceClassification.SINGLE_SOURCE,
        claim_type="mechanism",
        subtopic_id="sub_trans_01",
        primary_source_id="src_vaswani_2017",
        evidence_item_ids=["ev_t_01"],
        confidence=0.97,
    )
    pkg.add_claim(clm)

    # 3. Visuals & Formulas
    task_dir = ARTIFACTS_DIR / "task_acc_transformer"
    task_dir.mkdir(parents=True, exist_ok=True)

    # Formula asset
    eq_path = task_dir / "attention_eq.png"
    render_latex_formula(r"\mathrm{Attention}(Q, K, V) = \mathrm{softmax}\left(\frac{QK^T}{\sqrt{d_k}}\right)V", str(eq_path))
    formula = FormulaItem(
        formula_id="eq_attn",
        name="Scaled Dot-Product Attention",
        latex=r"\mathrm{Attention}(Q, K, V) = \mathrm{softmax}\left(\frac{QK^T}{\sqrt{d_k}}\right)V",
        explanation="Computes queries and keys dot-products scaled by the square root of dimension d_k.",
        variables={"Q": "Queries matrix", "K": "Keys matrix", "V": "Values matrix", "d_k": "Dimension of keys"},
        worked_example="For d_k = 64, sqrt(d_k) = 8. If Q.K^T product is 16.0, scaled score is 2.0.",
    )
    pkg.add_formula(formula)
    pkg.add_visual(
        VisualAsset(
            asset_id="vis_eq_attn",
            asset_type="formula",
            title="Attention Formula",
            url_or_path=str(eq_path),
            is_generated=True,
            metadata={"formula_id": "eq_attn"},
        )
    )

    # Diagram asset
    diag_path = task_dir / "transformer_arch.png"
    render_diagram_to_png("Transformer Architecture", str(diag_path))
    pkg.add_visual(
        VisualAsset(
            asset_id="vis_trans_arch",
            asset_type="diagram",
            title="Transformer Architecture Workflow",
            caption="Encoder layer with multi-head attention and feed-forward sublayers",
            url_or_path=str(diag_path),
            is_generated=True,
        )
    )

    # 4. Render deliverable
    out_pdf = task_dir / "Transformer_Architecture.pdf"
    rendered_path = render_artifact(pkg, "pdf", str(out_pdf))
    assert Path(rendered_path).exists()
    assert Path(rendered_path).stat().st_size > 5000

    # 5. Register in artifact registry & test API
    art_record = {
        "artifact_id": "art_transformer_pdf_01",
        "task_id": "task_acc_transformer",
        "conversation_id": "chat_acc_transformer",
        "filename": "Transformer_Architecture.pdf",
        "filepath": str(out_pdf),
        "file_format": "pdf",
        "mime_type": "application/pdf",
        "file_size_bytes": out_pdf.stat().st_size,
        "title": "Transformer Architecture Technical Report",
        "summary": "Complete explanation with equations, diagrams, and citations in PDF format.",
        "metadata": {"formulas": 1, "visuals": 2, "pages": 2},
    }
    db.save_artifact(art_record)

    client = TestClient(app)
    res_dl = client.get("/api/artifacts/art_transformer_pdf_01/download")
    assert res_dl.status_code == 200
    assert res_dl.content.startswith(b"%PDF")
    assert 'attachment; filename="Transformer_Architecture.pdf"' in res_dl.headers.get("Content-Disposition", "")


# ===========================================================================
# 2. TEST 2: Laptop Multi-Retailer Excel Comparison
# ===========================================================================
def test_acceptance_2_laptop_xlsx():
    """Compare the leading laptops with RTX 4060 and create an Excel comparison."""
    instruction = "Compare the leading laptops with RTX 4060 and create an Excel comparison."

    # 1. Intent routing
    cl = classify_intent(instruction)
    assert cl.is_research is True
    assert cl.requested_format == "xlsx"

    # 2. Autonomous discovery finds multiple retailers
    disc = website_resolver.discover_websites_for_task(instruction)
    assert disc["scenario"] == "B"
    assert disc["category"] == "shopping"
    selected_domains = [s["domain"] for s in disc["selected_sources"]]
    assert len(set(selected_domains)) >= 2  # At least 2 independent retailers

    # 3. Product Extraction across retailers
    amazon_page = """
    Acer Nitro V Gaming Laptop Intel Core i7-13620H (16 GB/512 GB SSD/RTX 4060 8GB Graphics/144Hz) ANV15-51
    ₹78,990 M.R.P: ₹99,999 (21% off)
    4.3 out of 5 stars
    """
    flipkart_page = """
    Lenovo LOQ 15IRH8 Intel Core i7 13th Gen - (16 GB/512 GB SSD/Windows 11 Home/8 GB Graphics/NVIDIA GeForce RTX 4060) 15IRH8
    ₹81,990
    4.4 ★ Ratings
    """
    croma_page = """
    ASUS TUF Gaming F15 Intel Core i7-13620H (16GB RAM/1TB SSD/8GB NVIDIA GeForce RTX 4060)
    ₹84,990
    4.5 out of 5 stars
    """

    listings = []
    listings.extend(extract_product_listings(amazon_page, "amazon.in", "https://amazon.in/dp/1"))
    listings.extend(extract_product_listings(flipkart_page, "flipkart.com", "https://flipkart.com/dp/2"))
    listings.extend(extract_product_listings(croma_page, "croma.com", "https://croma.com/dp/3"))
    assert len(listings) >= 3

    # 4. Compare products & build DataTable
    comp_table, claims = compare_product_listings(listings, topic_title="RTX 4060 Laptop Comparison")
    assert len(comp_table.rows) == 3
    assert comp_table.rows[0][1] == "amazon.in"  # Lowest price first
    assert "Best Price" in comp_table.rows[0][7]

    pkg = ResearchKnowledgePackage(
        package_id="pkg_acc_laptops",
        topic=ResearchTopic(
            title="RTX 4060 Laptop Market Comparison",
            objective=instruction,
            target_format="xlsx",
        ),
    )
    for l in listings:
        src = SourceReference(
            source_id=f"src_{l['domain'].replace('.', '_')}",
            url=l["url"],
            domain=l["domain"],
            title=l["title"],
            reliability_score=0.90,
        )
        pkg.add_source(src)

    for c in claims:
        pkg.add_claim(c)
    pkg.add_table(comp_table)

    # 5. Render XLSX deliverable
    task_dir = ARTIFACTS_DIR / "task_acc_laptops"
    task_dir.mkdir(parents=True, exist_ok=True)
    out_xlsx = task_dir / "RTX_4060_Laptop_Comparison.xlsx"
    render_artifact(pkg, "xlsx", str(out_xlsx))

    assert out_xlsx.exists()
    wb = openpyxl.load_workbook(out_xlsx)
    assert "Executive Summary" in wb.sheetnames
    assert "Comparison & Metrics" in wb.sheetnames
    assert "Verified Claims" in wb.sheetnames
    assert "Sources & Provenance" in wb.sheetnames

    # Check formulas injected in comparison sheet
    ws_comp = wb["Comparison & Metrics"]
    # Row 1 is header, Rows 2-4 are 3 laptops. Row 5 is Average, Row 6 is Min, Row 7 is Max.
    assert "AVERAGE" in str(ws_comp["C5"].value)
    assert "MIN" in str(ws_comp["C6"].value)
    assert "MAX" in str(ws_comp["C7"].value)


# ===========================================================================
# 3. TEST 3: Solid-State Battery DOCX with Contradictions
# ===========================================================================
def test_acceptance_3_battery_docx():
    """Research solid-state batteries and prepare a detailed report in Word docx."""
    instruction = "Research the latest developments in solid-state batteries and prepare a detailed report in Word docx."

    # 1. Intent routing
    cl = classify_intent(instruction)
    assert cl.is_research is True
    assert cl.requested_format == "docx"

    # 2. Autonomous discovery
    disc = website_resolver.discover_websites_for_task(instruction)
    assert disc["scenario"] == "B"
    assert disc["category"] == "research"
    # Primary scientific hubs must be ranked top
    domains = [c["domain"] for c in disc["candidate_sources"]]
    assert any(d in domains for d in ("nature.com", "arxiv.org", "science.org"))

    # 3. Simulate evidence harvesting with contradiction
    pkg = ResearchKnowledgePackage(
        package_id="pkg_acc_battery",
        topic=ResearchTopic(
            title="Solid-State Battery Breakthroughs & Commercial Roadmaps",
            objective=instruction,
            scope="Materials chemistry, sulfide vs oxide electrolytes, and manufacturing readiness",
            target_format="docx",
            subtopics=[
                ResearchSubtopic(
                    subtopic_id="sub_bat_01",
                    title="Electrolyte Materials & Ionic Transport",
                    findings_summary="Sulfide solid electrolytes achieve record conductivity but face air-sensitivity hurdles.",
                    claim_ids=["clm_b_01", "clm_b_02"],
                )
            ],
        ),
    )

    page_nature = """
    Advances in Sulfide-Based Solid Electrolytes for All-Solid-State Batteries
    By Dr. Jane Doe. Published 2024-04-10.
    Sulfide electrolytes achieved room-temperature ionic conductivity of 25 mS/cm, exceeding conventional liquid electrolytes.
    Commercial vehicle deployment is projected for 2028 due to dry-room manufacturing scaling constraints.
    """
    page_science = """
    High-Capacity Lithium Metal Solid-State Cells with LLZO Separators
    By Research Consortium. Published 2024-05-18.
    Prototype pouch cells demonstrated 480 Wh/kg specific energy density at 100% depth of discharge.
    Automotive commercial production is scheduled for 2026 by leading OEM joint ventures.
    """

    res_nat = harvest_page_evidence("https://nature.com/articles/battery-sulfide", "Nature: Sulfide Battery", page_nature, pkg)
    assert res_nat.status == "success"
    assert res_nat.evidence_count >= 1

    res_sci = harvest_page_evidence("https://science.org/doi/battery-lithium", "Science: LLZO Cells", page_science, pkg)
    assert res_sci.status == "success"

    # Verify contradiction detected and preserved
    assert len(pkg.contradictions) >= 1 or any(c.classification == EvidenceClassification.CONFLICTING for c in pkg.claims.values())

    # Add comparison table
    tbl = DataTable(
        table_id="tbl_bat",
        title="Electrolyte Class Performance Comparison",
        headers=["Electrolyte System", "Conductivity (mS/cm)", "Energy Density (Wh/kg)", "Projected Production"],
        rows=[
            ["Sulfide Li10GeP2S12", 25.0, 480, "2028 (Nature)"],
            ["Oxide LLZO", 1.8, 420, "2026 (Science)"],
            ["Polymer PEO", 0.4, 320, "Current Pilot"],
        ],
    )
    pkg.add_table(tbl)

    # 4. Render DOCX report
    task_dir = ARTIFACTS_DIR / "task_acc_battery"
    task_dir.mkdir(parents=True, exist_ok=True)
    out_docx = task_dir / "Solid_State_Battery_Report.docx"
    render_artifact(pkg, "docx", str(out_docx))

    assert out_docx.exists()
    doc = docx.Document(out_docx)
    headings = [p.text for p in doc.paragraphs if p.text.startswith(("1.", "2.", "4.", "6.", "7."))]
    assert any("Executive Summary" in h for h in headings)
    assert any("Discrepancies" in h for h in headings)
    assert any("References Cited" in h for h in headings)
    assert len(doc.tables) >= 3


# ===========================================================================
# 4. TEST 4: Operating System 12-Slide PPTX Presentation
# ===========================================================================
def test_acceptance_4_operating_system_12slide_pptx():
    """Research modern OS kernel architectures and scheduling. Prepare a 12-slide presentation."""
    instruction = "Research modern operating system kernel architectures and process scheduling algorithms. Prepare a 12-slide presentation."

    # 1. Intent routing
    cl = classify_intent(instruction)
    assert cl.is_research is True
    assert cl.requested_format == "pptx"

    # 2. Build 12-slide structured technical package
    subtopics = [
        ResearchSubtopic(subtopic_id="os_01", title="Monolithic vs Microkernel Architectures", findings_summary="Monolithic kernels (Linux) optimize for speed with in-kernel drivers, while microkernels (seL4, Zircon) isolate servers in user-space for formal verification."),
        ResearchSubtopic(subtopic_id="os_02", title="Hybrid Kernels & Operating Environments", findings_summary="Windows NT and macOS XNU blend monolithic performance with modular subsystem isolation."),
        ResearchSubtopic(subtopic_id="os_03", title="Completely Fair Scheduler (CFS) Mechanics", findings_summary="Linux CFS uses red-black trees indexed by virtual runtime (vruntime) to achieve O(log N) scheduling fairness."),
        ResearchSubtopic(subtopic_id="os_04", title="Multi-Level Feedback Queue (MLFQ) Scheduling", findings_summary="Dynamically adjusts priority based on CPU burst history to minimize response times for interactive jobs."),
        ResearchSubtopic(subtopic_id="os_05", title="Real-Time Scheduling: EDF & Rate Monotonic", findings_summary="Earliest Deadline First (EDF) provides dynamic priority guarantees, achieving 100% CPU utilization bounds."),
        ResearchSubtopic(subtopic_id="os_06", title="Virtual Memory & Paging Hierarchy", findings_summary="Multi-level 4-level and 5-level page tables with TLB caches facilitate 48-bit to 57-bit virtual address spaces."),
        ResearchSubtopic(subtopic_id="os_07", title="Inter-Process Communication (IPC) Latencies", findings_summary="Shared memory provides near-zero overhead, while message passing incurs context switch and copy costs."),
        ResearchSubtopic(subtopic_id="os_08", title="Hardware Isolation & Security Domains", findings_summary="CPU rings, ARM TrustZone, and confidential compute enclaves protect kernel invariants from untrusted code."),
    ]

    pkg = ResearchKnowledgePackage(
        package_id="pkg_acc_os",
        topic=ResearchTopic(
            title="Modern Operating System Kernel Architectures & Process Scheduling",
            objective=instruction,
            scope="Monolithic, microkernel, and hybrid models; scheduling fairness and IPC latency",
            target_format="pptx",
            subtopics=subtopics,
        ),
    )

    src = SourceReference(
        source_id="src_tanenbaum",
        url="https://cs.vu.nl/~ast/books/mos4e/",
        domain="cs.vu.nl",
        title="Modern Operating Systems",
        author="Andrew S. Tanenbaum",
        reliability_score=0.99,
        is_primary=True,
    )
    pkg.add_source(src)

    tbl = DataTable(
        table_id="tbl_kernel",
        title="Kernel Architecture Comparison",
        headers=["Architecture", "Context Switches", "IPC Overhead", "Formal Verification", "Failure Domain"],
        rows=[
            ["Monolithic (Linux)", "Low (Fast)", "Minimal (Pointers)", "Complex", "Entire OS Crash"],
            ["Microkernel (seL4)", "Higher (IPC)", "Message Passing", "Fully Verified", "Isolated Process"],
            ["Hybrid (Windows NT)", "Moderate", "LPC / ALPC", "Partial", "Subsystem Isolated"],
        ],
    )
    pkg.add_table(tbl)

    for idx, sub in enumerate(subtopics, start=1):
        clm = ResearchClaim(
            claim_id=f"clm_os_{idx:02d}",
            statement=sub.findings_summary or "",
            classification=EvidenceClassification.SINGLE_SOURCE,
            claim_type="mechanism",
            subtopic_id=sub.subtopic_id,
            primary_source_id="src_tanenbaum",
            confidence=0.95,
        )
        pkg.add_claim(clm)

    # 3. Render 16:9 Presentation
    task_dir = ARTIFACTS_DIR / "task_acc_os"
    task_dir.mkdir(parents=True, exist_ok=True)
    out_pptx = task_dir / "Operating_Systems_Architectures.pptx"
    render_artifact(pkg, "pptx", str(out_pptx))

    assert out_pptx.exists()
    prs = pptx.Presentation(out_pptx)
    # Mandated minimum 12 slides
    assert len(prs.slides) >= 12
    assert prs.slide_width == pptx.util.Inches(13.333)
    assert prs.slide_height == pptx.util.Inches(7.5)


# ===========================================================================
# 5. TEST 5: Computer Networks Multi-Format Export (Markdown, HTML, CSV)
# ===========================================================================
def test_acceptance_5_networks_multiformat():
    """Prepare a research package on computer networks and deliver in Markdown, HTML, and CSV."""
    instruction = "Prepare a research package on modern computer networks and deliver in Markdown, HTML, and CSV formats."

    pkg = ResearchKnowledgePackage(
        package_id="pkg_acc_networks",
        topic=ResearchTopic(
            title="Computer Networking Protocols: TCP, UDP, QUIC & Congestion Control",
            objective=instruction,
            scope="Transport layer protocols, BBR congestion control, and QUIC 0-RTT handshakes",
            target_format="markdown",
            subtopics=[
                ResearchSubtopic(
                    subtopic_id="sub_net_01",
                    title="TCP vs UDP vs QUIC Performance",
                    findings_summary="QUIC runs over UDP to eliminate head-of-line blocking and achieve 0-RTT connection establishment.",
                )
            ],
        ),
    )

    src = SourceReference(
        source_id="src_rfc9000",
        url="https://datatracker.ietf.org/doc/html/rfc9000",
        domain="ietf.org",
        title="RFC 9000: QUIC: A UDP-Based Multiplexed and Secure Transport",
        reliability_score=1.0,
        is_primary=True,
    )
    pkg.add_source(src)

    tbl = DataTable(
        table_id="tbl_protocols",
        title="Transport Protocol Comparison Matrix",
        headers=["Protocol", "Underlying Transport", "Handshake RTT", "Head-of-Line Blocking", "Congestion Control"],
        rows=[
            ["TCP", "IP", "1-2 RTT", "Yes (Byte-Stream)", "CUBIC / BBR / Reno"],
            ["UDP", "IP", "0 RTT", "No (Datagrams)", "None (Application Level)"],
            ["QUIC / HTTP/3", "UDP", "0-1 RTT", "No (Independent Streams)", "BBR / CUBIC"],
        ],
    )
    pkg.add_table(tbl)

    clm = ResearchClaim(
        claim_id="clm_net_01",
        statement="QUIC incorporates TLS 1.3 encryption directly into the transport handshake, enabling 0-RTT reconnection.",
        classification=EvidenceClassification.SINGLE_SOURCE,
        claim_type="advantage",
        primary_source_id="src_rfc9000",
        confidence=0.98,
    )
    pkg.add_claim(clm)

    task_dir = ARTIFACTS_DIR / "task_acc_networks"
    task_dir.mkdir(parents=True, exist_ok=True)

    # 1. Markdown deliverable
    out_md = task_dir / "Networks_Report.md"
    render_artifact(pkg, "markdown", str(out_md))
    assert out_md.exists()
    content_md = out_md.read_text(encoding="utf-8")
    assert "QUIC / HTTP/3" in content_md
    assert "ietf.org" in content_md

    # 2. HTML deliverable
    out_html = task_dir / "Networks_Report.html"
    render_artifact(pkg, "html", str(out_html))
    assert out_html.exists()
    content_html = out_html.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in content_html
    assert "QUIC / HTTP/3" in content_html

    # 3. CSV deliverable
    out_csv = task_dir / "Networks_Matrix.csv"
    render_artifact(pkg, "csv", str(out_csv))
    assert out_csv.exists()
    content_csv = out_csv.read_text(encoding="utf-8")
    assert "QUIC / HTTP/3,UDP,0-1 RTT,No (Independent Streams),BBR / CUBIC" in content_csv


# ===========================================================================
# 6. TEST 6: Resumption & Recovery Across Backend Restarts
# ===========================================================================
def test_acceptance_6_resumption_and_recovery():
    """Interrupted research mission resumes from checkpoint without re-running completed visits."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db = f.name

    try:
        db.init_db(db_path=temp_db)

        # 1. Simulate initial research run that harvested 2 sources before sudden power loss / restart
        task_id = "task_acc_resumed_01"
        conv_id = "conv_acc_resumed_01"

        partial_pkg = ResearchKnowledgePackage(
            package_id="pkg_interrupted",
            topic=ResearchTopic(
                title="Quantum Computing Fault-Tolerance",
                objective="Evaluate topological vs surface code error correction",
                target_format="docx",
            ),
        )
        s1 = SourceReference(
            source_id="src_arxiv_q",
            url="https://arxiv.org/abs/quantum-error",
            domain="arxiv.org",
            title="Surface Code Thresholds",
            reliability_score=0.98,
        )
        partial_pkg.add_source(s1)
        clm1 = ResearchClaim(
            claim_id="clm_q_01",
            statement="Surface code fault-tolerance threshold is approximately 1% physical error rate.",
            classification=EvidenceClassification.SINGLE_SOURCE,
            claim_type="statistic",
            primary_source_id="src_arxiv_q",
            confidence=0.95,
        )
        partial_pkg.add_claim(clm1)

        working_memory = {
            "schema_version": 1,
            "is_research": True,
            "research_stage": "collection",
            "requested_format": "docx",
            "research_knowledge_package": partial_pkg.to_dict(),
            "visited_domains": ["arxiv.org"],
            "autonomous_discovery": {
                "queries": ["quantum computing fault tolerance 2024"],
                "selected_domains": ["arxiv.org", "nature.com"],
            },
        }

        # Save checkpoint at step 2
        db.save_task_checkpoint(
            task_id=task_id,
            conversation_id=conv_id,
            session_id="sess_q_interrupted",
            step_index=2,
            last_url="https://arxiv.org",
            history=[{"action": {"type": "navigate", "params": {"url": "https://arxiv.org"}}, "result_ok": True}],
            working_memory=working_memory,
            db_path=temp_db,
        )

        # 2. Simulate Backend Restart: load checkpoint
        cp = db.get_task_checkpoint(task_id, db_path=temp_db)
        assert cp is not None
        assert cp["step_index"] == 2
        resumed_wm = cp["working_memory"]

        # 3. Verify prior work is remembered so arxiv.org is not re-harvested
        assert "arxiv.org" in resumed_wm["visited_domains"]
        resumed_pkg = ResearchKnowledgePackage.from_dict(resumed_wm["research_knowledge_package"])
        assert "src_arxiv_q" in resumed_pkg.sources
        assert "clm_q_01" in resumed_pkg.claims

        # 4. Continuation visits pending source (nature.com) only
        s2 = SourceReference(
            source_id="src_nature_q",
            url="https://nature.com/articles/quantum-topological",
            domain="nature.com",
            title="Majorana Zero Modes in Superconducting Wires",
            reliability_score=0.99,
            is_primary=True,
        )
        resumed_pkg.add_source(s2)
        clm2 = ResearchClaim(
            claim_id="clm_q_02",
            statement="Topological qubits offer hardware-level protection against local thermal perturbations.",
            classification=EvidenceClassification.SINGLE_SOURCE,
            claim_type="advantage",
            primary_source_id="src_nature_q",
            confidence=0.96,
        )
        resumed_pkg.add_claim(clm2)
        resumed_wm["visited_domains"].append("nature.com")
        resumed_wm["research_stage"] = "synthesis"
        resumed_wm["research_knowledge_package"] = resumed_pkg.to_dict()

        # Final checkpoint and deliverable rendering
        task_dir = ARTIFACTS_DIR / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        final_docx = task_dir / "Quantum_Fault_Tolerance.docx"
        render_artifact(resumed_pkg, "docx", str(final_docx))

        assert final_docx.exists()
        doc = docx.Document(final_docx)
        assert len(doc.tables) >= 2

        # Save final completed checkpoint
        db.save_task_checkpoint(
            task_id=task_id,
            conversation_id=conv_id,
            session_id="sess_q_interrupted",
            step_index=4,
            last_url="https://nature.com",
            history=cp["history"] + [{"action": {"type": "navigate", "params": {"url": "https://nature.com"}}, "result_ok": True}],
            working_memory=resumed_wm,
            db_path=temp_db,
        )

        final_cp = db.get_task_checkpoint(task_id, db_path=temp_db)
        assert final_cp["step_index"] == 4
        assert len(final_cp["working_memory"]["visited_domains"]) == 2

    finally:
        try:
            Path(temp_db).unlink(missing_ok=True)
        except OSError:
            pass
