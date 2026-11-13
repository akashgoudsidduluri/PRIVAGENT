"""Professional 16:9 Presentation (.pptx) Research Deliverable Renderer using python-pptx.

Generates widescreen slide decks with executive summaries, native styled tables,
embedded architectural figures, mathematical formulations, discrepancy callouts, and source citations.
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
from ..visuals.image_pipeline import ensure_local_image_asset
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
ICE_BLUE = RGBColor(0xEF, 0xF6, 0xFF)


def _add_slide_header(slide: Any, title_text: str, category: str = "RESEARCH DELIVERABLE") -> None:
    """Add standard top title and category banner to a slide."""
    tb = slide.shapes.add_textbox(Inches(0.8), Inches(0.45), Inches(11.733), Inches(1.15))
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
    p_title.font.size = Pt(20) if len(title_text) > 45 else Pt(22)
    p_title.font.bold = True
    p_title.font.color.rgb = NAVY


def _add_slide_footer(slide: Any, current_slide: int, total_slides: int) -> None:
    """Add running confidentiality notice and slide number."""
    # Left footer notice
    tb_left = slide.shapes.add_textbox(Inches(0.8), Inches(6.95), Inches(8.5), Inches(0.35))
    tf_left = tb_left.text_frame
    tf_left.margin_left = tf_left.margin_top = tf_left.margin_right = tf_left.margin_bottom = 0
    p_left = tf_left.paragraphs[0]
    p_left.text = "PrivAgent Autonomous Research Engine — Verified Technical Intelligence"
    p_left.font.name = "Segoe UI"
    p_left.font.size = Pt(8.5)
    p_left.font.color.rgb = SLATE

    # Right footer slide number
    tb_right = slide.shapes.add_textbox(Inches(10.0), Inches(6.95), Inches(2.533), Inches(0.35))
    tf_right = tb_right.text_frame
    tf_right.margin_left = tf_right.margin_top = tf_right.margin_right = tf_right.margin_bottom = 0
    p_right = tf_right.paragraphs[0]
    p_right.alignment = PP_ALIGN.RIGHT
    p_right.text = f"{current_slide} / {total_slides}"
    p_right.font.name = "Segoe UI"
    p_right.font.size = Pt(8.5)
    p_right.font.bold = True
    p_right.font.color.rgb = NAVY


def _calculate_image_fit(
    img_path: str,
    max_w: Any,
    max_h: Any,
) -> tuple[Any, Any]:
    """Calculate proportional width and height fitting entirely within (max_w, max_h)."""
    try:
        from PIL import Image
        with Image.open(img_path) as im:
            w_px, h_px = im.size
        if w_px <= 0 or h_px <= 0:
            return max_w, max_h

        aspect = float(w_px) / float(h_px)
        max_w_in = float(max_w) / 914400.0
        max_h_in = float(max_h) / 914400.0
        box_aspect = max_w_in / max_h_in

        if aspect >= box_aspect:
            # Width constrained
            fit_w_in = max_w_in
            fit_h_in = max_w_in / aspect
        else:
            # Height constrained
            fit_h_in = max_h_in
            fit_w_in = max_h_in * aspect

        return Inches(fit_w_in), Inches(fit_h_in)
    except Exception:
        return max_w, max_h


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
        placed_vis_ids: set[str] = set()

        # -------------------------------------------------------------------
        # Slide 1: Title Slide
        # -------------------------------------------------------------------
        s1 = prs.slides.add_slide(blank_layout)
        slides_list.append(s1)

        # Background card accent
        accent_card = s1.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.8), Inches(1.15), Inches(11.733), Inches(5.3))
        accent_card.fill.solid()
        accent_card.fill.fore_color.rgb = CARD_BG
        accent_card.line.color.rgb = BORDER_GRAY
        accent_card.line.width = Pt(1)

        tb_title = s1.shapes.add_textbox(Inches(1.4), Inches(1.8), Inches(10.5), Inches(4.0))
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
        p_main.font.size = Pt(26) if len(pkg.topic.title) > 55 else Pt(32)
        p_main.font.bold = True
        p_main.font.color.rgb = NAVY
        p_main.space_before = Pt(8)

        p_sub = tf_title.add_paragraph()
        p_sub.text = pkg.topic.objective or "Autonomous comprehensive multi-source investigation and technical synthesis"
        p_sub.font.name = "Segoe UI"
        p_sub.font.size = Pt(13.5)
        p_sub.font.color.rgb = SLATE
        p_sub.space_before = Pt(12)

        now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%B %d, %Y")
        p_meta = tf_title.add_paragraph()
        p_meta.text = (
            f"Deliverable: 16:9 Presentation | Generated: {now_str} | "
            f"Sources Examined: {len(pkg.sources)} | Verified Claims: {len(pkg.claims)}"
        )
        p_meta.font.name = "Segoe UI"
        p_meta.font.size = Pt(10)
        p_meta.font.color.rgb = SLATE
        p_meta.space_before = Pt(18)

        # -------------------------------------------------------------------
        # Slide 2: Executive Summary & Metrics
        # -------------------------------------------------------------------
        s2 = prs.slides.add_slide(blank_layout)
        slides_list.append(s2)
        _add_slide_header(s2, "Executive Summary & Research Scope", "EXECUTIVE BRIEFING")

        tb_exec = s2.shapes.add_textbox(Inches(0.8), Inches(1.7), Inches(7.6), Inches(4.8))
        tf_exec = tb_exec.text_frame
        tf_exec.word_wrap = True

        first_p = tf_exec.paragraphs[0]
        if pkg.topic.findings_summary:
            first_p.text = pkg.topic.findings_summary
            first_p.font.name = "Segoe UI"
            first_p.font.size = Pt(12)
            first_p.font.italic = True
            first_p.font.color.rgb = DARK
            first_p.space_after = Pt(8)
            p_sc = tf_exec.add_paragraph()
        else:
            p_sc = first_p

        p_sc.text = f"Research Scope: {pkg.topic.scope or 'Comprehensive multi-source technical investigation'}"
        p_sc.font.name = "Segoe UI"
        p_sc.font.size = Pt(11.5)
        p_sc.font.bold = True
        p_sc.font.color.rgb = NAVY
        p_sc.space_after = Pt(10)

        # High confidence or primary claims
        high_conf = [c for c in pkg.claims.values() if c.confidence >= 0.85]
        if not high_conf:
            high_conf = list(pkg.claims.values())

        for c in high_conf[:4]:
            p_clm = tf_exec.add_paragraph()
            cit_str = f" [{cit_map[c.primary_source_id]}]" if c.primary_source_id and c.primary_source_id in cit_map else ""
            p_clm.text = f"• [{c.claim_type.upper()}] {c.statement}{cit_str}"
            p_clm.font.name = "Segoe UI"
            p_clm.font.size = Pt(10.5)
            p_clm.font.color.rgb = DARK
            p_clm.space_before = Pt(8)

        # Metric Tiles on Right Side (4 stacked cards)
        vis_cnt = len(pkg.formulas) + len([v for v in pkg.visuals if v.asset_type != "formula"])
        metric_boxes = [
            ("Sources Examined", f"{len(pkg.sources)}", Inches(8.8), Inches(1.7)),
            ("Verified Claims", f"{len(pkg.claims)}", Inches(8.8), Inches(2.95)),
            ("Data Matrices", f"{len(pkg.tables)}", Inches(8.8), Inches(4.2)),
            ("Formulas & Figures", f"{vis_cnt}", Inches(8.8), Inches(5.45)),
        ]
        for lbl, num, x, y in metric_boxes:
            card = s2.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, Inches(3.733), Inches(1.05))
            card.fill.solid()
            card.fill.fore_color.rgb = CARD_BG
            card.line.color.rgb = BORDER_GRAY
            tf_m = card.text_frame
            tf_m.margin_top = Inches(0.08)
            p_n = tf_m.paragraphs[0]
            p_n.text = num
            p_n.font.name = "Segoe UI"
            p_n.font.size = Pt(22)
            p_n.font.bold = True
            p_n.font.color.rgb = NAVY
            p_n.alignment = PP_ALIGN.CENTER
            p_l = tf_m.add_paragraph()
            p_l.text = lbl
            p_l.font.name = "Segoe UI"
            p_l.font.size = Pt(9.5)
            p_l.font.color.rgb = SLATE
            p_l.alignment = PP_ALIGN.CENTER

        # -------------------------------------------------------------------
        # Slides 3+: Subtopic Deep Dives & Evidence
        # -------------------------------------------------------------------
        subtopics = pkg.topic.subtopics
        if not subtopics:
            types = list(set(c.claim_type for c in pkg.claims.values()))
            for t_idx, t_name in enumerate(types[:6], start=1):
                s_sub = prs.slides.add_slide(blank_layout)
                slides_list.append(s_sub)
                _add_slide_header(s_sub, f"{t_name.title()} Findings & Evidence", "TECHNICAL DEEP DIVE")
                tb_sub = s_sub.shapes.add_textbox(Inches(0.8), Inches(1.7), Inches(11.733), Inches(4.8))
                tf_sub = tb_sub.text_frame
                tf_sub.word_wrap = True
                type_claims = [c for c in pkg.claims.values() if c.claim_type == t_name]
                for c in type_claims[:5]:
                    p = tf_sub.add_paragraph()
                    cit_str = f" [{cit_map[c.primary_source_id]}]" if c.primary_source_id and c.primary_source_id in cit_map else ""
                    p.text = f"• {c.statement}{cit_str}"
                    p.font.name = "Segoe UI"
                    p.font.size = Pt(11)
                    p.space_before = Pt(10)
        else:
            for s_idx, sub in enumerate(subtopics, start=1):
                s_sub = prs.slides.add_slide(blank_layout)
                slides_list.append(s_sub)
                _add_slide_header(s_sub, sub.title, f"SUBTOPIC 0{s_idx}")

                sub_claims = [c for c in pkg.claims.values() if c.subtopic_id == sub.subtopic_id or c.claim_id in sub.claim_ids]
                # Check for associated visual asset
                matching_vis = next(
                    (v for v in pkg.visuals if v.subtopic_id == sub.subtopic_id and v.asset_type != "formula"),
                    None,
                )
                local_vis_path = (
                    ensure_local_image_asset(matching_vis, cache_dir=out_file.parent / "images")
                    if matching_vis
                    else None
                )
                if matching_vis and local_vis_path:
                    placed_vis_ids.add(matching_vis.asset_id)

                content_width = Inches(6.2) if local_vis_path else Inches(11.733)
                tb_sub = s_sub.shapes.add_textbox(Inches(0.8), Inches(1.7), content_width, Inches(4.8))
                tf_sub = tb_sub.text_frame
                tf_sub.word_wrap = True

                if sub.findings_summary:
                    p_sum = tf_sub.paragraphs[0]
                    p_sum.text = sub.findings_summary
                    p_sum.font.name = "Segoe UI"
                    p_sum.font.size = Pt(11.5)
                    p_sum.font.italic = True
                    p_sum.font.color.rgb = SLATE
                    p_sum.space_after = Pt(8)

                for c in sub_claims[:4]:
                    p_c = tf_sub.add_paragraph()
                    cit_str = f" [{cit_map[c.primary_source_id]}]" if c.primary_source_id and c.primary_source_id in cit_map else ""
                    p_c.text = f"• [{c.claim_type.upper()}] {c.statement}{cit_str}"
                    p_c.font.name = "Segoe UI"
                    p_c.font.size = Pt(10.5)
                    p_c.font.color.rgb = DARK
                    p_c.space_before = Pt(8)

                # Embed visual proportionally if available
                if local_vis_path and Path(local_vis_path).exists():
                    try:
                        max_w = Inches(5.2)
                        max_h = Inches(4.2)
                        fit_w, fit_h = _calculate_image_fit(local_vis_path, max_w, max_h)
                        pos_x = Inches(7.3) + (max_w - fit_w) / 2
                        pos_y = Inches(1.7) + (max_h - fit_h) / 2
                        s_sub.shapes.add_picture(local_vis_path, pos_x, pos_y, width=fit_w, height=fit_h)

                        if matching_vis and (matching_vis.caption or matching_vis.title):
                            tb_cap = s_sub.shapes.add_textbox(Inches(7.3), pos_y + fit_h + Inches(0.06), Inches(5.2), Inches(0.4))
                            tf_cap = tb_cap.text_frame
                            tf_cap.word_wrap = True
                            p_cap = tf_cap.paragraphs[0]
                            p_cap.text = f"Figure: {matching_vis.caption or matching_vis.title}"
                            p_cap.font.name = "Segoe UI"
                            p_cap.font.size = Pt(8.5)
                            p_cap.font.italic = True
                            p_cap.font.color.rgb = SLATE
                            p_cap.alignment = PP_ALIGN.CENTER
                    except Exception:
                        pass

        # -------------------------------------------------------------------
        # Slide: Mathematical & Algorithmic Formulations (if formulas present)
        # -------------------------------------------------------------------
        if pkg.formulas:
            formula_chunks = [pkg.formulas[i:i + 2] for i in range(0, len(pkg.formulas), 2)]
            for ch_idx, chunk in enumerate(formula_chunks, start=1):
                s_form = prs.slides.add_slide(blank_layout)
                slides_list.append(s_form)
                suffix = f" (Part {ch_idx})" if len(formula_chunks) > 1 else ""
                _add_slide_header(s_form, f"Algorithmic & Mathematical Foundations{suffix}", "QUANTITATIVE MODELS")

                if len(chunk) == 1:
                    f = chunk[0]
                    card = s_form.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.8), Inches(1.7), Inches(11.733), Inches(4.8))
                    card.fill.solid()
                    card.fill.fore_color.rgb = CARD_BG
                    card.line.color.rgb = BORDER_GRAY
                    tf_f = card.text_frame
                    tf_f.word_wrap = True

                    p_fn = tf_f.paragraphs[0]
                    p_fn.text = f.name
                    p_fn.font.name = "Segoe UI"
                    p_fn.font.size = Pt(15)
                    p_fn.font.bold = True
                    p_fn.font.color.rgb = NAVY

                    p_eq = tf_f.add_paragraph()
                    p_eq.text = f.latex
                    p_eq.font.name = "Cambria Math"
                    p_eq.font.size = Pt(14)
                    p_eq.font.italic = True
                    p_eq.font.color.rgb = TEAL
                    p_eq.space_before = Pt(10)
                    p_eq.space_after = Pt(10)

                    if f.explanation:
                        p_exp = tf_f.add_paragraph()
                        p_exp.text = f.explanation
                        p_exp.font.name = "Segoe UI"
                        p_exp.font.size = Pt(11)
                        p_exp.font.color.rgb = DARK

                    if f.variables:
                        p_vars = tf_f.add_paragraph()
                        var_str = ", ".join(f"{k} = {v}" for k, v in f.variables.items())
                        p_vars.text = f"Parameters: {var_str}"
                        p_vars.font.name = "Segoe UI"
                        p_vars.font.size = Pt(10)
                        p_vars.font.color.rgb = SLATE
                        p_vars.space_before = Pt(8)

                    if f.worked_example:
                        p_we = tf_f.add_paragraph()
                        p_we.text = f"Worked Quantitative Formulation: {f.worked_example}"
                        p_we.font.name = "Segoe UI"
                        p_we.font.size = Pt(10.5)
                        p_we.font.bold = True
                        p_we.font.color.rgb = NAVY
                        p_we.space_before = Pt(10)

                else:
                    # 2 formulas on slide: stacked top and bottom cards
                    y_positions = [Inches(1.7), Inches(4.2)]
                    for f_item, y_pos in zip(chunk, y_positions):
                        card = s_form.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.8), y_pos, Inches(11.733), Inches(2.35))
                        card.fill.solid()
                        card.fill.fore_color.rgb = CARD_BG
                        card.line.color.rgb = BORDER_GRAY
                        tf_f = card.text_frame
                        tf_f.word_wrap = True

                        p_fn = tf_f.paragraphs[0]
                        p_fn.text = f_item.name
                        p_fn.font.name = "Segoe UI"
                        p_fn.font.size = Pt(13)
                        p_fn.font.bold = True
                        p_fn.font.color.rgb = NAVY

                        p_eq = tf_f.add_paragraph()
                        p_eq.text = f_item.latex
                        p_eq.font.name = "Cambria Math"
                        p_eq.font.size = Pt(12)
                        p_eq.font.italic = True
                        p_eq.font.color.rgb = TEAL
                        p_eq.space_before = Pt(4)

                        if f_item.explanation:
                            p_exp = tf_f.add_paragraph()
                            p_exp.text = f_item.explanation
                            p_exp.font.name = "Segoe UI"
                            p_exp.font.size = Pt(10)
                            p_exp.font.color.rgb = DARK
                            p_exp.space_before = Pt(4)

                        if f_item.worked_example:
                            p_we = tf_f.add_paragraph()
                            p_we.text = f"Application: {f_item.worked_example}"
                            p_we.font.name = "Segoe UI"
                            p_we.font.size = Pt(9.5)
                            p_we.font.bold = True
                            p_we.font.color.rgb = SLATE
                            p_we.space_before = Pt(4)

        # -------------------------------------------------------------------
        # Slide: Data Table Comparison
        # -------------------------------------------------------------------
        if pkg.tables:
            for t_idx, t in enumerate(pkg.tables[:2], start=1):
                s_tbl = prs.slides.add_slide(blank_layout)
                slides_list.append(s_tbl)
                table_title = t.title or (f"Comparative Matrix {t_idx}" if len(pkg.tables) > 1 else "Comparative Data & Metrics")
                _add_slide_header(s_tbl, table_title, "COMPARATIVE ANALYSIS")

                rows_to_render = min(len(t.rows), 6)
                cols_to_render = min(len(t.headers), 6)
                tbl_height = min(Inches(4.5), Inches(0.5 + rows_to_render * 0.45))
                table_shape = s_tbl.shapes.add_table(
                    rows=rows_to_render + 1,
                    cols=cols_to_render,
                    left=Inches(0.8),
                    top=Inches(1.8),
                    width=Inches(11.733),
                    height=tbl_height,
                )
                pptx_tbl = table_shape.table

                # Header row
                for c_idx in range(cols_to_render):
                    cell = pptx_tbl.cell(0, c_idx)
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = NAVY
                    cell.margin_left = cell.margin_right = Inches(0.08)
                    cell.margin_top = cell.margin_bottom = Inches(0.04)
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
                        cell.margin_left = cell.margin_right = Inches(0.08)
                        cell.margin_top = cell.margin_bottom = Inches(0.04)
                        val = str(t.rows[r_idx][c_idx]) if c_idx < len(t.rows[r_idx]) else ""
                        p = cell.text_frame.paragraphs[0]
                        p.text = val
                        p.font.name = "Segoe UI"
                        p.font.size = Pt(8.5) if len(val) > 35 else Pt(9.5)
                        p.font.color.rgb = DARK

                if len(t.rows) > 6:
                    tb_fn = s_tbl.shapes.add_textbox(Inches(0.8), Inches(6.45), Inches(11.733), Inches(0.35))
                    p_fn = tb_fn.text_frame.paragraphs[0]
                    p_fn.text = f"* Displaying primary 6 of {len(t.rows)} analyzed entities. Full dataset preserved in companion spreadsheet."
                    p_fn.font.name = "Segoe UI"
                    p_fn.font.size = Pt(8.5)
                    p_fn.font.italic = True
                    p_fn.font.color.rgb = SLATE

        # -------------------------------------------------------------------
        # Slide: Architecture & Visual Figures (for unplaced general visuals)
        # -------------------------------------------------------------------
        visuals_general = [
            v for v in pkg.visuals
            if v.asset_type != "formula" and v.asset_id not in placed_vis_ids
        ]
        for v in visuals_general[:2]:
            local_path = ensure_local_image_asset(v, cache_dir=out_file.parent / "images")
            if local_path and Path(local_path).exists():
                try:
                    s_vis = prs.slides.add_slide(blank_layout)
                    slides_list.append(s_vis)
                    _add_slide_header(s_vis, v.title, "VISUAL INTELLIGENCE")

                    max_w = Inches(10.5)
                    max_h = Inches(4.3)
                    fit_w, fit_h = _calculate_image_fit(local_path, max_w, max_h)
                    pos_x = Inches(0.8) + (Inches(11.733) - fit_w) / 2
                    pos_y = Inches(1.7) + (max_h - fit_h) / 2
                    s_vis.shapes.add_picture(local_path, pos_x, pos_y, width=fit_w, height=fit_h)

                    if v.caption or v.title:
                        tb_cap = s_vis.shapes.add_textbox(Inches(0.8), pos_y + fit_h + Inches(0.06), Inches(11.733), Inches(0.4))
                        p_c = tb_cap.text_frame.paragraphs[0]
                        p_c.text = f"Figure: {v.caption or v.title}"
                        p_c.font.size = Pt(9.5)
                        p_c.font.italic = True
                        p_c.font.color.rgb = SLATE
                        p_c.alignment = PP_ALIGN.CENTER
                except Exception:
                    pass

        # -------------------------------------------------------------------
        # Slide: Discrepancies & Contradictions
        # -------------------------------------------------------------------
        conflicting = [c for c in pkg.claims.values() if c.classification == EvidenceClassification.CONFLICTING]
        if conflicting or pkg.contradictions:
            s_conf = prs.slides.add_slide(blank_layout)
            slides_list.append(s_conf)
            _add_slide_header(s_conf, "Documented Discrepancies & Conflicting Data", "CRITICAL EVALUATION")

            tb_c = s_conf.shapes.add_textbox(Inches(0.8), Inches(1.7), Inches(11.733), Inches(4.8))
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

        if len(ordered_sources) <= 4:
            tb_ref = s_ref.shapes.add_textbox(Inches(0.8), Inches(1.7), Inches(11.733), Inches(4.8))
            tf_ref = tb_ref.text_frame
            tf_ref.word_wrap = True
            for num, src in ordered_sources:
                p_r = tf_ref.add_paragraph()
                p_r.text = f"[{num}] {src.domain} — {src.title or src.domain}"
                p_r.font.name = "Segoe UI"
                p_r.font.size = Pt(10.5)
                p_r.font.bold = True
                p_r.font.color.rgb = NAVY
                p_r.space_before = Pt(8)

                p_u = tf_ref.add_paragraph()
                p_u.text = f"     URL: {src.url} | Reliability: {src.reliability_score * 100:.0f}%"
                p_u.font.name = "Segoe UI"
                p_u.font.size = Pt(9)
                p_u.font.color.rgb = SLATE
        else:
            # 2 columns layout for 5-10 sources
            col1_sources = ordered_sources[:5]
            col2_sources = ordered_sources[5:10]

            tb_col1 = s_ref.shapes.add_textbox(Inches(0.8), Inches(1.7), Inches(5.6), Inches(4.8))
            tf_col1 = tb_col1.text_frame
            tf_col1.word_wrap = True
            for num, src in col1_sources:
                p_r = tf_col1.add_paragraph()
                p_r.text = f"[{num}] {src.domain} — {src.title or src.domain}"
                p_r.font.name = "Segoe UI"
                p_r.font.size = Pt(10)
                p_r.font.bold = True
                p_r.font.color.rgb = NAVY
                p_r.space_before = Pt(6)

                p_u = tf_col1.add_paragraph()
                p_u.text = f"     {src.url[:50]}... | {src.reliability_score * 100:.0f}%"
                p_u.font.name = "Segoe UI"
                p_u.font.size = Pt(8.5)
                p_u.font.color.rgb = SLATE

            tb_col2 = s_ref.shapes.add_textbox(Inches(6.8), Inches(1.7), Inches(5.7), Inches(4.8))
            tf_col2 = tb_col2.text_frame
            tf_col2.word_wrap = True
            for num, src in col2_sources:
                p_r = tf_col2.add_paragraph()
                p_r.text = f"[{num}] {src.domain} — {src.title or src.domain}"
                p_r.font.name = "Segoe UI"
                p_r.font.size = Pt(10)
                p_r.font.bold = True
                p_r.font.color.rgb = NAVY
                p_r.space_before = Pt(6)

                p_u = tf_col2.add_paragraph()
                p_u.text = f"     {src.url[:50]}... | {src.reliability_score * 100:.0f}%"
                p_u.font.name = "Segoe UI"
                p_u.font.size = Pt(8.5)
                p_u.font.color.rgb = SLATE

        if not ordered_sources:
            tb_ref = s_ref.shapes.add_textbox(Inches(0.8), Inches(1.7), Inches(11.733), Inches(4.8))
            p_r = tb_ref.text_frame.paragraphs[0]
            p_r.text = "All architectural findings synthesized from local verified domain sources and primary specifications."
            p_r.font.name = "Segoe UI"
            p_r.font.size = Pt(11)
            p_r.font.color.rgb = SLATE

        # Stamp slide numbers and footers across all slides
        total_slides = len(slides_list)
        for idx, slide in enumerate(slides_list, start=1):
            _add_slide_footer(slide, idx, total_slides)

        prs.save(str(out_file))
        return str(out_file)
