"""Tesseract OCR wrapper with OpenCV preprocessing.

Falls back gracefully when tesseract isn't installed — callers will see empty
text and a logged warning, never an exception.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from app.config import get_settings
from app.utils.logging import logger


def _configure_tesseract() -> bool:
    s = get_settings()
    try:
        import pytesseract  # type: ignore
    except ImportError:
        logger.warning("pytesseract not installed; OCR disabled")
        return False
    if s.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = s.tesseract_cmd
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception as e:
        logger.warning("tesseract not found ({}); OCR will return empty strings", e)
        return False


_AVAILABLE: bool | None = None


def tesseract_available() -> bool:
    global _AVAILABLE
    if _AVAILABLE is None:
        _AVAILABLE = _configure_tesseract()
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
