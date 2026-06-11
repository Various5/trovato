"""Locate query terms on PDF pages as normalized rectangles.

Powers the viewer's on-image highlights: for each meaningful query term we ask
PyMuPDF where the term sits on the page and return the boxes as FRACTIONS of
the displayed page size, so the client can overlay them on a rendered page
image at any resolution (the cached PNGs exist in several DPIs — 144 from the
on-the-fly endpoint, 150/220/300 from the OCR scan path — so pixel coordinates
would be wrong as often as right).

Coordinate handling (verified against PyMuPDF 1.27):
- ``Page.search_for`` returns rects in the UNROTATED page space while
  ``page.rect`` and rendered pixmaps are rotation-applied → map every rect
  through ``page.rotation_matrix`` (identity for rotation 0).
- A 90°/270° rotation swaps rect corners → ``normalize()`` before reading
  x0/y0 as top-left.
- When cropbox != mediabox both search rects and pixmaps are already
  cropbox-relative → normalizing by ``page.rect`` is correct as-is.
- Scanned pages without a text layer simply yield no rects (OCR word boxes
  are not stored); callers must treat "no rects" as "nothing to draw", not
  as an error.
"""

from __future__ import annotations

from loguru import logger

MAX_TERMS = 10
MAX_PAGES = 80
MAX_RECTS_PER_PAGE = 300

# x, y, w, h as fractions of the displayed page rect (0..1).
NormRect = tuple[float, float, float, float]


def match_rects_for_pages(
    pdf_path: str,
    pages: list[int],
    terms: list[str],
    *,
    max_pages: int = MAX_PAGES,
) -> dict[int, list[NormRect]]:
    """``{page_number: [(x, y, w, h), …]}`` for every requested 1-based page.

    Opens the document once; pages out of range or failing to parse are
    skipped silently (the viewer just shows no overlay there).
    """
    terms = [t for t in terms if t][:MAX_TERMS]
    if not terms or not pages:
        return {}

    import fitz

    out: dict[int, list[NormRect]] = {}
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        logger.debug("match rects: cannot open {}: {}", pdf_path, e)
        return {}
    try:
        for page_no in pages[:max_pages]:
            if page_no < 1 or page_no > doc.page_count:
                continue
            try:
                page = doc.load_page(page_no - 1)
                pw, ph = page.rect.width, page.rect.height
                if pw <= 0 or ph <= 0:
                    continue
                # One text extraction reused across all terms on this page.
                tp = page.get_textpage()
                rects: list[NormRect] = []
                rot = page.rotation_matrix
                for term in terms:
                    for r in page.search_for(term, textpage=tp):
                        d = fitz.Rect(r) * rot
                        d.normalize()
                        rects.append(
                            (
                                round(d.x0 / pw, 5),
                                round(d.y0 / ph, 5),
                                round(d.width / pw, 5),
                                round(d.height / ph, 5),
                            )
                        )
                        if len(rects) >= MAX_RECTS_PER_PAGE:
                            break
                    if len(rects) >= MAX_RECTS_PER_PAGE:
                        break
                if rects:
                    out[page_no] = rects
            except Exception as e:
                logger.debug("match rects: page {} of {} failed: {}", page_no, pdf_path, e)
    finally:
        doc.close()
    return out
