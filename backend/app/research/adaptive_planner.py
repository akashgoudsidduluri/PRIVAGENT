"""Adaptive Research Planning and Intelligent Document Generation Engine.

Dynamically derives:
1. Research complexity and dynamic source bounds (no rigid quotas).
2. Prioritized subtopic decomposition with focused search queries.
3. Adaptive research expansion and evidence gap detection.
4. Content-driven report budgeting or strict adherence to user page/slide constraints
   (exact, approximate, maximum, or content-driven).
5. Visual intelligence selecting only relevant assets (architecture diagrams,
   comparison tables, charts, formulas, flowcharts).
6. Format-specific content planning across all 7 formats: PDF, DOCX, PPTX, XLSX, CSV, Markdown, HTML.
"""
from __future__ import annotations

import re
import time
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field

from .models import (
    DataTable,
    EvidenceClassification,
    FormulaItem,
    ResearchClaim,
    ResearchKnowledgePackage,
    ResearchSubtopic,
    SourceReference,
    VisualAsset,
)


class PageConstraintType(str, Enum):
    """Categorization of user-specified length/page bounds."""
    EXACT = "exact"                     # e.g., "create a 5-page report", "5-page PDF"
    APPROXIMATE = "approximate"         # e.g., "approximately 5 pages", "about 5 pages", "around 5 pages"
    MAXIMUM = "maximum"                 # e.g., "under 5 pages", "maximum 5 pages", "at most 5 pages"
    CONTENT_DRIVEN = "content_driven"   # Unspecified; length driven naturally by content and evidence


class ComplexityTier(str, Enum):
    """Complexity classification governing source scoping and subtopic depth."""
    SIMPLE = "simple"                     # 1-2 sources, brief/focused scope
    STANDARD = "standard"                 # 2-3 sources, multi-faceted technical topic
    COMPLEX_COMPARATIVE = "complex_comparative" # 3-5 sources, multiple frameworks/platforms or deep math/systems


WORD_TO_NUM: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
}


class SubtopicPlan(BaseModel):
    """Target subtopic in an adaptive research plan."""
    subtopic_id: str
    title: str
    search_query: str
    priority: int = 1               # 1=critical/core, 2=comparative/technical, 3=applications/tradeoffs
    min_evidence_needed: int = 1
    evidence_count: int = 0
    is_covered: bool = False
    findings_summary: Optional[str] = None

    model_config = {"populate_by_name": True}


class VisualPlan(BaseModel):
    """Visual intelligence plan determining what graphics and equations are relevant."""
    need_architecture_diagram: bool = False
    need_comparison_table: bool = False
    need_chart: bool = False
    need_formulas: bool = False
    need_flowchart: bool = False
    rationale: dict[str, str] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class SectionAllocation(BaseModel):
    """Space and content budget allocated to a report section."""
    section_id: str
    title: str
    page_number: Optional[int] = None       # Allocated page index (1-based) for exact/approximate constraints
    page_budget: float = 1.0               # Target page fraction/count
    word_budget: int = 350                 # Estimated word budget
    subtopic_ids: list[str] = Field(default_factory=list)
    requires_visual: bool = False
    visual_type: Optional[str] = None      # "diagram" | "table" | "formula" | "chart"

    model_config = {"populate_by_name": True}


class PageConstraint(BaseModel):
    """Explicit or derived document length constraint."""
    constraint_type: PageConstraintType = PageConstraintType.CONTENT_DRIVEN
    target_pages: Optional[int] = None
    min_pages: int = 1
    max_pages: int = 10
    tolerance: int = 0
    is_slides: bool = False
    target_slides: Optional[int] = None
    raw_expression: Optional[str] = None

    model_config = {"populate_by_name": True}


class ResearchPlan(BaseModel):
    """Comprehensive adaptive research and deliverable plan."""
    plan_id: str
    topic_title: str
    complexity: ComplexityTier
    requested_depth: str = "standard"       # "brief", "standard", "comprehensive", "exhaustive"
    min_sources: int = 2
    target_sources: int = 3
    max_sources: int = 5
    page_constraint: PageConstraint
    visual_plan: VisualPlan
    subtopics: list[SubtopicPlan] = Field(default_factory=list)
    section_allocations: list[SectionAllocation] = Field(default_factory=list)
    expansion_count: int = 0
    max_expansions: int = 2
    created_at: float = Field(default_factory=time.time)

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# 1. Page Constraint Parsing
# ---------------------------------------------------------------------------

PAGE_NUM_PATTERN = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty)"

RE_SLIDE_COUNT = re.compile(
    rf"\b({PAGE_NUM_PATTERN})(?:\s*-\s*({PAGE_NUM_PATTERN}))?\s*(?:-\s*)?slides?\b",
    re.IGNORECASE,
)
RE_APPROX_PAGE = re.compile(
    rf"\b(?:approximately|approx\.?|about|around|roughly|~)\s*({PAGE_NUM_PATTERN})\s*(?:-\s*)?pages?\b",
    re.IGNORECASE,
)
RE_MAX_PAGE = re.compile(
    rf"\b(?:under|less\s+than|at\s+most|maximum|max\.?|up\s+to|no\s+more\s+than)\s*({PAGE_NUM_PATTERN})\s*(?:-\s*)?pages?\b",
    re.IGNORECASE,
)
RE_RANGE_PAGE = re.compile(
    rf"\b({PAGE_NUM_PATTERN})\s*(?:-|–|—|to)\s*({PAGE_NUM_PATTERN})\s*(?:pages?|(?:-\s*)?page\s+(?:report|pdf|doc(?:ument)?|deliverable))\b",
    re.IGNORECASE,
)
RE_EXACT_PAGE = re.compile(
    rf"\b({PAGE_NUM_PATTERN})\s*-\s*page(?:\s+report|\s+pdf|\s+doc(?:ument)?|\s+deliverable)?\b|\b({PAGE_NUM_PATTERN})\s+pages?\b",
    re.IGNORECASE,
)


def _parse_num(val: str) -> Optional[int]:
    """Convert digit string or English word to integer."""
    if not val:
        return None
    val_clean = val.strip().lower()
    if val_clean.isdigit():
        return int(val_clean)
    return WORD_TO_NUM.get(val_clean)


def parse_page_constraint(
    instruction: str,
    deliverable_type: Optional[str] = None,
    format_hint: Optional[str] = None,
) -> PageConstraint:
    """Extract explicit or content-driven length constraints from instruction."""
    text = instruction or ""

    # Check for slides first
    m_slide = RE_SLIDE_COUNT.search(text)
    if m_slide or deliverable_type == "presentation" or format_hint == "pptx":
        if m_slide:
            n1 = _parse_num(m_slide.group(1)) or 5
            n2 = _parse_num(m_slide.group(2))
            target = n2 if n2 else n1
            return PageConstraint(
                constraint_type=PageConstraintType.EXACT,
                target_pages=target,
                min_pages=n1,
                max_pages=target,
                is_slides=True,
                target_slides=target,
                raw_expression=m_slide.group(0),
            )
        # Default presentation slide count
        return PageConstraint(
            constraint_type=PageConstraintType.CONTENT_DRIVEN,
            target_pages=5,
            min_pages=3,
            max_pages=8,
            is_slides=True,
            target_slides=5,
            raw_expression="default presentation",
        )

    # Approximate page count
    m_approx = RE_APPROX_PAGE.search(text)
    if m_approx:
        n = _parse_num(m_approx.group(1))
        if n and n > 0:
            return PageConstraint(
                constraint_type=PageConstraintType.APPROXIMATE,
                target_pages=n,
                min_pages=max(1, n - 1),
                max_pages=n + 1,
                tolerance=1,
                raw_expression=m_approx.group(0),
            )

    # Maximum page count
    m_max = RE_MAX_PAGE.search(text)
    if m_max:
        n = _parse_num(m_max.group(1))
        if n and n > 0:
            return PageConstraint(
                constraint_type=PageConstraintType.MAXIMUM,
                target_pages=None,
                min_pages=1,
                max_pages=n,
                raw_expression=m_max.group(0),
            )

    # Page range (e.g. "3-5 page report", "three to five pages")
    m_range = RE_RANGE_PAGE.search(text)
    if m_range:
        n1 = _parse_num(m_range.group(1))
        n2 = _parse_num(m_range.group(2))
        if n1 and n2 and 0 < n1 <= n2:
            return PageConstraint(
                constraint_type=PageConstraintType.APPROXIMATE,
                target_pages=n2,
                min_pages=n1,
                max_pages=n2,
                tolerance=n2 - n1,
                raw_expression=m_range.group(0),
            )

    # Exact page count (e.g. "5-page report", "five pages")
    m_exact = RE_EXACT_PAGE.search(text)
    if m_exact:
        num_str = m_exact.group(1) or m_exact.group(2)
        n = _parse_num(num_str)
        if n and n > 0:
            return PageConstraint(
                constraint_type=PageConstraintType.EXACT,
                target_pages=n,
                min_pages=n,
                max_pages=n,
                raw_expression=m_exact.group(0),
            )

    # Content-driven default
    return PageConstraint(
        constraint_type=PageConstraintType.CONTENT_DRIVEN,
        target_pages=None,
        min_pages=1,
        max_pages=10,
        raw_expression=None,
    )


# ---------------------------------------------------------------------------
# 2. Complexity Assessment & Dynamic Source Sizing
# ---------------------------------------------------------------------------

SIMPLE_KEYWORDS = [
    "what is", "define", "definition of", "quick overview", "brief summary",
    "simple explanation", "binary search", "how does a hashtable work",
    "how to use", "synopsis", "quick introduction",
]

COMPLEX_KEYWORDS = [
    "compare", "comparison", "versus", " vs ", "tradeoffs", "trade-offs",
    "benchmarks", "benchmark", "distributed systems", "platforms",
    "multi-head attention", "comprehensive", "in-depth", "exhaustive",
    "state of the art", "deep-dive", "independent sources", "credible sources",
    "multiple sources", "cross-reference", "architectural analysis",
    "evaluation", "ecosystem", "distributed", "consensus",
]


def assess_complexity_and_sources(
    instruction: str,
    topic: Optional[str] = None,
) -> tuple[ComplexityTier, int, int, int, str]:
    """Derive topic complexity, source bounds (min, target, max), and requested depth."""
    combined = f"{instruction} {topic or ''}".lower()

    # Requested depth modifier
    if any(k in combined for k in ["exhaustive", "deep-dive", "in-depth", "comprehensive"]):
        depth = "comprehensive"
    elif any(k in combined for k in ["brief", "quick", "short", "concise"]):
        depth = "brief"
    else:
        depth = "standard"

    # Evaluate complexity tier
    is_complex = any(k in combined for k in COMPLEX_KEYWORDS)
    is_simple = any(k in combined for k in SIMPLE_KEYWORDS) and not is_complex

    if is_simple and depth != "comprehensive":
        complexity = ComplexityTier.SIMPLE
        min_sources = 1
        target_sources = 1
        max_sources = 2
    elif is_complex or depth == "comprehensive":
        complexity = ComplexityTier.COMPLEX_COMPARATIVE
        min_sources = 3
        target_sources = 4
        max_sources = 6
    else:
        complexity = ComplexityTier.STANDARD
        min_sources = 2
        target_sources = 3
        max_sources = 4

    # Depth adjustments
    if depth == "brief":
        min_sources = max(1, min_sources - 1)
        target_sources = max(1, target_sources - 1)

    return complexity, min_sources, target_sources, max_sources, depth


# ---------------------------------------------------------------------------
# 3. Visual Intelligence Utility
# ---------------------------------------------------------------------------

DIAGRAM_KEYWORDS = [
    "architecture", "diagram", "neural", "network", "transformer", "encoder",
    "decoder", "layer", "model", "pipeline", "distributed", "system design",
    "component", "structure", "subsystems",
]

TABLE_KEYWORDS = [
    "compare", "comparison", "vs", "versus", "tradeoff", "trade-offs",
    "difference", "benchmark", "platforms", "frameworks", "pros and cons",
    "matrix", "options", "evaluation",
]

CHART_KEYWORDS = [
    "chart", "statistics", "benchmark", "metrics", "performance",
    "throughput", "latency", "accuracy", "speed", "numbers", "evaluation",
    "growth", "volume",
]

FORMULA_KEYWORDS = [
    "formula", "equation", "math", "mathematical", "attention", "softmax",
    "loss", "gradient", "algorithm", "dot-product", "weights", "qk", "q, k, v",
    "positional encoding", "scaling",
]

FLOWCHART_KEYWORDS = [
    "flowchart", "workflow", "lifecycle", "process", "steps", "protocol",
    "sequence", "pipeline stages",
]


def derive_visual_plan(
    instruction: str,
    topic: Optional[str] = None,
    complexity: ComplexityTier = ComplexityTier.STANDARD,
) -> VisualPlan:
    """Select only relevant graphical and equation assets based on domain utility."""
    combined = f"{instruction} {topic or ''}".lower()
    rationale: dict[str, str] = {}

    need_diag = any(k in combined for k in DIAGRAM_KEYWORDS)
    if need_diag:
        rationale["architecture_diagram"] = "System or neural architecture warrants structural visualization."

    need_tbl = any(k in combined for k in TABLE_KEYWORDS) or complexity == ComplexityTier.COMPLEX_COMPARATIVE
    if need_tbl:
        rationale["comparison_table"] = "Tradeoff or multi-platform comparison is best expressed in structured tabular form."

    need_chart = any(k in combined for k in CHART_KEYWORDS)
    if need_chart:
        rationale["chart"] = "Quantitative or benchmark metrics benefit from statistical chart rendering."

    need_form = any(k in combined for k in FORMULA_KEYWORDS)
    if need_form:
        rationale["formulas"] = "Mathematical formulations or attention equations are central to the topic."

    need_flow = any(k in combined for k in FLOWCHART_KEYWORDS)
    if need_flow:
        rationale["flowchart"] = "Multi-step algorithmic or protocol workflow benefits from flowchart sequencing."

    return VisualPlan(
        need_architecture_diagram=need_diag,
        need_comparison_table=need_tbl,
        need_chart=need_chart,
        need_formulas=need_form,
        need_flowchart=need_flow,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# 4. Prioritized Subtopic Decomposition
# ---------------------------------------------------------------------------

def decompose_subtopics(
    instruction: str,
    topic: str,
    complexity: ComplexityTier,
) -> list[SubtopicPlan]:
    """Decompose topic into 3-5 prioritized subtopics with targeted search queries."""
    text = f"{instruction} {topic}".lower()
    subtopics: list[SubtopicPlan] = []

    # Domain: Transformers / Attention
    if "transformer" in text or "attention" in text:
        subtopics.append(SubtopicPlan(
            subtopic_id="sub_01",
            title="Transformer Architecture & Foundational Mechanics",
            search_query="Transformer architecture foundation Vaswani attention is all you need",
            priority=1,
            min_evidence_needed=1,
        ))
        subtopics.append(SubtopicPlan(
            subtopic_id="sub_02",
            title="Scaled Dot-Product Self-Attention Mechanism",
            search_query="Scaled dot-product attention equation softmax square root key dimension",
            priority=1,
            min_evidence_needed=1,
        ))
        subtopics.append(SubtopicPlan(
            subtopic_id="sub_03",
            title="Multi-Head Attention & Representation Subspaces",
            search_query="Multi-head attention projections representation subspaces concatenation",
            priority=1,
            min_evidence_needed=1,
        ))
        subtopics.append(SubtopicPlan(
            subtopic_id="sub_04",
            title="Encoder-Only vs Decoder-Only Architectures",
            search_query="Encoder-only BERT vs Decoder-only GPT causal masking comparison",
            priority=2,
            min_evidence_needed=1,
        ))
        subtopics.append(SubtopicPlan(
            subtopic_id="sub_05",
            title="Practical Applications, Implementations & Tradeoffs",
            search_query="Transformer practical applications PyTorch Hugging Face computational complexity tradeoffs",
            priority=3,
            min_evidence_needed=1,
        ))

    # Domain: Distributed Systems / Backend Frameworks
    elif "distributed" in text or "backend" in text or "database" in text or "framework" in text:
        subtopics.append(SubtopicPlan(
            subtopic_id="sub_01",
            title="Architectural Paradigms & Core Design Principles",
            search_query=f"{topic} architecture design principles paradigms",
            priority=1,
            min_evidence_needed=1,
        ))
        subtopics.append(SubtopicPlan(
            subtopic_id="sub_02",
            title="Performance Benchmarks, Concurrency & Scalability",
            search_query=f"{topic} performance throughput latency scalability benchmarks",
            priority=1,
            min_evidence_needed=1,
        ))
        subtopics.append(SubtopicPlan(
            subtopic_id="sub_03",
            title="Comparative Framework Evaluation & Feature Matrix",
            search_query=f"{topic} comparative analysis feature matrix tradeoffs",
            priority=2,
            min_evidence_needed=1,
        ))
        subtopics.append(SubtopicPlan(
            subtopic_id="sub_04",
            title="Operational Complexity, Ecosystem & Industry Adoption",
            search_query=f"{topic} operational maintenance ecosystem production use cases",
            priority=3,
            min_evidence_needed=1,
        ))

    # Generic Simple Topic
    elif complexity == ComplexityTier.SIMPLE:
        subtopics.append(SubtopicPlan(
            subtopic_id="sub_01",
            title=f"{topic} Core Principles & Definition",
            search_query=f"{topic} definition explanation fundamentals",
            priority=1,
            min_evidence_needed=1,
        ))
        subtopics.append(SubtopicPlan(
            subtopic_id="sub_02",
            title="Operational Mechanics & Practical Application",
            search_query=f"{topic} mechanics how it works practical implementation",
            priority=2,
            min_evidence_needed=1,
        ))

    # Generic Standard / Complex Topic
    else:
        subtopics.append(SubtopicPlan(
            subtopic_id="sub_01",
            title=f"{topic} Architectural Overview & Fundamentals",
            search_query=f"{topic} overview fundamental architecture technical principles",
            priority=1,
            min_evidence_needed=1,
        ))
        subtopics.append(SubtopicPlan(
            subtopic_id="sub_02",
            title="Core Technical Mechanics & Methodologies",
            search_query=f"{topic} technical mechanics implementation methods",
            priority=1,
            min_evidence_needed=1,
        ))
        subtopics.append(SubtopicPlan(
            subtopic_id="sub_03",
            title="Comparative Evaluation, Tradeoffs & Best Practices",
            search_query=f"{topic} comparison tradeoffs limitations best practices",
            priority=2,
            min_evidence_needed=1,
        ))
        subtopics.append(SubtopicPlan(
            subtopic_id="sub_04",
            title="Real-World Applications & Industry Standards",
            search_query=f"{topic} industry standards applications production case studies",
            priority=3,
            min_evidence_needed=1,
        ))

    return subtopics


# ---------------------------------------------------------------------------
# 5. Section Space Allocation & Document Budgeting
# ---------------------------------------------------------------------------

def create_section_allocations(
    plan_id: str,
    page_constraint: PageConstraint,
    subtopics: list[SubtopicPlan],
    visual_plan: VisualPlan,
) -> list[SectionAllocation]:
    """Map subtopics and visual assets into page and word allocations."""
    target_pages = page_constraint.target_pages

    # 5-Page Exact Allocation
    if target_pages == 5:
        return [
            SectionAllocation(
                section_id="sec_01",
                title="1. Executive Summary & Core Architectural Mechanics",
                page_number=1,
                page_budget=1.0,
                word_budget=350,
                subtopic_ids=[s.subtopic_id for s in subtopics[:1]],
                requires_visual=False,
            ),
            SectionAllocation(
                section_id="sec_02",
                title="2. Algorithmic Formulations & Attention Mechanisms",
                page_number=2,
                page_budget=1.0,
                word_budget=420,
                subtopic_ids=[s.subtopic_id for s in subtopics[1:2]],
                requires_visual=visual_plan.need_formulas,
                visual_type="formula" if visual_plan.need_formulas else None,
            ),
            SectionAllocation(
                section_id="sec_03",
                title="3. Architectural Variants & Multi-Head Projections",
                page_number=3,
                page_budget=1.0,
                word_budget=400,
                subtopic_ids=[s.subtopic_id for s in subtopics[2:4]],
                requires_visual=visual_plan.need_architecture_diagram,
                visual_type="diagram" if visual_plan.need_architecture_diagram else None,
            ),
            SectionAllocation(
                section_id="sec_04",
                title="4. Structured Platform Comparison & Framework Tradeoffs",
                page_number=4,
                page_budget=1.0,
                word_budget=350,
                subtopic_ids=[s.subtopic_id for s in subtopics[4:]],
                requires_visual=visual_plan.need_comparison_table,
                visual_type="table" if visual_plan.need_comparison_table else None,
            ),
            SectionAllocation(
                section_id="sec_05",
                title="5. Discrepancies, Source Provenance & References Cited",
                page_number=5,
                page_budget=1.0,
                word_budget=300,
                subtopic_ids=[],
                requires_visual=True,
                visual_type="table",
            ),
        ]

    # Slide Deck Allocation (1:1 with slides)
    if page_constraint.is_slides:
        target_s = page_constraint.target_slides or 5
        allocations: list[SectionAllocation] = []
        allocations.append(SectionAllocation(
            section_id="slide_01",
            title="Title Slide & Metadata Overview",
            page_number=1,
            page_budget=1.0,
            word_budget=80,
            subtopic_ids=[],
        ))
        allocations.append(SectionAllocation(
            section_id="slide_02",
            title="Executive Summary & Core Architecture",
            page_number=2,
            page_budget=1.0,
            word_budget=150,
            subtopic_ids=[s.subtopic_id for s in subtopics[:1]],
        ))
        allocations.append(SectionAllocation(
            section_id="slide_03",
            title="Technical Foundations & Formulations",
            page_number=3,
            page_budget=1.0,
            word_budget=150,
            subtopic_ids=[s.subtopic_id for s in subtopics[1:3]],
            requires_visual=visual_plan.need_formulas,
            visual_type="formula",
        ))
        allocations.append(SectionAllocation(
            section_id="slide_04",
            title="Structured Architectural & Platform Comparison",
            page_number=4,
            page_budget=1.0,
            word_budget=120,
            subtopic_ids=[s.subtopic_id for s in subtopics[3:]],
            requires_visual=visual_plan.need_comparison_table,
            visual_type="table",
        ))
        allocations.append(SectionAllocation(
            section_id="slide_05",
            title="References Cited & Source Provenance",
            page_number=5,
            page_budget=1.0,
            word_budget=100,
            subtopic_ids=[],
            requires_visual=True,
            visual_type="table",
        ))
        # If user asked for more slides, add targeted expansion slides
        while len(allocations) < target_s:
            idx = len(allocations) + 1
            allocations.insert(-1, SectionAllocation(
                section_id=f"slide_{idx:02d}",
                title=f"Detailed Technical Analysis - Part {idx-2}",
                page_number=idx,
                page_budget=1.0,
                word_budget=150,
                subtopic_ids=[s.subtopic_id for s in subtopics],
            ))
        return allocations

    # General Content-Driven or Other Target Allocation
    allocations = []
    allocations.append(SectionAllocation(
        section_id="sec_01",
        title="1. Executive Summary & Core Architectural Mechanics",
        page_budget=1.0,
        word_budget=350,
        subtopic_ids=[s.subtopic_id for s in subtopics[:1]],
    ))
    if visual_plan.need_formulas:
        allocations.append(SectionAllocation(
            section_id="sec_02",
            title="2. Algorithmic & Mathematical Formulations",
            page_budget=1.0,
            word_budget=400,
            subtopic_ids=[s.subtopic_id for s in subtopics[1:2]],
            requires_visual=True,
            visual_type="formula",
        ))
    allocations.append(SectionAllocation(
        section_id="sec_03",
        title="3. Detailed Technical Findings & Subtopic Analysis",
        page_budget=1.5,
        word_budget=600,
        subtopic_ids=[s.subtopic_id for s in subtopics],
        requires_visual=visual_plan.need_architecture_diagram,
        visual_type="diagram" if visual_plan.need_architecture_diagram else None,
    ))
    if visual_plan.need_comparison_table:
        allocations.append(SectionAllocation(
            section_id="sec_04",
            title="4. Structured Comparison & Tradeoff Matrix",
            page_budget=1.0,
            word_budget=300,
            requires_visual=True,
            visual_type="table",
        ))
    allocations.append(SectionAllocation(
        section_id="sec_05",
        title="5. References Cited & Source Provenance",
        page_budget=0.8,
        word_budget=200,
        requires_visual=True,
        visual_type="table",
    ))
    return allocations


# ---------------------------------------------------------------------------
# 6. Unified Plan Generation
# ---------------------------------------------------------------------------

def generate_adaptive_research_plan(
    instruction: str,
    context: Optional[dict[str, Any]] = None,
) -> ResearchPlan:
    """Create a complete adaptive research and deliverable plan from user prompt."""
    ctx = context or {}
    topic_title = ctx.get("research_topic") or instruction
    # Clean topic title if it looks like a long command
    if len(topic_title) > 80:
        clean_title = re.sub(r"^(?:research|generate|create|build|write|investigate)\s+", "", topic_title, flags=re.IGNORECASE).strip()
        first_clause = clean_title.split(",")[0].split(".")[0].strip()
        topic_title = first_clause.title() if len(first_clause) < 60 else first_clause[:60].title()

    deliverable_type = ctx.get("deliverable_type")
    format_hint = ctx.get("requested_format")

    # 1. Parse page bounds
    page_constraint = parse_page_constraint(instruction, deliverable_type, format_hint)

    # 2. Assess complexity and dynamic sources
    complexity, min_src, target_src, max_src, depth = assess_complexity_and_sources(instruction, topic_title)

    # 3. Derive visual plan
    visual_plan = derive_visual_plan(instruction, topic_title, complexity)

    # 4. Decompose into prioritized subtopics
    subtopics = decompose_subtopics(instruction, topic_title, complexity)

    # 5. Allocate space and document sections
    plan_id = f"plan_{int(time.time()*1000)}"
    allocations = create_section_allocations(plan_id, page_constraint, subtopics, visual_plan)

    return ResearchPlan(
        plan_id=plan_id,
        topic_title=topic_title,
        complexity=complexity,
        requested_depth=depth,
        min_sources=min_src,
        target_sources=target_src,
        max_sources=max_src,
        page_constraint=page_constraint,
        visual_plan=visual_plan,
        subtopics=subtopics,
        section_allocations=allocations,
        expansion_count=0,
        max_expansions=2,
    )


# ---------------------------------------------------------------------------
# 7. Adaptive Research Expansion & Evidence Gap Detection
# ---------------------------------------------------------------------------

def detect_evidence_gaps(
    plan: ResearchPlan,
    pkg: ResearchKnowledgePackage,
) -> list[SubtopicPlan]:
    """Scan knowledge package claims and evidence to find planned subtopics lacking backing."""
    gaps: list[SubtopicPlan] = []

    # Map claims and evidence to subtopics
    claims_list = list(pkg.claims.values())
    evidence_list = list(pkg.evidence.values())

    for sub in plan.subtopics:
        # Match by explicit subtopic ID
        matching_claims = [c for c in claims_list if c.subtopic_id == sub.subtopic_id]
        
        # Match by title keyword relevance
        keywords = [w.lower() for w in re.findall(r"\b[A-Za-z]{4,}\b", sub.title) if w.lower() not in {"architecture", "mechanics", "overview", "core"}]
        if not matching_claims and keywords:
            for c in claims_list:
                stmt_lower = c.statement.lower()
                if any(kw in stmt_lower for kw in keywords):
                    matching_claims.append(c)

        # Match evidence items
        matching_ev = []
        if keywords:
            for ev in evidence_list:
                quote_lower = ev.quote.lower()
                if any(kw in quote_lower for kw in keywords):
                    matching_ev.append(ev)

        total_backing = len(matching_claims) + len(matching_ev)
        sub.evidence_count = total_backing
        sub.is_covered = (total_backing >= sub.min_evidence_needed)

        if not sub.is_covered:
            gaps.append(sub)

    # Sort gaps by priority ascending (priority 1 critical gaps first)
    gaps.sort(key=lambda s: s.priority)
    return gaps


def generate_targeted_gap_queries(
    gaps: list[SubtopicPlan],
    topic: str,
) -> list[str]:
    """Formulate clean technical search queries targeting missing evidence gaps."""
    queries: list[str] = []
    for gap in gaps:
        q = gap.search_query.strip()
        # Ensure query contains main topic entity
        if topic.lower() not in q.lower():
            q = f"{topic} {q}"
        # Strip conversational or punctuation noise
        q = re.sub(r"[?!,;\"']", " ", q)
        q = re.sub(r"\s+", " ", q).strip()
        if q and q not in queries:
            queries.append(q)
    return queries

