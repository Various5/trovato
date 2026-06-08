"""Regression test for 'Delete source does nothing'.

``Document.source_id`` and ``ScanJob.source_id`` are foreign keys to
``document_sources.id`` with no cascade, and the engine runs with
``PRAGMA foreign_keys=ON``. A bare ``session.delete(source)`` therefore raised
``IntegrityError`` the instant the source had any document or scan-job history,
which bubbled out of the UI click handler so nothing happened.

``delete_source_cascade`` tears the children down in FK-safe order first. This
test wires up a source with a document (+chunk/page/image/tag-link) and a scan
job (+item referencing the document) and asserts the whole graph is removed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import select

from app.database import init_db, session_scope
from app.models import (
    Document,
    DocumentChunk,
    DocumentImage,
    DocumentPage,
    DocumentSource,
    DocumentStatus,
    DocumentTagLink,
    ScanJob,
    ScanJobItem,
    ScanJobStatus,
    SourceType,
    Tag,
    Visibility,
)
from app.services.sources import delete_source_cascade


def _seed(tmp_path: Path) -> tuple[int, int, int]:
    """Create a source with a fully-wired document + scan job. Returns ids."""
    init_db()
    with session_scope() as s:
        src = DocumentSource(
            name="del-test",
            type=SourceType.local,
            path=str(tmp_path),
            owner_id=None,
            visibility=Visibility.private,
        )
        s.add(src)
        s.flush()

        doc = Document(
            source_id=src.id,
            path=str(tmp_path / "a.pdf"),
            filename="a.pdf",
            content_hash="deadbeef",
            status=DocumentStatus.indexed,
        )
        s.add(doc)
        s.flush()

        s.add(DocumentChunk(document_id=doc.id, text="hello", page_from=1, page_to=1))
        s.add(DocumentPage(document_id=doc.id, page_number=1, native_text="hello"))
        s.add(
            DocumentImage(
                document_id=doc.id,
                page_number=1,
                image_index=0,
                image_hash="img1",
                cache_path=str(tmp_path / "img1.png"),
            )
        )
        tag = Tag(name="del-test-tag")
        s.add(tag)
        s.flush()
        s.add(DocumentTagLink(document_id=doc.id, tag_id=tag.id))

        job = ScanJob(source_id=src.id, status=ScanJobStatus.completed)
        s.add(job)
        s.flush()
        # The item references BOTH the job and the document — the trickiest FK.
        s.add(ScanJobItem(job_id=job.id, document_id=doc.id, path=str(tmp_path / "a.pdf")))

        return src.id, doc.id, job.id


def test_delete_source_cascade_removes_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The vector store is out of scope here — stub the Chroma delete.
    monkeypatch.setattr("app.vectorstore.delete_for_document", lambda *a, **k: None)

    src_id, doc_id, job_id = _seed(tmp_path)

    assert delete_source_cascade(src_id) is True

    with session_scope() as s:
        assert s.get(DocumentSource, src_id) is None
        assert s.get(Document, doc_id) is None
        assert s.get(ScanJob, job_id) is None
        assert s.exec(select(DocumentChunk).where(DocumentChunk.document_id == doc_id)).all() == []
        assert s.exec(select(DocumentPage).where(DocumentPage.document_id == doc_id)).all() == []
        assert s.exec(select(DocumentImage).where(DocumentImage.document_id == doc_id)).all() == []
        assert s.exec(select(DocumentTagLink).where(DocumentTagLink.document_id == doc_id)).all() == []
        assert s.exec(select(ScanJobItem).where(ScanJobItem.job_id == job_id)).all() == []


def test_delete_missing_source_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.vectorstore.delete_for_document", lambda *a, **k: None)
    init_db()
    assert delete_source_cascade(999_999) is False
