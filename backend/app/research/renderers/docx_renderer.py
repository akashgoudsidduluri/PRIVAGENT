"""Professional DOCX Research Document Renderer using python-docx.

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
      Format-Specific Renderer (DocxRenderer)
              |
              v
       Final Deliverable

Contains zero domain-specific or topic-specific assumptions. Generates beautifully
styled Word reports with cover metadata, executive summary, subtopic deep-dives,
tables with styled headers, embedded visual figures, discrepancy callout blocks,
and complete source provenance across any domain.
"""
from __future__ import annotations

import datetime
from pathlib import Path
import re
from typing import Any, Optional

import docx  # type: ignore
from docx.enum.table import WD_TABLE_ALIGNMENT  # type: ignore
from docx.enum.text import WD_ALIGN_PARAGRAPH  # type: ignore
from docx.oxml import OxmlElement, parse_xml  # type: ignore
from docx.oxml.ns import nsdecls, qn  # type: ignore
from docx.shared import Inches, Pt, RGBColor  # type: ignore

from ..document_plan import (
    BlockType,
    ContentBlock,
    DocumentSection,
    build_structured_document_plan,
)
from ..models import (
    EvidenceClassification,
    ResearchKnowledgePackage,
)
from ..visuals.image_pipeline import ensure_local_image_asset
from .base import BaseRenderer


# Styling Constants
NAVY = RGBColor(0x1E, 0x3A, 0x8A)
SLATE = RGBColor(0x33, 0x41, 0x55)
DARK_GRAY = RGBColor(0x1E, 0x29, 0x3B)
LIGHT_GRAY = RGBColor(0x64, 0x74, 0x8B)
AMBER = RGBColor(0xD9, 0x77, 0x06)


def _set_cell_background(cell: Any, fill_hex: str) -> None:
    """Set XML shading for table cell."""
    tcPr = cell._tc.get_or_add_tcPr()
    shd = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{fill_hex}"/>')
    tcPr.append(shd)


def _set_cell_margins(cell: Any, top: int = 100, bottom: int = 100, left: int = 150, right: int = 150) -> None:
    """Set inner cell padding in twips (1/20 of a pt)."""
    tcPr = cell._tc.get_or_add_tcPr()
    tcMar = parse_xml(
        f'<w:tcMar {nsdecls("w")}>'
        f'<w:top w:w="{top}" w:type="dxa"/>'
        f'<w:bottom w:w="{bottom}" w:type="dxa"/>'
        f'<w:left w:w="{left}" w:type="dxa"/>'
        f'<w:right w:w="{right}" w:type="dxa"/>'
        f'</w:tcMar>'
    )
    tcPr.append(tcMar)


def _clean_markdown_text(text: str) -> str:
    """Strip top-level markdown formatting such as block markers or headers."""
    if not text:
        return ""
    clean = re.sub(r'^(?:#{1,6}\s+)', '', text.strip())
    clean = re.sub(r'^(?:[-*+]\s+|\d+[\.\)]\s+)', '', clean)
    return clean.strip()


def _append_formatted_runs(
    paragraph: Any,
    text: str,
    base_font_size: Pt = Pt(10.5),
    default_color: RGBColor = DARK_GRAY,
) -> None:
    """Parse inline markdown (bold, italic, inline code, inline math, html tags) into Word runs."""
    if not text:
        return

    # Strip simple HTML wrapper tags like <b>, <i>, <font>
    cleaned = re.sub(r'</?(?:b|i|font|span|div|p)[^>]*>', '', text)

    token_pattern = re.compile(
        r'(\$\$.*?\$\$|\$.*?\$|\*\*.*?\*\*|__.*?__|`.*?`|\*[^*\n]+?\*|_[^_\n]+?_)'
    )
    tokens = [t for t in token_pattern.split(cleaned) if t]
    for token in tokens:
        if token.startswith("$$") and token.endswith("$$") and len(token) > 4:
            content = token[2:-2].strip()
            run = paragraph.add_run(content)
            run.font.name = "Cambria Math"
            run.font.size = Pt(base_font_size.pt + 0.5)
            run.font.italic = True
            run.font.color.rgb = NAVY
        elif token.startswith("$") and token.endswith("$") and len(token) > 2:
            content = token[1:-1].strip()
            run = paragraph.add_run(content)
            run.font.name = "Cambria Math"
            run.font.size = base_font_size
            run.font.italic = True
            run.font.color.rgb = NAVY
        elif (token.startswith("**") and token.endswith("**") and len(token) > 4) or \
             (token.startswith("__") and token.endswith("__") and len(token) > 4):
            content = token[2:-2]
            run = paragraph.add_run(content)
            run.font.name = "Calibri"
            run.font.size = base_font_size
            run.font.bold = True
            run.font.color.rgb = default_color
        elif (token.startswith("*") and token.endswith("*") and len(token) > 2) or \
             (token.startswith("_") and token.endswith("_") and len(token) > 2):
            content = token[1:-1]
            run = paragraph.add_run(content)
            run.font.name = "Calibri"
            run.font.size = base_font_size
            run.font.italic = True
            run.font.color.rgb = default_color
        elif token.startswith("`") and token.endswith("`") and len(token) > 2:
            content = token[1:-1]
            run = paragraph.add_run(content)
            run.font.name = "Consolas"
            run.font.size = Pt(max(8.0, base_font_size.pt - 1.0))
            run.font.color.rgb = SLATE
        else:
            run = paragraph.add_run(token)
            run.font.name = "Calibri"
            run.font.size = base_font_size
            run.font.color.rgb = default_color


class DocxRenderer(BaseRenderer):
    """Generates professional Microsoft Word (.docx) research deliverables via StructuredDocumentPlan."""

    def render(self, pkg: ResearchKnowledgePackage, output_path: str, **kwargs: Any) -> str:
        out_file = self.prepare_output_path(output_path)
        cit_map = self.build_citation_map(pkg)
        ordered_sources = self.get_ordered_sources(pkg, cit_map)

        # 1. Build Canonical Structured Document Plan
        page_constraint = kwargs.pop("page_constraint", None)
        doc_plan = build_structured_document_plan(
            pkg,
            target_format="docx",
            page_constraint=page_constraint,
            **kwargs,
        )

        doc = docx.Document()

        # Set 1-inch margins
        for section in doc.sections:
            section.top_margin = Inches(1.0)
            section.bottom_margin = Inches(1.0)
            section.left_margin = Inches(1.0)
            section.right_margin = Inches(1.0)

            # Header & Footer setup
            header = section.header
            hp = header.paragraphs[0]
            hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            hrun = hp.add_run(f"{doc_plan.metadata.title} | PrivAgent Research Engine")
            hrun.font.name = "Calibri"
            hrun.font.size = Pt(8.5)
            hrun.font.color.rgb = LIGHT_GRAY

            footer = section.footer
            fp = footer.paragraphs[0]
            fp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            frun1 = fp.add_run("PrivAgent Autonomous Research Deliverable — Confidential & Verified        Page ")
            frun1.font.name = "Calibri"
            frun1.font.size = Pt(8.5)
            frun1.font.color.rgb = LIGHT_GRAY
            fld_page = parse_xml(r'<w:fldSimple %s w:instr="PAGE"/>' % nsdecls('w'))
            fp._p.append(fld_page)
            frun2 = fp.add_run(" of ")
            frun2.font.name = "Calibri"
            frun2.font.size = Pt(8.5)
            frun2.font.color.rgb = LIGHT_GRAY
            fld_numpages = parse_xml(r'<w:fldSimple %s w:instr="NUMPAGES"/>' % nsdecls('w'))
            fp._p.append(fld_numpages)

        # Configure Normal style
        style_normal = doc.styles["Normal"]
        style_normal.font.name = "Calibri"
        style_normal.font.size = Pt(10.5)
        style_normal.font.color.rgb = DARK_GRAY
        style_normal.paragraph_format.line_spacing = 1.15
        style_normal.paragraph_format.space_after = Pt(4)

        # -------------------------------------------------------------------
        # Cover / Document Title Header
        # -------------------------------------------------------------------
        p_title = doc.add_paragraph()
        p_title.alignment = WD_ALIGN_PARAGRAPH.LEFT
        p_title.paragraph_format.space_before = Pt(0)
        p_title.paragraph_format.space_after = Pt(4)
        r_title = p_title.add_run(doc_plan.metadata.title)
        r_title.font.name = "Calibri"
        r_title.font.size = Pt(22)
        r_title.font.bold = True
        r_title.font.color.rgb = NAVY

        p_sub = doc.add_paragraph()
        p_sub.paragraph_format.space_after = Pt(10)
        r_sub = p_sub.add_run(doc_plan.metadata.subtitle)
        r_sub.font.size = Pt(11.5)
        r_sub.font.italic = True
        r_sub.font.color.rgb = SLATE

        # Metadata Callout Card
        meta_table = doc.add_table(rows=2, cols=2)
        meta_table.alignment = WD_TABLE_ALIGNMENT.CENTER
        for row in meta_table.rows:
            row.cells[0].width = Inches(3.25)
            row.cells[1].width = Inches(3.25)

        meta_cells = [
            ("Research Objective:", doc_plan.metadata.objective, 0, 0),
            ("Date Generated:", doc_plan.metadata.date_str, 0, 1),
            ("Sources Examined:", f"{doc_plan.metadata.sources_count} authoritative sources", 1, 0),
            ("Verified Claims:", f"{doc_plan.metadata.claims_count} synthesized claims", 1, 1),
        ]
        for label, val, r, c in meta_cells:
            cell = meta_table.cell(r, c)
            _set_cell_background(cell, "F1F5F9")
            _set_cell_margins(cell, top=80, bottom=80, left=120, right=120)
            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(2)
            run_lbl = p.add_run(f"{label} ")
            run_lbl.font.bold = True
            run_lbl.font.size = Pt(9.0)
            run_lbl.font.color.rgb = SLATE
            run_val = p.add_run(val)
            run_val.font.size = Pt(9.0)

        doc.add_paragraph().paragraph_format.space_after = Pt(8)

        # -------------------------------------------------------------------
        # Render Sections from Structured Document Plan
        # -------------------------------------------------------------------
        for sec in doc_plan.sections:
            if sec.page_break_before:
                doc.add_page_break()

            # Section Heading
            h = doc.add_heading(sec.title, level=sec.level)
            if h.runs:
                h.runs[0].font.color.rgb = NAVY if sec.level == 1 else SLATE
            h.paragraph_format.space_before = Pt(14 if sec.level == 1 else 10)
            h.paragraph_format.space_after = Pt(4)

            # Render Content Blocks
            for block in sec.blocks:
                if block.block_type == BlockType.TEXT:
                    if block.title:
                        p_t = doc.add_paragraph()
                        p_t.paragraph_format.space_before = Pt(4)
                        p_t.paragraph_format.space_after = Pt(2)
                        r = p_t.add_run(block.title)
                        r.font.bold = True
                        r.font.size = Pt(11)
                        r.font.color.rgb = SLATE
                    if block.text:
                        p_txt = doc.add_paragraph()
                        _append_formatted_runs(p_txt, block.text)

                elif block.block_type == BlockType.BULLET_LIST:
                    if block.title:
                        p_bt = doc.add_paragraph()
                        p_bt.paragraph_format.space_before = Pt(4)
                        p_bt.paragraph_format.space_after = Pt(2)
                        r = p_bt.add_run(block.title)
                        r.font.bold = True
                        r.font.size = Pt(10.5)
                        r.font.color.rgb = SLATE
                    for item_text in block.items:
                        p_item = doc.add_paragraph(style="List Bullet")
                        p_item.paragraph_format.space_after = Pt(3)
                        _append_formatted_runs(p_item, item_text)

                elif block.block_type == BlockType.CALLOUT:
                    # Styled callout card
                    is_contra = block.callout_type == "contradiction"
                    bg_color = "FFFBEB" if is_contra else "EFF6FF"
                    border_color = AMBER if is_contra else NAVY

                    callout = doc.add_table(rows=1, cols=1)
                    callout.alignment = WD_TABLE_ALIGNMENT.CENTER
                    c_cell = callout.cell(0, 0)
                    c_cell.width = Inches(6.5)
                    _set_cell_background(c_cell, bg_color)
                    _set_cell_margins(c_cell, top=100, bottom=100, left=140, right=140)

                    p_c = c_cell.paragraphs[0]
                    lead_txt = "⚠️ Discrepancies & Divergent Findings Identified:" if is_contra else "ℹ️ Technical Note & Methodology:"
                    r_lead = p_c.add_run(lead_txt + "\n")
                    r_lead.font.bold = True
                    r_lead.font.color.rgb = border_color

                    if block.text:
                        p_b = c_cell.add_paragraph()
                        _append_formatted_runs(p_b, block.text)

                    for c_item in block.items:
                        p_it = c_cell.add_paragraph(style="List Bullet")
                        _append_formatted_runs(p_it, c_item)

                    doc.add_paragraph().paragraph_format.space_after = Pt(6)

                elif block.block_type == BlockType.IMAGE:
                    vis = block.visual_asset
                    if vis:
                        p_fig_head = doc.add_paragraph()
                        p_fig_head.paragraph_format.space_before = Pt(6)
                        p_fig_head.paragraph_format.space_after = Pt(2)
                        r_fig = p_fig_head.add_run(vis.title)
                        r_fig.font.bold = True
                        r_fig.font.size = Pt(10.5)
                        r_fig.font.color.rgb = SLATE

                        # Embed image: check local or safely fetch remote URL
                        local_path = ensure_local_image_asset(vis, cache_dir=out_file.parent / "images")
                        has_img = False
                        if local_path and Path(local_path).exists():
                            try:
                                p_img = doc.add_paragraph()
                                p_img.alignment = WD_ALIGN_PARAGRAPH.CENTER
                                p_img.paragraph_format.space_before = Pt(4)
                                p_img.paragraph_format.space_after = Pt(3)
                                p_img.add_run().add_picture(local_path, width=Inches(5.0))
                                has_img = True
                            except Exception:
                                has_img = False

                        if not has_img:
                            # Render figure box representation
                            fig_box = doc.add_table(rows=1, cols=1)
                            fig_box.alignment = WD_TABLE_ALIGNMENT.CENTER
                            fb_cell = fig_box.cell(0, 0)
                            fb_cell.width = Inches(6.5)
                            _set_cell_background(fb_cell, "F8FAFC")
                            _set_cell_margins(fb_cell, top=100, bottom=100, left=140, right=140)
                            p_box = fb_cell.paragraphs[0]
                            p_box.alignment = WD_ALIGN_PARAGRAPH.CENTER
                            r_b1 = p_box.add_run(f"[Visual Asset: {vis.title}]\n")
                            r_b1.font.bold = True
                            r_b1.font.color.rgb = NAVY
                            if vis.caption:
                                r_b2 = p_box.add_run(f"{vis.caption}\n")
                                r_b2.font.italic = True
                                r_b2.font.size = Pt(9.5)
                            if vis.attribution:
                                r_b3 = p_box.add_run(f"{vis.attribution}")
                                r_b3.font.size = Pt(8.5)
                                r_b3.font.color.rgb = LIGHT_GRAY

                        # Caption & Provenance Attribution
                        if vis.caption or vis.attribution:
                            p_cap = doc.add_paragraph()
                            p_cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
                            p_cap.paragraph_format.space_after = Pt(6)
                            if vis.caption:
                                r_c = p_cap.add_run(f"Figure: {vis.caption} ")
                                r_c.font.italic = True
                                r_c.font.size = Pt(9.0)
                                r_c.font.color.rgb = LIGHT_GRAY
                            if vis.attribution:
                                r_a = p_cap.add_run(f"({vis.attribution})")
                                r_a.font.size = Pt(8.5)
                                r_a.font.color.rgb = LIGHT_GRAY

                elif block.block_type == BlockType.FORMULA:
                    formula = block.formula_item
                    if formula:
                        p_fh = doc.add_paragraph()
                        p_fh.paragraph_format.space_before = Pt(6)
                        p_fh.paragraph_format.space_after = Pt(2)
                        r_fh = p_fh.add_run(formula.name)
                        r_fh.font.bold = True
                        r_fh.font.size = Pt(11)
                        r_fh.font.color.rgb = SLATE

                        if formula.explanation:
                            p_fe = doc.add_paragraph()
                            _append_formatted_runs(p_fe, formula.explanation)

                        # Look for rendered PNG or display equation block
                        matching_vis = next(
                            (v for v in pkg.visuals if v.metadata.get("formula_id") == formula.formula_id),
                            None,
                        )
                        has_rendered_eq = False
                        if matching_vis and matching_vis.url_or_path and Path(matching_vis.url_or_path).exists():
                            try:
                                p_eq_img = doc.add_paragraph()
                                p_eq_img.alignment = WD_ALIGN_PARAGRAPH.CENTER
                                p_eq_img.paragraph_format.space_before = Pt(3)
                                p_eq_img.paragraph_format.space_after = Pt(3)
                                p_eq_img.add_run().add_picture(matching_vis.url_or_path, width=Inches(4.2))
                                has_rendered_eq = True
                            except Exception:
                                has_rendered_eq = False

                        if not has_rendered_eq:
                            # Formula callout display
                            p_form = doc.add_paragraph()
                            p_form.paragraph_format.left_indent = Inches(0.4)
                            p_form.paragraph_format.space_before = Pt(2)
                            p_form.paragraph_format.space_after = Pt(4)
                            r_math = p_form.add_run(formula.latex)
                            r_math.font.name = "Cambria Math"
                            r_math.font.size = Pt(11)
                            r_math.font.italic = True
                            r_math.font.color.rgb = NAVY

                        if formula.worked_example:
                            p_we = doc.add_paragraph()
                            p_we.add_run("Worked Formulation Example: ").bold = True
                            _append_formatted_runs(p_we, formula.worked_example)

                elif block.block_type == BlockType.TABLE:
                    tbl = block.data_table
                    if tbl:
                        p_th = doc.add_paragraph()
                        p_th.paragraph_format.space_before = Pt(6)
                        p_th.paragraph_format.space_after = Pt(2)
                        r_th = p_th.add_run(tbl.title)
                        r_th.font.bold = True
                        r_th.font.size = Pt(11)
                        r_th.font.color.rgb = SLATE

                        num_cols = len(tbl.headers)
                        num_rows = len(tbl.rows) + 1
                        table_obj = doc.add_table(rows=num_rows, cols=num_cols)
                        table_obj.alignment = WD_TABLE_ALIGNMENT.CENTER
                        col_w = Inches(6.5 / max(num_cols, 1))

                        # Header Row
                        for col_idx, header_text in enumerate(tbl.headers):
                            cell = table_obj.cell(0, col_idx)
                            cell.width = col_w
                            _set_cell_background(cell, "1E3A8A")
                            _set_cell_margins(cell, top=100, bottom=100, left=120, right=120)
                            p = cell.paragraphs[0]
                            p.paragraph_format.space_after = Pt(0)
                            r = p.add_run(str(header_text))
                            r.font.bold = True
                            r.font.size = Pt(9.5)
                            r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

                        # Data Rows
                        for row_idx, row_data in enumerate(tbl.rows, start=1):
                            bg_c = "F8FAFC" if row_idx % 2 == 1 else "FFFFFF"
                            for col_idx in range(num_cols):
                                cell = table_obj.cell(row_idx, col_idx)
                                cell.width = col_w
                                _set_cell_background(cell, bg_c)
                                _set_cell_margins(cell, top=80, bottom=80, left=120, right=120)
                                val = str(row_data[col_idx]) if col_idx < len(row_data) else ""
                                p = cell.paragraphs[0]
                                p.paragraph_format.space_after = Pt(0)
                                _append_formatted_runs(p, val, base_font_size=Pt(9))

                        if tbl.caption:
                            p_tcap = doc.add_paragraph(tbl.caption)
                            p_tcap.paragraph_format.space_after = Pt(6)
                            p_tcap.runs[0].font.italic = True
                            p_tcap.runs[0].font.size = Pt(9)
                            p_tcap.runs[0].font.color.rgb = LIGHT_GRAY

                elif block.block_type == BlockType.PROVENANCE_TABLE:
                    if ordered_sources:
                        p_ph = doc.add_paragraph()
                        p_ph.paragraph_format.space_before = Pt(6)
                        p_ph.paragraph_format.space_after = Pt(2)
                        r_ph = p_ph.add_run("Source Provenance & Verification Ledger")
                        r_ph.font.bold = True
                        r_ph.font.size = Pt(11)
                        r_ph.font.color.rgb = SLATE

                        ledger = doc.add_table(rows=len(ordered_sources) + 1, cols=3)
                        ledger.alignment = WD_TABLE_ALIGNMENT.CENTER
                        col_widths = [Inches(0.6), Inches(4.4), Inches(1.5)]
                        headers = ["#", "Document Title & Publisher", "Verification Tier"]

                        for col_idx, h_text in enumerate(headers):
                            cell = ledger.cell(0, col_idx)
                            cell.width = col_widths[col_idx]
                            _set_cell_background(cell, "1E3A8A")
                            _set_cell_margins(cell, top=80, bottom=80, left=100, right=100)
                            p = cell.paragraphs[0]
                            p.paragraph_format.space_after = Pt(0)
                            r = p.add_run(h_text)
                            r.font.bold = True
                            r.font.size = Pt(9.0)
                            r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

                        for row_idx, (num, src) in enumerate(ordered_sources, start=1):
                            bg = "F8FAFC" if row_idx % 2 == 1 else "FFFFFF"
                            tier_val = (getattr(src, "reliability_tier", None) or "credible_reference").replace("_", " ").title()
                            vals = [
                                f"[{num}]",
                                f"{src.title or src.domain} ({src.domain})",
                                tier_val,
                            ]
                            for col_idx, val in enumerate(vals):
                                cell = ledger.cell(row_idx, col_idx)
                                cell.width = col_widths[col_idx]
                                _set_cell_background(cell, bg)
                                _set_cell_margins(cell, top=60, bottom=60, left=100, right=100)
                                p = cell.paragraphs[0]
                                p.paragraph_format.space_after = Pt(0)
                                _append_formatted_runs(p, val, base_font_size=Pt(8.5))

                        doc.add_paragraph().paragraph_format.space_after = Pt(6)

                elif block.block_type == BlockType.BIBLIOGRAPHY:
                    if ordered_sources:
                        p_bh = doc.add_paragraph()
                        p_bh.paragraph_format.space_before = Pt(6)
                        p_bh.paragraph_format.space_after = Pt(4)
                        r_bh = p_bh.add_run("Bibliographic Citations & Verified URLs:")
                        r_bh.font.bold = True
                        r_bh.font.size = Pt(9.5)
                        r_bh.font.color.rgb = SLATE

                        for num, src in ordered_sources:
                            p_ref = doc.add_paragraph()
                            p_ref.paragraph_format.left_indent = Inches(0.4)
                            p_ref.paragraph_format.first_line_indent = Inches(-0.4)
                            p_ref.paragraph_format.space_after = Pt(4)
                            p_ref.paragraph_format.line_spacing = 1.15

                            r_num = p_ref.add_run(f"[{num}] ")
                            r_num.font.bold = True
                            r_num.font.color.rgb = NAVY
                            r_num.font.size = Pt(9.0)

                            title_text = src.title or src.domain
                            r_title = p_ref.add_run(f'"{title_text}". ')
                            r_title.font.bold = True
                            r_title.font.size = Pt(9.0)
                            r_title.font.color.rgb = DARK_GRAY

                            if src.domain:
                                r_dom = p_ref.add_run(f"{src.domain}")
                                r_dom.font.size = Pt(8.5)
                                r_dom.font.color.rgb = SLATE

                            tier_name = getattr(src, "reliability_tier", None)
                            if tier_name:
                                tier_display = str(tier_name).replace("_", " ").title()
                                r_tier = p_ref.add_run(f" • Tier: {tier_display}")
                                r_tier.font.size = Pt(8.0)
                                r_tier.font.color.rgb = LIGHT_GRAY

                            if getattr(src, "publication_date", None):
                                r_pub = p_ref.add_run(f" • Published: {src.publication_date}")
                                r_pub.font.size = Pt(8.0)
                                r_pub.font.color.rgb = LIGHT_GRAY
                            elif getattr(src, "access_timestamp", None):
                                try:
                                    dt = datetime.datetime.fromtimestamp(src.access_timestamp, datetime.timezone.utc)
                                    r_date = p_ref.add_run(f" • Accessed: {dt.strftime('%Y-%m-%d')}")
                                    r_date.font.size = Pt(8.0)
                                    r_date.font.color.rgb = LIGHT_GRAY
                                except Exception:
                                    pass

                            if src.url:
                                p_ref.add_run("\n     ")
                                r_url = p_ref.add_run(src.url)
                                r_url.font.size = Pt(8.0)
                                r_url.font.color.rgb = NAVY
                                r_url.font.underline = True
                    else:
                        p_no = doc.add_paragraph()
                        r_no = p_no.add_run("No external sources were consulted or cited for this deliverable. Findings are compiled from conversational context without verified external citations.")
                        r_no.italic = True
                        r_no.font.size = Pt(9.0)

        doc.save(str(out_file))
        return str(out_file)
