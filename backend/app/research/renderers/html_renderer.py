"""Accessible, Responsive HTML Research Deliverable Renderer.

Generates standalone, self-contained HTML5 documents with semantic landmarks,
high contrast accessibility, print stylesheets, responsive tables, and XSS sanitization.
"""
from __future__ import annotations

import datetime
import html
from pathlib import Path
from typing import Any

from ..models import (
    EvidenceClassification,
    ResearchKnowledgePackage,
)
from .base import BaseRenderer


CSS_STYLES = """
:root {
  --primary: #1e3a8a;
  --primary-light: #eff6ff;
  --secondary: #0d9488;
  --text-main: #0f172a;
  --text-muted: #475569;
  --bg-page: #f8fafc;
  --bg-card: #ffffff;
  --border: #e2e8f0;
  --border-dark: #cbd5e1;
  --amber-bg: #fffbeb;
  --amber-border: #f59e0b;
  --amber-text: #92400e;
}

* {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
  color: var(--text-main);
  background-color: var(--bg-page);
  line-height: 1.6;
  padding: 2rem 1rem;
}

.container {
  max-width: 900px;
  margin: 0 auto;
  background: var(--bg-card);
  padding: 2.5rem 3rem;
  border-radius: 8px;
  box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -1px rgba(0, 0, 0, 0.03);
  border: 1px solid var(--border);
}

header.report-header {
  border-bottom: 2px solid var(--primary);
  padding-bottom: 1.5rem;
  margin-bottom: 2rem;
}

.category-tag {
  font-size: 0.8rem;
  font-weight: 700;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--secondary);
  margin-bottom: 0.5rem;
}

h1.report-title {
  color: var(--primary);
  font-size: 2.2rem;
  line-height: 1.25;
  margin-bottom: 0.75rem;
}

p.report-subtitle {
  color: var(--text-muted);
  font-size: 1.05rem;
  margin-bottom: 1.25rem;
}

.meta-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 0.75rem;
  background: var(--bg-page);
  padding: 1rem;
  border-radius: 6px;
  border: 1px solid var(--border);
  font-size: 0.9rem;
}

.meta-item strong {
  color: var(--primary);
}

section {
  margin-bottom: 2.5rem;
}

h2.section-title {
  color: var(--primary);
  font-size: 1.4rem;
  border-bottom: 1px solid var(--border);
  padding-bottom: 0.4rem;
  margin-bottom: 1rem;
}

h3.subtopic-title {
  color: var(--text-muted);
  font-size: 1.15rem;
  margin: 1.25rem 0 0.5rem 0;
}

ul.claim-list {
  list-style: none;
  padding-left: 0;
}

ul.claim-list li {
  position: relative;
  padding-left: 1.5rem;
  margin-bottom: 0.6rem;
  font-size: 0.95rem;
}

ul.claim-list li::before {
  content: "•";
  position: absolute;
  left: 0.5rem;
  color: var(--primary);
  font-weight: bold;
}

.badge {
  display: inline-block;
  font-size: 0.75rem;
  font-weight: 600;
  padding: 0.15rem 0.45rem;
  border-radius: 4px;
  background: var(--primary-light);
  color: var(--primary);
  margin-right: 0.4rem;
  text-transform: uppercase;
}

.citation-ref {
  font-weight: bold;
  color: var(--primary);
  text-decoration: none;
}

.citation-ref:hover {
  text-decoration: underline;
}

.table-wrapper {
  overflow-x: auto;
  margin: 1.25rem 0;
  border: 1px solid var(--border);
  border-radius: 6px;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.9rem;
  text-align: left;
}

th {
  background: var(--primary);
  color: #ffffff;
  padding: 0.65rem 0.85rem;
  font-weight: 600;
}

td {
  padding: 0.6rem 0.85rem;
  border-top: 1px solid var(--border);
}

tbody tr:nth-child(even) {
  background: var(--bg-page);
}

tbody tr:hover {
  background: #f1f5f9;
}

figure.report-figure {
  margin: 1.5rem 0;
  text-align: center;
}

figure.report-figure img {
  max-width: 100%;
  height: auto;
  border-radius: 6px;
  border: 1px solid var(--border);
}

figcaption {
  font-size: 0.85rem;
  color: var(--text-muted);
  font-style: italic;
  margin-top: 0.4rem;
}

pre.formula-code {
  background: var(--bg-page);
  padding: 0.85rem;
  border-radius: 6px;
  border: 1px solid var(--border);
  font-family: Consolas, Monaco, "Courier New", monospace;
  font-size: 0.9rem;
  overflow-x: auto;
  margin: 0.6rem 0;
}

.callout-warning {
  background: var(--amber-bg);
  border-left: 4px solid var(--amber-border);
  padding: 1rem 1.25rem;
  border-radius: 0 6px 6px 0;
  margin: 1.5rem 0;
  color: var(--amber-text);
}

.callout-warning h4 {
  font-size: 1rem;
  font-weight: 700;
  margin-bottom: 0.4rem;
}

footer.report-footer {
  border-top: 1px solid var(--border);
  padding-top: 1rem;
  margin-top: 2.5rem;
  font-size: 0.8rem;
  color: var(--text-muted);
  text-align: center;
}

@media print {
  body {
    background: #ffffff;
    padding: 0;
  }
  .container {
    max-width: 100%;
    box-shadow: none;
    border: none;
    padding: 0;
  }
}
"""


def _safe_href(raw_url: str) -> str:
    """Ensure URL is safe for HTML href/src attributes (rejects javascript:, vbscript:, data:html)."""
    if not raw_url:
        return "#"
    clean = raw_url.strip()
    lower = clean.lower()
    if lower.startswith("javascript:") or lower.startswith("vbscript:") or lower.startswith("data:text/html"):
        return "#"
    if (
        lower.startswith("http://")
        or lower.startswith("https://")
        or lower.startswith("/")
        or lower.startswith("./")
        or lower.startswith("#")
    ):
        return html.escape(clean)
    return "#"


class HtmlRenderer(BaseRenderer):
    """Generates accessible, responsive standalone HTML5 research reports."""

    def render(self, pkg: ResearchKnowledgePackage, output_path: str, **kwargs: Any) -> str:
        out_file = self.prepare_output_path(output_path)
        cit_map = self.build_citation_map(pkg)
        ordered_sources = self.get_ordered_sources(pkg, cit_map)

        now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%B %d, %Y")

        title_esc = html.escape(pkg.topic.title)
        obj_esc = html.escape(pkg.topic.objective)
        scope_esc = html.escape(pkg.topic.scope or "Comprehensive multi-source research investigation")

        doc_parts: list[str] = [
            "<!DOCTYPE html>",
            '<html lang="en">',
            "<head>",
            '  <meta charset="utf-8">',
            '  <meta name="viewport" content="width=device-width, initial-scale=1.0">',
            f"  <title>{title_esc} — PrivAgent Research</title>",
            f"  <style>{CSS_STYLES}</style>",
            "</head>",
            "<body>",
            '  <div class="container">',
            '    <header class="report-header" role="banner">',
            '      <div class="category-tag">PrivAgent Autonomous Technical Report</div>',
            f'      <h1 class="report-title">{title_esc}</h1>',
            f'      <p class="report-subtitle">{obj_esc}</p>',
            '      <div class="meta-grid">',
            f'        <div class="meta-item"><strong>Date:</strong> {now_str}</div>',
            f'        <div class="meta-item"><strong>Sources Examined:</strong> {len(pkg.sources)}</div>',
            f'        <div class="meta-item"><strong>Verified Claims:</strong> {len(pkg.claims)}</div>',
            f'        <div class="meta-item"><strong>Deliverable Format:</strong> Interactive HTML</div>',
            "      </div>",
            "    </header>",
            '    <main role="main">',
        ]

        # 1. Executive Summary
        doc_parts.append('      <section id="executive-summary" aria-labelledby="exec-title">')
        doc_parts.append('        <h2 id="exec-title" class="section-title">1. Executive Summary</h2>')
        doc_parts.append(f"        <p><strong>Scope:</strong> {scope_esc}</p>")

        high_conf = [c for c in pkg.claims.values() if c.confidence >= 0.85]
        if high_conf:
            doc_parts.append('        <p style="margin-top: 0.75rem;"><strong>Key Verified Findings:</strong></p>')
            doc_parts.append('        <ul class="claim-list">')
            for c in high_conf[:5]:
                src_ids = ([c.primary_source_id] if c.primary_source_id else []) + c.supporting_source_ids
                cit = self.format_citation_brackets(src_ids, cit_map)
                cit_html = f' <a href="#ref-{cit_map.get(c.primary_source_id, 1)}" class="citation-ref">{html.escape(cit)}</a>' if cit else ""
                stmt_esc = html.escape(c.statement)
                doc_parts.append(f'          <li><span class="badge">{html.escape(c.claim_type)}</span>{stmt_esc}{cit_html}</li>')
            doc_parts.append("        </ul>")
        doc_parts.append("      </section>")

        # 2. Detailed Technical Findings
        doc_parts.append('      <section id="detailed-findings" aria-labelledby="findings-title">')
        doc_parts.append('        <h2 id="findings-title" class="section-title">2. Detailed Technical Findings & Evidence</h2>')

        if pkg.topic.subtopics:
            for idx, sub in enumerate(pkg.topic.subtopics, start=1):
                sub_title_esc = html.escape(sub.title)
                doc_parts.append(f'        <h3 class="subtopic-title">2.{idx} {sub_title_esc}</h3>')
                if sub.findings_summary:
                    doc_parts.append(f"        <p>{html.escape(sub.findings_summary)}</p>")

                sub_claims = [c for c in pkg.claims.values() if c.subtopic_id == sub.subtopic_id or c.claim_id in sub.claim_ids]
                if sub_claims:
                    doc_parts.append('        <ul class="claim-list">')
                    for c in sub_claims:
                        src_ids = ([c.primary_source_id] if c.primary_source_id else []) + c.supporting_source_ids
                        cit = self.format_citation_brackets(src_ids, cit_map)
                        cit_html = f' <a href="#ref-{cit_map.get(c.primary_source_id, 1)}" class="citation-ref">{html.escape(cit)}</a>' if cit else ""
                        stmt_esc = html.escape(c.statement)
                        doc_parts.append(f'          <li><span class="badge">{html.escape(c.claim_type)}</span>{stmt_esc}{cit_html}</li>')
                    doc_parts.append("        </ul>")
        else:
            doc_parts.append('        <ul class="claim-list">')
            for c in pkg.claims.values():
                src_ids = ([c.primary_source_id] if c.primary_source_id else []) + c.supporting_source_ids
                cit = self.format_citation_brackets(src_ids, cit_map)
                cit_html = f' <a href="#ref-{cit_map.get(c.primary_source_id, 1)}" class="citation-ref">{html.escape(cit)}</a>' if cit else ""
                stmt_esc = html.escape(c.statement)
                doc_parts.append(f'          <li><span class="badge">{html.escape(c.claim_type)}</span>{stmt_esc}{cit_html}</li>')
            doc_parts.append("        </ul>")
        doc_parts.append("      </section>")

        # 3. Formulas & Algorithmic Models
        if pkg.formulas:
            doc_parts.append('      <section id="formulas" aria-labelledby="formulas-title">')
            doc_parts.append('        <h2 id="formulas-title" class="section-title">3. Algorithmic & Mathematical Formulations</h2>')
            for f in pkg.formulas:
                doc_parts.append(f'        <h3 class="subtopic-title">{html.escape(f.name)}</h3>')
                if f.explanation:
                    doc_parts.append(f"        <p>{html.escape(f.explanation)}</p>")
                doc_parts.append(f'        <pre class="formula-code"><code>{html.escape(f.latex)}</code></pre>')
                if f.worked_example:
                    doc_parts.append(f"        <p><strong>Worked Example:</strong> {html.escape(f.worked_example)}</p>")
            doc_parts.append("      </section>")

        # 4. Comparative Data Tables
        if pkg.tables:
            doc_parts.append('      <section id="data-tables" aria-labelledby="tables-title">')
            doc_parts.append('        <h2 id="tables-title" class="section-title">4. Structured Data & Comparative Tables</h2>')
            for t in pkg.tables:
                doc_parts.append(f'        <h3 class="subtopic-title">{html.escape(t.title)}</h3>')
                if t.caption:
                    doc_parts.append(f'        <p style="font-size: 0.85rem; color: var(--text-muted); font-style: italic;">{html.escape(t.caption)}</p>')
                doc_parts.append('        <div class="table-wrapper">')
                doc_parts.append("          <table>")
                doc_parts.append("            <thead><tr>")
                for h in t.headers:
                    doc_parts.append(f'              <th scope="col">{html.escape(str(h))}</th>')
                doc_parts.append("            </tr></thead>")
                doc_parts.append("            <tbody>")
                for r in t.rows:
                    doc_parts.append("              <tr>")
                    for cell in r:
                        doc_parts.append(f"                <td>{html.escape(str(cell))}</td>")
                    doc_parts.append("              </tr>")
                doc_parts.append("            </tbody>")
                doc_parts.append("          </table>")
                doc_parts.append("        </div>")
            doc_parts.append("      </section>")

        # 5. Visual Assets & Figures
        visuals_to_embed = [
            v for v in pkg.visuals
            if v.url_or_path and (v.url_or_path.startswith(("http://", "https://")) or Path(v.url_or_path).exists()) and v.asset_type != "formula"
        ]
        if visuals_to_embed:
            doc_parts.append('      <section id="visuals" aria-labelledby="visuals-title">')
            doc_parts.append('        <h2 id="visuals-title" class="section-title">5. Visual Intelligence & Architecture Figures</h2>')
            for v in visuals_to_embed:
                v_title_esc = html.escape(v.title)
                doc_parts.append('        <figure class="report-figure">')
                doc_parts.append(f'          <img src="{_safe_href(v.url_or_path)}" alt="{v_title_esc}">')
                if v.caption:
                    doc_parts.append(f"          <figcaption>Figure: {html.escape(v.caption)}</figcaption>")
                doc_parts.append("        </figure>")
            doc_parts.append("      </section>")

        # 6. Contradictions Callout Box
        conflicting = [c for c in pkg.claims.values() if c.classification == EvidenceClassification.CONFLICTING]
        if conflicting or pkg.contradictions:
            doc_parts.append('      <section id="discrepancies" aria-labelledby="discrepancies-title">')
            doc_parts.append('        <h2 id="discrepancies-title" class="section-title">6. Documented Discrepancies & Contradictions</h2>')
            doc_parts.append('        <div class="callout-warning" role="alert">')
            doc_parts.append("          <h4>⚠️ Conflicting Findings Across Independent Sources</h4>")
            doc_parts.append("          <p>PrivAgent identified contradictory data across researched websites. Disagreements are preserved below:</p>")
            doc_parts.append('          <ul style="padding-left: 1.25rem; margin-top: 0.5rem;">')
            for c in conflicting:
                doc_parts.append(f"            <li><strong>Assertion:</strong> {html.escape(c.statement)}</li>")
                if c.contradiction_notes:
                    doc_parts.append(f"            <li style=\"list-style: none; font-style: italic; color: var(--text-muted);\">Discrepancy Details: {html.escape(c.contradiction_notes)}</li>")
            for cd in pkg.contradictions:
                doc_parts.append(f"            <li><em>Conflict Record:</em> {html.escape(cd)}</li>")
            doc_parts.append("          </ul>")
            doc_parts.append("        </div>")
            doc_parts.append("      </section>")

        # 7. References Cited Table
        doc_parts.append('      <section id="references" aria-labelledby="references-title">')
        doc_parts.append('        <h2 id="references-title" class="section-title">7. References Cited & Source Provenance</h2>')
        if ordered_sources:
            doc_parts.append('        <div class="table-wrapper">')
            doc_parts.append("          <table>")
            doc_parts.append("            <thead><tr>")
            doc_parts.append('              <th scope="col">#</th>')
            doc_parts.append('              <th scope="col">Document Title</th>')
            doc_parts.append('              <th scope="col">Domain</th>')
            doc_parts.append('              <th scope="col">URL</th>')
            doc_parts.append('              <th scope="col">Reliability</th>')
            doc_parts.append("            </tr></thead>")
            doc_parts.append("            <tbody>")
            for num, src in ordered_sources:
                doc_parts.append(f'              <tr id="ref-{num}">')
                doc_parts.append(f"                <td>[{num}]</td>")
                doc_parts.append(f"                <td>{html.escape(src.title or src.domain)}</td>")
                doc_parts.append(f"                <td><code>{html.escape(src.domain)}</code></td>")
                doc_parts.append(f'                <td><a href="{_safe_href(src.url)}" target="_blank" rel="noopener noreferrer">{html.escape(src.url)}</a></td>')
                doc_parts.append(f"                <td>{src.reliability_score * 100:.0f}%</td>")
                doc_parts.append("              </tr>")
            doc_parts.append("            </tbody>")
            doc_parts.append("          </table>")
            doc_parts.append("        </div>")
        else:
            doc_parts.append('        <p><em>No external sources were consulted or cited for this deliverable. Findings are compiled from conversational context without verified external citations.</em></p>')
        doc_parts.append("      </section>")

        doc_parts.append("    </main>")
        doc_parts.append('    <footer class="report-footer" role="contentinfo">')
        doc_parts.append("      <p>PrivAgent Autonomous Research & Deliverable Generation Engine &copy; 2026. Confidential & Verified Intelligence.</p>")
        doc_parts.append("    </footer>")
        doc_parts.append("  </div>")
        doc_parts.append("</body>")
        doc_parts.append("</html>")

        out_file.write_text("\n".join(doc_parts), encoding="utf-8")
        return str(out_file)

