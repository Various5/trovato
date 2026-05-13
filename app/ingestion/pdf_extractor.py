"""PDF extraction: text per page, embedded images, page renders, OCR fallback."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from app.config import get_settings
from app.ocr.tesseract import ocr_image_bytes
from app.utils.hashing import sha256_bytes
from app.utils.logging import logger
from app.utils.paths import safe_filename


@dataclass
class ExtractedImage:
    page_number: int
    image_index: int
    image_hash: str
    width: int
    height: int
    cache_path: str
    bytes_: bytes = b""
    ocr_text: str = ""


@dataclass
class PageContent:
    page_number: int
    native_text: str = ""
    ocr_text: str = ""
    width: int = 0
    height: int = 0
    has_images: bool = False
    has_tables: bool = False
    rendered_image_path: str = ""
    images: list[ExtractedImage] = field(default_factory=list)


def _safe_open(pdf_path: str | Path):
    import fitz  # PyMuPDF

    return fitz.open(str(pdf_path))


def render_page_to_png(page, dpi: int = 200) -> bytes:
    import fitz

    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    return pix.tobytes("png")


def extract_pdf(
    pdf_path: str | Path,
    *,
    doc_id_for_cache: int | str,
    force_ocr: bool = False,
    extract_images: bool = True,
) -> Iterator[PageContent]:
    """Stream page contents from a PDF.

    OCR is performed when the native text on a page falls below
    ``settings.ocr_min_text_chars`` or when ``force_ocr`` is True.
    """
    s = get_settings()
    cache_dir = s.cache_path / "pages" / str(doc_id_for_cache)
    img_cache_dir = s.cache_path / "images" / str(doc_id_for_cache)
    cache_dir.mkdir(parents=True, exist_ok=True)
    img_cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        doc = _safe_open(pdf_path)
    except Exception as e:
        logger.error("cannot open pdf {}: {}", pdf_path, e)
        return

    try:
        for i, page in enumerate(doc, start=1):
            rect = page.rect
            native = (page.get_text("text") or "").strip()

            content = PageContent(
                page_number=i,
                native_text=native,
                width=int(rect.width),
                height=int(rect.height),
            )

            # Embedded images
            if extract_images:
                try:
                    for img_idx, info in enumerate(page.get_images(full=True)):
                        xref = info[0]
                        try:
                            base = doc.extract_image(xref)
                        except Exception:
                            continue
                        data: bytes = base.get("image", b"")
                        if not data:
                            continue
                        ext = (base.get("ext") or "png").lower()
                        h = sha256_bytes(data)
                        out_path = img_cache_dir / safe_filename(f"p{i:04d}_i{img_idx:02d}_{h[:12]}.{ext}")
                        if not out_path.exists():
                            out_path.write_bytes(data)
                        content.images.append(
                            ExtractedImage(
                                page_number=i,
                                image_index=img_idx,
                                image_hash=h,
                                width=int(base.get("width", 0)),
                                height=int(base.get("height", 0)),
                                cache_path=str(out_path),
                                bytes_=data,
                            )
                        )
                    content.has_images = bool(content.images)
                except Exception as e:
                    logger.debug("image extract failed page {}: {}", i, e)

            # Detect tables cheaply (heuristic — many vertical line glyphs)
            content.has_tables = native.count("|") > 6 or native.count("\t") > 6

            # OCR fallback
            need_ocr = force_ocr or len(native) < s.ocr_min_text_chars
            if need_ocr:
                try:
                    png = render_page_to_png(page, dpi=220)
                    rendered_path = cache_dir / f"page_{i:04d}.png"
                    rendered_path.write_bytes(png)
                    content.rendered_image_path = str(rendered_path)
                    content.ocr_text = ocr_image_bytes(png)
                except Exception as e:
                    logger.warning("OCR failed page {} of {}: {}", i, pdf_path, e)

            # OCR per image (small ones only, to keep it cheap)
            for img in content.images:
                if img.width * img.height > 0 and img.width * img.height < 4_000_000:
                    try:
                        img.ocr_text = ocr_image_bytes(img.bytes_)
                    except Exception:
                        img.ocr_text = ""
                img.bytes_ = b""  # drop bytes after OCR to save memory

            yield content
    finally:
        try:
            doc.close()
        except Exception:
            pass


def quick_meta(pdf_path: str | Path) -> dict:
    """Return PDF metadata without extracting text."""
    try:
        doc = _safe_open(pdf_path)
        try:
            meta = dict(doc.metadata or {})
            meta["page_count"] = doc.page_count
            return meta
        finally:
            doc.close()
    except Exception as e:
        logger.warning("quick_meta failed for {}: {}", pdf_path, e)
        return {}


_ = io  # keep import for potential future inline streaming
