"""PaddleOCR adapter — optional dependency.

PaddleOCR is heavy (PyTorch / paddle), so we lazy-import. If unavailable, the
calls return empty strings and a single warning is logged.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Optional

from app.config import get_settings
from app.utils.logging import logger

_OCR: object | None = None
_INIT_FAILED = False


def _build() -> object | None:
    global _OCR, _INIT_FAILED
    if _OCR is not None or _INIT_FAILED:
        return _OCR
    try:
        from paddleocr import PaddleOCR  # type: ignore
    except Exception as e:
        logger.warning("paddleocr not available ({}); falling back to tesseract", e)
        _INIT_FAILED = True
        return None
    s = get_settings()
    primary = (s.ocr_lang.split("+")[0] if s.ocr_lang else "en").replace("eng", "en").replace("deu", "german")
    try:
        _OCR = PaddleOCR(use_angle_cls=True, lang=primary, show_log=False)
    except Exception as e:
        logger.warning("paddleocr init failed ({}); disabling", e)
        _INIT_FAILED = True
        _OCR = None
    return _OCR


def is_available() -> bool:
    return _build() is not None


def ocr_image_bytes(data: bytes) -> str:
    ocr = _build()
    if ocr is None:
        return ""
    try:
        import numpy as np
        from PIL import Image

        img = Image.open(io.BytesIO(data)).convert("RGB")
        arr = np.array(img)
        result = ocr.ocr(arr, cls=True)  # type: ignore[attr-defined]
        if not result:
            return ""
        lines: list[str] = []
        for page in result:
            if not page:
                continue
            for entry in page:
                try:
                    txt = entry[1][0]
                    if txt:
                        lines.append(txt)
                except Exception:
                    continue
        return "\n".join(lines).strip()
    except Exception as e:
        logger.warning("paddleocr inference failed: {}", e)
        return ""


def ocr_image(path: str | Path) -> str:
    try:
        return ocr_image_bytes(Path(path).read_bytes())
    except OSError as e:
        logger.warning("paddleocr read failed for {}: {}", path, e)
        return ""


_OPT = Optional  # silence unused import
