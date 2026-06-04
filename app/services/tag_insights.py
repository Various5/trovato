"""Tag insights — the "clever" logic behind the Tags page.

Pure analysis over the tag ↔ document graph:

* **frequency ranking + denoise** — tags ranked by how many documents carry
  them; one-off tags can be folded away so the meaningful ones stand out;
* **group by kind** — system/auto tags that use a ``prefix:`` convention
  (``lang:``, ``has:``, ``type:`` …) are grouped separately from free topics;
* **related / co-occurring tags** — for each tag, the tags that most often
  appear on the *same* documents;
* **near-duplicate detection** — tags whose names normalise to the same key
  (``invoice`` / ``Invoices`` / ``invoice``) are clustered so they can be merged.

Only :func:`merge_tags` mutates data, and it does so through the serialized
``write_session`` writer (see the DB write-serialization invariant).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import select

from app.database import session_scope, write_session
from app.models import DocumentTagLink, Tag


@dataclass
class TagStat:
    id: int
    name: str
    count: int
    auto: bool
    kind: str
    related: list[tuple[str, int]] = field(default_factory=list)


def _kind(name: str) -> str:
    """The grouping key: the ``prefix:`` for system tags, else ``"topic"``."""
    if ":" in name:
        prefix = name.split(":", 1)[0].strip().lower()
        if prefix:
            return prefix
    return "topic"


def normalize_tag(name: str) -> str:
    """Fold a tag name to a comparison key for near-duplicate detection:
    lower-case, collapse separators, and drop a trailing plural ``s``."""
    s = (name or "").strip().lower()
    s = re.sub(r"[\s_\-]+", " ", s).strip()
    if len(s) > 3 and s.endswith("s") and not s.endswith("ss"):
        s = s[:-1]
    return s


def tag_overview(*, related_limit: int = 6) -> dict[str, Any]:
    """Return ranked, grouped tag stats + near-duplicate clusters.

    Shape::

        {"groups": {kind: [TagStat, ...]},   # each list ranked by count desc
         "dups": [[TagStat, ...], ...],       # near-duplicate clusters (size>1)
         "total": int}
    """
    with session_scope() as session:
        tags = session.exec(select(Tag)).all()
        links = session.exec(select(DocumentTagLink)).all()

    name_by_id = {t.id: t.name for t in tags}
    docs_by_tag: dict[int, set[int]] = defaultdict(set)
    tags_by_doc: dict[int, set[int]] = defaultdict(set)
    for link in links:
        docs_by_tag[link.tag_id].add(link.document_id)
        tags_by_doc[link.document_id].add(link.tag_id)

    cooc: dict[int, Counter] = defaultdict(Counter)
    for tids in tags_by_doc.values():
        for a in tids:
            for b in tids:
                if a != b:
                    cooc[a][b] += 1

    stats: list[TagStat] = []
    for t in tags:
        related = [
            (name_by_id[bid], n) for bid, n in cooc[t.id].most_common(related_limit) if bid in name_by_id
        ]
        stats.append(
            TagStat(
                id=t.id,
                name=t.name,
                count=len(docs_by_tag.get(t.id, ())),
                auto=bool(t.auto),
                kind=_kind(t.name),
                related=related,
            )
        )

    stats.sort(key=lambda s: (-s.count, s.name.lower()))

    groups: dict[str, list[TagStat]] = defaultdict(list)
    for s in stats:
        groups[s.kind].append(s)

    norm_map: dict[str, list[TagStat]] = defaultdict(list)
    for s in stats:
        norm_map[normalize_tag(s.name)].append(s)
    dups = [sorted(grp, key=lambda s: -s.count) for grp in norm_map.values() if len(grp) > 1]
    dups.sort(key=lambda grp: -sum(s.count for s in grp))

    return {"groups": dict(groups), "dups": dups, "total": len(tags)}


def cooccurring(tag_id: int, *, limit: int = 8) -> list[tuple[str, int]]:
    """Tags that most often appear on the same documents as ``tag_id``."""
    with session_scope() as session:
        my_docs = {
            link.document_id
            for link in session.exec(
                select(DocumentTagLink).where(DocumentTagLink.tag_id == tag_id)  # type: ignore
            ).all()
        }
        if not my_docs:
            return []
        others = session.exec(
            select(DocumentTagLink).where(DocumentTagLink.document_id.in_(my_docs))  # type: ignore
        ).all()
        counter: Counter = Counter()
        for link in others:
            if link.tag_id != tag_id:
                counter[link.tag_id] += 1
        if not counter:
            return []
        names = {
            t.id: t.name
            for t in session.exec(select(Tag).where(Tag.id.in_(set(counter)))).all()  # type: ignore
        }
    return [(names[tid], n) for tid, n in counter.most_common(limit) if tid in names]


def merge_tags(canonical_id: int, other_ids: list[int]) -> int:
    """Merge ``other_ids`` into ``canonical_id``: re-point their document links
    to the canonical tag (dropping links that would duplicate), then delete the
    merged tags. Returns the number of tags removed.

    Destructive — goes through the serialized writer.
    """
    other_ids = [oid for oid in other_ids if oid != canonical_id]
    if not other_ids:
        return 0
    removed = 0
    with write_session() as session:
        existing_docs = {
            link.document_id
            for link in session.exec(
                select(DocumentTagLink).where(DocumentTagLink.tag_id == canonical_id)  # type: ignore
            ).all()
        }
        for oid in other_ids:
            for link in session.exec(
                select(DocumentTagLink).where(DocumentTagLink.tag_id == oid)  # type: ignore
            ).all():
                if link.document_id in existing_docs:
                    session.delete(link)  # would duplicate the canonical link
                else:
                    link.tag_id = canonical_id
                    existing_docs.add(link.document_id)
                    session.add(link)
            tag = session.get(Tag, oid)
            if tag is not None:
                session.delete(tag)
                removed += 1
    return removed
