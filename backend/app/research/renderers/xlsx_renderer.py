"""Professional Multi-Sheet Excel (.xlsx) Research Deliverable Renderer using openpyxl.

Generates structured workbooks with styled executive summaries, auto-sized columns,
freeze panes, formula calculations (MIN, MAX, AVERAGE), verified claims, and
hyperlinked source provenance.
"""
from __future__ import annotations

import datetime
import re
from typing import Any, Optional

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from ..models import (
    ResearchKnowledgePackage,
)
from .base import BaseRenderer


# Styling Themes
NAVY_FILL = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
LIGHT_NAVY_FILL = PatternFill(start_color="EEF2FF", end_color="EEF2FF", fill_type="solid")
ALT_ROW_FILL = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
WHITE_FILL = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")
SUMMARY_FILL = PatternFill(start_color="E2E8F0", end_color="E2E8F0", fill_type="solid")

HEADER_FONT = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
TITLE_FONT = Font(name="Segoe UI", size=15, bold=True, color="1E3A8A")
BOLD_FONT = Font(name="Segoe UI", size=10, bold=True, color="1E293B")
REGULAR_FONT = Font(name="Segoe UI", size=10, color="1E293B")
LINK_FONT = Font(name="Segoe UI", size=10, color="0284C7", underline="single")

THIN_SIDE = Side(border_style="thin", color="CBD5E1")
DOUBLE_BOTTOM = Side(border_style="double", color="334155")
CELL_BORDER = Border(left=THIN_SIDE, right=THIN_SIDE, top=THIN_SIDE, bottom=THIN_SIDE)
SUMMARY_BORDER = Border(left=THIN_SIDE, right=THIN_SIDE, top=THIN_SIDE, bottom=DOUBLE_BOTTOM)


def _autofit_columns(ws: Any, max_len_cap: int = 60) -> None:
    """Adjust worksheet column widths to fit content with padding."""
    for col in ws.columns:
        col_letter = get_column_letter(col[0].column)
        max_len = 0
        for cell in col:
            val_str = str(cell.value or "")
            if cell.coordinate in ws.merged_cells:
                continue
            max_len = max(max_len, len(val_str))
        ws.column_dimensions[col_letter].width = min(max(max_len + 4, 12), max_len_cap)


def _parse_numeric_for_excel(val: Any) -> tuple[Any, bool]:
    """Check if cell string is a pure currency or number and return clean number for formulas."""
    if val is None:
        return "", False
    if isinstance(val, (int, float)):
        return val, True
    s = str(val).strip()
    if not s:
        return "", False

    # If the string contains words or parentheses (e.g. "(128 GB)", "per month"), preserve it as text!
    if re.search(r"[a-zA-Z\(\)]", s):
        # Allow pure standard currency codes like "USD 1200" or "INR 50000"
        m_code = re.match(r"^(?:USD|INR|EUR|GBP|AUD|CAD)\s*([0-9,]+(?:\.[0-9]+)?)$", s, re.IGNORECASE)
        if m_code:
            try:
                clean_num = m_code.group(1).replace(",", "")
                return float(clean_num) if "." in clean_num else int(clean_num), True
            except ValueError:
                pass
        return val, False

    # Standalone Currency symbol with Indian or Western grouping: e.g. "₹1,19,900", "$450.50", "₹129999"
    m_curr = re.match(r"^(?:₹|Rs\.?|\$|€|£)?\s*([0-9]{1,3}(?:,[0-9]{2,3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)\s*$", s)
    if m_curr:
        try:
            clean_num = m_curr.group(1).replace(",", "")
            if clean_num:
                return float(clean_num) if "." in clean_num else int(clean_num), True
        except ValueError:
            pass

    # Percentage: e.g. "94.6%", "18%"
    m_pct = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*%$", s)
    if m_pct:
        try:
            return float(m_pct.group(1)), True
        except ValueError:
            pass

    # Plain number
    m_num = re.match(r"^[-+]?[0-9]*\.?[0-9]+$", s.replace(",", ""))
    if m_num:
        try:
            clean_num = s.replace(",", "")
            return float(clean_num) if "." in clean_num else int(clean_num), True
        except ValueError:
            pass
    return val, False


class XlsxRenderer(BaseRenderer):
    """Generates professional multi-sheet Excel workbooks (.xlsx)."""

    def render(self, pkg: ResearchKnowledgePackage, output_path: str, **kwargs: Any) -> str:
        out_file = self.prepare_output_path(output_path)
        cit_map = self.build_citation_map(pkg)
        ordered_sources = self.get_ordered_sources(pkg, cit_map)

        wb = openpyxl.Workbook()

        # -------------------------------------------------------------------
        # Sheet 1: Executive Summary
        # -------------------------------------------------------------------
        ws_sum = wb.active
        ws_sum.title = "Executive Summary"
        ws_sum.views.sheetView[0].showGridLines = True

        # Title Block
        ws_sum.merge_cells("A1:D2")
        title_cell = ws_sum["A1"]
        title_cell.value = f"PrivAgent Autonomous Research: {pkg.topic.title}"
        title_cell.font = TITLE_FONT
        title_cell.fill = LIGHT_NAVY_FILL
        title_cell.alignment = Alignment(horizontal="center", vertical="center")

        ws_sum["A4"] = "Research Objective"
        ws_sum["B4"] = pkg.topic.objective
        ws_sum["A5"] = "Research Scope"
        ws_sum["B5"] = pkg.topic.scope or "Comprehensive multi-source investigation"
        ws_sum["A6"] = "Generated Date"
        ws_sum["B6"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        ws_sum["A7"] = "Authoritative Sources Examined"
        ws_sum["B7"] = len(pkg.sources)
        ws_sum["A8"] = "Synthesized Claims Verified"
        ws_sum["B8"] = len(pkg.claims)
        ws_sum["A9"] = "Comparison Tables"
        ws_sum["B9"] = len(pkg.tables)

        for r in range(4, 10):
            ws_sum[f"A{r}"].font = BOLD_FONT
            ws_sum[f"A{r}"].fill = ALT_ROW_FILL
            ws_sum[f"A{r}"].border = CELL_BORDER
            ws_sum[f"B{r}"].font = REGULAR_FONT
            ws_sum[f"B{r}"].border = CELL_BORDER

        # High Confidence Findings
        ws_sum["A11"] = "Key Synthesized Findings"
        ws_sum["A11"].font = Font(name="Segoe UI", size=12, bold=True, color="1E3A8A")

        row_cursor = 12
        for c in list(pkg.claims.values())[:6]:
            ws_sum[f"A{row_cursor}"] = c.claim_type.upper()
            ws_sum[f"A{row_cursor}"].font = BOLD_FONT
            ws_sum[f"A{row_cursor}"].border = CELL_BORDER
            ws_sum[f"B{row_cursor}"] = c.statement
            ws_sum[f"B{row_cursor}"].font = REGULAR_FONT
            ws_sum[f"B{row_cursor}"].border = CELL_BORDER
            row_cursor += 1

        _autofit_columns(ws_sum)

        # -------------------------------------------------------------------
        # Sheet 2+: Data & Comparison Tables (Dynamic Multi-Sheet Support)
        # -------------------------------------------------------------------
        seen_sheet_titles = {"executive summary"}
        for t_idx, t in enumerate(pkg.tables, start=1):
            if t_idx == 1:
                clean_title = "Comparison & Metrics"
            else:
                raw_title = t.title or f"Table {t_idx} Metrics"
                clean_title = re.sub(r"[\\/*?:\[\]]", "_", raw_title).strip()[:30] or f"Metrics {t_idx}"
            if clean_title.lower() in seen_sheet_titles:
                clean_title = f"{clean_title[:24]} ({t_idx})"
            seen_sheet_titles.add(clean_title.lower())

            ws_data = wb.create_sheet(title=clean_title)
            ws_data.views.sheetView[0].showGridLines = True
            ws_data.freeze_panes = "A2"

            # Write headers
            for col_idx, h in enumerate(t.headers, start=1):
                c = ws_data.cell(row=1, column=col_idx, value=str(h))
                c.fill = NAVY_FILL
                c.font = HEADER_FONT
                c.alignment = Alignment(horizontal="center", vertical="center")
                c.border = CELL_BORDER

            # Write data rows
            num_cols = len(t.headers)
            numeric_col_counts: dict[int, int] = {c: 0 for c in range(1, num_cols + 1)}

            for row_idx, r in enumerate(t.rows, start=2):
                bg = ALT_ROW_FILL if row_idx % 2 == 1 else WHITE_FILL
                for col_idx, val in enumerate(r, start=1):
                    parsed_val, is_num = _parse_numeric_for_excel(val)
                    if is_num:
                        numeric_col_counts[col_idx] += 1
                    cell = ws_data.cell(row=row_idx, column=col_idx, value=parsed_val)
                    cell.font = REGULAR_FONT
                    cell.fill = bg
                    cell.border = CELL_BORDER
                    if is_num:
                        cell.alignment = Alignment(horizontal="right")

            # A column is only truly numeric for calculation if at least 60% of its rows are numeric
            threshold = max(2, int(len(t.rows) * 0.60))
            numeric_cols = {c for c, count in numeric_col_counts.items() if count >= threshold}

            # Add Calculation Summary Rows (Average, Min, Max) if numeric columns exist
            if numeric_cols and len(t.rows) >= 2:
                last_data_row = len(t.rows) + 1
                formulas = [
                    ("Average", "AVERAGE", last_data_row + 1),
                    ("Lowest / Minimum", "MIN", last_data_row + 2),
                    ("Highest / Maximum", "MAX", last_data_row + 3),
                ]
                for label, func, r_idx in formulas:
                    ws_data.cell(row=r_idx, column=1, value=label).font = BOLD_FONT
                    ws_data.cell(row=r_idx, column=1).fill = SUMMARY_FILL
                    ws_data.cell(row=r_idx, column=1).border = SUMMARY_BORDER
                    for c_idx in range(2, num_cols + 1):
                        cell = ws_data.cell(row=r_idx, column=c_idx)
                        cell.fill = SUMMARY_FILL
                        cell.border = SUMMARY_BORDER
                        if c_idx in numeric_cols:
                            col_let = get_column_letter(c_idx)
                            cell.value = f"={func}({col_let}2:{col_let}{last_data_row})"
                            cell.font = BOLD_FONT
                            cell.alignment = Alignment(horizontal="right")

            _autofit_columns(ws_data)

        # -------------------------------------------------------------------
        # Sheet 3: Verified Claims
        # -------------------------------------------------------------------
        ws_claims = wb.create_sheet(title="Verified Claims")
        ws_claims.views.sheetView[0].showGridLines = True
        ws_claims.freeze_panes = "A2"

        claim_headers = ["Claim ID", "Statement", "Classification", "Claim Type", "Confidence", "Primary Source", "Corroborating Sources"]
        for col_idx, h in enumerate(claim_headers, start=1):
            c = ws_claims.cell(row=1, column=col_idx, value=h)
            c.fill = NAVY_FILL
            c.font = HEADER_FONT
            c.alignment = Alignment(horizontal="center")
            c.border = CELL_BORDER

        for row_idx, clm in enumerate(pkg.claims.values(), start=2):
            bg = ALT_ROW_FILL if row_idx % 2 == 1 else WHITE_FILL
            vals = [
                clm.claim_id,
                clm.statement,
                clm.classification.value,
                clm.claim_type,
                f"{clm.confidence * 100:.1f}%",
                clm.primary_source_id or "N/A",
                ", ".join(clm.supporting_source_ids) if clm.supporting_source_ids else "None",
            ]
            for col_idx, val in enumerate(vals, start=1):
                cell = ws_claims.cell(row=row_idx, column=col_idx, value=val)
                cell.font = REGULAR_FONT
                cell.fill = bg
                cell.border = CELL_BORDER

        _autofit_columns(ws_claims)

        # -------------------------------------------------------------------
        # Sheet 4: Sources & Provenance
        # -------------------------------------------------------------------
        ws_src = wb.create_sheet(title="Sources & Provenance")
        ws_src.views.sheetView[0].showGridLines = True
        ws_src.freeze_panes = "A2"

        src_headers = ["Citation #", "Title / Entity", "Domain", "URL", "Reliability", "Content Type", "Is Primary"]
        for col_idx, h in enumerate(src_headers, start=1):
            c = ws_src.cell(row=1, column=col_idx, value=h)
            c.fill = NAVY_FILL
            c.font = HEADER_FONT
            c.alignment = Alignment(horizontal="center")
            c.border = CELL_BORDER

        for row_idx, (num, src) in enumerate(ordered_sources, start=2):
            bg = ALT_ROW_FILL if row_idx % 2 == 1 else WHITE_FILL
            c_type = (src.metadata or {}).get("content_type", "general")
            vals = [
                f"[{num}]",
                src.title or src.domain,
                src.domain,
                src.url,
                f"{src.reliability_score * 100:.0f}%",
                c_type,
                "Yes" if src.is_primary else "No",
            ]
            for col_idx, val in enumerate(vals, start=1):
                cell = ws_src.cell(row=row_idx, column=col_idx, value=val)
                cell.font = REGULAR_FONT
                cell.fill = bg
                cell.border = CELL_BORDER
                if col_idx == 4 and src.url:  # URL column with clickable hyperlink
                    cell.hyperlink = src.url
                    cell.font = LINK_FONT

        _autofit_columns(ws_src)

        wb.save(str(out_file))
        return str(out_file)

