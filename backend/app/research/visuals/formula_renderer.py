"""Mathematical & Scientific LaTeX Formula Renderer using Matplotlib MathText.

Renders LaTeX equations into high-resolution PNG image assets for embedding
into DOCX, PDF, and PPTX reports.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.mathtext as mathtext
import matplotlib.pyplot as plt


def clean_latex(latex_str: str) -> str:
    """Normalize LaTeX formula string for Matplotlib MathText engine."""
    if not latex_str:
        return ""
    s = latex_str.strip()
    # Remove enclosing display delimiters
    s = re.sub(r"^\$\$\s*", "", s)
    s = re.sub(r"\s*\$\$$", "", s)
    s = re.sub(r"^\\\[\s*", "", s)
    s = re.sub(r"\s*\\\]$", "", s)
    s = re.sub(r"^\$\s*", "", s)
    s = re.sub(r"\s*\$$", "", s)
    s = s.strip()

    # Normalize common LaTeX macros that MathText expects
    s = s.replace(r"\begin{equation}", "").replace(r"\end{equation}", "").strip()
    s = s.replace(r"\text{", r"\mathrm{")
    return s


def render_latex_formula(
    latex: str,
    output_path: str,
    dpi: int = 200,
    fontsize: int = 14,
) -> str:
    """Render a LaTeX formula string into a crisp PNG asset.

    Returns the absolute path to the generated image file.
    """
    clean = clean_latex(latex)
    if not clean:
        raise ValueError("Cannot render empty LaTeX formula string.")

    out_file = Path(output_path).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)

    math_expr = f"${clean}$"

    try:
        # Fast native MathText rendering
        mathtext.math_to_image(math_expr, str(out_file), dpi=dpi, format="png")
    except Exception:
        # Fallback to figure text rendering if specialized MathText parsing failed
        fig = plt.figure(figsize=(6, 1.2), dpi=dpi)
        fig.patch.set_facecolor("white")
        try:
            fig.text(0.5, 0.5, math_expr, ha="center", va="center", fontsize=fontsize)
        except Exception:
            # Final fallback: plain text without math parsing
            fig.text(0.5, 0.5, clean, ha="center", va="center", fontsize=fontsize, fontfamily="monospace")
        plt.axis("off")
        fig.savefig(str(out_file), bbox_inches="tight", pad_inches=0.15, facecolor="white", edgecolor="none")
        plt.close(fig)

    return str(out_file)

