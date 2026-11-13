"""Professional, Topic-Agnostic PDF Research Document Renderer using ReportLab.

Consumes the canonical StructuredDocumentPlan:
    Research Knowledge Package
              |
              v
       Content Analysis
              |
              v
      AI Document Planner
              |
              v
     Structured Document Plan
              |
              v
      Format-Specific Renderer (PdfRenderer)
              |
              v
       Final Deliverable

Contains zero domain-specific or topic-specific assumptions. Handles layout, typography,
two-pass page numbering ('Page X of Y'), visual figure placement, mathematical equations,
data tables, and verified source provenance across any domain (computer science, science,
finance, operating systems, product research).
"""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    HRFlowable,
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from PIL import Image as PILImage

from ..document_plan import BlockType, ContentBlock, DocumentSection, build_structured_document_plan
from ..models import (
    EvidenceClassification,
    ResearchKnowledgePackage,
)
from ..visuals.image_pipeline import ensure_local_image_asset
from .base import BaseRenderer


class NumberedCanvas(canvas.Canvas):
    """Two-pass canvas to compute total pages and stamp running headers and footers."""
    doc_title: str = "PrivAgent Autonomous Research Report"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._saved_page_states: list[dict[str, Any]] = []

    def showPage(self) -> None:
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count: int) -> None:
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#64748B"))

        # Running Header (pages > 1)
        if self._pageNumber > 1:
            hdr_text = self.doc_title[:65] + ("..." if len(self.doc_title) > 65 else "")
            self.drawString(54, 11 * 72 - 36, hdr_text)
            self.drawRightString(8.5 * 72 - 54, 11 * 72 - 36, "VERIFIED DELIVERABLE")
            self.setStrokeColor(colors.HexColor("#CBD5E1"))
            self.setLineWidth(0.5)
            self.line(54, 11 * 72 - 40, 8.5 * 72 - 54, 11 * 72 - 40)

        # Running Footer (all pages)
        self.setStrokeColor(colors.HexColor("#CBD5E1"))
        self.setLineWidth(0.5)
        self.line(54, 45, 8.5 * 72 - 54, 45)
        self.drawString(54, 32, "PrivAgent Autonomous Research Engine — Confidential & Verified")
        page_str = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(8.5 * 72 - 54, 32, page_str)
        self.restoreState()


class PdfRenderer(BaseRenderer):
    """Generates professional, topic-agnostic PDF deliverables using ReportLab."""

    def render(self, pkg: ResearchKnowledgePackage, output_path: str, **kwargs: Any) -> str:
        out_file = self.prepare_output_path(output_path)
        cit_map = self.build_citation_map(pkg)
        ordered_sources = self.get_ordered_sources(pkg, cit_map)

        # 1. Build Reusable Structured Document Plan
        page_constraint = kwargs.pop("page_constraint", None)
        doc_plan = build_structured_document_plan(
            pkg,
            target_format="pdf",
            page_constraint=page_constraint,
            **kwargs,
        )

        # Set canvas running header document title
        NumberedCanvas.doc_title = doc_plan.metadata.title or "PrivAgent Autonomous Research Report"

        doc = SimpleDocTemplate(
            str(out_file),
            pagesize=letter,
            leftMargin=54,
            rightMargin=54,
            topMargin=54,
            bottomMargin=54,
        )

        styles = getSampleStyleSheet()

        # Custom Publication-Grade Typography Styles
        badge_style = ParagraphStyle(
            "DocBadge",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            textColor=colors.HexColor("#2563EB"),
            spaceAfter=4,
        )
        title_style = ParagraphStyle(
            "DocTitle",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=21,
            leading=25,
            textColor=colors.HexColor("#0F172A"),
            spaceAfter=4,
        )
        subtitle_style = ParagraphStyle(
            "DocSubTitle",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=10.5,
            leading=14,
            textColor=colors.HexColor("#475569"),
            spaceAfter=12,
        )
        h1_style = ParagraphStyle(
            "Heading1_Custom",
            parent=styles["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=13.5,
            leading=17,
            textColor=colors.HexColor("#1E3A8A"),
            spaceBefore=14,
            spaceAfter=3,
            keepWithNext=True,
        )
        h2_style = ParagraphStyle(
            "Heading2_Custom",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=14.5,
            textColor=colors.HexColor("#1E293B"),
            spaceBefore=10,
            spaceAfter=4,
            keepWithNext=True,
        )
        body_style = ParagraphStyle(
            "Body_Custom",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=9.5,
            leading=14,
            textColor=colors.HexColor("#1E293B"),
            spaceAfter=6,
        )
        bullet_style = ParagraphStyle(
            "Bullet_Custom",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=9.5,
            leading=13.5,
            textColor=colors.HexColor("#1E293B"),
            leftIndent=14,
            firstLineIndent=-8,
            spaceAfter=3,
        )
        caption_style = ParagraphStyle(
            "Caption_Custom",
            parent=styles["Normal"],
            fontName="Helvetica-Oblique",
            fontSize=8.5,
            leading=11,
            textColor=colors.HexColor("#64748B"),
            alignment=1,  # Center
            spaceBefore=4,
            spaceAfter=8,
        )

        story: list[Any] = []

        # 2. Document Classification Badge & Title Block
        story.append(Paragraph(
            "<font color='#2563EB'><b>AUTONOMOUS TECHNICAL DOSSIER</b></font> &nbsp;|&nbsp; "
            "<font color='#64748B'>CROSS-VERIFIED EVIDENCE & PROVENANCE</font>",
            badge_style,
        ))

        if doc_plan.is_slides:
            disp_title = f"Slide 1: {doc_plan.metadata.title}"
        else:
            disp_title = doc_plan.metadata.title

        title_block = Table(
            [[
                Paragraph(disp_title, title_style),
            ]],
            colWidths=[504],
        )
        title_block.setStyle(
            TableStyle([
                ("LINELEFT", (0, 0), (0, -1), 4.0, colors.HexColor("#1E3A8A")),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ])
        )
        story.append(title_block)
        story.append(Spacer(1, 4))
        story.append(Paragraph(doc_plan.metadata.subtitle, subtitle_style))

        # 3. Modern Metadata Card (2x2 Grid)
        meta_data = [
            [
                Paragraph(f"<b>Research Scope:</b> {doc_plan.metadata.objective}", body_style),
                Paragraph(f"<b>Generation Date:</b> {doc_plan.metadata.date_str}", body_style),
            ],
            [
                Paragraph(
                    f"<b>Authoritative Sources:</b> {doc_plan.metadata.sources_count} visited websites"
                    if doc_plan.metadata.sources_count > 0
                    else "<b>Authoritative Sources:</b> Direct technical synthesis",
                    body_style,
                ),
                Paragraph(f"<b>Verified Empirical Findings:</b> {doc_plan.metadata.claims_count} validated claims", body_style),
            ],
        ]
        meta_table = Table(meta_data, colWidths=[330, 174])
        meta_table.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
                ("PADDING", (0, 0), (-1, -1), 7),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#E2E8F0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ])
        )
        story.append(meta_table)
        story.append(Spacer(1, 10))

        # 4. Render Sections and Generic Content Blocks from Document Plan
        for sec_idx, sec in enumerate(doc_plan.sections):
            if sec.page_break_before and story:
                story.append(PageBreak())

            sec_title_style = h1_style if sec.level == 1 else h2_style
            if doc_plan.is_slides and sec_idx > 0:
                story.append(Paragraph(f"Slide {sec_idx + 1}: {sec.title}", sec_title_style))
            else:
                story.append(Paragraph(sec.title, sec_title_style))
                if sec.level == 1:
                    story.append(HRFlowable(width="100%", thickness=1.0, color=colors.HexColor("#1E3A8A"), spaceBefore=2, spaceAfter=8))

            for block in sec.blocks:
                if block.block_type == BlockType.TEXT and block.text:
                    if block.title:
                        story.append(Paragraph(f"<b>{block.title}</b>", h2_style))
                    story.append(Paragraph(block.text, body_style))

                elif block.block_type == BlockType.BULLET_LIST and block.items:
                    if block.title:
                        story.append(Paragraph(f"<b>{block.title}</b>", body_style))
                    for item in block.items:
                        story.append(Paragraph(f"• {item}", bullet_style))
                    story.append(Spacer(1, 4))

                elif block.block_type == BlockType.CALLOUT:
                    callout_items = []
                    bg_col = colors.HexColor("#F8FAFC")
                    bar_col = colors.HexColor("#2563EB")

                    if block.callout_type == "contradiction":
                        bg_col = colors.HexColor("#FFFBEB")
                        bar_col = colors.HexColor("#F59E0B")
                        callout_items.append(Paragraph("<b><font color='#D97706'>⚠️ Contradictory Evidence Identified Across Literature</font></b>", body_style))
                    elif block.callout_type in ("takeaway", "synthesis"):
                        bg_col = colors.HexColor("#F0FDF4")
                        bar_col = colors.HexColor("#059669")
                        callout_items.append(Paragraph("<b><font color='#059669'>📌 Strategic Takeaway & Architectural Synthesis</font></b>", body_style))

                    if block.text:
                        callout_items.append(Paragraph(block.text, body_style))
                    for itm in block.items:
                        callout_items.append(Paragraph(f"• {itm}", bullet_style))

                    callout_tbl = Table([[callout_items]], colWidths=[504])
                    callout_tbl.setStyle(
                        TableStyle([
                            ("BACKGROUND", (0, 0), (-1, -1), bg_col),
                            ("LINELEFT", (0, 0), (0, -1), 3.5, bar_col),
                            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                            ("PADDING", (0, 0), (-1, -1), 7),
                            ("LEFTPADDING", (0, 0), (-1, -1), 12),
                        ])
                    )
                    story.append(callout_tbl)
                    story.append(Spacer(1, 8))

                elif block.block_type == BlockType.IMAGE and block.visual_asset:
                    v = block.visual_asset
                    local_img_path = ensure_local_image_asset(v, cache_dir=out_file.parent / "images")
                    if local_img_path and Path(local_img_path).exists():
                        story.append(Paragraph(f"<b>{v.title}</b>", h2_style))
                        try:
                            with PILImage.open(local_img_path) as pimg:
                                orig_w, orig_h = pimg.size
                            aspect = orig_h / max(orig_w, 1)
                            target_w = min(480.0, float(orig_w))
                            target_h = target_w * aspect
                            if target_h > 210.0:
                                target_h = 210.0
                                target_w = target_h / aspect
                            img = Image(local_img_path, width=target_w, height=target_h)
                            img.hAlign = "CENTER"
                            story.append(img)
                        except Exception:
                            img = Image(local_img_path, width=420, height=170)
                            img.hAlign = "CENTER"
                            story.append(img)
                        cap_text = f"Figure: {v.caption}" if v.caption else f"Figure: {v.title}"
                        if v.attribution:
                            cap_text += f" | {v.attribution}"
                        story.append(Paragraph(cap_text, caption_style))
                        story.append(Spacer(1, 6))

                elif block.block_type == BlockType.FORMULA and block.formula_item:
                    f = block.formula_item
                    formula_card_items = []
                    formula_card_items.append(Paragraph(f"<b>Mathematical Formulation: {f.name}</b>", h2_style))
                    if f.explanation:
                        formula_card_items.append(Paragraph(f.explanation, body_style))
                    matching_vis = next((va for va in pkg.visuals if va.metadata.get("formula_id") == f.formula_id), None)
                    if matching_vis and matching_vis.url_or_path and Path(matching_vis.url_or_path).exists():
                        f_img = Image(matching_vis.url_or_path, width=320, height=52)
                        f_img.hAlign = "CENTER"
                        formula_card_items.append(f_img)
                    else:
                        formula_card_items.append(Paragraph(f"<font face='Courier' color='#1E3A8A'><b>$$ {f.latex} $$</b></font>", body_style))
                    if f.worked_example:
                        formula_card_items.append(Spacer(1, 2))
                        formula_card_items.append(Paragraph(f"<b>Worked Example & Parameterization:</b> {f.worked_example}", body_style))

                    f_card = Table([[formula_card_items]], colWidths=[504])
                    f_card.setStyle(
                        TableStyle([
                            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
                            ("LINELEFT", (0, 0), (0, -1), 3.0, colors.HexColor("#4F46E5")),
                            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                            ("PADDING", (0, 0), (-1, -1), 7),
                            ("LEFTPADDING", (0, 0), (-1, -1), 12),
                        ])
                    )
                    story.append(f_card)
                    story.append(Spacer(1, 8))

                elif block.block_type == BlockType.TABLE and block.data_table:
                    t = block.data_table
                    story.append(Paragraph(f"<b>Table: {t.title}</b>", h2_style))
                    if t.caption:
                        story.append(Paragraph(t.caption, caption_style))

                    tbl_data = []
                    header_row = [Paragraph(f"<b><font color='#FFFFFF'>{h}</font></b>", body_style) for h in t.headers]
                    tbl_data.append(header_row)

                    for r in t.rows:
                        row_cells = [Paragraph(str(cell), body_style) for cell in r]
                        tbl_data.append(row_cells)

                    col_w = 504.0 / max(len(t.headers), 1)
                    rep_table = Table(tbl_data, colWidths=[col_w] * len(t.headers))
                    rep_table.setStyle(
                        TableStyle([
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E293B")),
                            ("PADDING", (0, 0), (-1, -1), 5),
                            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                            ("TOPPADDING", (0, 0), (-1, 0), 6),
                            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#FFFFFF"), colors.HexColor("#F8FAFC")]),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ])
                    )
                    story.append(rep_table)
                    story.append(Spacer(1, 10))

                elif block.block_type == BlockType.PROVENANCE_TABLE:
                    story.append(Paragraph(f"<b>{block.title or 'Source Provenance & Verification Ledger'}</b>", h2_style))
                    if ordered_sources:
                        ref_rows = [
                            [
                                Paragraph("<b><font color='#FFFFFF'>#</font></b>", body_style),
                                Paragraph("<b><font color='#FFFFFF'>Document Title & Publisher</font></b>", body_style),
                                Paragraph("<b><font color='#FFFFFF'>Canonical URL</font></b>", body_style),
                                Paragraph("<b><font color='#FFFFFF'>Verification Tier</font></b>", body_style),
                            ]
                        ]
                        for num, src in ordered_sources:
                            tier = src.reliability_tier or ("Peer-Reviewed" if src.is_primary else "Credible Reference")
                            ref_rows.append([
                                Paragraph(f"[{num}]", body_style),
                                Paragraph(f"{src.title or src.domain}<br/><font color='#64748B'>{src.domain}</font>", body_style),
                                Paragraph(f"<font color='#0284C7'>{src.url}</font>", body_style),
                                Paragraph(f"{tier}", body_style),
                            ])
                        ref_table = Table(ref_rows, colWidths=[24, 180, 210, 90])
                        ref_table.setStyle(
                            TableStyle([
                                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E293B")),
                                ("PADDING", (0, 0), (-1, -1), 5),
                                ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                                ("TOPPADDING", (0, 0), (-1, 0), 6),
                                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#FFFFFF"), colors.HexColor("#F8FAFC")]),
                                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ])
                        )
                        story.append(ref_table)
                        story.append(Spacer(1, 10))
                        story.append(Spacer(1, 10))

                elif block.block_type == BlockType.BIBLIOGRAPHY:
                    if ordered_sources:
                        story.append(Paragraph("<b>Bibliographic Citations & Verified URLs:</b>", body_style))
                        for num, src in ordered_sources:
                            c_title = src.title or src.domain
                            acc_date = datetime.datetime.fromtimestamp(src.access_timestamp, datetime.timezone.utc).strftime("%Y.%m.%d")
                            tier = src.reliability_tier or ("Peer-Reviewed" if src.is_primary else "Credible Reference")
                            p_txt = f"[{num}] <b>\"{c_title}\"</b>. <i>{src.domain}</i> • Tier: {tier} • Accessed: {acc_date}<br/>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<font color='#0284C7'>{src.url}</font>"
                            story.append(Paragraph(p_txt, bullet_style))
                    else:
                        story.append(
                            Paragraph(
                                "<i>No external sources were consulted or cited for this deliverable. "
                                "Findings are compiled from conversational context without verified external citations.</i>",
                                body_style,
                            )
                        )

        doc.build(story, canvasmaker=NumberedCanvas)
        return str(out_file)
