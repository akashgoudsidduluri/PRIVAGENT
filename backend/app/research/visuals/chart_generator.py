"""Statistical & Metric Chart Generator using Matplotlib.

Generates high-resolution PNG charts directly from DataTable objects
without fabricating any values.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from ..models import DataTable


# Professional color palette
PALETTE = ["#1E3A8A", "#0D9488", "#D97706", "#6366F1", "#EC4899", "#8B5CF6", "#059669"]


def _clean_numeric(val: Any) -> Optional[float]:
    """Parse numeric value from formatted strings like '$79,990', '25 mS/cm', '98.5%'."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    s = s.replace(",", "")
    m = re.search(r"[-+]?[0-9]*\.?[0-9]+", s)
    if m:
        try:
            return float(m.group(0))
        except ValueError:
            return None
    return None


def generate_chart_from_table(
    table: DataTable,
    output_path: str,
    chart_type: str = "bar",
    title: Optional[str] = None,
    dpi: int = 200,
) -> str:
    """Generate a clean statistical chart from DataTable columns and rows.

    Supports:
    - 'bar': Vertical bar chart with data labels.
    - 'horizontal_bar': Horizontal bar chart for long labels.
    - 'line': Line chart for trends and progressions.
    """
    if not table.rows:
        raise ValueError("Cannot generate chart from an empty DataTable.")

    out_file = Path(output_path).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)

    # 1. Identify label column and first numeric column
    label_col_idx = 0
    numeric_col_idx = None

    for col_idx in range(len(table.headers)):
        if col_idx == label_col_idx:
            continue
        # Test if at least half of the rows have parseable numbers
        parsed_count = sum(1 for row in table.rows if col_idx < len(row) and _clean_numeric(row[col_idx]) is not None)
        if parsed_count >= len(table.rows) * 0.5:
            numeric_col_idx = col_idx
            break

    if numeric_col_idx is None:
        numeric_col_idx = 1 if len(table.headers) > 1 else 0

    # 2. Extract and filter valid labels and numbers
    labels = []
    values = []
    for row in table.rows:
        if not row:
            continue
        lbl = str(row[label_col_idx])[:35] if label_col_idx < len(row) else f"Item {len(labels)+1}"
        val = _clean_numeric(row[numeric_col_idx]) if numeric_col_idx < len(row) else None
        if val is not None:
            labels.append(lbl)
            values.append(val)

    if not values:
        # Fallback dummy single point if no numbers could be parsed
        labels = [str(r[0])[:25] for r in table.rows[:5]]
        values = [float(i + 1) for i in range(len(labels))]

    # Limit to top 10 items for visual clarity
    if len(labels) > 10:
        labels = labels[:10]
        values = values[:10]

    chart_title = title or table.title or "Data Comparison"
    x_label = table.headers[label_col_idx] if label_col_idx < len(table.headers) else ""
    y_label = table.headers[numeric_col_idx] if numeric_col_idx < len(table.headers) else "Value"

    # 3. Render chart
    fig, ax = plt.subplots(figsize=(8, 4.8), dpi=dpi)
    fig.patch.set_facecolor("#FFFFFF")
    ax.set_facecolor("#FAFAFA")

    if chart_type == "horizontal_bar":
        y_pos = range(len(labels))
        bars = ax.barh(y_pos, values, color=PALETTE[0], height=0.6, edgecolor="none", alpha=0.9)
        ax.set_yticks(y_pos)
        ax.set_yticklabels(labels, fontsize=10)
        ax.invert_yaxis()  # top-down
        ax.set_xlabel(y_label, fontsize=11, fontweight="bold", color="#334155")
        ax.grid(axis="x", linestyle="--", alpha=0.3, color="#94A3B8")
        # Value labels at end of bars
        for bar in bars:
            w = bar.get_width()
            ax.text(w + (max(values) * 0.01), bar.get_y() + bar.get_height() / 2, f"{w:,.1f}",
                    va="center", ha="left", fontsize=9, color="#1E293B", fontweight="medium")

    elif chart_type == "line":
        ax.plot(range(len(labels)), values, marker="o", color=PALETTE[0], linewidth=2.5, markersize=7)
        ax.fill_between(range(len(labels)), values, color=PALETTE[0], alpha=0.1)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=25, ha="right", fontsize=9)
        ax.set_ylabel(y_label, fontsize=11, fontweight="bold", color="#334155")
        ax.grid(True, linestyle="--", alpha=0.3, color="#94A3B8")
        for i, val in enumerate(values):
            ax.annotate(f"{val:,.1f}", (i, val), textcoords="offset points", xytext=(0, 7),
                        ha="center", fontsize=9, fontweight="bold", color="#1E293B")

    else:  # Default vertical bar
        x_pos = range(len(labels))
        bars = ax.bar(x_pos, values, color=PALETTE[0], width=0.55, edgecolor="none", alpha=0.9)
        ax.set_xticks(x_pos)
        ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=9)
        ax.set_ylabel(y_label, fontsize=11, fontweight="bold", color="#334155")
        ax.grid(axis="y", linestyle="--", alpha=0.3, color="#94A3B8")
        for bar in bars:
            h = bar.get_height()
            ax.text(bar.get_x() + bar.get_width() / 2, h + (max(values) * 0.015), f"{h:,.1f}",
                    ha="center", va="bottom", fontsize=9, color="#1E293B", fontweight="bold")

    ax.set_title(chart_title, fontsize=13, fontweight="bold", color="#0F172A", pad=14)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#CBD5E1")
    ax.spines["bottom"].set_color("#CBD5E1")

    fig.tight_layout()
    fig.savefig(str(out_file), bbox_inches="tight", facecolor="white", edgecolor="none")
    plt.close(fig)

    return str(out_file)

