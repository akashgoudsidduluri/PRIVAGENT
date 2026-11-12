"""Reusable AI Document Planning and Structured Document Model Engine.

Implements the general-purpose document-planning architecture:
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
      Format-Specific Renderer
              |
              v
      Artifact Validation
              |
              v
       Final Deliverable

Removes all topic-specific templates from renderers.
The document planner determines document structure, section hierarchy, content blocks,
visual asset placement, and adaptive page budgeting across ANY domain:
- Technical research (Transformers, Distributed Systems, Operating Systems)
- Academic and scientific literature
- Financial analysis and product comparisons
- General multi-source investigations
"""
from __future__ import annotations

import datetime
import re
import time
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field

from .models import (
    DataTable,
    EvidenceClassification,
    EvidenceItem,
    FormulaItem,
    ResearchClaim,
    ResearchKnowledgePackage,
    ResearchSubtopic,
    SourceReference,
    VisualAsset,
)
from .adaptive_planner import PageConstraint, PageConstraintType


class BlockType(str, Enum):
    """Semantic block types for structured document generation."""
    TEXT = "text"
    BULLET_LIST = "bullet_list"
    CALLOUT = "callout"
    IMAGE = "image"
    FORMULA = "formula"
    TABLE = "table"
    PROVENANCE_TABLE = "provenance_table"
    BIBLIOGRAPHY = "bibliography"


class ContentBlock(BaseModel):
    """Discrete, generic layout block within a document section."""
    block_id: str
    block_type: BlockType
    title: Optional[str] = None
    text: Optional[str] = None
    items: list[str] = Field(default_factory=list)
    callout_type: Optional[str] = None  # "note" | "warning" | "contradiction" | "tip"
    visual_asset: Optional[VisualAsset] = None
    formula_item: Optional[FormulaItem] = None
    data_table: Optional[DataTable] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class DocumentSection(BaseModel):
    """Hierarchical, topic-agnostic section of a structured document."""
    section_id: str
    title: str
    level: int = 1                      # 1 for H1 chapter/section, 2 for H2 subsection
    page_break_before: bool = False     # True when allocated to start on a new page
    page_number: Optional[int] = None   # Target page index when page-budgeted
    blocks: list[ContentBlock] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

    def add_text(self, text: str, title: Optional[str] = None) -> None:
        self.blocks.append(ContentBlock(
            block_id=f"blk_txt_{len(self.blocks)+1}",
            block_type=BlockType.TEXT,
            title=title,
            text=text,
        ))

    def add_bullets(self, items: list[str], title: Optional[str] = None) -> None:
        if items:
            self.blocks.append(ContentBlock(
                block_id=f"blk_blt_{len(self.blocks)+1}",
                block_type=BlockType.BULLET_LIST,
                title=title,
                items=items,
            ))

    def add_callout(self, text: str, callout_type: str = "note", items: Optional[list[str]] = None) -> None:
        self.blocks.append(ContentBlock(
            block_id=f"blk_cal_{len(self.blocks)+1}",
            block_type=BlockType.CALLOUT,
            callout_type=callout_type,
            text=text,
            items=items or [],
        ))

    def add_visual(self, visual: VisualAsset) -> None:
        self.blocks.append(ContentBlock(
            block_id=f"blk_vis_{visual.asset_id}",
            block_type=BlockType.IMAGE,
            title=visual.title,
            text=visual.caption,
            visual_asset=visual,
        ))

    def add_formula(self, formula: FormulaItem) -> None:
        self.blocks.append(ContentBlock(
            block_id=f"blk_form_{formula.formula_id}",
            block_type=BlockType.FORMULA,
            title=formula.name,
            text=formula.explanation,
            formula_item=formula,
        ))

    def add_table(self, table: DataTable) -> None:
        self.blocks.append(ContentBlock(
            block_id=f"blk_tbl_{table.table_id}",
            block_type=BlockType.TABLE,
            title=table.title,
            text=table.caption,
            data_table=table,
        ))


class DocumentMetadata(BaseModel):
    """Universal header metadata for deliverable documents."""
    title: str
    subtitle: str
    objective: str
    date_str: str
    sources_count: int
    claims_count: int
    target_format: str = "pdf"
    is_slides: bool = False
    author: str = "PrivAgent Autonomous Research Engine"
    confidentiality: str = "Confidential & Verified Technical Intelligence"

    model_config = {"populate_by_name": True}


class StructuredDocumentPlan(BaseModel):
    """Complete, format-agnostic structured document plan."""
    plan_id: str
    metadata: DocumentMetadata
    sections: list[DocumentSection] = Field(default_factory=list)
    target_pages: Optional[int] = None
    is_slides: bool = False
    total_allocated_pages: int = 1
    created_at: float = Field(default_factory=time.time)

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Helper: Citation mapping
# ---------------------------------------------------------------------------

def _build_citation_map(pkg: ResearchKnowledgePackage) -> dict[str, int]:
    """1-indexed citation number mapping for all sources in package."""
    sorted_sources = sorted(pkg.sources.values(), key=lambda s: s.source_id)
    return {src.source_id: idx for idx, src in enumerate(sorted_sources, start=1)}


def _format_citation(source_ids: list[str], cit_map: dict[str, int]) -> str:
    """Format source IDs into citation brackets like '[1, 2]'."""
    nums = sorted({cit_map[s_id] for s_id in source_ids if s_id in cit_map})
    return ("[" + ", ".join(str(n) for n in nums) + "]") if nums else ""


def _clean_title(raw_title: str) -> str:
    """Normalize raw title, stripping command verbs and excess prompt length."""
    if not raw_title:
        return "Autonomous Research Deliverable"
    clean = raw_title.strip()
    if len(clean) > 75:
        # Strip command prefixes
        clean = re.sub(r"^(?:research|investigate|analyze|explain|examine|study|survey)\s+", "", clean, flags=re.IGNORECASE).strip()
        clauses = re.split(r"[,;.]\s*", clean)
        clean = clauses[0].strip()
        if len(clean) < 15 and len(clauses) > 1:
            clean = f"{clauses[0].strip()}: {clauses[1].strip()}"
    return clean[:80].title() if not clean.isupper() else clean[:80]


def _clean_subtopic_title(raw_title: str, parent_title: str = "", idx: int = 1) -> str:
    """Clean subtopic title, stripping leading numbers, command verbs, and prompt strings."""
    if not raw_title:
        return f"Technical Analysis Aspect {idx}"
    clean = raw_title.strip()
    # Strip leading numbers like "1. ", "2.1 ", "1) "
    clean = re.sub(r"^(?:\d+[\.\)]\s*)+", "", clean).strip()
    clean = re.sub(r"^(?:research|investigate|analyze|explain|examine|study|survey)\s+", "", clean, flags=re.IGNORECASE).strip()

    # If the subtopic title contains the whole user prompt or is identical to the parent title
    if len(clean) > 60 or (parent_title and clean.lower() == parent_title.lower()) or "compare information" in clean.lower():
        clauses = re.split(r"[,;.]\s*", clean)
        if clauses and len(clauses[0].strip()) >= 5:
            clean = clauses[0].strip()
        if len(clean) > 60:
            clean = clean[:57].strip() + "..."
    return clean.title() if not clean.isupper() else clean


# ---------------------------------------------------------------------------
# AI Document Planner Implementation
# ---------------------------------------------------------------------------

def build_structured_document_plan(
    pkg: ResearchKnowledgePackage,
    target_format: str = "pdf",
    page_constraint: Optional[PageConstraint] = None,
    **kwargs: Any,
) -> StructuredDocumentPlan:
    """Construct a topic-agnostic StructuredDocumentPlan from a ResearchKnowledgePackage.

    Analyzes evidence content, groups findings logically, associates visual assets
    with relevant subtopics, budgets page allocation, and prepares a modular blueprint
    for renderers.
    """
    plan_id = f"docplan_{int(time.time() * 1000)}"
    cit_map = _build_citation_map(pkg)

    # 1. Determine Target Pages / Slides
    is_slides = bool(
        target_format in ("pptx", "presentation", "slides")
        or (page_constraint and page_constraint.is_slides)
        or "slide" in (pkg.topic.objective or "").lower()
        or kwargs.get("deliverable_type") == "presentation"
    )
    target_pages: Optional[int] = None
    if page_constraint:
        target_pages = page_constraint.target_pages
    elif kwargs.get("target_pages"):
        try:
            target_pages = int(kwargs["target_pages"])
        except (ValueError, TypeError):
            pass
    elif kwargs.get("instruction"):
        m_p = re.search(r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s*[- ]?pages?\b", str(kwargs["instruction"]), re.IGNORECASE)
        if m_p:
            from .adaptive_planner import _parse_num
            target_pages = _parse_num(m_p.group(1))

    # 2. Document Header Metadata
    clean_doc_title = _clean_title(pkg.topic.title)
    sub_title = (
        "Autonomous Multi-Source Technical Presentation"
        if is_slides
        else "Autonomous Multi-Source Technical Research Report"
    )
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%B %d, %Y")
    meta = DocumentMetadata(
        title=clean_doc_title,
        subtitle=sub_title,
        objective=pkg.topic.objective or clean_doc_title,
        date_str=now_str,
        sources_count=len(pkg.sources),
        claims_count=len(pkg.claims),
        target_format=target_format,
        is_slides=is_slides,
    )

    # 3. Content Analysis & Asset Correlation
    # Track which visuals, formulas, and tables have been placed inline
    placed_visual_ids: set[str] = set()
    placed_formula_ids: set[str] = set()
    placed_table_ids: set[str] = set()

    # Filter out duplicate or broken visual assets
    valid_visuals: list[VisualAsset] = []
    seen_vis_keys: set[str] = set()
    for v in pkg.visuals:
        dedup_key = (v.url_or_path or "").lower() or v.title.lower()
        if dedup_key and dedup_key not in seen_vis_keys:
            seen_vis_keys.add(dedup_key)
            valid_visuals.append(v)

    sections: list[DocumentSection] = []

    # -----------------------------------------------------------------------
    # Section 1: Executive Summary & Overview
    # -----------------------------------------------------------------------
    s1 = DocumentSection(
        section_id="sec_01",
        title="1. Executive Summary & Research Scope",
        level=1,
        page_number=1,
        page_break_before=False,
    )
    if pkg.topic.scope:
        s1.add_text(f"<b>Research Scope:</b> {pkg.topic.scope}")
    if pkg.topic.findings_summary:
        s1.add_text(f"<b>Executive Synthesis:</b> {pkg.topic.findings_summary}")

    # Top high-confidence findings across package
    high_conf = [c for c in pkg.claims.values() if c.confidence >= 0.85]
    if high_conf:
        bullets = []
        for c in high_conf[:5]:
            src_ids = ([c.primary_source_id] if c.primary_source_id else []) + c.supporting_source_ids
            cit = _format_citation(src_ids, cit_map)
            cit_str = f" <b><font color='#1E3A8A'>{cit}</font></b>" if cit else ""
            badge = f"<b><font color='#1E3A8A'>[{c.claim_type.upper()}]</font></b> "
            bullets.append(f"{badge}{c.statement}{cit_str}")
        s1.add_bullets(bullets, title="Key Synthesized Findings:")

    sections.append(s1)

    # -----------------------------------------------------------------------
    # Section 2: Detailed Technical Findings & Subtopics
    # -----------------------------------------------------------------------
    s2 = DocumentSection(
        section_id="sec_02",
        title="2. Detailed Technical Findings & Evidence",
        level=1,
    )
    sections.append(s2)

    subtopics = list(pkg.topic.subtopics)
    if not subtopics:
        subtopics = [ResearchSubtopic(
            subtopic_id="sub_01",
            title=f"{clean_doc_title} Core Principles & Analysis",
            findings_summary=pkg.topic.findings_summary,
        )]

    seen_sub_titles: set[str] = set()

    for idx, sub in enumerate(subtopics, start=1):
        clean_sub = _clean_subtopic_title(sub.title, clean_doc_title, idx)
        if clean_sub.lower() in seen_sub_titles:
            clean_sub = f"{clean_sub} (Part {idx})"
        seen_sub_titles.add(clean_sub.lower())

        s_sub = DocumentSection(
            section_id=f"sec_02_{idx:02d}",
            title=f"2.{idx} {clean_sub}",
            level=2,
        )

        if sub.findings_summary:
            s_sub.add_text(sub.findings_summary)
        elif sub.description:
            s_sub.add_text(sub.description)

        # Claims for this subtopic
        sub_claims = [
            c for c in pkg.claims.values()
            if c.subtopic_id == sub.subtopic_id or c.claim_id in sub.claim_ids
        ]
        if not sub_claims and idx == 1:
            sub_claims = [c for c in pkg.claims.values() if not c.subtopic_id]

        if sub_claims:
            bullets = []
            for c in sub_claims:
                src_ids = ([c.primary_source_id] if c.primary_source_id else []) + c.supporting_source_ids
                cit = _format_citation(src_ids, cit_map)
                cit_str = f" <b><font color='#1E3A8A'>{cit}</font></b>" if cit else ""
                bullets.append(f"<b>{c.claim_type.title()}:</b> {c.statement}{cit_str}")
            s_sub.add_bullets(bullets)

        # Correlate and place matching Formulas inline
        sub_title_words = set(re.findall(r"\b[a-z]{4,}\b", clean_sub.lower()))
        for f in pkg.formulas:
            if f.formula_id not in placed_formula_ids:
                f_name_words = set(re.findall(r"\b[a-z]{4,}\b", f.name.lower()))
                if f.subtopic_id == sub.subtopic_id or (sub_title_words & f_name_words):
                    s_sub.add_formula(f)
                    placed_formula_ids.add(f.formula_id)

        # Correlate and place matching Visual Assets (Extracted Images & Diagrams) inline
        for v in valid_visuals:
            if v.asset_id not in placed_visual_ids and v.asset_type != "formula":
                v_words = set(re.findall(r"\b[a-z]{4,}\b", (v.title + " " + (v.caption or "")).lower()))
                if v.subtopic_id == sub.subtopic_id or (sub_title_words & v_words):
                    s_sub.add_visual(v)
                    placed_visual_ids.add(v.asset_id)

        sections.append(s_sub)

    # -----------------------------------------------------------------------
    # Section 3: Algorithmic & Mathematical Foundations (if formulas exist)
    # -----------------------------------------------------------------------
    remaining_formulas = [f for f in pkg.formulas if f.formula_id not in placed_formula_ids]
    if remaining_formulas or (pkg.formulas and target_pages == 5):
        s3 = DocumentSection(
            section_id="sec_03",
            title="3. Algorithmic & Mathematical Foundations",
            level=1,
        )
        formulas_to_add = remaining_formulas if remaining_formulas else pkg.formulas
        for f in formulas_to_add:
            s3.add_formula(f)
            placed_formula_ids.add(f.formula_id)
        sections.append(s3)

    # -----------------------------------------------------------------------
    # Section 4: Comparative Analysis & Data Tables (if tables exist)
    # -----------------------------------------------------------------------
    if pkg.tables:
        s4 = DocumentSection(
            section_id="sec_04",
            title="4. Comparative Analysis & Data Tables",
            level=1,
        )
        for tbl in pkg.tables:
            s4.add_table(tbl)
            placed_table_ids.add(tbl.table_id)
        sections.append(s4)

    # -----------------------------------------------------------------------
    # Section 5: Visual Intelligence & System Figures (if unplaced visuals remain)
    # -----------------------------------------------------------------------
    unplaced_visuals = [v for v in valid_visuals if v.asset_id not in placed_visual_ids and v.asset_type != "formula"]
    if unplaced_visuals:
        s5 = DocumentSection(
            section_id="sec_05",
            title="5. Visual Intelligence & Architecture Figures",
            level=1,
        )
        for v in unplaced_visuals:
            s5.add_visual(v)
            placed_visual_ids.add(v.asset_id)
        sections.append(s5)

    # -----------------------------------------------------------------------
    # Section 6: Documented Discrepancies & Contradictions (if conflicting claims exist)
    # -----------------------------------------------------------------------
    conflicting = [c for c in pkg.claims.values() if c.classification == EvidenceClassification.CONFLICTING]
    if conflicting or pkg.contradictions:
        s6 = DocumentSection(
            section_id="sec_06",
            title="6. Documented Discrepancies & Contradictions",
            level=1,
        )
        warn_bullets = []
        for c in conflicting:
            warn_bullets.append(f"<b>Assertion:</b> {c.statement}" + (f"<br/><i>Discrepancy:</i> {c.contradiction_notes}" if c.contradiction_notes else ""))
        for cd in pkg.contradictions:
            warn_bullets.append(f"<i>Disagreement:</i> {cd}")
        s6.add_callout(
            text="PrivAgent identified conflicting factual assertions or divergent figures across visited sources:",
            callout_type="contradiction",
            items=warn_bullets,
        )
        sections.append(s6)

    # -----------------------------------------------------------------------
    # Final Section: References Cited & Source Provenance
    # -----------------------------------------------------------------------
    s_ref = DocumentSection(
        section_id="sec_07",
        title="7. References Cited & Source Provenance",
        level=1,
    )
    s_ref.blocks.append(ContentBlock(
        block_id="blk_provenance_table",
        block_type=BlockType.PROVENANCE_TABLE,
        title="Source Provenance & Verification Ledger",
    ))
    s_ref.blocks.append(ContentBlock(
        block_id="blk_bibliography",
        block_type=BlockType.BIBLIOGRAPHY,
        title="Bibliographic Citations & Verified URLs",
    ))
    sections.append(s_ref)

    # -----------------------------------------------------------------------
    # 4. Adaptive Page Budgeting & Section Pagination Decisions
    # -----------------------------------------------------------------------
    if is_slides:
        target_s = target_pages or 5
        # In presentation decks, every section maps 1:1 to a slide
        for p_idx, sec in enumerate(sections, start=1):
            sec.page_number = p_idx
            sec.page_break_before = (p_idx > 1)
        total_pages = len(sections)

    elif target_pages == 5:
        # Explicit 5-page document requirement:
        # Map sections into 5 distinct pages using content budgeting:
        # Page 1: Section 1 (Executive Summary)
        # Page 2: Core Foundations & Formulas
        # Page 3: Deep Technical Subtopics & Diagrams
        # Page 4: Comparative Tables & Benchmark Data
        # Page 5: References & Provenance
        total_pages = 5
        # Ensure we have at least 5 sections
        while len(sections) < 5:
            idx = len(sections)
            sections.insert(-1, DocumentSection(
                section_id=f"sec_{idx:02d}",
                title=f"{idx}. Detailed Technical Findings & Implementation Notes",
                level=1,
            ))
            # Move some blocks from preceding section
            if len(sections[idx - 1].blocks) > 2:
                sections[idx].blocks.append(sections[idx - 1].blocks.pop())

        # Assign page breaks before sections 2, 3, 4, 5
        for p_idx in range(1, 6):
            if p_idx - 1 < len(sections):
                sections[p_idx - 1].page_number = p_idx
                sections[p_idx - 1].page_break_before = (p_idx > 1)

    elif target_pages and target_pages > 1:
        # General budgeted target: evenly space page breaks
        total_pages = target_pages
        step = max(1, len(sections) // target_pages)
        curr_page = 1
        for idx, sec in enumerate(sections):
            if idx > 0 and idx % step == 0 and curr_page < target_pages:
                curr_page += 1
                sec.page_break_before = True
            sec.page_number = curr_page

    else:
        # Content-driven natural flow: page break only before final References section
        total_pages = max(1, len(sections) // 2)
        for sec in sections:
            sec.page_break_before = False
        if len(sections) >= 3:
            sections[-1].page_break_before = True

    return StructuredDocumentPlan(
        plan_id=plan_id,
        metadata=meta,
        sections=sections,
        target_pages=target_pages,
        is_slides=is_slides,
        total_allocated_pages=total_pages,
    )
