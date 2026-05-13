"""pdfplumber-based table extraction.

Returns per-page tables as Markdown (pipe-style) strings so they can flow into
the chunker as a separate ``ChunkSource.table`` chunk source.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from app.utils.logging import logger


def _row_to_markdown(row: list[str | None]) -> str:
    return "| " + " | ".join((cell or "").replace("|", "\\|").replace("\n", " ").strip() for cell in row) + " |"


def extract_tables_markdown(pdf_path: str | Path) -> Iterator[tuple[int, str]]:
    """Yield ``(page_number, markdown_table)`` for every table found."""
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        logger.debug("pdfplumber not installed; skipping table extraction")
        return
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                try:
                    tables = page.extract_tables() or []
                except Exception as e:
                    logger.debug("pdfplumber failed page {}: {}", i, e)
                    continue
                for tbl in tables:
                    if not tbl or len(tbl) < 2:
                        continue
                    header = tbl[0]
                    body = tbl[1:]
                    md_lines = [_row_to_markdown(header)]
                    md_lines.append(
                        "| " + " | ".join("---" for _ in header) + " |"
                    )
                    for r in body:
                        md_lines.append(_row_to_markdown(r))
                    yield i, "\n".join(md_lines)
    except Exception as e:
        logger.warning("pdfplumber open failed for {}: {}", pdf_path, e)
