"""Tests for the tag-insights service (ranking, grouping, co-occurrence,
near-duplicate clustering, merge)."""

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
from app.services.tag_insights import (
    cooccurring,
    merge_tags,
    normalize_tag,
    tag_overview,
)


def _seed() -> dict[str, int]:
    init_db()
    with session_scope() as session:
        # The test DB is shared across the session — clear any prior run so the
        # unique tag names / content hashes don't collide on re-seed.
        for n in ("invoice", "Invoices", "lang:de", "finance"):
            for tg in session.exec(select(Tag).where(Tag.name == n)).all():
                for lk in session.exec(select(DocumentTagLink).where(DocumentTagLink.tag_id == tg.id)).all():
                    session.delete(lk)
                session.delete(tg)
        for d in session.exec(select(Document).where(Document.content_hash.like("ti%"))).all():
            session.delete(d)
        session.flush()
        src = DocumentSource(
            name="ti-src", type=SourceType.local, path="/tmp/ti", visibility=Visibility.shared
        )
        session.add(src)
        session.flush()
        docs = []
        for i in range(3):
            d = Document(
                source_id=src.id,
                path=f"/tmp/ti/{i}.pdf",
                filename=f"{i}.pdf",
                content_hash=f"ti{i}",
                status=DocumentStatus.indexed,
                visibility=Visibility.shared,
            )
            session.add(d)
            docs.append(d)
        session.flush()
        names = ["invoice", "Invoices", "lang:de", "finance"]
        tag = {}
        for n in names:
            t = Tag(name=n, auto=(":" in n))
            session.add(t)
            session.flush()
            tag[n] = t.id
        # invoice on all 3; finance + lang:de on d0,d1; Invoices on d0 only
        links = [
            ("invoice", 0),
            ("invoice", 1),
            ("invoice", 2),
            ("finance", 0),
            ("finance", 1),
            ("lang:de", 0),
            ("lang:de", 1),
            ("Invoices", 0),
        ]
        for n, di in links:
            session.add(DocumentTagLink(document_id=docs[di].id, tag_id=tag[n]))
        return tag


def test_normalize_folds_case_and_plural() -> None:
    assert normalize_tag("Invoices") == normalize_tag("invoice")
    assert normalize_tag("  Legal_Docs ") == "legal doc"
    assert normalize_tag("class") == "class"  # not over-stripped (…ss)


def test_overview_ranks_groups_and_finds_dups() -> None:
    _seed()
    ov = tag_overview()
    # The session-scoped DB may hold tags from other tests, so assert on our
    # seeded tags rather than absolute positions.
    topic = {s.name: s for s in ov["groups"]["topic"]}
    assert topic["invoice"].count == 3
    assert topic["finance"].count == 2
    # Ranked by count desc → invoice (3) sorts above finance (2).
    order = [s.name for s in ov["groups"]["topic"] if s.name in {"invoice", "finance"}]
    assert order == ["invoice", "finance"]
    # lang:de grouped under its prefix kind.
    assert "lang:de" in [s.name for s in ov["groups"].get("lang", [])]
    # near-duplicate cluster invoice/Invoices detected.
    dup_sets = [{s.name for s in grp} for grp in ov["dups"]]
    assert {"invoice", "Invoices"} in dup_sets
    # Related tags are TOPICS only — system tags (lang:de) are filtered out as noise.
    rel_names = {n for n, _ in topic["invoice"].related}
    assert "finance" in rel_names
    assert "lang:de" not in rel_names
    # Library-level topic pairs include the finance+invoice cluster.
    pair_keys = {frozenset(k) for k, _ in ov["pairs"]}
    assert frozenset({"invoice", "finance"}) in pair_keys


def test_cooccurring_orders_by_shared_docs() -> None:
    tag = _seed()
    rel = dict(cooccurring(tag["invoice"]))
    # finance & lang:de share 2 docs with invoice; Invoices shares 1
    assert rel.get("finance") == 2
    assert rel.get("lang:de") == 2
    assert rel.get("Invoices") == 1


def test_merge_reassigns_and_deletes() -> None:
    tag = _seed()
    removed = merge_tags(tag["invoice"], [tag["Invoices"]])
    assert removed == 1
    with session_scope() as session:
        assert session.get(Tag, tag["Invoices"]) is None
        # invoice still on its 3 distinct docs (no duplicate created for d0)
        n = len(session.exec(select(DocumentTagLink).where(DocumentTagLink.tag_id == tag["invoice"])).all())
    assert n == 3
