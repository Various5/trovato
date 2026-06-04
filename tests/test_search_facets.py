"""Tests for result-driven search facets (document_facets).

The search sidebar is built from these counts so it reflects the actual result
set (per distinct document) rather than a static dump of the whole library.
"""

from __future__ import annotations

from app.database import init_db, session_scope
from app.models import (
    Document,
    DocumentSource,
    DocumentStatus,
    DocumentTagLink,
    SourceType,
    Tag,
    Visibility,
)
from app.services.search_service import document_facets


class _Hit:
    """Minimal stand-in — document_facets only reads .document_id."""

    def __init__(self, document_id: int) -> None:
        self.document_id = document_id


def _seed() -> tuple[int, int, int]:
    init_db()
    with session_scope() as session:
        sa = DocumentSource(
            name="facet-src-A", type=SourceType.local, path="/tmp/fa", visibility=Visibility.shared
        )
        sb = DocumentSource(
            name="facet-src-B", type=SourceType.local, path="/tmp/fb", visibility=Visibility.shared
        )
        session.add(sa)
        session.add(sb)
        session.flush()
        a1 = Document(
            source_id=sa.id,
            path="/tmp/fa/1.pdf",
            filename="1.pdf",
            content_hash="fa1",
            doc_type="invoice-x",
            status=DocumentStatus.indexed,
            visibility=Visibility.shared,
        )
        a2 = Document(
            source_id=sa.id,
            path="/tmp/fa/2.pdf",
            filename="2.pdf",
            content_hash="fa2",
            doc_type="invoice-x",
            status=DocumentStatus.indexed,
            visibility=Visibility.shared,
        )
        b1 = Document(
            source_id=sb.id,
            path="/tmp/fb/1.pdf",
            filename="b1.pdf",
            content_hash="fb1",
            doc_type="contract-x",
            status=DocumentStatus.indexed,
            visibility=Visibility.shared,
        )
        for d in (a1, a2, b1):
            session.add(d)
        session.flush()
        t_alpha = Tag(name="alpha-x", auto=True)
        t_beta = Tag(name="beta-x", auto=True)
        session.add(t_alpha)
        session.add(t_beta)
        session.flush()
        session.add(DocumentTagLink(document_id=a1.id, tag_id=t_alpha.id))
        session.add(DocumentTagLink(document_id=a2.id, tag_id=t_alpha.id))
        session.add(DocumentTagLink(document_id=b1.id, tag_id=t_beta.id))
        return a1.id, a2.id, b1.id


def test_facets_count_distinct_documents() -> None:
    a1, a2, b1 = _seed()
    # a1 appears twice (two chunk hits) but must count as ONE document.
    facets = document_facets([_Hit(a1), _Hit(a1), _Hit(a2), _Hit(b1)])

    sources = {name: c for _sid, name, c in facets["sources"]}
    assert sources == {"facet-src-A": 2, "facet-src-B": 1}

    assert dict(facets["doc_types"]) == {"invoice-x": 2, "contract-x": 1}
    assert dict(facets["tags"]) == {"alpha-x": 2, "beta-x": 1}

    # Sorted by count desc — the bigger source/type/tag comes first.
    assert facets["sources"][0][1] == "facet-src-A"
    assert facets["doc_types"][0] == ("invoice-x", 2)
    assert facets["tags"][0] == ("alpha-x", 2)

    # meta lets the UI filter client-side without re-querying.
    assert facets["meta"][a1]["doc_type"] == "invoice-x"
    assert "alpha-x" in facets["meta"][a1]["tags"]


def test_facets_empty_for_no_hits() -> None:
    init_db()
    facets = document_facets([])
    assert facets == {"sources": [], "doc_types": [], "tags": [], "meta": {}}
