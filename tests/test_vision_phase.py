"""Regression test: a Vision scan must re-process already-text-indexed docs.

The indexer skips a document when the file's content-hash is unchanged and no
``force_*`` flag is set. That silently broke the Vision phase: a doc indexed by
an earlier text/ocr pass is byte-identical, so a plain ``phase="vision"`` scan
skipped it before extracting a single image — finishing in seconds and never
describing anything (observed: 51 docs, 536 embedded images, 0 descriptions).

The fix: don't treat a doc as "unchanged" for a vision pass until it actually
has vision descriptions. This test indexes a doc with text, then runs a vision
pass on the same file and asserts the image gets described.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlmodel import select

from app.config import get_settings, save_user_settings
from app.database import init_db, session_scope
from app.ingestion.pdf_extractor import ExtractedImage, PageContent
from app.models import DocumentImage, DocumentSource, SourceType, Visibility
from app.services import indexer


def _make_source(tmp_path: Path) -> DocumentSource:
    init_db()
    with session_scope() as session:
        s = DocumentSource(
            name="vis",
            type=SourceType.local,
            path=str(tmp_path),
            owner_id=None,
            visibility=Visibility.private,
        )
        session.add(s)
        session.flush()
        return DocumentSource(**s.model_dump())


@pytest.mark.asyncio
async def test_vision_phase_reprocesses_text_indexed_doc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = tmp_path / "with_image.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%stub")
    img_file = tmp_path / "pic.png"
    img_file.write_bytes(b"\x89PNG\r\n\x1a\nstub")

    # A vision model must be configured for descriptions to run.
    save_user_settings({"vision_model": "test-vlm"})
    get_settings.cache_clear()

    def _extract(*a: Any, **kw: Any) -> list[PageContent]:
        # One page with real text (so the doc counts as fully indexed) + one
        # embedded image. Returned identically for every phase.
        return [
            PageContent(
                page_number=1,
                native_text="A photo of a swimming pool next to a green meadow. " * 5,
                width=600,
                height=800,
                has_images=True,
                images=[
                    ExtractedImage(
                        page_number=1,
                        image_index=0,
                        image_hash="imgpool1",
                        width=400,
                        height=300,
                        cache_path=str(img_file),
                    )
                ],
            )
        ]

    monkeypatch.setattr(indexer, "extract_pdf", _extract)
    monkeypatch.setattr("app.ingestion.tables.extract_tables_markdown", lambda *a, **k: iter([]))
    monkeypatch.setattr(indexer, "add_chunks", lambda **kw: None)
    monkeypatch.setattr(indexer, "delete_for_document", lambda *a, **kw: None)

    describe_calls: list[str] = []

    class _Client:
        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.01] * 8 for _ in texts]

        async def describe_image(self, p: Any) -> str:
            describe_calls.append(str(p))
            return "A swimming pool beside a meadow."

        async def preflight_embed(self) -> tuple[bool, str]:
            return True, "ok"

    monkeypatch.setattr(indexer, "get_client", lambda: _Client())

    src = _make_source(tmp_path)

    # 1) Text pass — indexes the doc; vision is NOT run, so no descriptions yet.
    text_id = await indexer.index_document(src, pdf, phase="text")
    assert text_id is not None
    assert describe_calls == []
    with session_scope() as s:
        described = s.exec(
            select(DocumentImage).where(
                DocumentImage.document_id == text_id, DocumentImage.vision_description != ""
            )
        ).all()
    assert described == []  # text phase produced no vision descriptions

    # 2) Vision pass on the SAME (unchanged) file — must NOT skip; it should
    #    extract the image and describe it.
    vis_id = await indexer.index_document(src, pdf, phase="vision")
    assert vis_id == text_id  # same document row
    assert describe_calls, "vision pass should have called describe_image"
    with session_scope() as s:
        described = s.exec(
            select(DocumentImage).where(
                DocumentImage.document_id == vis_id, DocumentImage.vision_description != ""
            )
        ).all()
    assert described, "vision pass should have stored an image description"
    assert "pool" in described[0].vision_description.lower()

    # 3) A second vision pass IS allowed to skip now (vision data already exists).
    describe_calls.clear()
    again_id = await indexer.index_document(src, pdf, phase="vision")
    assert again_id == text_id
    assert describe_calls == []  # already has vision data → skipped, no re-describe
