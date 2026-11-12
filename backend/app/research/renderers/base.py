"""Base Abstract Renderer and Citation Management for Multi-Format Artifacts.

Provides canonical citation mapping, contradiction extraction, and common formatting
utilities across DOCX, PDF, PPTX, XLSX, CSV, Markdown, and HTML renderers.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

from ..models import (
    EvidenceClassification,
    ResearchClaim,
    ResearchKnowledgePackage,
    SourceReference,
)


class BaseRenderer(ABC):
    """Abstract base class for all PrivAgent research artifact renderers."""

    @abstractmethod
    def render(self, pkg: ResearchKnowledgePackage, output_path: str, **kwargs: Any) -> str:
        """Render the canonical ResearchKnowledgePackage into a deliverable artifact file."""
        pass

    @staticmethod
    def build_citation_map(pkg: ResearchKnowledgePackage) -> dict[str, int]:
        """Create a stable 1-indexed citation number mapping for all sources in the package."""
        sorted_sources = sorted(pkg.sources.values(), key=lambda s: s.source_id)
        return {src.source_id: idx for idx, src in enumerate(sorted_sources, start=1)}

    @staticmethod
    def format_citation_brackets(source_ids: list[str], citation_map: dict[str, int]) -> str:
        """Format a list of source IDs into a bracketed citation string, e.g. '[1, 2]'."""
        nums = sorted({citation_map[s_id] for s_id in source_ids if s_id in citation_map})
        if not nums:
            return ""
        return "[" + ", ".join(str(n) for n in nums) + "]"

    @staticmethod
    def get_ordered_sources(
        pkg: ResearchKnowledgePackage,
        citation_map: dict[str, int],
    ) -> list[tuple[int, SourceReference]]:
        """Return sources sorted by their assigned citation number."""
        ordered = []
        for src_id, num in citation_map.items():
            if src_id in pkg.sources:
                ordered.append((num, pkg.sources[src_id]))
        ordered.sort(key=lambda x: x[0])
        return ordered

    @staticmethod
    def prepare_output_path(output_path: str) -> Path:
        """Ensure parent directory exists and return resolved Path object."""
        p = Path(output_path).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

