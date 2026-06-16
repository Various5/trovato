"""Tesseract OCR wrapper with OpenCV preprocessing.

Falls back gracefully when tesseract isn't installed — callers will see empty
text and a logged warning, never an exception.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from app.config import get_settings
from app.utils.logging import logger


def _candidate_cmds() -> list[str]:
    """tesseract executable locations to probe when no valid path is configured.

    A Windows user who installed the UB-Mannheim build but didn't add it to PATH
    has tesseract at a well-known location even though ``shutil.which`` and the
    bare ``tesseract`` command both fail — which is exactly the "says not found
    even though it's installed" report. Probe those locations too.
    """
    import os
    import shutil

    return [
        shutil.which("tesseract") or "",
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
        os.path.expandvars(r"%USERPROFILE%\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
        "/opt/homebrew/bin/tesseract",
        "/usr/local/bin/tesseract",
        "/usr/bin/tesseract",
    ]


def find_tesseract_binary() -> str:
    """Best-effort path to a tesseract executable: the configured ``tesseract_cmd``
    if it points at a real file, otherwise the first existing common install
    location (or one found on PATH). Returns ``""`` when nothing is found.

    Shared by the auto-detect button and the availability probe so the Settings
    page and the Diagnostics page can never disagree about what's installed.
    """
    configured = (get_settings().tesseract_cmd or "").strip()
    if configured and Path(configured).is_file():
        return configured
    for c in _candidate_cmds():
        if c and Path(c).is_file():
            return c
    return ""


def _configure_tesseract() -> bool:
    try:
        import pytesseract  # type: ignore
    except ImportError:
        logger.warning("pytesseract not installed; OCR disabled")
        return False
    cmd = find_tesseract_binary()
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception as e:
        logger.warning("tesseract not found ({}); OCR will return empty strings", e)
        return False


_AVAILABLE: bool | None = None
_AVAILABLE_KEY: str | None = None


def tesseract_available(*, refresh: bool = False) -> bool:
    """Whether OCR via tesseract is usable.

    The result is cached, but the cache is keyed on the configured
    ``tesseract_cmd`` so changing it (manually or via auto-detect, which clears
    the settings cache) re-probes automatically instead of returning a stale
    ``False`` forever. Pass ``refresh=True`` to force a fresh probe — the
    Diagnostics page does, so it always reflects the current install state.
    """
    global _AVAILABLE, _AVAILABLE_KEY
    key = (get_settings().tesseract_cmd or "").strip()
    if refresh or _AVAILABLE is None or key != _AVAILABLE_KEY:
        _AVAILABLE = _configure_tesseract()
        _AVAILABLE_KEY = key
    return _AVAILABLE


def preprocess_for_ocr(img: np.ndarray) -> np.ndarray:
    """Light OpenCV pipeline: grayscale, denoise, adaptive threshold."""
    try:
        import cv2  # type: ignore
    except ImportError:
        return img
    if img.ndim == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        gray = img
    gray = cv2.fastNlMeansDenoising(gray, h=10)
    gray = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10)
    return gray


def ocr_image(path: str | Path, lang: str | None = None) -> str:
    if not tesseract_available():
        return ""
    import pytesseract  # type: ignore
    from PIL import Image  # type: ignore

    s = get_settings()
    lang = lang or s.ocr_lang
    try:
        img = Image.open(path)
        return pytesseract.image_to_string(img, lang=lang).strip()
    except Exception as e:
        logger.warning("OCR failed for {}: {}", path, e)
        return ""


def ocr_image_bytes(data: bytes, lang: str | None = None) -> str:
    if not tesseract_available():
        return ""
    import io

    import pytesseract  # type: ignore
    from PIL import Image  # type: ignore

    s = get_settings()
    lang = lang or s.ocr_lang
    try:
        img = Image.open(io.BytesIO(data))
        return pytesseract.image_to_string(img, lang=lang).strip()
    except Exception as e:
        logger.warning("OCR (bytes) failed: {}", e)
        return ""
