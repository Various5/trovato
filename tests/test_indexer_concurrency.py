"""Regression test for 'sqlite3.OperationalError: database is locked'.

Scanning a folder fans documents out across a thread pool, and every worker
opened its own SQLite write transaction. SQLite allows a single writer, so the
concurrent ``_persist_index_sync`` calls collided and crashed with
``database is locked`` (and the in-session ``fts_insert`` opened a *second*
connection that fought the same session's lock).

The fix serializes write transactions through a process-global lock
(``write_session``) and routes the FTS mirror through the session's own
connection. This test hammers ``index_document`` concurrently and asserts every
document indexes cleanly, the chunks persist, and the FTS mirror is populated.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import select

from app.config import get_settings
from app.database import init_db, session_scope
from app.database.engine import fts_search
from app.ingestion.pdf_extractor import PageContent
from app.models import Document, DocumentChunk, DocumentSource, SourceType, Visibility
from app.services import indexer

_PAGE_TEXT = (
    "Psychologische Nothilfe und betriebliche Notfallversorgung. "
    "Grundlagen, Dokumente und Vorgaben fuer den Lehrgang Bauleiter. "
) * 20


def _make_source(tmp_path: Path) -> DocumentSource:
    init_db()
    with session_scope() as session:
        s = DocumentSource(
            name="concurrent",
            type=SourceType.local,
            path=str(tmp_path),
            owner_id=None,
            visibility=Visibility.private,
        )
        session.add(s)
        session.flush()
        return DocumentSource(**s.model_dump())


@pytest.mark.asyncio
async def test_concurrent_index_no_database_locked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _make_source(tmp_path)

    # 24 files, indexed concurrently — enough overlap to expose the race.
    files = []
    for i in range(24):
        p = tmp_path / f"doc_{i}.pdf"
        p.write_bytes(b"%PDF-1.4\n%stub")
        files.append(p)

    def _fake_extract(path: Any, *a: Any, **kw: Any) -> list[PageContent]:
        return [
            PageContent(page_number=n, native_text=_PAGE_TEXT, width=600, height=800) for n in range(1, 4)
        ]

    monkeypatch.setattr(indexer, "extract_pdf", _fake_extract)
    # Table extraction would try to open the stub PDF with pdfplumber; short it.
    monkeypatch.setattr("app.ingestion.tables.extract_tables_markdown", lambda *a, **k: iter([]))
    # Isolate the SQLite write path: stub the vector store (its own concurrency
    # is a separate concern from the 'database is locked' fix under test).
    monkeypatch.setattr(indexer, "add_chunks", lambda **kw: None)
    monkeypatch.setattr(indexer, "delete_for_document", lambda *a, **kw: None)

    class _Client:
        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.01 * (i + 1)] * 8 for i, _ in enumerate(texts)]

        async def describe_image(self, p: Any) -> str:
            return ""

        async def preflight_embed(self) -> tuple[bool, str]:
            return True, "ok"

    client = _Client()
    monkeypatch.setattr(indexer, "get_client", lambda: client)

    # Run the full text-phase persist path for every file at once. Pre-fix this
    # raised OperationalError('database is locked') from a colliding worker.
    results = await asyncio.gather(*(indexer.index_document(src, p, phase="text") for p in files))

    # Every document indexed (no None = no crash/skip).
    assert all(doc_id is not None for doc_id in results), results
    assert len(set(results)) == len(files)  # distinct documents

    # Scope to the ids produced here — the session-shared test DB may hold rows
    # from other tests.
    doc_ids = [r for r in results if r is not None]
    with session_scope() as session:
        docs = session.exec(select(Document).where(Document.id.in_(doc_ids))).all()  # type: ignore
        chunks = session.exec(
            select(DocumentChunk).where(DocumentChunk.document_id.in_(doc_ids))  # type: ignore
        ).all()
    assert len(docs) == len(files)
    assert all(d.status.value == "indexed" for d in docs)
    assert len(chunks) >= len(files)  # at least one chunk per document

    # The FTS mirror (written via the session connection) is searchable.
    # FTS5 is SQLite-only, so only assert it on that backend.
    if get_settings().effective_db_url.startswith("sqlite"):
        hits = fts_search("Bauleiter", limit=50)
        assert hits, "expected FTS hits for an indexed term"
