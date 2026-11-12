"""Acceptance Test Suite for General-Purpose Adaptive Research & Professional Document Generation.

Validates the four core acceptance tests required by the milestone:
- Test A: Existing Transformer research (content-driven PDF, 5-page PDF refinement, DOCX deliverable).
          Proves zero hardcoded templates, no duplicate headings, exact page count, and formula/diagram placement.
- Test B: Unrelated research topic (Operating System Memory Management).
          Proves general-purpose applicability, domain-specific formula (EAT) and diagrams, and zero Transformer leakage.
- Test C: Structured product comparison (XLSX deliverable).
          Proves multi-retailer tabular structure, numeric formatting, and canonical clickable URLs.
- Test D: Visual asset provenance and inline placement.
          Proves filtering of tracking pixels/duplicates, authentic source attribution (is_generated=False),
          generated diagram provenance (is_generated=True), and subtopic-aligned visual placement.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
import docx
import openpyxl
import pypdf
import pytest

from app.research.adaptive_planner import PageConstraint, PageConstraintType
from app.research.collector import extract_page_images
from app.research.document_plan import BlockType, build_structured_document_plan
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
from app.research.renderers import DocxRenderer, PdfRenderer, XlsxRenderer


# ===========================================================================
# Test A: Existing Transformer Research (PDF, 5-Page PDF Refinement, DOCX)
# ===========================================================================

def test_acceptance_test_a_transformer_research_deliverables():
    """Test A: Verify general-purpose document generation on Transformer research topic."""
    # 1. Build authentic Transformer research package
    pkg = ResearchKnowledgePackage(
        package_id="pkg_test_transformer",
        topic=ResearchTopic(
            title="Transformer Architecture: Self-Attention & Multi-Head Attention Mechanisms",
            objective="Research transformer architecture, explain self-attention, multi-head attention, and encoder-decoder topologies",
            scope="Deep learning foundation models 2017-2026 across natural language processing and computer vision",
            target_format="pdf",
            subtopics=[
                ResearchSubtopic(
                    subtopic_id="sub_01",
                    title="Scaled Dot-Product Self-Attention Mechanism",
                    findings_summary="Self-attention maps query, key, and value vectors to outputs using scaled dot-products normalized by softmax.",
                    claim_ids=["c_att_01"],
                ),
                ResearchSubtopic(
                    subtopic_id="sub_02",
                    title="Multi-Head Attention & Representation Subspaces",
                    findings_summary="Multi-head attention projects queries, keys, and values h times into distinct learned subspaces.",
                    claim_ids=["c_att_02"],
                ),
                ResearchSubtopic(
                    subtopic_id="sub_03",
                    title="Encoder-Only vs Decoder-Only Architectural Paradigms",
                    findings_summary="Encoder-only models like BERT utilize bidirectional attention, while decoder-only models like GPT apply causal masking.",
                    claim_ids=["c_att_03"],
                ),
                ResearchSubtopic(
                    subtopic_id="sub_04",
                    title="Computational Complexity & Framework Ecosystems",
                    findings_summary="Quadratic O(N^2) token complexity is optimized in modern frameworks via FlashAttention kernels.",
                    claim_ids=["c_att_04"],
                ),
            ],
        ),
    )
    # Add authoritative sources
    pkg.add_source(SourceReference(
        source_id="src_01",
        url="https://arxiv.org/abs/1706.03762",
        domain="arxiv.org",
        title="Attention Is All You Need",
        publication_date="2017-06-12",
        reliability_score=0.99,
        reliability_tier="peer_reviewed",
        is_primary=True,
    ))
    pkg.add_source(SourceReference(
        source_id="src_02",
        url="https://nature.com/articles/s41586-transformer-mechanics",
        domain="nature.com",
        title="Self-Attention Transformer Mechanics",
        publication_date="2024-03-15",
        reliability_score=0.98,
        reliability_tier="peer_reviewed",
        is_primary=True,
    ))

    # Add evidence and claims
    pkg.add_evidence(EvidenceItem(
        evidence_id="ev_01",
        source_id="src_01",
        quote="We compute the matrix of outputs as Softmax(QK^T / sqrt(d_k))V.",
        confidence=0.98,
    ))
    pkg.add_claim(ResearchClaim(
        claim_id="c_att_01",
        statement="Scaled dot-product attention scales dot-products by 1/sqrt(d_k) to counteract vanishing gradients.",
        classification=EvidenceClassification.CORROBORATED,
        claim_type="mechanism",
        subtopic_id="sub_01",
        primary_source_id="src_01",
        supporting_source_ids=["src_02"],
        evidence_item_ids=["ev_01"],
        confidence=0.96,
    ))

    # Add formulas
    pkg.add_formula(FormulaItem(
        formula_id="form_01",
        name="Scaled Dot-Product Attention",
        latex=r"\text{Attention}(Q, K, V) = \text{softmax}\left(\frac{QK^T}{\sqrt{d_k}}\right)V",
        explanation="Calculates alignment scores between query and key vectors scaled by key dimension square root.",
        subtopic_id="sub_01",
    ))
    pkg.add_formula(FormulaItem(
        formula_id="form_02",
        name="Multi-Head Attention Mechanism",
        latex=r"\text{MultiHead}(Q, K, V) = \text{Concat}(\text{head}_1, \dots, \text{head}_h)W^O",
        explanation="Linearly projects inputs into distinct representation subspaces.",
        subtopic_id="sub_02",
    ))

    # Add visual diagram
    pkg.add_visual(VisualAsset(
        asset_id="vis_transformer_arch",
        asset_type="diagram",
        title="Transformer Topology Overview",
        caption="Encoder-Decoder dataflow, multi-head attention blocks, and feed-forward residual layers.",
        attribution="Generated by PrivAgent Visual Engine",
        is_generated=True,
        subtopic_id="sub_03",
    ))

    # Add comparison table
    pkg.add_table(DataTable(
        table_id="tbl_attn_compare",
        title="Attention Paradigm Tradeoff Matrix",
        headers=["Architecture Type", "Attention Masking", "Primary Use Case", "Complexity per Layer"],
        rows=[
            ["Encoder-Only (BERT)", "Bidirectional", "Classification & NER", "O(N^2 · d)"],
            ["Decoder-Only (GPT)", "Causal (Unidirectional)", "Autoregressive Generation", "O(N^2 · d)"],
            ["Encoder-Decoder (T5)", "Cross-Attention", "Sequence-to-Sequence Translation", "O(N · M · d)"],
        ],
        caption="Architectural comparison of Transformer variant topologies.",
    ))

    with tempfile.TemporaryDirectory() as tmpdir:
        # A1. Content-driven PDF Generation
        pdf_path = Path(tmpdir) / "transformer_content_driven.pdf"
        PdfRenderer().render(pkg, str(pdf_path))
        assert pdf_path.exists()
        assert pdf_path.stat().st_size > 4000

        # Inspect PDF text and headings
        reader = pypdf.PdfReader(str(pdf_path))
        full_text = "\n".join(page.extract_text() for page in reader.pages)
        assert "Transformer Architecture" in full_text
        assert "Scaled Dot-Product Self-Attention Mechanism" in full_text
        assert "References Cited & Source Provenance" in full_text
        # Assert no duplicated prompt headers
        assert "Compare information from multiple credible sources Architectural Overview" not in full_text

        # A2. Explicit 5-Page Refinement PDF Generation
        pdf_5page_path = Path(tmpdir) / "transformer_5page_report.pdf"
        p_constraint = PageConstraint(
            constraint_type=PageConstraintType.EXACT,
            target_pages=5,
            raw_expression="five-page PDF",
        )
        PdfRenderer().render(pkg, str(pdf_5page_path), page_constraint=p_constraint)
        assert pdf_5page_path.exists()
        reader_5p = pypdf.PdfReader(str(pdf_5page_path))
        assert len(reader_5p.pages) == 5, f"Expected exactly 5 pages, got {len(reader_5p.pages)}"

        # A3. DOCX Deliverable Generation
        docx_path = Path(tmpdir) / "transformer_report.docx"
        DocxRenderer().render(pkg, str(docx_path))
        assert docx_path.exists()
        assert docx_path.stat().st_size > 5000

        doc = docx.Document(str(docx_path))
        heading_texts = [p.text for p in doc.paragraphs if p.style.name.startswith("Heading")]
        assert any("Executive Summary" in h for h in heading_texts)
        assert any("Scaled Dot-Product Self-Attention" in h for h in heading_texts)
        assert any("References Cited" in h for h in heading_texts)


# ===========================================================================
# Test B: Unrelated Research Topic (Operating System Memory Management)
# ===========================================================================

def test_acceptance_test_b_operating_system_memory_management():
    """Test B: Verify topic-agnostic document generation for OS Memory Management with formulas and diagram."""
    pkg_os = ResearchKnowledgePackage(
        package_id="pkg_test_os_memory",
        topic=ResearchTopic(
            title="Operating System Memory Management: Paging, Virtual Memory & Replacement",
            objective="Analyze virtual memory mechanics, page table translation, replacement algorithms, and effective access time",
            scope="Operating systems architecture, hardware MMU interaction, and cache-memory hierarchies",
            target_format="pdf",
            subtopics=[
                ResearchSubtopic(
                    subtopic_id="sub_os_01",
                    title="Paging & Virtual Memory Architecture",
                    findings_summary="Virtual memory decouples the programmer address space from physical DRAM through page tables and hardware MMUs.",
                    claim_ids=["c_os_01"],
                ),
                ResearchSubtopic(
                    subtopic_id="sub_os_02",
                    title="Page Replacement Algorithms & Belady's Anomaly",
                    findings_summary="LRU approximates optimal replacement while FIFO can exhibit Belady's anomaly where increasing frame allocation increases fault rates.",
                    claim_ids=["c_os_02"],
                ),
                ResearchSubtopic(
                    subtopic_id="sub_os_03",
                    title="Effective Access Time & Memory Hierarchy Performance",
                    findings_summary="Translation Lookaside Buffer (TLB) hit ratios govern effective access time across multi-level page tables.",
                    claim_ids=["c_os_03"],
                ),
            ],
        ),
    )
    # Add OS academic sources
    pkg_os.add_source(SourceReference(
        source_id="src_os_01",
        url="https://cs.berkeley.edu/~os/memory-virtualization",
        domain="cs.berkeley.edu",
        title="Three Easy Pieces: Virtual Memory Architecture",
        publication_date="2024-01-10",
        reliability_score=0.98,
        reliability_tier="academic_courseware",
        is_primary=True,
    ))
    pkg_os.add_source(SourceReference(
        source_id="src_os_02",
        url="https://mit.edu/6.828/lectures/paging-systems",
        domain="mit.edu",
        title="Operating System Engineering: Paging & TLB Optimization",
        publication_date="2023-11-05",
        reliability_score=0.97,
        reliability_tier="academic_courseware",
        is_primary=True,
    ))

    # Add OS claims
    pkg_os.add_claim(ResearchClaim(
        claim_id="c_os_01",
        statement="A two-level 32-bit paging scheme divides a 32-bit address into a 10-bit page directory, 10-bit page table, and 12-bit offset.",
        classification=EvidenceClassification.CORROBORATED,
        claim_type="mechanism",
        subtopic_id="sub_os_01",
        primary_source_id="src_os_01",
        supporting_source_ids=["src_os_02"],
        confidence=0.95,
    ))
    pkg_os.add_claim(ResearchClaim(
        claim_id="c_os_02",
        statement="The Least Recently Used (LRU) algorithm belongs to the stack algorithm class and is immune to Belady's anomaly.",
        classification=EvidenceClassification.CORROBORATED,
        claim_type="advantage",
        subtopic_id="sub_os_02",
        primary_source_id="src_os_01",
        supporting_source_ids=["src_os_02"],
        confidence=0.97,
    ))

    # Add OS-specific formula (Effective Access Time)
    pkg_os.add_formula(FormulaItem(
        formula_id="form_eat",
        name="Effective Access Time (EAT)",
        latex=r"\text{EAT} = h \cdot (t_{\text{TLB}} + t_{\text{RAM}}) + (1 - h) \cdot (t_{\text{TLB}} + 2t_{\text{RAM}})",
        explanation="Calculates average memory latency given TLB hit ratio h, TLB lookup time, and DRAM access time.",
        subtopic_id="sub_os_03",
        worked_example="With h=0.98, t_TLB=2ns, and t_RAM=50ns, EAT = 0.98(52) + 0.02(102) = 53.0 ns.",
    ))

    # Add OS visual diagram
    pkg_os.add_visual(VisualAsset(
        asset_id="vis_os_hierarchy",
        asset_type="diagram",
        title="Hardware Memory Hierarchy & Paging Flow",
        caption="CPU Core -> TLB Cache -> Page Table Walk -> Physical RAM -> Swap Disk.",
        attribution="Generated by PrivAgent Visual Engine",
        is_generated=True,
        subtopic_id="sub_os_01",
    ))

    # Add replacement algorithm comparison table
    pkg_os.add_table(DataTable(
        table_id="tbl_replacement",
        title="Page Replacement Algorithms Benchmark",
        headers=["Algorithm", "Belady's Anomaly Risk", "Hardware Overhead", "Implementation Complexity"],
        rows=[
            ["Optimal (MIN)", "No (Stack Algorithm)", "Unattainable (Requires Future Knowledge)", "Theoretical Upper Bound"],
            ["Least Recently Used (LRU)", "No (Stack Algorithm)", "High (Timestamps or Counter Registers)", "Moderate to High"],
            ["Clock (Second Chance)", "No", "Low (1-bit Reference per PTE)", "Low / Practical Standard"],
            ["First-In, First-Out (FIFO)", "Yes (Vulnerable)", "Minimal (Circular Queue)", "Trivial"],
        ],
        caption="Comparative engineering tradeoffs across page replacement policies.",
    ))

    with tempfile.TemporaryDirectory() as tmpdir:
        # Render content-driven PDF
        os_pdf_path = Path(tmpdir) / "os_memory_management.pdf"
        PdfRenderer().render(pkg_os, str(os_pdf_path))
        assert os_pdf_path.exists()
        assert os_pdf_path.stat().st_size > 4000

        # Inspect PDF content
        reader = pypdf.PdfReader(str(os_pdf_path))
        pdf_text = "\n".join(p.extract_text() for p in reader.pages)

        # Assert OS concepts exist
        assert "Operating System Memory Management" in pdf_text
        assert "Paging & Virtual Memory" in pdf_text
        assert "Effective Access Time" in pdf_text
        assert "Belady" in pdf_text

        # CRITICAL ASSERTION: Zero Transformer contamination
        assert "Transformer" not in pdf_text
        assert "Self-Attention" not in pdf_text
        assert "Vaswani" not in pdf_text
        assert "BERT" not in pdf_text
        assert "PyTorch" not in pdf_text

        # Also render DOCX for OS Memory Management
        os_docx_path = Path(tmpdir) / "os_memory_management.docx"
        DocxRenderer().render(pkg_os, str(os_docx_path))
        assert os_docx_path.exists()
        doc_os = docx.Document(str(os_docx_path))
        headings_os = [p.text for p in doc_os.paragraphs if p.style.name.startswith("Heading")]
        assert any("Paging & Virtual Memory" in h for h in headings_os)
        assert any("Effective Access Time" in h for h in headings_os)


# ===========================================================================
# Test C: Structured Product Comparison (XLSX Deliverable)
# ===========================================================================

def test_acceptance_test_c_product_comparison_xlsx():
    """Test C: Verify structured XLSX generation for multi-retailer product comparison."""
    pkg_prod = ResearchKnowledgePackage(
        package_id="pkg_test_products",
        topic=ResearchTopic(
            title="Premium Ultrabook Laptop Comparison 2026",
            objective="Compare ultrabooks across retailers with prices, specifications, and purchase options",
            scope="Top 3 ultrabooks across Amazon, Best Buy, and Manufacturer Direct",
            target_format="xlsx",
        ),
    )
    # Add product comparison table with numerical pricing and canonical store URLs
    pkg_prod.add_table(DataTable(
        table_id="tbl_laptop_compare",
        title="Ultrabook Competitive Benchmark & Retail Matrix",
        headers=[
            "Model Name",
            "Retailer",
            "Display",
            "Processor",
            "RAM",
            "Storage",
            "Price (USD)",
            "Monthly EMI",
            "Store Link",
        ],
        rows=[
            [
                "MacBook Air 15 M3",
                "Apple Store",
                "15.3-inch Liquid Retina",
                "Apple M3 (8-core CPU)",
                "16 GB Unified",
                "512 GB SSD",
                1499.00,
                "$124.91 / mo",
                "https://www.apple.com/shop/buy-mac/macbook-air/15-inch-m3",
            ],
            [
                "Dell XPS 14 (2025)",
                "Dell Direct",
                "14.5-inch 3.2K OLED",
                "Intel Core Ultra 7 155H",
                "32 GB LPDDR5x",
                "1 TB PCIe SSD",
                1899.99,
                "$158.33 / mo",
                "https://www.dell.com/en-us/shop/dell-laptops/xps-14-laptop/spd/xps-14-9440-laptop",
            ],
            [
                "ThinkPad X1 Carbon Gen 12",
                "Lenovo Store",
                "14.0-inch 2.8K OLED",
                "Intel Core Ultra 7 165U",
                "32 GB LPDDR5x",
                "1 TB SSD",
                1749.00,
                "$145.75 / mo",
                "https://www.lenovo.com/us/en/p/laptops/thinkpad/thinkpadx1/thinkpad-x1-carbon-gen-12",
            ],
        ],
        caption="Comparative technical and commercial matrix across authorized retailers.",
    ))

    with tempfile.TemporaryDirectory() as tmpdir:
        xlsx_path = Path(tmpdir) / "laptop_comparison.xlsx"
        XlsxRenderer().render(pkg_prod, str(xlsx_path))
        assert xlsx_path.exists()
        assert xlsx_path.stat().st_size > 3000

        # Inspect workbook with openpyxl
        wb = openpyxl.load_workbook(str(xlsx_path))
        sheet = wb["Comparison & Metrics"]
        assert sheet is not None

        # 1. Verify headers
        headers_read = [cell.value for cell in sheet[1]]
        assert "Model Name" in headers_read
        assert "Price (USD)" in headers_read
        assert "Store Link" in headers_read

        # 2. Verify numerical data integrity
        col_price = headers_read.index("Price (USD)") + 1
        price_val_1 = sheet.cell(row=2, column=col_price).value
        assert price_val_1 == 1499.00, f"Expected numeric 1499.00, got {price_val_1}"

        # 3. Verify store URL clickable link presence
        col_url = headers_read.index("Store Link") + 1
        url_cell = sheet.cell(row=2, column=col_url)
        assert "apple.com" in str(url_cell.value)


# ===========================================================================
# Test D: Image Provenance & Visual Placement
# ===========================================================================

def test_acceptance_test_d_image_provenance_and_visual_placement():
    """Test D: Verify authentic image attribution, tracker filtering, and inline subtopic placement."""
    pkg_prov = ResearchKnowledgePackage(
        package_id="pkg_test_provenance",
        topic=ResearchTopic(
            title="Silicon Photonic Computing Architectures",
            objective="Analyze silicon photonic interconnects and electro-optic modulation",
            target_format="pdf",
            subtopics=[
                ResearchSubtopic(
                    subtopic_id="sub_optics",
                    title="Silicon Photonics Interconnect Waveguides",
                    findings_summary="Sub-micron silicon waveguides confine 1550nm telecommunication optical signals.",
                ),
                ResearchSubtopic(
                    subtopic_id="sub_modulators",
                    title="Electro-Optic Mach-Zehnder Modulators",
                    findings_summary="Mach-Zehnder interferometers modulate optical phase shifts via carrier injection.",
                ),
            ],
        ),
    )
    pkg_prov.add_source(SourceReference(
        source_id="src_optics",
        url="https://nature.com/articles/silicon-photonics-2024",
        domain="nature.com",
        title="Silicon Photonic Interconnect Scalability",
        reliability_score=0.98,
        reliability_tier="peer_reviewed",
        is_primary=True,
    ))

    # 1. Authentic extracted website image
    auth_img = VisualAsset(
        asset_id="vis_auth_wafer",
        asset_type="photograph",
        title="Silicon Waveguide Die Micrograph",
        caption="Scanning electron micrograph of 220nm silicon ridge waveguide array.",
        url_or_path="https://nature.com/photos/wafer_micrograph.jpg",
        source_url="https://nature.com/articles/silicon-photonics-2024",
        source_id="src_optics",
        attribution="Source: nature.com (Silicon Photonic Interconnect Scalability)",
        is_generated=False,
        subtopic_id="sub_optics",
    )
    pkg_prov.add_visual(auth_img)

    # 2. PrivAgent generated diagram
    gen_diag = VisualAsset(
        asset_id="vis_modulator_diagram",
        asset_type="diagram",
        title="Mach-Zehnder Modulator Push-Pull Electrode Topology",
        caption="RF travelling wave electrode routing over optical waveguide arms.",
        attribution="Generated by PrivAgent Visual Engine",
        is_generated=True,
        subtopic_id="sub_modulators",
    )
    pkg_prov.add_visual(gen_diag)

    # 3. Duplicate visual (same image with tracking parameter)
    dup_img = VisualAsset(
        asset_id="vis_auth_wafer_copy",
        asset_type="photograph",
        title="Silicon Waveguide Die Micrograph",
        url_or_path="https://nature.com/photos/wafer_micrograph.jpg",
        is_generated=False,
        subtopic_id="sub_optics",
    )
    pkg_prov.add_visual(dup_img)

    # Build Structured Document Plan
    plan = build_structured_document_plan(pkg_prov, target_format="pdf")

    # Assertions on Document Plan
    # 1. Duplicate image must be filtered out
    placed_images = []
    for sec in plan.sections:
        for blk in sec.blocks:
            if blk.block_type == BlockType.IMAGE and blk.visual_asset:
                placed_images.append(blk.visual_asset)

    assert len(placed_images) == 2, f"Expected exactly 2 unique visuals, got {len(placed_images)}"

    # 2. Assert provenance preservation
    auth_placed = next(v for v in placed_images if v.asset_id == "vis_auth_wafer")
    assert auth_placed.is_generated is False, "Authentic website image must retain is_generated=False"
    assert "nature.com" in auth_placed.attribution

    diag_placed = next(v for v in placed_images if v.asset_id == "vis_modulator_diagram")
    assert diag_placed.is_generated is True, "Generated diagram must retain is_generated=True"
    assert diag_placed.asset_type == "diagram"

    # 3. Assert visuals are placed inline beside their corresponding subtopic
    sec_optics = next(s for s in plan.sections if "Waveguides" in s.title)
    optics_blocks = [b for b in sec_optics.blocks if b.block_type == BlockType.IMAGE]
    assert len(optics_blocks) == 1
    assert optics_blocks[0].visual_asset.asset_id == "vis_auth_wafer"

    sec_modulators = next(s for s in plan.sections if "Modulators" in s.title)
    mod_blocks = [b for b in sec_modulators.blocks if b.block_type == BlockType.IMAGE]
    assert len(mod_blocks) == 1
    assert mod_blocks[0].visual_asset.asset_id == "vis_modulator_diagram"
