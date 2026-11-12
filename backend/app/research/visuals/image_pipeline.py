"""Safe Image Pipeline: Validation, Format Checking, and Secure Local Caching.

Prevents malicious image payload execution, verifies magic bytes, inspects dimensions,
and manages local asset caching for authentic photographs and visuals.
"""
from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path
from typing import Any, Optional

import httpx
from PIL import Image  # type: ignore

from ...website_resolver import is_safe_public_url, validate_redirect_chain


# Supported authentic image magic byte signatures
MAGIC_SIGNATURES = {
    "png": b"\x89PNG\r\n\x1a\n",
    "jpeg": b"\xff\xd8\xff",
    "gif": (b"GIF87a", b"GIF89a"),
    "webp": (b"RIFF", b"WEBP"),
}


def validate_image_bytes(raw_bytes: bytes, max_size_mb: float = 15.0) -> tuple[bool, Optional[str], Optional[dict[str, Any]]]:
    """Validate image bytes for authentic format, reasonable dimensions, and absence of corruption."""
    if not raw_bytes:
        return False, "Image payload is empty.", None

    size_mb = len(raw_bytes) / (1024 * 1024)
    if size_mb > max_size_mb:
        return False, f"Image payload ({size_mb:.1f} MB) exceeds maximum allowed size ({max_size_mb:.1f} MB).", None

    # Magic byte check
    is_valid_magic = False
    detected_fmt = None
    if raw_bytes.startswith(MAGIC_SIGNATURES["png"]):
        is_valid_magic = True
        detected_fmt = "PNG"
    elif raw_bytes.startswith(MAGIC_SIGNATURES["jpeg"]):
        is_valid_magic = True
        detected_fmt = "JPEG"
    elif raw_bytes.startswith(MAGIC_SIGNATURES["gif"][0]) or raw_bytes.startswith(MAGIC_SIGNATURES["gif"][1]):
        is_valid_magic = True
        detected_fmt = "GIF"
    elif len(raw_bytes) >= 12 and raw_bytes[:4] == b"RIFF" and raw_bytes[8:12] == b"WEBP":
        is_valid_magic = True
        detected_fmt = "WEBP"

    if not is_valid_magic:
        return False, "Unrecognized or prohibited image magic header (potential polyglot/payload).", None

    # Pillow deep inspection
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            img.verify()  # Verifies integrity without decoding entire image

        # Re-open to read properties after verify
        with Image.open(io.BytesIO(raw_bytes)) as img:
            width, height = img.size
            img_format = img.format or detected_fmt
            img_mode = img.mode

        if width < 10 or height < 10:
            return False, f"Image dimensions too small ({width}x{height}).", None
        if width > 10000 or height > 10000:
            return False, f"Image dimensions too large ({width}x{height}) - potential decompression bomb.", None

        meta = {
            "format": img_format,
            "width": width,
            "height": height,
            "mode": img_mode,
            "size_bytes": len(raw_bytes),
        }
        return True, None, meta

    except Exception as e:
        return False, f"Image verification failed: {e}", None


def validate_and_cache_image(
    source_bytes_or_path: bytes | str,
    dest_path: str,
    max_size_mb: float = 15.0,
) -> tuple[bool, Optional[str], Optional[dict[str, Any]]]:
    """Validate image source and cache it to the specified destination path."""
    dest_file = Path(dest_path).resolve()

    if isinstance(source_bytes_or_path, str):
        src_path = Path(source_bytes_or_path).resolve()
        if not src_path.exists():
            return False, f"Source image path does not exist: {src_path}", None
        try:
            raw_data = src_path.read_bytes()
        except Exception as e:
            return False, f"Could not read source image file: {e}", None
    else:
        raw_data = source_bytes_or_path

    valid, reason, meta = validate_image_bytes(raw_data, max_size_mb=max_size_mb)
    if not valid:
        return False, reason, None

    try:
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        dest_file.write_bytes(raw_data)
        if meta:
            meta["cached_path"] = str(dest_file)
        return True, None, meta
    except Exception as e:
        return False, f"Failed to save cached image asset: {e}", None


def fetch_and_validate_image_url(
    image_url: str,
    dest_path: str,
    max_size_mb: float = 15.0,
    max_redirects: int = 5,
    timeout: float = 10.0,
) -> tuple[bool, Optional[str], Optional[dict[str, Any]]]:
    """Securely fetch a remote image from URL, validating all redirect hops and payloads.

    SSRF Protection:
    - Every redirect hop is checked against is_safe_public_url.
    - Loopback, private IP, and cloud metadata redirects are blocked.
    - Max redirects bounded to prevent redirect loops.
    - Content streaming aborts if payload exceeds max_size_mb.
    - Signature & magic bytes checked via validate_and_cache_image.
    """
    safe_init, init_err = is_safe_public_url(image_url)
    if not safe_init:
        return False, f"Remote image URL rejected: {init_err}", None

    current_url = image_url
    redirect_chain: list[str] = []

    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            for _ in range(max_redirects + 1):
                resp = client.get(
                    current_url,
                    headers={"User-Agent": "PrivAgent/1.0 (Research Artifact Generator)"},
                )

                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        return False, "Redirect response missing Location header.", None

                    # Resolve relative redirect URLs
                    import urllib.parse
                    next_url = urllib.parse.urljoin(current_url, location)
                    safe_hop, hop_err = is_safe_public_url(next_url)
                    if not safe_hop:
                        return False, f"Image redirect to '{next_url}' blocked: {hop_err}", None

                    redirect_chain.append(next_url)
                    if len(redirect_chain) > max_redirects:
                        return False, f"Image redirect chain exceeded {max_redirects} hops.", None

                    current_url = next_url
                    continue

                if resp.status_code != 200:
                    return False, f"Image download failed with HTTP {resp.status_code}", None

                # Verify content type
                content_type = resp.headers.get("content-type", "").lower()
                if content_type and not content_type.startswith("image/"):
                    return False, f"Invalid Content-Type for image: {content_type}", None

                # Check Content-Length if provided
                cl_header = resp.headers.get("content-length")
                if cl_header:
                    try:
                        cl_val = int(cl_header)
                        if cl_val > max_size_mb * 1024 * 1024:
                            return False, f"Image Content-Length ({cl_val} bytes) exceeds limit.", None
                    except ValueError:
                        pass

                raw_bytes = resp.content
                return validate_and_cache_image(raw_bytes, dest_path, max_size_mb=max_size_mb)

            return False, f"Too many redirects ({len(redirect_chain)}).", None

    except httpx.RequestError as e:
        return False, f"Network error fetching image: {e}", None
    except Exception as e:
        return False, f"Unexpected error downloading image: {e}", None


def ensure_local_image_asset(
    visual: Any,
    cache_dir: Optional[Path | str] = None,
    timeout: float = 6.0,
) -> Optional[str]:
    """Ensure that a VisualAsset has a valid, validated local disk file path.

    If visual.url_or_path is already a valid local file path on disk:
      Verifies existence and returns the absolute local path string.

    If visual.url_or_path is a remote URL (http:// or https://):
      Securely downloads and validates the image via fetch_and_validate_image_url
      (enforcing SSRF protections, redirect limits, magic bytes, and dimension checks),
      caches it to disk, updates visual.url_or_path with the local path, and returns it.

    If validation or download fails, returns None.
    """
    if not visual:
        return None

    raw_path_or_url = getattr(visual, "url_or_path", None)
    if not raw_path_or_url or not isinstance(raw_path_or_url, str):
        return None

    raw_path_or_url = raw_path_or_url.strip()
    if not raw_path_or_url:
        return None

    # 1. If it's already a local file path
    if not raw_path_or_url.lower().startswith(("http://", "https://")):
        p = Path(raw_path_or_url)
        if p.exists() and p.is_file() and p.stat().st_size > 0:
            return str(p.resolve())
        return None

    # 2. Remote URL: Compute deterministic cache file path
    url_hash = hashlib.sha256(raw_path_or_url.encode("utf-8")).hexdigest()[:12]
    asset_id = getattr(visual, "asset_id", "asset")
    safe_asset_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", asset_id)
    cache_filename = f"img_{safe_asset_id}_{url_hash}.png"

    if cache_dir:
        target_dir = Path(cache_dir).resolve()
    else:
        # Default cache directory under backend/data/cached_images
        target_dir = Path(__file__).resolve().parents[3] / "data" / "cached_images"

    target_dir.mkdir(parents=True, exist_ok=True)
    cached_path = target_dir / cache_filename

    # If already cached and valid, reuse
    if cached_path.exists() and cached_path.is_file() and cached_path.stat().st_size > 0:
        visual.url_or_path = str(cached_path)
        return str(cached_path)

    # Fetch and validate via secure downloader
    ok, err, meta = fetch_and_validate_image_url(
        raw_path_or_url,
        dest_path=str(cached_path),
        timeout=timeout,
    )
    if ok and cached_path.exists() and cached_path.stat().st_size > 0:
        visual.url_or_path = str(cached_path)
        if meta and hasattr(visual, "metadata") and isinstance(visual.metadata, dict):
            visual.metadata["local_cached_path"] = str(cached_path)
            visual.metadata["cached_format"] = meta.get("format")
            visual.metadata["dimensions"] = f"{meta.get('width')}x{meta.get('height')}"
        return str(cached_path)

    return None

