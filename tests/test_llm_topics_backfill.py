"""backfill_llm_topics attaches LLM subject tags and respects only_missing."""

from __future__ import annotations

from sqlmodel import select

from app.database import init_db, session_scope
from app.models import (
    Document,
    DocumentChunk,
    DocumentSource,
    DocumentStatus,
    DocumentTagLink,
    SourceType,
    Tag,
    Visibility,
)
from app.services import indexer


class _FakeClient:
    async def chat(self, messages, **kw) -> str:
        return '["bautechnik", "kostenplanung"]'


def _cleanup() -> None:
    init_db()
    with session_scope() as s:
        # No ON DELETE CASCADE + no ORM relationship → delete children first and
        # flush so the FK-checked statements run in the right order.
        tags = [
            tg
            for n in ("bautechnik", "kostenplanung", "vorhanden")
            for tg in s.exec(select(Tag).where(Tag.name == n)).all()
        ]
        tag_ids = [tg.id for tg in tags]
        if tag_ids:
            for lk in s.exec(select(DocumentTagLink).where(DocumentTagLink.tag_id.in_(tag_ids))).all():
                s.delete(lk)
            s.flush()
            for tg in tags:
                s.delete(tg)
            s.flush()
        for d in s.exec(select(Document).where(Document.content_hash.like("llmbf%"))).all():
            for lk in s.exec(select(DocumentTagLink).where(DocumentTagLink.document_id == d.id)).all():
                s.delete(lk)
            for ch in s.exec(select(DocumentChunk).where(DocumentChunk.document_id == d.id)).all():
                s.delete(ch)
            s.flush()
            s.delete(d)


def _seed_doc(content_hash: str, *, with_topic: bool = False) -> int:
    init_db()
    with session_scope() as s:
        src = s.exec(select(DocumentSource).where(DocumentSource.name == "llm-src")).first()
        if not src:
            src = DocumentSource(
                name="llm-src", type=SourceType.local, path="/tmp/llm", visibility=Visibility.shared
            )
            s.add(src)
            s.flush()
        d = Document(
            source_id=src.id,
            path=f"/tmp/llm/{content_hash}.pdf",
            filename=f"{content_hash}.pdf",
            content_hash=content_hash,
            status=DocumentStatus.indexed,
            visibility=Visibility.shared,
            language="de",
        )
        s.add(d)
        s.flush()
        s.add(
            DocumentChunk(
                document_id=d.id,
                text="Langer Text über Baukosten und Planung im Bauwesen. " * 8,
                page_from=1,
                page_to=1,
            )
        )
        if with_topic:
            tag = Tag(name="vorhanden", auto=False)
            s.add(tag)
            s.flush()
            s.add(DocumentTagLink(document_id=d.id, tag_id=tag.id, auto=False))
        return d.id


def _topics_of(did: int) -> set[str]:
    with session_scope() as s:
        rows = s.exec(
            select(Tag.name)
            .join(DocumentTagLink, DocumentTagLink.tag_id == Tag.id)
            .where(DocumentTagLink.document_id == did)
        ).all()
    return set(rows)


async def test_backfill_attaches_topics(monkeypatch) -> None:
    _cleanup()
    did = _seed_doc("llmbf0")
    monkeypatch.setattr("app.llm.get_client", lambda: _FakeClient())
    n = await indexer.backfill_llm_topics()
    assert n >= 1
    assert {"bautechnik", "kostenplanung"} <= _topics_of(did)
    _cleanup()


async def test_backfill_skips_already_topiced(monkeypatch) -> None:
    _cleanup()
    did = _seed_doc("llmbf1", with_topic=True)
    monkeypatch.setattr("app.llm.get_client", lambda: _FakeClient())
    await indexer.backfill_llm_topics()
    # The doc already had a topic ("vorhanden") so only_missing skips it.
    assert _topics_of(did) == {"vorhanden"}
    _cleanup()
