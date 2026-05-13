"""OCR dispatcher.

Backend is selected at runtime by ``settings.ocr_backend``:
    - ``tesseract`` (default) — pytesseract + OpenCV preprocessing
    - ``paddle``  — PaddleOCR (lazy, optional, heavy)

Both backends expose the same ``ocr_image(path)`` / ``ocr_image_bytes(bytes)``
contract.
"""

from __future__ import annotations

from pathlib import Path

from app.config import get_settings
from app.ocr.tesseract import (  # noqa: F401
    ocr_image as _tess_image,
    ocr_image_bytes as _tess_bytes,
    preprocess_for_ocr,
    tesseract_available,
)
from app.utils.logging import logger


def _backend() -> str:
    return (get_settings().ocr_backend or "tesseract").lower()


def ocr_image(path: str | Path, lang: str | None = None) -> str:
    backend = _backend()
    if backend == "paddle":
        from app.ocr import paddle

        if paddle.is_available():
            return paddle.ocr_image(path)
        logger.debug("paddle unavailable; using tesseract")
    return _tess_image(path, lang=lang)


def ocr_image_bytes(data: bytes, lang: str | None = None) -> str:
    backend = _backend()
    if backend == "paddle":
        from app.ocr import paddle

        if paddle.is_available():
            return paddle.ocr_image_bytes(data)
    return _tess_bytes(data, lang=lang)


__all__ = ["ocr_image", "ocr_image_bytes", "preprocess_for_ocr", "tesseract_available"]
