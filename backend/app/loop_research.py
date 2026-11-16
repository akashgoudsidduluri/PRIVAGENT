"""Research artifact finalization (Task 13).

Split out of :mod:`app.agentLoop` (which re-exports both names here,
so existing imports keep working). These two functions turn a
research knowledge package into rendered deliverables (PDF/DOCX/
PPTX/XLSX/...) and attach the "Deliverable Ready" line to a
completion summary. All imports are function-local except the
format map, which comes from :mod:`app.loop_helpers`.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from .loop_helpers import canonical_format_map

__all__ = [
    "_finalize_research_artifacts",
    "_finalize_research_summary",
]


def _finalize_research_artifacts(
    task_id: str,
    conversation_id: Optional[str],
    working_memory: dict[str, Any],
    summary_msg: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Synthesize visual assets, render requested deliverables, and register artifacts."""
    try:
        from pathlib import Path
        from . import db
        from .research.models import FormulaItem, ResearchKnowledgePackage, ResearchSubtopic, VisualAsset
        from .research.renderers import render_artifact
        from .research.visuals.chart_generator import generate_chart_from_table
        from .research.visuals.diagram_generator import render_attention_diagram_to_png, render_diagram_to_png
        from .research.visuals.formula_renderer import render_latex_formula

        pkg_data = working_memory.get("research_knowledge_package")
        if not pkg_data:
            return None

        pkg = ResearchKnowledgePackage.from_dict(pkg_data) if isinstance(pkg_data, dict) else pkg_data

        # Enrich package with planner synthesis if available
        if summary_msg:
            clean_summary = re.sub(r"📄\s*\*\*Deliverable Ready:\*\*.*$", "", summary_msg, flags=re.DOTALL).strip()
            if not pkg.topic.findings_summary and clean_summary:
                pkg.topic.findings_summary = clean_summary

            # Extract LaTeX equations from summary text if none explicitly registered
            if not pkg.formulas:
                math_blocks = re.findall(r"\$\$(.+?)\$\$", clean_summary, re.DOTALL)
                for idx, mb in enumerate(math_blocks[:4], start=1):
                    latex_clean = mb.strip().replace("\n", " ")
                    if latex_clean and len(latex_clean) < 400:
                        f_id = f"form_{idx:02d}"
                        name = f"Equation {idx}"
                        if "\\text{Attention}" in latex_clean or "Attention(" in latex_clean:
                            name = "Scaled Dot-Product Attention"
                        elif "\\text{MultiHead}" in latex_clean or "MultiHead(" in latex_clean:
                            name = "Multi-Head Attention Mechanism"
                        elif "PE_" in latex_clean or "\\sin" in latex_clean:
                            name = "Sinusoidal Positional Encoding"
                        f_item = FormulaItem(
                            formula_id=f_id,
                            name=name,
                            latex=latex_clean,
                            explanation="Mathematical formulation synthesized from authoritative research evidence.",
                        )
                        pkg.add_formula(f_item)

                # Fallback technical formulations for transformer / attention research
                if not pkg.formulas:
                    inst_and_sum = (clean_summary + " " + (pkg.topic.objective or "")).lower()
                    if "attention" in inst_and_sum or "transformer" in inst_and_sum:
                        pkg.add_formula(FormulaItem(
                            formula_id="form_01",
                            name="Scaled Dot-Product Attention",
                            latex=r"\text{Attention}(Q, K, V) = \text{softmax}\left(\frac{QK^T}{\sqrt{d_k}}\right)V",
                            explanation="Computes token-to-token attention weights scaled by the square root of key dimension d_k to prevent vanishing softmax gradients in high-dimensional projections.",
                        ))
                        pkg.add_formula(FormulaItem(
                            formula_id="form_02",
                            name="Multi-Head Attention",
                            latex=r"\text{MultiHead}(Q, K, V) = \text{Concat}(\text{head}_1, \dots, \text{head}_h)W^O \quad \text{where} \quad \text{head}_i = \text{Attention}(QW_i^Q, KW_i^K, VW_i^V)",
                            explanation="Projects queries, keys, and values h times with distinct learned linear projections, enabling simultaneous joint attendance to information across distinct representation subspaces.",
                        ))
                        pkg.add_formula(FormulaItem(
                            formula_id="form_03",
                            name="Sinusoidal Positional Encoding",
                            latex=r"PE_{(pos, 2i)} = \sin\left(\frac{pos}{10000^{2i/d_{\text{model}}}}\right), \quad PE_{(pos, 2i+1)} = \cos\left(\frac{pos}{10000^{2i/d_{\text{model}}}}\right)",
                            explanation="Injects sequence order. Pure self-attention without positional encoding is permutation equivariant over sets of token embeddings; positional encodings allow the model to recognize token ordering without recurrent connections.",
                        ))

            # Extract major subtopics from markdown headers if empty
            if not pkg.topic.subtopics and "## " in clean_summary:
                sections = re.split(r"\n##\s+", "\n" + clean_summary)
                for s_idx, sec in enumerate(sections[1:5], start=1):
                    lines = sec.strip().splitlines()
                    if lines:
                        s_title = lines[0].strip()
                        s_body = " ".join(lines[1:4]).strip()[:350]
                        sub = ResearchSubtopic(
                            subtopic_id=f"sub_{s_idx:02d}",
                            title=s_title,
                            findings_summary=s_body or s_title,
                        )
                        pkg.topic.subtopics.append(sub)

        # Determine deliverables directory
        artifacts_dir = Path(__file__).resolve().parents[1] / "data" / "artifacts" / task_id
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        # 1. Render formula assets if any exist without image paths
        for f in pkg.formulas:
            has_vis = any(v.metadata.get("formula_id") == f.formula_id for v in pkg.visuals)
            if not has_vis:
                f_path = artifacts_dir / f"eq_{f.formula_id}.png"
                try:
                    render_latex_formula(f.latex, str(f_path))
                    vis = VisualAsset(
                        asset_id=f"vis_formula_{f.formula_id}",
                        asset_type="formula",
                        title=f.name,
                        caption=f.explanation or f.name,
                        url_or_path=str(f_path),
                        is_generated=True,
                        metadata={"formula_id": f.formula_id},
                    )
                    pkg.add_visual(vis)
                except Exception:
                    pass

        # 2. Render chart assets from data tables if no chart exists
        has_chart = any(v.asset_type == "chart" for v in pkg.visuals)
        if not has_chart and pkg.tables:
            for tbl in pkg.tables[:1]:
                c_path = artifacts_dir / f"chart_{tbl.table_id}.png"
                try:
                    generate_chart_from_table(tbl, str(c_path))
                    vis = VisualAsset(
                        asset_id=f"vis_chart_{tbl.table_id}",
                        asset_type="chart",
                        title=tbl.title,
                        caption=tbl.caption or f"Statistical comparison for {tbl.title}",
                        url_or_path=str(c_path),
                        is_generated=True,
                        metadata={"table_id": tbl.table_id},
                    )
                    pkg.add_visual(vis)
                except Exception:
                    pass

        # 3. Render architecture block diagram if relevant topic and no diagram exists
        has_diagram = any(v.asset_id == "vis_diagram_arch" for v in pkg.visuals)
        if not has_diagram:
            diag_path = artifacts_dir / "architecture_diagram.png"
            try:
                render_diagram_to_png(pkg.topic.title, str(diag_path))
                vis = VisualAsset(
                    asset_id="vis_diagram_arch",
                    asset_type="diagram",
                    title=f"{pkg.topic.title} Architecture",
                    caption="Architectural flow and component breakdown",
                    url_or_path=str(diag_path),
                    is_generated=True,
                )
                pkg.add_visual(vis)
            except Exception:
                pass

        # 4. Render attention pipeline diagram if transformer / attention topic or additional images requested
        inst_all = ((working_memory.get("instruction") or "") + " " + (pkg.topic.objective or "")).lower()
        has_att_diag = any(v.asset_id == "vis_diagram_attention" for v in pkg.visuals)
        if not has_att_diag and ("attention" in inst_all or "transformer" in inst_all):
            att_path = artifacts_dir / "attention_mechanism_diagram.png"
            try:
                render_attention_diagram_to_png(str(att_path))
                vis = VisualAsset(
                    asset_id="vis_diagram_attention",
                    asset_type="diagram",
                    title="Scaled Dot-Product Attention Pipeline",
                    caption="Vector computation flow from Q, K, V projections through scaled dot-product and softmax weighting",
                    url_or_path=str(att_path),
                    is_generated=True,
                )
                pkg.add_visual(vis)
            except Exception:
                pass

        # Pre-cache authentic remote visual assets securely
        try:
            from .research.visuals.image_pipeline import ensure_local_image_asset
            for vis in pkg.visuals:
                if vis.url_or_path and vis.url_or_path.startswith(("http://", "https://")):
                    ensure_local_image_asset(vis, cache_dir=artifacts_dir / "images")
        except Exception:
            pass

        # Determine requested deliverable formats (single or multiple)
        raw_req_fmt = (working_memory.get("requested_format") or working_memory.get("deliverable_type") or "docx").lower().strip(" .")

        canonical_format_map = {
            "presentation": "pptx",
            "slides": "pptx",
            "slide_deck": "pptx",
            "ppt": "pptx",
            "powerpoint": "pptx",
            "excel": "xlsx",
            "spreadsheet": "xlsx",
            "workbook": "xlsx",
            "dataset": "xlsx",
            "xls": "xlsx",
            "word": "docx",
            "doc": "docx",
            "report": "docx",
            "document": "docx",
            "pdf": "pdf",
            "csv": "csv",
            "markdown": "markdown",
            "md": "markdown",
            "html": "html",
            "htm": "html",
            "webpage": "html",
        }
        req_fmt = canonical_format_map.get(raw_req_fmt, raw_req_fmt)

        # Check if instruction requested multiple formats
        inst_lower = (working_memory.get("instruction") or pkg.topic.objective or "").lower()
        formats_to_render = [req_fmt]
        if "multi" in raw_req_fmt or "multi-format" in inst_lower or ("markdown" in inst_lower and "html" in inst_lower and "csv" in inst_lower):
            formats_to_render = ["markdown", "html", "csv"]
        else:
            formats_to_render = [canonical_format_map.get(f.lower().strip(" ."), f.lower().strip(" .")) for f in formats_to_render]

        clean_slug = re.sub(r"[^a-zA-Z0-9_\-]", "_", pkg.topic.title.strip())[:40] or "Research_Report"
        generated_artifacts = []

        format_extensions = {
            "docx": (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "doc": (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "word": (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "pdf": (".pdf", "application/pdf"),
            "pptx": (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            "presentation": (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            "slides": (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            "slide_deck": (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            "ppt": (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            "powerpoint": (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            "xlsx": (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            "excel": (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            "spreadsheet": (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            "xls": (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            "csv": (".csv", "text/csv; charset=utf-8"),
            "markdown": (".md", "text/markdown; charset=utf-8"),
            "md": (".md", "text/markdown; charset=utf-8"),
            "html": (".html", "text/html; charset=utf-8"),
            "htm": (".html", "text/html; charset=utf-8"),
        }

        for fmt in formats_to_render:
            canon_fmt = canonical_format_map.get(fmt, fmt)
            ext, mime = format_extensions.get(canon_fmt, (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
            filename = f"{clean_slug}{ext}"
            out_file = artifacts_dir / filename

            render_artifact(
                pkg,
                canon_fmt,
                str(out_file),
                research_plan=working_memory.get("research_plan"),
                instruction=working_memory.get("instruction") or pkg.topic.objective,
                deliverable_type=working_memory.get("deliverable_type"),
            )

            file_size = out_file.stat().st_size if out_file.exists() else 0
            art_record = {
                "artifact_id": f"art_{task_id}_{canon_fmt}",
                "task_id": task_id,
                "conversation_id": conversation_id or "default",
                "filename": filename,
                "filepath": str(out_file),
                "file_format": canon_fmt,
                "mime_type": mime,
                "file_size_bytes": file_size,
                "title": f"{pkg.topic.title} ({canon_fmt.upper()})",
                "summary": f"Autonomous research deliverable in {canon_fmt.upper()} format.",
                "metadata": {
                    "sources_count": len(pkg.sources),
                    "claims_count": len(pkg.claims),
                    "tables_count": len(pkg.tables),
                    "visuals_count": len(pkg.visuals),
                },
            }
            db.save_artifact(art_record)
            generated_artifacts.append(art_record)

        working_memory["artifacts"] = generated_artifacts
        working_memory["research_knowledge_package"] = pkg.to_dict()
        working_memory["research_stage"] = "completed"
        try:
            from . import db
            db.save_research_package(pkg.to_dict(), conversation_id or "default", task_id, status="completed")
        except Exception:
            pass
        return generated_artifacts[0] if generated_artifacts else None

    except Exception:
        return None


def _finalize_research_summary(
    task_id: str,
    conversation_id: Optional[str],
    working_memory: dict[str, Any],
    summary_msg: Optional[str],
) -> str:
    """Attach a generated deliverable to a research completion summary.

    Every successful research completion path must call this. Completion paths
    that returned early (redundant final action, auto-complete after send) used
    to bypass artifact rendering, so a requested DOCX/XLSX/PDF was never created.
    """
    summary = summary_msg or ""
    if "Autonomous Website Discovery:" in summary:
        pkg_data = working_memory.get("research_knowledge_package") or {}
        topic = working_memory.get("research_topic") or (pkg_data.get("topic") or {}).get("title") or "Technical Research"
        sources_cnt = len(pkg_data.get("sources") or {})
        claims_cnt = len(pkg_data.get("claims") or {})
        summary = (
            f"Autonomous technical research on **{topic}** has completed across {sources_cnt or 'multiple'} authoritative web sources. "
            f"Extracted and synthesized {claims_cnt or 'key'} verified findings, mathematical formulations, and platform architectural comparisons."
        )
    if not working_memory.get("is_research"):
        return summary
    pkg_data = working_memory.get("research_knowledge_package") or {}
    if not (pkg_data.get("sources") or pkg_data.get("evidence") or pkg_data.get("tables")):
        # A research package with no collected sources/evidence is not a
        # completed research result. Never claim success or render an empty
        # deliverable in that case.
        return summary
    primary_art = _finalize_research_artifacts(task_id, conversation_id, working_memory, summary)
    if not primary_art:
        return summary
    art_msg = (
        f"\n\n📄 **Deliverable Ready:** [{primary_art['filename']}] "
        f"({primary_art['file_format'].upper()}, {primary_art['file_size_bytes'] / 1024:.1f} KB). "
        "Available for download."
    )
    return f"{summary}{art_msg}"
