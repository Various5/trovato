"""Tests for query-less document browse (tag / source / doc-type filters).

Backs the /search?tag=X deep-link: clicking a tag chip browses documents with
that tag even though there's no text query.
"""

from __future__ import annotations

from sqlmodel import select

from app.database import init_db, session_scope
from app.models import (
    ChunkSource,
    Document,
    DocumentChunk,
    DocumentSource,
    DocumentStatus,
    DocumentTagLink,
    SourceType,
    Tag,
    Visibility,
)
from app.services.search_service import browse_documents

TAG = "browse-tag-xyz"


def _seed() -> tuple[int, int, int]:
    """Create a source + two docs (one tagged TAG), return (d1_id, d2_id, src_id)."""
    init_db()
    with session_scope() as session:
        src = DocumentSource(
            name="browse-src",
            type=SourceType.local,
            path="/tmp/browse",
            owner_id=None,
            visibility=Visibility.shared,
        )
        session.add(src)
        session.flush()
        d1 = Document(
            source_id=src.id,
            path="/tmp/browse/a.pdf",
            filename="a.pdf",
            content_hash="browsehash-a",
            doc_type="invoice-xyz",
            status=DocumentStatus.indexed,
            page_count=3,
            visibility=Visibility.shared,
        )
        d2 = Document(
            source_id=src.id,
            path="/tmp/browse/b.pdf",
            filename="b.pdf",
            content_hash="browsehash-b",
            doc_type="contract-xyz",
            status=DocumentStatus.indexed,
            page_count=1,
            visibility=Visibility.shared,
        )
        session.add(d1)
        session.add(d2)
        session.flush()
        # A chunk on page 2 of d1 — browse should surface it as the snippet/page.
        session.add(
            DocumentChunk(
                document_id=d1.id,
                page_from=2,
                page_to=2,
                text="Invoice total 100 EUR — thank you",
                source=ChunkSource.native_text,
                token_count=6,
            )
        )
        tag = session.exec(select(Tag).where(Tag.name == TAG)).first()
        if tag is None:
            tag = Tag(name=TAG, auto=True)
            session.add(tag)
            session.flush()
        session.add(DocumentTagLink(document_id=d1.id, tag_id=tag.id))
        return d1.id, d2.id, src.id


def test_browse_by_tag_returns_only_tagged() -> None:
    d1, d2, _src = _seed()
    hits = browse_documents(tags=[TAG])
    ids = {h.document_id for h in hits}
    assert d1 in ids
    assert d2 not in ids
    hit = next(h for h in hits if h.document_id == d1)
    assert hit.page_from == 2  # came from the chunk, not a default
    assert "Invoice" in hit.snippet


def test_browse_unknown_tag_is_empty() -> None:
    _seed()
    assert browse_documents(tags=["no-such-tag-zzz"]) == []


def test_browse_by_doc_type() -> None:
    d1, d2, _src = _seed()
    ids = {h.document_id for h in browse_documents(doc_types=["contract-xyz"])}
    assert d2 in ids
    assert d1 not in ids


def test_browse_by_source_returns_all_in_source() -> None:
    d1, d2, src = _seed()
    ids = {h.document_id for h in browse_documents(source_ids=[src])}
    assert {d1, d2} <= ids


def test_browse_no_filters_returns_documents() -> None:
    d1, _d2, _src = _seed()
    # No filters → lists documents (capped); our freshly-added doc is newest.
    hits = browse_documents(top_k=100)
    assert any(h.document_id == d1 for h in hits)
