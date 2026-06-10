"""backfill_tag_quality() cleans pre-existing noisy auto-tags in place."""

from __future__ import annotations

from sqlmodel import select

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
from app.services.indexer import backfill_tag_quality


def _seed_legacy_tags() -> int:
    init_db()
    with session_scope() as session:
        for n in ("rechnung", "has:dates", "finanzen", "sensitive:finanzen", "echtes-thema"):
            for tg in session.exec(select(Tag).where(Tag.name == n)).all():
                for lk in session.exec(select(DocumentTagLink).where(DocumentTagLink.tag_id == tg.id)).all():
                    session.delete(lk)
                session.delete(tg)
        for d in session.exec(select(Document).where(Document.content_hash.like("bf%"))).all():
            session.delete(d)
        session.flush()
        src = DocumentSource(
            name="bf-src", type=SourceType.local, path="/tmp/bf", visibility=Visibility.shared
        )
        session.add(src)
        session.flush()
        doc = Document(
            source_id=src.id,
            path="/tmp/bf/0.pdf",
            filename="0.pdf",
            content_hash="bf0",
            status=DocumentStatus.indexed,
            visibility=Visibility.shared,
        )
        session.add(doc)
        session.flush()
        # 'echtes-thema' is a genuine user topic (auto=False) and must survive.
        for name, auto in (
            ("rechnung", True),
            ("has:dates", True),
            ("finanzen", True),
            ("echtes-thema", False),
        ):
            tag = Tag(name=name, auto=auto)
            session.add(tag)
            session.flush()
            session.add(DocumentTagLink(document_id=doc.id, tag_id=tag.id, auto=auto))
        return doc.id


def test_backfill_cleans_legacy_tags() -> None:
    did = _seed_legacy_tags()
    backfill_tag_quality()
    with session_scope() as session:
        names = {t.name for t in session.exec(select(Tag)).all()}
        # Redundant doc-type tag + near-universal flag are removed.
        assert "rechnung" not in names
        assert "has:dates" not in names
        # Bare sensitivity tag is namespaced.
        assert "finanzen" not in names
        assert "sensitive:finanzen" in names
        # A genuine user topic is untouched.
        assert "echtes-thema" in names
        # The namespaced sensitivity tag still links the document (link re-pointed).
        sens = session.exec(select(Tag).where(Tag.name == "sensitive:finanzen")).first()
        assert sens is not None
        links = session.exec(select(DocumentTagLink).where(DocumentTagLink.tag_id == sens.id)).all()
        assert any(link.document_id == did for link in links)


def test_backfill_is_idempotent() -> None:
    _seed_legacy_tags()
    first = backfill_tag_quality()
    second = backfill_tag_quality()
    assert first >= 3  # rechnung dropped, has:dates dropped, finanzen renamed
    assert second == 0  # nothing left to clean
