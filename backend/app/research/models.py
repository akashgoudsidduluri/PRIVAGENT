"""Canonical Research Knowledge Package and Evidence Provenance Models.

Defines the core domain schema for autonomous research in PrivAgent:
- Separates research evidence and facts from presentation and output renderers.
- Preserves fine-grained claim-to-source provenance, confidence scores, and timestamps.
- Distinguishes verified, single-source, independently corroborated, conflicting,
  user-stated, and unverified information.
- Accommodates rich visual assets (photos, charts, diagrams), LaTeX formulas, and data tables.
- Supports reliable, lossless JSON serialization and deserialization across task checkpoints.
"""
from __future__ import annotations

import json
import time
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


class EvidenceClassification(str, Enum):
    """Rigorous classification of research evidence and synthesized claims."""
    USER_STATED = "user_stated"                     # Supplied directly by the user in the prompt
    OBSERVED = "observed"                           # Directly observed in DOM or page state
    SINGLE_SOURCE = "single_source"                 # Supported by exactly one external source
    CORROBORATED = "independently_corroborated"    # Confirmed by 2+ independent, non-derivative sources
    CONFLICTING = "conflicting"                     # Contradicted by one or more sources
    UNVERIFIED = "unverified"                       # Assertion lacking verifiable source backing

    @classmethod
    def from_str(cls, val: str) -> EvidenceClassification:
        v = (val or "").strip().lower().replace(" ", "_").replace("-", "_")
        for member in cls:
            if member.value == v or member.name.lower() == v:
                return member
        if "corroborat" in v or "independent" in v:
            return cls.CORROBORATED
        if "single" in v:
            return cls.SINGLE_SOURCE
        if "conflict" in v or "contradict" in v:
            return cls.CONFLICTING
        if "observ" in v:
            return cls.OBSERVED
        if "user" in v:
            return cls.USER_STATED
        return cls.UNVERIFIED


class SourceReference(BaseModel):
    """External source visited, examined, or cited during research."""
    source_id: str = Field(description="Unique source identifier, e.g. 'src_1'")
    url: str = Field(description="Canonical URL of the source")
    domain: str = Field(description="Normalized domain or hostname, e.g. 'nature.com'")
    title: Optional[str] = Field(default=None, description="Document or page title")
    author: Optional[str] = Field(default=None, description="Author or publishing entity")
    publication_date: Optional[str] = Field(default=None, description="Publication or last-modified date string")
    access_timestamp: float = Field(default_factory=time.time, description="Unix timestamp when accessed")
    reliability_score: float = Field(default=1.0, ge=0.0, le=1.0, description="Heuristic source reliability score")
    reliability_tier: Optional[str] = Field(default=None, description="Defensible reliability tier: peer_reviewed, official_documentation, credible_reference, secondary_reporting, unverified")
    is_primary: bool = Field(default=False, description="True if official, peer-reviewed, or primary source")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional source metadata")

    model_config = {"populate_by_name": True}


class EvidenceItem(BaseModel):
    """Specific textual quote or factual snippet extracted from a source."""
    evidence_id: str = Field(description="Unique evidence identifier, e.g. 'ev_1'")
    source_id: str = Field(description="Identifier of the supporting SourceReference")
    quote: str = Field(description="Verbatim or close textual snippet extracted from page")
    context: Optional[str] = Field(default=None, description="Surrounding paragraph or section context")
    location: Optional[str] = Field(default=None, description="Heading, CSS selector, or section title")
    extracted_at: float = Field(default_factory=time.time, description="Unix timestamp when extracted")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Extraction confidence score")

    model_config = {"populate_by_name": True}


class ResearchClaim(BaseModel):
    """Synthesized claim, finding, or conclusion backed by evidence and sources."""
    claim_id: str = Field(description="Unique claim identifier, e.g. 'clm_1'")
    statement: str = Field(description="Core assertion or factual statement")
    classification: EvidenceClassification = Field(
        default=EvidenceClassification.UNVERIFIED,
        description="Verification classification of this claim",
    )
    subtopic_id: Optional[str] = Field(default=None, description="Optional parent subtopic identifier")
    primary_source_id: Optional[str] = Field(default=None, description="Lead source identifier")
    supporting_source_ids: list[str] = Field(
        default_factory=list,
        description="Source IDs that corroborate this claim",
    )
    evidence_item_ids: list[str] = Field(
        default_factory=list,
        description="IDs of EvidenceItems supporting this claim",
    )
    counter_evidence_ids: list[str] = Field(
        default_factory=list,
        description="IDs of EvidenceItems contradicting this claim",
    )
    contradiction_notes: Optional[str] = Field(
        default=None,
        description="Details on observed discrepancies or conflicting claims",
    )
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Overall confidence")
    claim_type: str = Field(
        default="general",
        description="Claim category: 'definition' | 'mechanism' | 'advantage' | 'disadvantage' | 'statistic' | 'comparison' | 'general'",
    )
    tags: list[str] = Field(default_factory=list, description="Categorization tags")

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def validate_classification_integrity(self) -> ResearchClaim:
        """Enforce strict integrity between classification, counter-evidence, and corroboration."""
        # A claim cannot be classified as corroborated or single-source if it lacks evidence items
        if not self.evidence_item_ids and self.classification in (EvidenceClassification.CORROBORATED, EvidenceClassification.SINGLE_SOURCE):
            self.classification = EvidenceClassification.UNVERIFIED
            self.confidence = min(self.confidence, 0.4)

        # A claim cannot be classified as corroborated if it has counter-evidence or contradiction notes
        if self.counter_evidence_ids or self.contradiction_notes:
            if self.classification == EvidenceClassification.CORROBORATED:
                raise ValueError(
                    f"Claim {self.claim_id!r} cannot be classified as CORROBORATED because it has counter-evidence or contradiction notes. Must be CONFLICTING."
                )
        if self.classification == EvidenceClassification.CORROBORATED:
            # Corroboration requires at least two distinct sources (primary + supporting)
            total_sources = set(self.supporting_source_ids)
            if self.primary_source_id:
                total_sources.add(self.primary_source_id)
            if len(total_sources) < 2:
                raise ValueError(
                    f"Claim {self.claim_id!r} classified as CORROBORATED requires at least 2 distinct sources, but found {len(total_sources)}."
                )
        return self


class DataTable(BaseModel):
    """Structured tabular data, metrics, comparisons, or calculation outputs."""
    table_id: str = Field(description="Unique table identifier, e.g. 'tbl_1'")
    title: str = Field(description="Table title or heading")
    headers: list[str] = Field(description="List of column header labels")
    rows: list[list[Any]] = Field(description="2D matrix of row cells")
    caption: Optional[str] = Field(default=None, description="Optional table caption or explanation")
    source_ids: list[str] = Field(default_factory=list, description="Source IDs backing the table data")
    subtopic_id: Optional[str] = Field(default=None, description="Related subtopic identifier")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Formatting or units metadata")

    model_config = {"populate_by_name": True}


class VisualAsset(BaseModel):
    """Photograph, chart, diagram, or illustration associated with research."""
    asset_id: str = Field(description="Unique asset identifier, e.g. 'vis_1'")
    asset_type: str = Field(
        description="Asset type: 'photograph' | 'chart' | 'diagram' | 'flowchart' | 'screenshot' | 'illustration'",
    )
    title: str = Field(description="Descriptive title of the visual asset")
    caption: Optional[str] = Field(default=None, description="Caption or explanation of what the visual depicts")
    url_or_path: Optional[str] = Field(
        default=None,
        description="Local cached file path or remote image URL",
    )
    source_url: Optional[str] = Field(default=None, description="Original page where asset was located")
    source_id: Optional[str] = Field(default=None, description="Linked SourceReference identifier")
    attribution: Optional[str] = Field(
        default=None,
        description="Photographer, author, or publisher credit and provenance",
    )
    is_generated: bool = Field(
        default=False,
        description="True for generated diagrams/charts; False for authentic external photographs",
    )
    license: Optional[str] = Field(default=None, description="Usage license or copyright notice")
    subtopic_id: Optional[str] = Field(default=None, description="Related subtopic identifier")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Dimensions, format, or render specs")

    model_config = {"populate_by_name": True}


class FormulaItem(BaseModel):
    """Mathematical equation, scientific formula, or algorithmic notation."""
    formula_id: str = Field(description="Unique formula identifier, e.g. 'eq_1'")
    name: str = Field(description="Name or concept, e.g. 'Scaled Dot-Product Attention'")
    latex: str = Field(description="LaTeX representation of the formula")
    explanation: Optional[str] = Field(default=None, description="Conceptual explanation of the equation")
    variables: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping of variable symbols to explanations (e.g. {'Q': 'Queries matrix'})",
    )
    worked_example: Optional[str] = Field(default=None, description="Step-by-step worked numerical or algebraic example")
    subtopic_id: Optional[str] = Field(default=None, description="Related subtopic identifier")

    model_config = {"populate_by_name": True}


class ResearchSubtopic(BaseModel):
    """Target subtopic, research question, or investigative angle."""
    subtopic_id: str = Field(description="Unique subtopic identifier, e.g. 'sub_1'")
    title: str = Field(description="Subtopic title or question")
    description: Optional[str] = Field(default=None, description="Detailed subtopic scope")
    queries: list[str] = Field(
        default_factory=list,
        description="Target search queries formulated to investigate this subtopic",
    )
    status: str = Field(
        default="pending",
        description="'pending' | 'in_progress' | 'completed' | 'insufficient_data'",
    )
    findings_summary: Optional[str] = Field(default=None, description="Synthesized summary for this subtopic")
    claim_ids: list[str] = Field(
        default_factory=list,
        description="Identifiers of claims addressing this subtopic",
    )

    model_config = {"populate_by_name": True}


class ResearchTopic(BaseModel):
    """Top-level research objective, scope, and deliverables."""
    title: str = Field(description="Research topic title")
    objective: str = Field(description="Overall research objective or primary question")
    scope: Optional[str] = Field(default=None, description="Boundaries or depth of the research")
    target_format: Optional[str] = Field(
        default=None,
        description="Requested output deliverable format ('docx', 'pdf', 'pptx', 'xlsx', 'csv', 'markdown', 'html')",
    )
    subtopics: list[ResearchSubtopic] = Field(
        default_factory=list,
        description="Structured subtopics covering the objective",
    )
    keywords: list[str] = Field(default_factory=list, description="Core keywords and entity names")
    findings_summary: Optional[str] = Field(
        default=None,
        description="Executive synthesis and findings summary",
    )

    model_config = {"populate_by_name": True}


class ResearchKnowledgePackage(BaseModel):
    """Structured, format-agnostic research package ready for rendering into any artifact.

    Encapsulates all sources, evidence snippets, verified claims, tables, visual assets,
    and formulas collected during an autonomous research mission.
    """
    package_id: str = Field(description="Unique package identifier")
    topic: ResearchTopic = Field(description="Research topic and subtopic definition")
    created_at: float = Field(default_factory=time.time, description="Unix timestamp created")
    updated_at: float = Field(default_factory=time.time, description="Unix timestamp last updated")
    sources: dict[str, SourceReference] = Field(
        default_factory=dict,
        description="All visited external sources indexed by source_id",
    )
    evidence: dict[str, EvidenceItem] = Field(
        default_factory=dict,
        description="All extracted evidence snippets indexed by evidence_id",
    )
    claims: dict[str, ResearchClaim] = Field(
        default_factory=dict,
        description="All synthesized claims indexed by claim_id",
    )
    tables: list[DataTable] = Field(default_factory=list, description="Extracted and structured data tables")
    visuals: list[VisualAsset] = Field(default_factory=list, description="Visual assets, diagrams, and figures")
    formulas: list[FormulaItem] = Field(default_factory=list, description="Mathematical and scientific formulas")
    unsupported_claims: list[str] = Field(
        default_factory=list,
        description="Claims examined but lacking sufficient source support",
    )
    contradictions: list[str] = Field(
        default_factory=list,
        description="Documented contradictions or disagreements between sources",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Mission checkpoints, execution stats, or renderer hints",
    )

    model_config = {"populate_by_name": True}

    def add_source(self, source: SourceReference) -> str:
        """Register a source reference in the package."""
        self.sources[source.source_id] = source
        self.updated_at = time.time()
        return source.source_id

    def add_evidence(self, evidence: EvidenceItem) -> str:
        """Register an evidence snippet in the package."""
        self.evidence[evidence.evidence_id] = evidence
        self.updated_at = time.time()
        return evidence.evidence_id

    def add_claim(self, claim: ResearchClaim, validate_refs: bool = False) -> str:
        """Register a synthesized claim in the package."""
        if validate_refs:
            errs = self.validate_claim_references(claim)
            if errs:
                raise ValueError(f"Cannot add claim {claim.claim_id} with broken references: {'; '.join(errs)}")
        self.claims[claim.claim_id] = claim
        self.updated_at = time.time()
        return claim.claim_id

    def add_table(self, table: DataTable) -> str:
        """Append a data table to the package."""
        self.tables.append(table)
        self.updated_at = time.time()
        return table.table_id

    def add_visual(self, visual: VisualAsset) -> str:
        """Append a visual asset to the package."""
        self.visuals.append(visual)
        self.updated_at = time.time()
        return visual.asset_id

    def add_formula(self, formula: FormulaItem) -> str:
        """Append a formula to the package."""
        self.formulas.append(formula)
        self.updated_at = time.time()
        return formula.formula_id

    def validate_claim_references(self, claim: ResearchClaim) -> list[str]:
        """Validate that all source and evidence IDs referenced in a claim exist in the package."""
        errors: list[str] = []
        if claim.primary_source_id and claim.primary_source_id not in self.sources:
            errors.append(f"Claim {claim.claim_id} references unknown primary_source_id {claim.primary_source_id!r}")
        for s_id in claim.supporting_source_ids:
            if s_id not in self.sources:
                errors.append(f"Claim {claim.claim_id} references unknown supporting_source_id {s_id!r}")
        for e_id in claim.evidence_item_ids:
            if e_id not in self.evidence:
                errors.append(f"Claim {claim.claim_id} references unknown evidence_item_id {e_id!r}")
        for c_id in claim.counter_evidence_ids:
            if c_id not in self.evidence:
                errors.append(f"Claim {claim.claim_id} references unknown counter_evidence_id {c_id!r}")
        return errors

    def validate_package_integrity(self) -> list[str]:
        """Full integrity scan of all claims, sources, and evidence in this package."""
        all_errors: list[str] = []
        for claim in self.claims.values():
            all_errors.extend(self.validate_claim_references(claim))
        for ev in self.evidence.values():
            if ev.source_id not in self.sources:
                all_errors.append(f"Evidence {ev.evidence_id} references unknown source_id {ev.source_id!r}")
        for tbl in self.tables:
            for s_id in tbl.source_ids:
                if s_id not in self.sources:
                    all_errors.append(f"Table {tbl.table_id} references unknown source_id {s_id!r}")
        for vis in self.visuals:
            if vis.source_id and vis.source_id not in self.sources:
                all_errors.append(f"VisualAsset {vis.asset_id} references unknown source_id {vis.source_id!r}")
        return all_errors

    def get_claims_by_classification(self, classification: EvidenceClassification) -> list[ResearchClaim]:
        """Filter claims matching a specific verification classification."""
        return [c for c in self.claims.values() if c.classification == classification]

    def get_claims_for_source(self, source_id: str) -> list[ResearchClaim]:
        """Retrieve all claims citing a given source."""
        return [
            c for c in self.claims.values()
            if c.primary_source_id == source_id or source_id in c.supporting_source_ids
        ]

    def get_corroborated_claims(self) -> list[ResearchClaim]:
        """Retrieve all independently corroborated claims."""
        return self.get_claims_by_classification(EvidenceClassification.CORROBORATED)

    def get_conflicting_claims(self) -> list[ResearchClaim]:
        """Retrieve all conflicting claims."""
        return self.get_claims_by_classification(EvidenceClassification.CONFLICTING)

    def to_dict(self) -> dict[str, Any]:
        """Lossless dictionary representation."""
        return self.model_dump()

    def to_json(self, indent: Optional[int] = None) -> str:
        """Lossless JSON string representation."""
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResearchKnowledgePackage:
        """Reconstruct package from dictionary representation."""
        return cls.model_validate(data)

    @classmethod
    def from_json(cls, json_str: str) -> ResearchKnowledgePackage:
        """Reconstruct package from JSON string representation."""
        return cls.model_validate_json(json_str)
