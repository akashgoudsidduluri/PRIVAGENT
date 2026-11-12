"""GitHub-Flavored Markdown Artifact Renderer.

Renders canonical ResearchKnowledgePackage into structured GFM documents
with YAML frontmatter, tables, Mermaid diagrams, LaTeX math, and citations.
"""
from __future__ import annotations

import datetime
from typing import Any

from ..models import (
    EvidenceClassification,
    ResearchKnowledgePackage,
)
from .base import BaseRenderer


class MarkdownRenderer(BaseRenderer):
    """Generates structured GitHub-Flavored Markdown research reports."""

    def render(self, pkg: ResearchKnowledgePackage, output_path: str, **kwargs: Any) -> str:
        out_file = self.prepare_output_path(output_path)
        cit_map = self.build_citation_map(pkg)
        ordered_sources = self.get_ordered_sources(pkg, cit_map)

        now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        lines: list[str] = [
            "---",
            f'title: "{pkg.topic.title}"',
            f'objective: "{pkg.topic.objective}"',
            f'generated_at: "{now_str}"',
            f"sources_count: {len(pkg.sources)}",
            f"claims_count: {len(pkg.claims)}",
            f"format: markdown",
            "generator: PrivAgent Autonomous Research Engine",
            "---",
            "",
            f"# {pkg.topic.title}",
            "",
            "> **PrivAgent Research Deliverable**  ",
            f"> **Objective:** {pkg.topic.objective}  ",
            f"> **Generated:** {now_str} | **Sources Examined:** {len(pkg.sources)} | **Verified Claims:** {len(pkg.claims)}",
            "",
            "## 1. Executive Summary",
            "",
        ]

        # Executive summary text
        if pkg.topic.scope:
            lines.append(f"**Research Scope:** {pkg.topic.scope}\n")

        high_conf_claims = [c for c in pkg.claims.values() if c.confidence >= 0.85]
        if high_conf_claims:
            lines.append("### Key Synthesized Findings")
            for c in high_conf_claims[:5]:
                src_ids = ([c.primary_source_id] if c.primary_source_id else []) + c.supporting_source_ids
                cit = self.format_citation_brackets(src_ids, cit_map)
                badge = f"`[{c.classification.value}]`"
                type_badge = f"`{c.claim_type}`"
                lines.append(f"- {badge} {type_badge} {c.statement} {cit}".strip())
            lines.append("")

        # Subtopics Section
        lines.append("## 2. Detailed Findings & Analysis")
        lines.append("")

        if pkg.topic.subtopics:
            for idx, sub in enumerate(pkg.topic.subtopics, start=1):
                lines.append(f"### 2.{idx} {sub.title}")
                if sub.description:
                    lines.append(f"*{sub.description}*\n")
                if sub.findings_summary:
                    lines.append(f"{sub.findings_summary}\n")

                # Claims addressing this subtopic
                sub_claims = [c for c in pkg.claims.values() if c.subtopic_id == sub.subtopic_id or c.claim_id in sub.claim_ids]
                if sub_claims:
                    lines.append("#### Verified Claims")
                    for c in sub_claims:
                        src_ids = ([c.primary_source_id] if c.primary_source_id else []) + c.supporting_source_ids
                        cit = self.format_citation_brackets(src_ids, cit_map)
                        lines.append(f"- **{c.claim_type.title()}:** {c.statement} {cit}")
                    lines.append("")
        else:
            # Group claims by claim_type if no subtopics
            grouped_claims: dict[str, list] = {}
            for c in pkg.claims.values():
                grouped_claims.setdefault(c.claim_type, []).append(c)

            for c_type, claims in grouped_claims.items():
                lines.append(f"### {c_type.title()} Findings")
                for c in claims:
                    src_ids = ([c.primary_source_id] if c.primary_source_id else []) + c.supporting_source_ids
                    cit = self.format_citation_brackets(src_ids, cit_map)
                    lines.append(f"- {c.statement} {cit}")
                lines.append("")

        # Mathematical and Algorithmic Formulas
        if pkg.formulas:
            lines.append("## 3. Formulas & Algorithmic Formulations")
            lines.append("")
            for f in pkg.formulas:
                lines.append(f"### {f.name}")
                if f.explanation:
                    lines.append(f"{f.explanation}\n")
                lines.append(f"$$\n{f.latex}\n$$\n")
                if f.variables:
                    lines.append("**Variables:**")
                    for var, desc in f.variables.items():
                        lines.append(f"- `${var}$`: {desc}")
                    lines.append("")
                if f.worked_example:
                    lines.append(f"**Worked Example:**\n{f.worked_example}\n")

        # Tabular Data Section
        if pkg.tables:
            lines.append("## 4. Structured Data & Comparative Tables")
            lines.append("")
            for t in pkg.tables:
                lines.append(f"### {t.title}")
                if t.caption:
                    lines.append(f"*{t.caption}*\n")
                # GFM Table
                lines.append("| " + " | ".join(str(h) for h in t.headers) + " |")
                lines.append("| " + " | ".join(["---"] * len(t.headers)) + " |")
                for r in t.rows:
                    lines.append("| " + " | ".join(str(cell) for cell in r) + " |")
                lines.append("")

        # Visual Assets & Diagrams Section
        if pkg.visuals:
            lines.append("## 5. Architectural Diagrams & Visual Assets")
            lines.append("")
            for v in pkg.visuals:
                lines.append(f"### {v.title}")
                if v.caption:
                    lines.append(f"*{v.caption}*\n")

                # If visual has mermaid syntax in metadata, render as Mermaid code block
                mermaid_code = (v.metadata or {}).get("mermaid")
                if mermaid_code:
                    lines.append(f"```mermaid\n{mermaid_code}\n```\n")
                elif v.url_or_path:
                    lines.append(f"![{v.title}]({v.url_or_path})\n")

                if v.attribution:
                    lines.append(f"*Provenance: {v.attribution}*\n")

        # Contradictions and Discrepancies Alert
        conflicting_claims = [c for c in pkg.claims.values() if c.classification == EvidenceClassification.CONFLICTING]
        if conflicting_claims or pkg.contradictions:
            lines.append("## 6. Documented Discrepancies & Conflicting Data")
            lines.append("")
            lines.append("> [!WARNING]")
            lines.append("> **Independent Source Disagreement Detected**")
            lines.append("> PrivAgent detected conflicting metrics or contradictory assertions across researched websites.")
            lines.append("> Disagreements are preserved below rather than forced into artificial consensus:\n>")

            for c in conflicting_claims:
                lines.append(f"> - **Assertion:** {c.statement}")
                if c.contradiction_notes:
                    lines.append(f">   - *Discrepancy Details:* {c.contradiction_notes}")

            for dis in pkg.contradictions:
                lines.append(f"> - *Conflict Record:* {dis}")
            lines.append("")

        # References & Provenance Section
        lines.append("## 7. References Cited & Source Provenance")
        lines.append("")
        if ordered_sources:
            lines.append("| # | Source Title | Domain | URL | Reliability | Type |")
            lines.append("|---|--------------|--------|-----|-------------|------|")
            for num, src in ordered_sources:
                title_clean = (src.title or src.domain).replace("|", "-")
                c_type = (src.metadata or {}).get("content_type", "general")
                primary_tag = " ⭐ Primary" if src.is_primary else ""
                lines.append(
                    f"| [{num}] | {title_clean} | `{src.domain}` | [{src.url}]({src.url}) | {src.reliability_score * 100:.0f}% | {c_type}{primary_tag} |"
                )
            lines.append("")
        else:
            lines.append("*No external sources were consulted or cited for this deliverable. Findings are compiled from conversational context without verified external citations.*")

        content = "\n".join(lines)
        out_file.write_text(content, encoding="utf-8")
        return str(out_file)

