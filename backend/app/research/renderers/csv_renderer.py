"""RFC 4180 Standard CSV Deliverable Renderer.

Exports structured tabular comparison data or verified claims matrix
using Python's standard library csv module with UTF-8 encoding.
"""
from __future__ import annotations

import csv
from typing import Any

from ..models import (
    ResearchKnowledgePackage,
)
from .base import BaseRenderer


class CsvRenderer(BaseRenderer):
    """Generates standard RFC 4180 CSV export files."""

    def render(self, pkg: ResearchKnowledgePackage, output_path: str, **kwargs: Any) -> str:
        out_file = self.prepare_output_path(output_path)

        with open(out_file, mode="w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)

            if pkg.tables:
                # Primary comparison data table export
                t = pkg.tables[0]
                writer.writerow(t.headers)
                for row in t.rows:
                    writer.writerow(row)
            else:
                # Fallback to verified claims export
                writer.writerow(["Claim ID", "Statement", "Classification", "Claim Type", "Confidence", "Primary Source"])
                for clm in pkg.claims.values():
                    writer.writerow([
                        clm.claim_id,
                        clm.statement,
                        clm.classification.value,
                        clm.claim_type,
                        f"{clm.confidence:.2f}",
                        clm.primary_source_id or "",
                    ])

        return str(out_file)

