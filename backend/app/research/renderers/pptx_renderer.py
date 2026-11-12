"""Professional 16:9 Presentation (.pptx) Research Deliverable Renderer using python-pptx.

Generates widescreen slide decks with executive summaries, native styled tables,
embedded architectural figures, charts, discrepancy callouts, and source citations.
"""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Optional

import pptx
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

from ..models import (
    EvidenceClassification,
    ResearchKnowledgePackage,
)
from .base import BaseRenderer


# Theme Palette
NAVY = RGBColor(0x1E, 0x3A, 0x8A)
SLATE = RGBColor(0x47, 0x55, 0x69)
DARK = RGBColor(0x0F, 0x17, 0x2A)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
TEAL = RGBColor(0x0D, 0x94, 0x88)
AMBER = RGBColor(0xD9, 0x77, 0x06)
CARD_BG = RGBColor(0xF1, 0xF5, 0xF9)
BORDER_GRAY = RGBColor(0xCB, 0xD5, 0xE1)


def _add_slide_header(slide: Any, title_text: str, category: str = "RESEARCH DELIVERABLE") -> None:
    """Add standard top title and category banner to a slide."""
    tb = slide.shapes.add_textbox(Inches(0.8), Inches(0.5), Inches(11.7), Inches(1.1))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_top = tf.margin_right = tf.margin_bottom = 0

    p_cat = tf.paragraphs[0]
    p_cat.text = category.upper()
    p_cat.font.name = "Segoe UI"
    p_cat.font.size = Pt(9.5)
    p_cat.font.bold = True
    p_cat.font.color.rgb = TEAL

    p_title = tf.add_paragraph()
    p_title.text = title_text
    p_title.font.name = "Segoe UI"
    p_title.font.size = Pt(22)
    p_title.font.bold = True
    p_title.font.color.rgb = NAVY


def _add_slide_footer(slide: Any, current_slide: int, total_slides: int) -> None:
    """Add running confidentiality notice and slide number."""
    tb = slide.shapes.add_textbox(Inches(0.8), Inches(6.9), Inches(11.7), Inches(0.4))
    tf = tb.text_frame
    tf.margin_left = tf.margin_top = tf.margin_right = tf.margin_bottom = 0
    p = tf.paragraphs[0]
    p.text = "PrivAgent Autonomous Research Engine — Verified Technical Intelligence"
    p.font.name = "Segoe UI"
    p.font.size = Pt(8.5)
    p.font.color.rgb = SLATE

    p_num = tf.add_paragraph()
    p_num.alignment = PP_ALIGN.RIGHT
    p_num.text = f"{current_slide} / {total_slides}"
    p_num.font.name = "Segoe UI"
    p_num.font.size = Pt(8.5)
    p_num.font.color.rgb = SLATE


class PptxRenderer(BaseRenderer):
    """Generates 16:9 widescreen presentation slide decks (.pptx)."""

    def render(self, pkg: ResearchKnowledgePackage, output_path: str, **kwargs: Any) -> str:
        out_file = self.prepare_output_path(output_path)
        cit_map = self.build_citation_map(pkg)
        ordered_sources = self.get_ordered_sources(pkg, cit_map)

        prs = pptx.Presentation()
        prs.slide_width = Inches(13.333)
        prs.slide_height = Inches(7.5)
        blank_layout = prs.slide_layouts[6]

        slides_list: list[Any] = []

        # -------------------------------------------------------------------
        # Slide 1: Title Slide
        # -------------------------------------------------------------------
        s1 = prs.slides.add_slide(blank_layout)
        slides_list.append(s1)

        # Background card accent
        accent_card = s1.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.8), Inches(1.2), Inches(11.733), Inches(5.1))
        accent_card.fill.solid()
        accent_card.fill.fore_color.rgb = CARD_BG
        accent_card.line.color.rgb = BORDER_GRAY
        accent_card.line.width = Pt(1)

        tb_title = s1.shapes.add_textbox(Inches(1.4), Inches(2.0), Inches(10.5), Inches(3.2))
        tf_title = tb_title.text_frame
        tf_title.word_wrap = True

        p_pre = tf_title.paragraphs[0]
        p_pre.text = "PRIVAGENT AUTONOMOUS RESEARCH"
        p_pre.font.name = "Segoe UI"
        p_pre.font.size = Pt(12)
        p_pre.font.bold = True
        p_pre.font.color.rgb = TEAL

        p_main = tf_title.add_paragraph()
        p_main.text = pkg.topic.title
        p_main.font.name = "Segoe UI"
        p_main.font.size = Pt(32)
        p_main.font.bold = True
        p_main.font.color.rgb = NAVY
        p_main.space_before = Pt(8)

        p_sub = tf_title.add_paragraph()
        p_sub.text = pkg.topic.objective
        p_sub.font.name = "Segoe UI"
        p_sub.font.size = Pt(14)
        p_sub.font.color.rgb = SLATE
        p_sub.space_before = Pt(12)

        now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%B %d, %Y")
        p_meta = tf_title.add_paragraph()
        p_meta.text = f"Deliverable Format: Presentation | Generated: {now_str} | Sources Examined: {len(pkg.sources)}"
        p_meta.font.name = "Segoe UI"
        p_meta.font.size = Pt(10)
        p_meta.font.color.rgb = SLATE
        p_meta.space_before = Pt(16)

        # -------------------------------------------------------------------
        # Slide 2: Executive Summary & Metrics
        # -------------------------------------------------------------------
        s2 = prs.slides.add_slide(blank_layout)
        slides_list.append(s2)
        _add_slide_header(s2, "Executive Summary & Core Objectives", "EXECUTIVE BRIEFING")

        tb_exec = s2.shapes.add_textbox(Inches(0.8), Inches(1.8), Inches(7.5), Inches(4.8))
        tf_exec = tb_exec.text_frame
        tf_exec.word_wrap = True

        p_sc = tf_exec.paragraphs[0]
        p_sc.text = f"Research Scope: {pkg.topic.scope or 'Comprehensive multi-source investigation'}"
        p_sc.font.name = "Segoe UI"
        p_sc.font.size = Pt(12)
        p_sc.font.bold = True
        p_sc.font.color.rgb = DARK

        high_conf = [c for c in pkg.claims.values() if c.confidence >= 0.85]
        for c in high_conf[:4]:
            p_clm = tf_exec.add_paragraph()
            p_clm.text = f"• [{c.claim_type.upper()}] {c.statement}"
            p_clm.font.name = "Segoe UI"
            p_clm.font.size = Pt(11)
            p_clm.font.color.rgb = DARK
            p_clm.space_before = Pt(10)

        # Metric Tiles on Right Side
        metric_boxes = [
            ("Sources Examined", f"{len(pkg.sources)}", Inches(8.8), Inches(1.8)),
            ("Verified Claims", f"{len(pkg.claims)}", Inches(8.8), Inches(3.2)),
            ("Data Tables", f"{len(pkg.tables)}", Inches(8.8), Inches(4.6)),
        ]
        for lbl, num, x, y in metric_boxes:
            card = s2.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, Inches(3.7), Inches(1.2))
            card.fill.solid()
            card.fill.fore_color.rgb = CARD_BG
            card.line.color.rgb = BORDER_GRAY
            tf_m = card.text_frame
            p_n = tf_m.paragraphs[0]
            p_n.text = num
            p_n.font.name = "Segoe UI"
            p_n.font.size = Pt(26)
            p_n.font.bold = True
            p_n.font.color.rgb = NAVY
            p_n.alignment = PP_ALIGN.CENTER
            p_l = tf_m.add_paragraph()
            p_l.text = lbl
            p_l.font.name = "Segoe UI"
            p_l.font.size = Pt(10)
            p_l.font.color.rgb = SLATE
            p_l.alignment = PP_ALIGN.CENTER

        # -------------------------------------------------------------------
        # Slides 3+: Subtopic Deep Dives & Analysis
        # -------------------------------------------------------------------
        subtopics = pkg.topic.subtopics
        if not subtopics:
            # Create synthetic subtopic groups from claims if none provided
            types = list(set(c.claim_type for c in pkg.claims.values()))
            for t_idx, t_name in enumerate(types[:6], start=1):
                s_sub = prs.slides.add_slide(blank_layout)
                slides_list.append(s_sub)
                _add_slide_header(s_sub, f"{t_name.title()} Findings", "TECHNICAL DEEP DIVE")
                tb_sub = s_sub.shapes.add_textbox(Inches(0.8), Inches(1.8), Inches(11.7), Inches(4.8))
                tf_sub = tb_sub.text_frame
                tf_sub.word_wrap = True
                type_claims = [c for c in pkg.claims.values() if c.claim_type == t_name]
                for c in type_claims[:5]:
                    p = tf_sub.add_paragraph()
                    p.text = f"• {c.statement}"
                    p.font.name = "Segoe UI"
                    p.font.size = Pt(12)
                    p.space_before = Pt(12)
        else:
            for s_idx, sub in enumerate(subtopics, start=1):
                s_sub = prs.slides.add_slide(blank_layout)
                slides_list.append(s_sub)
                _add_slide_header(s_sub, sub.title, f"SUBTOPIC 0{s_idx}")

                sub_claims = [c for c in pkg.claims.values() if c.subtopic_id == sub.subtopic_id or c.claim_id in sub.claim_ids]
                # Check for associated visual asset
                matching_vis = next((v for v in pkg.visuals if v.subtopic_id == sub.subtopic_id and v.url_or_path and Path(v.url_or_path).exists()), None)

                content_width = Inches(6.0) if matching_vis else Inches(11.7)
                tb_sub = s_sub.shapes.add_textbox(Inches(0.8), Inches(1.8), content_width, Inches(4.8))
                tf_sub = tb_sub.text_frame
                tf_sub.word_wrap = True

                if sub.findings_summary:
                    p_sum = tf_sub.paragraphs[0]
                    p_sum.text = sub.findings_summary
                    p_sum.font.name = "Segoe UI"
                    p_sum.font.size = Pt(12)
                    p_sum.font.italic = True
                    p_sum.font.color.rgb = SLATE

                for c in sub_claims[:4]:
                    p_c = tf_sub.add_paragraph()
                    p_c.text = f"• [{c.claim_type.upper()}] {c.statement}"
                    p_c.font.name = "Segoe UI"
                    p_c.font.size = Pt(11)
                    p_c.font.color.rgb = DARK
                    p_c.space_before = Pt(10)

                # Embed visual if available
                if matching_vis and matching_vis.url_or_path and Path(matching_vis.url_or_path).exists():
                    s_sub.shapes.add_picture(matching_vis.url_or_path, Inches(7.2), Inches(1.8), width=Inches(5.3))

        # -------------------------------------------------------------------
        # Slide: Data Table Comparison
        # -------------------------------------------------------------------
        if pkg.tables:
            s_tbl = prs.slides.add_slide(blank_layout)
            slides_list.append(s_tbl)
            t = pkg.tables[0]
            _add_slide_header(s_tbl, t.title or "Comparative Data & Metrics", "DATA MATRIX")

            rows_to_render = min(len(t.rows), 7)
            cols_to_render = min(len(t.headers), 6)
            table_shape = s_tbl.shapes.add_table(
                rows=rows_to_render + 1,
                cols=cols_to_render,
                left=Inches(0.8),
                top=Inches(1.8),
                width=Inches(11.7),
                height=Inches(4.5),
            )
            pptx_tbl = table_shape.table

            # Header row
            for c_idx in range(cols_to_render):
                cell = pptx_tbl.cell(0, c_idx)
                cell.fill.solid()
                cell.fill.fore_color.rgb = NAVY
                p = cell.text_frame.paragraphs[0]
                p.text = str(t.headers[c_idx])
                p.font.name = "Segoe UI"
                p.font.size = Pt(10)
                p.font.bold = True
                p.font.color.rgb = WHITE
                p.alignment = PP_ALIGN.CENTER

            # Data rows
            for r_idx in range(rows_to_render):
                bg = CARD_BG if r_idx % 2 == 1 else WHITE
                for c_idx in range(cols_to_render):
                    cell = pptx_tbl.cell(r_idx + 1, c_idx)
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = bg
                    val = str(t.rows[r_idx][c_idx]) if c_idx < len(t.rows[r_idx]) else ""
                    p = cell.text_frame.paragraphs[0]
                    p.text = val
                    p.font.name = "Segoe UI"
                    p.font.size = Pt(9.5)
                    p.font.color.rgb = DARK

        # -------------------------------------------------------------------
        # Slide: Architecture & Visual Figures
        # -------------------------------------------------------------------
        visuals_general = [v for v in pkg.visuals if v.url_or_path and Path(v.url_or_path).exists() and v.asset_type != "formula"]
        for v in visuals_general[:2]:
            s_vis = prs.slides.add_slide(blank_layout)
            slides_list.append(s_vis)
            _add_slide_header(s_vis, v.title, "VISUAL INTELLIGENCE")
            s_vis.shapes.add_picture(v.url_or_path, Inches(1.5), Inches(1.8), width=Inches(10.3))
            if v.caption:
                tb_cap = s_vis.shapes.add_textbox(Inches(1.5), Inches(6.3), Inches(10.3), Inches(0.5))
                p_c = tb_cap.text_frame.paragraphs[0]
                p_c.text = f"Figure: {v.caption}"
                p_c.font.size = Pt(9.5)
                p_c.font.italic = True
                p_c.font.color.rgb = SLATE
                p_c.alignment = PP_ALIGN.CENTER

        # -------------------------------------------------------------------
        # Slide: Discrepancies & Contradictions
        # -------------------------------------------------------------------
        conflicting = [c for c in pkg.claims.values() if c.classification == EvidenceClassification.CONFLICTING]
        if conflicting or pkg.contradictions:
            s_conf = prs.slides.add_slide(blank_layout)
            slides_list.append(s_conf)
            _add_slide_header(s_conf, "Documented Discrepancies & Conflicting Data", "CRITICAL EVALUATION")

            tb_c = s_conf.shapes.add_textbox(Inches(0.8), Inches(1.8), Inches(11.7), Inches(4.8))
            tf_c = tb_c.text_frame
            tf_c.word_wrap = True

            p_w = tf_c.paragraphs[0]
            p_w.text = "⚠️ Independent Source Disagreements Identified During Research"
            p_w.font.name = "Segoe UI"
            p_w.font.size = Pt(13)
            p_w.font.bold = True
            p_w.font.color.rgb = AMBER

            for c in conflicting[:4]:
                p_item = tf_c.add_paragraph()
                p_item.text = f"• Assertion: {c.statement}"
                p_item.font.name = "Segoe UI"
                p_item.font.size = Pt(11)
                p_item.font.bold = True
                p_item.font.color.rgb = DARK
                p_item.space_before = Pt(8)
                if c.contradiction_notes:
                    p_dt = tf_c.add_paragraph()
                    p_dt.text = f"   Discrepancy Details: {c.contradiction_notes}"
                    p_dt.font.name = "Segoe UI"
                    p_dt.font.size = Pt(10)
                    p_dt.font.italic = True
                    p_dt.font.color.rgb = SLATE

        # -------------------------------------------------------------------
        # Final Slide: References & Source Provenance
        # -------------------------------------------------------------------
        s_ref = prs.slides.add_slide(blank_layout)
        slides_list.append(s_ref)
        _add_slide_header(s_ref, "References Cited & Source Provenance", "PROVENANCE & INTEGRITY")

        tb_ref = s_ref.shapes.add_textbox(Inches(0.8), Inches(1.8), Inches(11.7), Inches(4.8))
        tf_ref = tb_ref.text_frame
        tf_ref.word_wrap = True

        for num, src in ordered_sources[:8]:
            p_r = tf_ref.add_paragraph()
            p_r.text = f"[{num}] {src.title or src.domain} — {src.domain} ({src.url}) [Reliability: {src.reliability_score*100:.0f}%]"
            p_r.font.name = "Segoe UI"
            p_r.font.size = Pt(10)
            p_r.font.color.rgb = DARK
            p_r.space_before = Pt(6)

        # Stamp slide numbers and footers across all slides
        total_slides = len(slides_list)
        for idx, slide in enumerate(slides_list, start=1):
            _add_slide_footer(slide, idx, total_slides)

        prs.save(str(out_file))
        return str(out_file)

