"""Screenshot OCR (Task 7) — bounded, opt-in, no new dependency.

The agent's only vision today is the screenshot a *remote* model may see
when the operator opts into ``ALLOW_SCREENSHOTS`` (Gemini provider). OCR
adds a local, text-only alternative: run the system ``tesseract`` binary
over the screenshot and feed the extracted words to the planner as
labelled-untrusted text. No OCR library is vendored and no service key
is involved — if the binary is absent, OCR is simply unavailable and the
planner never sees an OCR block.

Rules:
- Disabled by default (``PRIVAGENT_OCR_ENABLED``) AND gated on
  ``ALLOW_SCREENSHOTS``: OCR text comes from a rendered image that may
  contain PII the DOM redactor never saw, so it inherits the screenshot
  opt-in. It is also capped (``PRIVAGENT_OCR_MAX_CHARS``) and labelled
  untrusted in the prompt — OCR misreads text, and page text is data,
  never instructions.
- Never raises: any failure (no binary, bad image, timeout) returns
  ``None`` and the planner proceeds text-only.
"""
from __future__ import annotations

import base64
import logging
import re
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger("navigator.ocr")

_TESSERACT_TIMEOUT_S = 15
_DATA_URL_RE = re.compile(r"^data:image/(?:jpeg|jpg|png|webp);base64,", re.I)


def ocr_available() -> bool:
    """True when a ``tesseract`` binary is on PATH."""
    return shutil.which("tesseract") is not None


def _image_bytes(screenshot: str) -> Optional[bytes]:
    if not isinstance(screenshot, str) or not screenshot:
        return None
    data = screenshot
    if _DATA_URL_RE.match(data):
        _, _, data = data.partition(",")
    try:
        raw = base64.b64decode(data, validate=True)
    except Exception:
        return None
    if not raw or len(raw) > 4 * 1024 * 1024:
        return None
    return raw


def extract_ocr_text(
    screenshot: Optional[str], *, enabled: bool, max_chars: int = 2000
) -> Optional[str]:
    """OCR *screenshot* (a data URL) to plain text, or None.

    Returns None when OCR is disabled, no screenshot was supplied, the
    binary is missing, or the run fails. The caller labels the result
    untrusted; this function only extracts.
    """
    if not enabled or not screenshot:
        return None
    if not ocr_available():
        return None
    image = _image_bytes(screenshot)
    if image is None:
        return None
    try:
        proc = subprocess.run(
            ["tesseract", "stdin", "stdout", "--psm", "6"],
            input=image,
            capture_output=True,
            timeout=_TESSERACT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("ocr: tesseract run failed (%s); continuing text-only", exc)
        return None
    if proc.returncode != 0:
        logger.warning(
            "ocr: tesseract exited %s; continuing text-only", proc.returncode
        )
        return None
    text = proc.stdout.decode("utf-8", errors="replace")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    if not text:
        return None
    return text[:max_chars] if max_chars > 0 else text


__all__ = ["extract_ocr_text", "ocr_available"]
