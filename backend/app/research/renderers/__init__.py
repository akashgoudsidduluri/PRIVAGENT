"""Document Renderers Package: DOCX, PDF, PPTX, XLSX, CSV, Markdown, and HTML."""
from __future__ import annotations

from typing import Any

from ..models import ResearchKnowledgePackage
from .base import BaseRenderer
from .csv_renderer import CsvRenderer
from .docx_renderer import DocxRenderer
from .html_renderer import HtmlRenderer
from .markdown_renderer import MarkdownRenderer
from .pdf_renderer import PdfRenderer
from .pptx_renderer import PptxRenderer
from .xlsx_renderer import XlsxRenderer

RENDERER_REGISTRY: dict[str, type[BaseRenderer]] = {
    "docx": DocxRenderer,
    "doc": DocxRenderer,
    "word": DocxRenderer,
    "pdf": PdfRenderer,
    "pptx": PptxRenderer,
    "ppt": PptxRenderer,
    "presentation": PptxRenderer,
    "slides": PptxRenderer,
    "xlsx": XlsxRenderer,
    "xls": XlsxRenderer,
    "excel": XlsxRenderer,
    "spreadsheet": XlsxRenderer,
    "csv": CsvRenderer,
    "markdown": MarkdownRenderer,
    "md": MarkdownRenderer,
    "html": HtmlRenderer,
}


def render_artifact(
    pkg: ResearchKnowledgePackage,
    deliverable_format: str,
    output_path: str,
    **kwargs: Any,
) -> str:
    """Unified dispatcher: Render a canonical ResearchKnowledgePackage into requested format."""
    clean_fmt = (deliverable_format or "docx").lower().strip(" .")
    renderer_cls = RENDERER_REGISTRY.get(clean_fmt, DocxRenderer)
    renderer = renderer_cls()
    return renderer.render(pkg, output_path, **kwargs)


__all__ = [
    "BaseRenderer",
    "CsvRenderer",
    "DocxRenderer",
    "HtmlRenderer",
    "MarkdownRenderer",
    "PdfRenderer",
    "PptxRenderer",
    "RENDERER_REGISTRY",
    "XlsxRenderer",
    "render_artifact",
]

