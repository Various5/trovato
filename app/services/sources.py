"""Document-source lifecycle helpers.

Deleting a source is not a single ``DELETE`` — the schema enforces foreign
keys (``PRAGMA foreign_keys=ON``), and ``Document.source_id`` / ``ScanJob.source_id``
reference ``document_sources.id`` with no ``ON DELETE`` cascade. So a bare
``session.delete(source)`` raises ``IntegrityError`` the moment the source has
*any* document or scan-job history — which is exactly why the Sources page
"Delete" button looked like it did nothing (the exception bubbled out of the
click handler, the dialog never closed, and no row was removed).

``delete_source_cascade`` tears the children down in FK-safe order first, then
the source. It runs the SQLite work inside ``write_session`` (serialized with
the indexer's writers) and drops vectors outside that lock so a Chroma call
can't pin the write lock.
"""

from __future__ import annotations

from sqlmodel import delete as sqldelete
from sqlmodel import select

from app.database import session_scope, write_session
from app.database.engine import fts_delete_for_document
from app.models import (
    Document,
    DocumentChunk,
    DocumentImage,
    DocumentPage,
    DocumentSource,
    DocumentTagLink,
    ScanJob,
    ScanJobItem,
)
from app.utils.logging import logger


def delete_source_cascade(source_id: int) -> bool:
    """Delete a source and everything that references it.

    Returns ``True`` if the source existed (and was deleted), ``False`` if there
    was no such source. Safe to call from a worker thread (uses ``write_session``).

    Teardown order matters under enforced FKs:
      1. ``ScanJobItem`` rows (reference both documents and scan jobs)
      2. each document's chunks / pages / images / tag-links / FTS rows, then the
         document itself
      3. the source's ``ScanJob`` rows
      4. the source
    """
    # 1) Gather ids with a lock-free read; bail early if the source is gone.
    with session_scope() as s:
        if s.get(DocumentSource, source_id) is None:
            return False
        doc_ids = [
            d.id
            for d in s.exec(select(Document).where(Document.source_id == source_id)).all()
            if d.id is not None
        ]
        job_ids = [
            j.id
            for j in s.exec(select(ScanJob).where(ScanJob.source_id == source_id)).all()
            if j.id is not None
        ]

    # 2) Drop vector-store entries outside the SQLite write lock (Chroma I/O).
    if doc_ids:
        from app.vectorstore import delete_for_document

        for did in doc_ids:
            try:
                delete_for_document(did)
            except Exception as e:  # best-effort — never block the DB teardown
                logger.debug("vector delete failed for doc {}: {}", did, e)

    # 3) SQLite teardown in one serialized write transaction. Per-row core
    #    DELETEs keyed on a single id avoid both loading rows into memory and
    #    SQLite's bound-parameter limit on large IN (...) lists.
    with write_session() as s:
        conn = s.connection()
        for jid in job_ids:
            s.exec(sqldelete(ScanJobItem).where(ScanJobItem.job_id == jid))
        for did in doc_ids:
            for table in (DocumentChunk, DocumentPage, DocumentImage, DocumentTagLink):
                s.exec(sqldelete(table).where(table.document_id == did))  # type: ignore[attr-defined]
            fts_delete_for_document(did, conn=conn)
            s.exec(sqldelete(Document).where(Document.id == did))
        for jid in job_ids:
            s.exec(sqldelete(ScanJob).where(ScanJob.id == jid))
        s.exec(sqldelete(DocumentSource).where(DocumentSource.id == source_id))

    logger.info("deleted source {} (+{} documents, {} scan jobs)", source_id, len(doc_ids), len(job_ids))
    return True
