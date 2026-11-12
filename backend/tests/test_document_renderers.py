"""Unit tests for Phase 4.4, 4.7, and 4.8 Document Renderers."""
from __future__ import annotations

import tempfile
from pathlib import Path

import docx
import openpyxl
import pptx

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
from app.research.renderers import (
    CsvRenderer,
    DocxRenderer,
    HtmlRenderer,
    MarkdownRenderer,
    PdfRenderer,
    PptxRenderer,
    XlsxRenderer,
    render_artifact,
)


def _build_test_package() -> ResearchKnowledgePackage:
    topic = ResearchTopic(
        title="Modern Solid-State Battery Breakthroughs",
        objective="Analyze energy density, ionic conductivity, and commercial viability",
        scope="Commercial applications 2024-2028 across EV and aerospace sectors",
        target_format="docx",
        subtopics=[
            ResearchSubtopic(
                subtopic_id="sub_01",
                title="Sulfide vs Oxide Electrolytes",
                status="completed",
                findings_summary="Sulfide solid electrolytes exhibit ionic conductivity exceeding 20 mS/cm.",
                claim_ids=["clm_01", "clm_02"],
            )
        ],
    )
    pkg = ResearchKnowledgePackage(
        package_id="pkg_test_renderers",
        topic=topic,
    )
    s1 = SourceReference(
        source_id="src_01",
        url="https://nature.com/articles/s41586-battery",
        domain="nature.com",
        title="Solid Electrolyte Ionic Transport",
        author="Dr. M. Stanley",
        reliability_score=0.98,
        is_primary=True,
    )
    s2 = SourceReference(
        source_id="src_02",
        url="https://arxiv.org/abs/2403.99999",
        domain="arxiv.org",
        title="Energy Density Scaling in Lithium Metal Cells",
        reliability_score=0.92,
        is_primary=True,
    )
    pkg.add_source(s1)
    pkg.add_source(s2)

    ev1 = EvidenceItem(
        evidence_id="ev_01",
        source_id="src_01",
        quote="Sulfide electrolytes demonstrated 25 mS/cm conductivity at room temperature.",
        confidence=0.95,
    )
    pkg.add_evidence(ev1)

    c1 = ResearchClaim(
        claim_id="clm_01",
        statement="Sulfide solid electrolytes achieved 25 mS/cm ionic conductivity at 25°C.",
        classification=EvidenceClassification.SINGLE_SOURCE,
        claim_type="statistic",
        subtopic_id="sub_01",
        primary_source_id="src_01",
        evidence_item_ids=["ev_01"],
        confidence=0.94,
    )
    c2 = ResearchClaim(
        claim_id="clm_02",
        statement="Full cell energy density reached 450 Wh/kg in prototype pouch cells.",
        classification=EvidenceClassification.CORROBORATED,
        claim_type="advantage",
        subtopic_id="sub_01",
        primary_source_id="src_01",
        supporting_source_ids=["src_02"],
        confidence=0.91,
    )
    # Contradicting claim
    c3 = ResearchClaim(
        claim_id="clm_03",
        statement="Production scaling is anticipated for 2026.",
        classification=EvidenceClassification.CONFLICTING,
        claim_type="statistic",
        primary_source_id="src_02",
        contradiction_notes="Discrepancy with Nature report projecting 2028 commercial availability.",
    )
    pkg.add_claim(c1)
    pkg.add_claim(c2)
    pkg.add_claim(c3)
    pkg.contradictions.append("Conflict between src_01 (2028 timeline) and src_02 (2026 timeline).")

    tbl = DataTable(
        table_id="tbl_spec",
        title="Electrolyte Technical Comparison",
        headers=["Electrolyte Class", "Conductivity (mS/cm)", "Density (Wh/kg)", "Commercial Ready"],
        rows=[
            ["Sulfide (Li10GeP2S12)", 25.0, 450, "2027"],
            ["Oxide (LLZO)", 1.5, 380, "2026"],
            ["Polymer (PEO-based)", 0.3, 300, "Present"],
        ],
        caption="Comparative conductivity and energy metrics.",
    )
    pkg.add_table(tbl)

    formula = FormulaItem(
        formula_id="eq_01",
        name="Ionic Transport Conductivity",
        latex=r"\sigma = \frac{n q^2 D}{k_B T}",
        explanation="Nernst-Einstein relationship for solid ionic diffusion.",
    )
    pkg.add_formula(formula)

    return pkg


def test_markdown_renderer():
    """Verify Markdown generation with frontmatter, GFM tables, and citations."""
    pkg = _build_test_package()
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = Path(tmp_dir) / "report.md"
        res = MarkdownRenderer().render(pkg, str(out_path))

        assert Path(res).exists()
        content = Path(res).read_text(encoding="utf-8")
        assert "---" in content
        assert 'title: "Modern Solid-State Battery Breakthroughs"' in content
        assert "# Modern Solid-State Battery Breakthroughs" in content
        assert "## 1. Executive Summary" in content
        assert "## 4. Structured Data & Comparative Tables" in content
        assert "| Sulfide (Li10GeP2S12) | 25.0 | 450 | 2027 |" in content
        assert "## 6. Documented Discrepancies & Conflicting Data" in content
        assert "## 7. References Cited & Source Provenance" in content
        assert "[1]" in content
        assert "nature.com" in content


def test_docx_renderer():
    """Verify Word document generation with styles, tables, callouts, and footers."""
    pkg = _build_test_package()
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = Path(tmp_dir) / "report.docx"
        res = DocxRenderer().render(pkg, str(out_path))

        assert Path(res).exists()
        assert Path(res).stat().st_size > 5000

        # Load back with docx to verify valid structure
        doc = docx.Document(res)
        headings = [p.text for p in doc.paragraphs if p.text.startswith(("1.", "2.", "4.", "7."))]
        assert any("Executive Summary" in h for h in headings)
        assert any("References Cited" in h for h in headings)
        assert len(doc.tables) >= 3  # Metadata, Comparison, References


def test_pdf_renderer():
    """Verify ReportLab PDF generation with running headers and page counts."""
    pkg = _build_test_package()
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = Path(tmp_dir) / "report.pdf"
        res = PdfRenderer().render(pkg, str(out_path))

        assert Path(res).exists()
        data = Path(res).read_bytes()
        assert data.startswith(b"%PDF")
        assert len(data) > 3000


def test_xlsx_renderer():
    """Verify multi-sheet Excel generation with calculations and auto-sizing."""
    pkg = _build_test_package()
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = Path(tmp_dir) / "report.xlsx"
        res = XlsxRenderer().render(pkg, str(out_path))

        assert Path(res).exists()
        wb = openpyxl.load_workbook(res)
        assert "Executive Summary" in wb.sheetnames
        assert "Comparison & Metrics" in wb.sheetnames
        assert "Verified Claims" in wb.sheetnames
        assert "Sources & Provenance" in wb.sheetnames

        ws_data = wb["Comparison & Metrics"]
        assert ws_data["A1"].value == "Electrolyte Class"
        # Check that calculation formulas were injected
        avg_formula = ws_data["B5"].value
        min_formula = ws_data["B6"].value
        assert avg_formula is not None
        assert "AVERAGE" in str(avg_formula)
        assert min_formula is not None
        assert "MIN" in str(min_formula)


def test_csv_renderer():
    """Verify standard RFC 4180 CSV export."""
    pkg = _build_test_package()
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = Path(tmp_dir) / "report.csv"
        res = CsvRenderer().render(pkg, str(out_path))

        assert Path(res).exists()
        content = Path(res).read_text(encoding="utf-8")
        assert "Electrolyte Class,Conductivity (mS/cm),Density (Wh/kg),Commercial Ready" in content
        assert "Sulfide (Li10GeP2S12),25.0,450,2027" in content


def test_pptx_renderer():
    """Verify 16:9 widescreen PowerPoint presentation generation."""
    pkg = _build_test_package()
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = Path(tmp_dir) / "report.pptx"
        res = PptxRenderer().render(pkg, str(out_path))

        assert Path(res).exists()
        prs = pptx.Presentation(res)
        assert len(prs.slides) >= 4
        # Verify 16:9 dimensions
        assert prs.slide_width == pptx.util.Inches(13.333)
        assert prs.slide_height == pptx.util.Inches(7.5)


def test_html_renderer():
    """Verify accessible standalone HTML report generation."""
    pkg = _build_test_package()
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = Path(tmp_dir) / "report.html"
        res = HtmlRenderer().render(pkg, str(out_path))

        assert Path(res).exists()
        content = Path(res).read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in content
        assert "<html lang=\"en\">" in content
        assert "Modern Solid-State Battery Breakthroughs" in content
        assert '<section id="executive-summary"' in content
        assert "Sulfide (Li10GeP2S12)" in content
        assert "callout-warning" in content
        assert "nature.com" in content


def test_render_artifact_dispatcher():
    """Verify unified dispatcher function routes deliverable formats correctly."""
    pkg = _build_test_package()
    with tempfile.TemporaryDirectory() as tmp_dir:
        p_docx = render_artifact(pkg, "docx", str(Path(tmp_dir) / "out.docx"))
        assert Path(p_docx).name == "out.docx"

        p_pdf = render_artifact(pkg, "pdf", str(Path(tmp_dir) / "out.pdf"))
        assert Path(p_pdf).name == "out.pdf"

        p_xlsx = render_artifact(pkg, "excel", str(Path(tmp_dir) / "out.xlsx"))
        assert Path(p_xlsx).name == "out.xlsx"

        p_md = render_artifact(pkg, "markdown", str(Path(tmp_dir) / "out.md"))
        assert Path(p_md).name == "out.md"
