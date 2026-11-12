"""Unit tests for Phase 4.5: Visual Intelligence Engine."""
from __future__ import annotations

import tempfile
from pathlib import Path
from PIL import Image  # type: ignore

from app.research.collector import extract_page_images
from app.research.models import DataTable
from app.research.visuals.chart_generator import generate_chart_from_table
from app.research.visuals.diagram_generator import generate_mermaid_diagram, render_diagram_to_png
from app.research.visuals.formula_renderer import clean_latex, render_latex_formula
from app.research.visuals.image_pipeline import (
    ensure_local_image_asset,
    validate_and_cache_image,
    validate_image_bytes,
)


def test_clean_latex():
    assert clean_latex("$$ E = mc^2 $$") == "E = mc^2"
    assert clean_latex(r"\[ \sigma = \frac{1}{1 + e^{-z}} \]") == r"\sigma = \frac{1}{1 + e^{-z}}"
    assert clean_latex(r"\text{Attention}") == r"\mathrm{Attention}"


def test_render_latex_formula():
    """Verify rendering mathematical equations to PNG assets."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = Path(tmp_dir) / "attention.png"
        latex = r"\mathrm{Attention}(Q, K, V) = \mathrm{softmax}\left(\frac{QK^T}{\sqrt{d_k}}\right)V"

        result = render_latex_formula(latex, str(out_path), dpi=200)
        assert Path(result).exists()
        assert Path(result).stat().st_size > 500

        # Check image dimensions with Pillow
        with Image.open(result) as img:
            assert img.format == "PNG"
            assert img.width > 50
            assert img.height > 20


def test_generate_chart_from_table():
    """Verify generating vertical bar, horizontal bar, and line charts from DataTables."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        table = DataTable(
            table_id="tbl_density",
            title="Battery Energy Density Progress",
            headers=["Year", "Energy Density (Wh/kg)"],
            rows=[
                ["2020", "260"],
                ["2022", "300"],
                ["2024", "400"],
                ["2026 (Est.)", "500"],
            ],
        )

        # 1. Vertical bar chart
        bar_path = Path(tmp_dir) / "density_bar.png"
        res_bar = generate_chart_from_table(table, str(bar_path), chart_type="bar")
        assert Path(res_bar).exists()
        assert Path(res_bar).stat().st_size > 1000

        # 2. Line chart
        line_path = Path(tmp_dir) / "density_line.png"
        res_line = generate_chart_from_table(table, str(line_path), chart_type="line")
        assert Path(res_line).exists()
        assert Path(res_line).stat().st_size > 1000

        # 3. Horizontal bar chart
        hbar_path = Path(tmp_dir) / "density_hbar.png"
        res_hbar = generate_chart_from_table(table, str(hbar_path), chart_type="horizontal_bar")
        assert Path(res_hbar).exists()
        assert Path(res_hbar).stat().st_size > 1000


def test_generate_mermaid_diagram_and_png():
    """Verify Mermaid syntax generation and block-diagram PNG rendering."""
    # 1. Mermaid syntax checks
    mm_transformer = generate_mermaid_diagram("Transformer Architecture")
    assert "flowchart TD" in mm_transformer
    assert "Multi-Head Self-Attention" in mm_transformer
    assert "Softmax Output" in mm_transformer

    mm_battery = generate_mermaid_diagram("Solid-State Battery")
    assert "flowchart LR" in mm_battery
    assert "Solid Lithium Metal Anode" in mm_battery

    mm_os = generate_mermaid_diagram("Operating System Architecture")
    assert "flowchart TD" in mm_os
    assert "Process Manager" in mm_os

    # 2. PNG block diagram rendering
    with tempfile.TemporaryDirectory() as tmp_dir:
        diag_path = Path(tmp_dir) / "transformer_block.png"
        res = render_diagram_to_png("Transformer Architecture", str(diag_path))
        assert Path(res).exists()
        assert Path(res).stat().st_size > 1000

        with Image.open(res) as img:
            assert img.format == "PNG"
            assert img.width > 200
            assert img.height > 100


def test_image_pipeline_validation():
    """Verify image magic bytes, decompression checks, and local caching."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Create a valid test PNG image using Pillow
        test_img_path = Path(tmp_dir) / "source.png"
        img = Image.new("RGB", (200, 100), color=(30, 58, 138))
        img.save(test_img_path, format="PNG")

        dest_cache = Path(tmp_dir) / "cached_dest.png"
        valid, err, meta = validate_and_cache_image(str(test_img_path), str(dest_cache))

        assert valid is True
        assert err is None
        assert meta is not None
        assert meta["format"] == "PNG"
        assert meta["width"] == 200
        assert meta["height"] == 100
        assert dest_cache.exists()

        # Reject corrupted bytes
        corrupt_bytes = b"NOT_AN_IMAGE_HEADER_DATA"
        valid_bad, err_bad, _ = validate_image_bytes(corrupt_bytes)
        assert valid_bad is False
        assert "Unrecognized or prohibited" in err_bad


def test_extract_page_images_filtering_and_provenance():
    """Verify authentic website image extraction, tracker/ad filtering, and provenance tracking."""
    html_sample = """
    <html>
      <head>
        <meta property="og:image" content="https://example.com/assets/og_hero.jpg">
      </head>
      <body>
        <!-- Should be filtered: Site logo -->
        <img src="/images/site-logo.png" alt="Company Logo" width="40" height="40">
        <!-- Should be filtered: Tracking pixel -->
        <img src="https://tracker.adnetwork.com/pixel.gif?utm_source=ad" width="1" height="1">
        <!-- Should be filtered: Data URL spacer -->
        <img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7">
        
        <!-- Should be extracted: Substantive architecture figure -->
        <img src="https://example.com/photos/transformer_arch.png" alt="Transformer Architecture Flowchart" width="800" height="600">
        
        <!-- Should be extracted: Figure with caption -->
        <figure>
          <img src="/photos/memory_hierarchy.png">
          <figcaption>Operating System Memory Hierarchy Levels</figcaption>
        </figure>

        <!-- Should be extracted: Markdown image -->
        ![Self-Attention Matrix Computation](https://example.com/math/attention_matrix.png)

        <!-- Duplicate image with tracking query parameter -->
        <img src="https://example.com/photos/transformer_arch.png?utm_source=newsletter" alt="Transformer Copy">
      </body>
    </html>
    """
    visuals = extract_page_images(
        url="https://example.com/research/transformers",
        text_or_html=html_sample,
        title="Transformer Deep Dive - TechReview",
        domain="example.com",
        source_id="src_001",
    )

    # 1. Assert exactly the 4 substantive unique images are extracted
    # (og_hero.jpg, transformer_arch.png, memory_hierarchy.png, attention_matrix.png)
    assert len(visuals) == 4

    # 2. Assert trackers, 1x1 pixels, data URLs, and logos are completely excluded
    urls_extracted = [v.url_or_path for v in visuals]
    assert not any("pixel" in u or "logo" in u or "data:" in u for u in urls_extracted)

    # 3. Assert deduplication worked (transformer_arch.png appears only once despite duplicate with tracking param)
    arch_images = [v for v in visuals if "transformer_arch" in v.url_or_path]
    assert len(arch_images) == 1

    # 4. Assert provenance integrity
    for vis in visuals:
        assert vis.is_generated is False, "Authentic website images must have is_generated=False"
        assert vis.source_id == "src_001"
        assert vis.source_url == "https://example.com/research/transformers"
        assert "example.com" in vis.attribution
        assert vis.url_or_path.startswith("https://example.com/")


def test_ensure_local_image_asset_local_and_remote(tmp_path, monkeypatch):
    """Verify ensure_local_image_asset validates local images and securely downloads remote images."""
    from app.research.models import VisualAsset

    # 1. Local image
    test_img = Image.new("RGB", (120, 80), color="blue")
    local_path = tmp_path / "valid_local.png"
    test_img.save(local_path, format="PNG")

    vis_local = VisualAsset(
        asset_id="vis_local_1",
        asset_type="photograph",
        title="Valid Local Image",
        url_or_path=str(local_path),
        is_generated=False,
    )
    resolved_local = ensure_local_image_asset(vis_local, cache_dir=tmp_path / "cache")
    assert resolved_local is not None
    assert Path(resolved_local).resolve() == local_path.resolve()

    # 2. Remote image (mocking fetch_and_validate_image_url)
    remote_cached_file = tmp_path / "cache" / "img_vis_remote_1_mocked.png"
    remote_cached_file.parent.mkdir(parents=True, exist_ok=True)
    test_img.save(remote_cached_file, format="PNG")

    def mock_fetch(url, dest_path, **kwargs):
        Path(dest_path).write_bytes(remote_cached_file.read_bytes())
        return True, None, {"format": "PNG", "width": 120, "height": 80}

    monkeypatch.setattr("app.research.visuals.image_pipeline.fetch_and_validate_image_url", mock_fetch)

    vis_remote = VisualAsset(
        asset_id="vis_remote_1",
        asset_type="photograph",
        title="Valid Remote Image",
        url_or_path="https://credible-science.org/figures/microscopy.png",
        attribution="Photo by Dr. Smith (credible-science.org)",
        is_generated=False,
    )
    resolved_remote = ensure_local_image_asset(vis_remote, cache_dir=tmp_path / "cache")
    assert resolved_remote is not None
    assert Path(resolved_remote).exists()
    assert vis_remote.url_or_path == resolved_remote


def test_pdf_and_docx_render_with_authenticated_image_attribution(tmp_path):
    """Verify PDF and DOCX renderers embed authentic website images near content with attribution preserved."""
    from app.research.models import (
        EvidenceClassification,
        EvidenceItem,
        ResearchClaim,
        ResearchKnowledgePackage,
        ResearchTopic,
        SourceReference,
        VisualAsset,
    )
    from app.research.renderers.docx_renderer import DocxRenderer
    from app.research.renderers.pdf_renderer import PdfRenderer

    # Create real sample image
    test_img = Image.new("RGB", (400, 200), color="darkgreen")
    img_file = tmp_path / "authentic_figure.png"
    test_img.save(img_file, format="PNG")

    pkg = ResearchKnowledgePackage(
        package_id="pkg_photo_test",
        topic=ResearchTopic(
            title="Solar Cell Efficiency Advances",
            objective="Analyze silicon heterojunction photovoltaic cell efficiency records.",
            findings_summary="Recent silicon tandem cells demonstrate 33.9% power conversion efficiency under standard AM1.5G testing.",
        ),
    )
    src = SourceReference(
        source_id="src_nrel",
        url="https://www.nrel.gov/pv/cell-efficiency.html",
        title="Best Research-Cell Efficiency Chart",
        domain="nrel.gov",
    )
    pkg.add_source(src)
    pkg.add_evidence(EvidenceItem(
        evidence_id="ev_001",
        source_id="src_nrel",
        quote="Tandem perovskite-silicon devices exceed 33% efficiency.",
    ))
    pkg.add_claim(ResearchClaim(
        claim_id="clm_001",
        statement="Tandem perovskite-silicon photovoltaics exceed 33% power conversion efficiency.",
        classification=EvidenceClassification.SINGLE_SOURCE,
        primary_source_id="src_nrel",
    ))
    vis = VisualAsset(
        asset_id="vis_cell_photo",
        asset_type="photograph",
        title="Microscopy Cross-Section of Tandem Perovskite Cell",
        caption="High-resolution electron microscopy of top perovskite absorber layer.",
        url_or_path=str(img_file),
        attribution="Credit: National Renewable Energy Laboratory (NREL)",
        is_generated=False,
    )
    pkg.add_visual(vis)

    # 1. Render PDF
    pdf_out = tmp_path / "solar_report.pdf"
    pdf_renderer = PdfRenderer()
    pdf_path = pdf_renderer.render(pkg, str(pdf_out))
    assert Path(pdf_path).exists()
    assert Path(pdf_path).stat().st_size > 3000

    # 2. Render DOCX
    docx_out = tmp_path / "solar_report.docx"
    docx_renderer = DocxRenderer()
    docx_path = docx_renderer.render(pkg, str(docx_out))
    assert Path(docx_path).exists()
    assert Path(docx_path).stat().st_size > 3000

    # Check DOCX text contains the attribution
    import docx  # type: ignore
    doc = docx.Document(docx_path)
    full_text = "\n".join(p.text for p in doc.paragraphs)
    assert "Credit: National Renewable Energy Laboratory (NREL)" in full_text



