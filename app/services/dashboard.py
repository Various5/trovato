"""Aggregations for the dashboard's mini charts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import select

from app.database import session_scope
from app.models import Document, DocumentSource


def docs_indexed_per_day(days: int = 14) -> list[tuple[str, int]]:
    """Return ``[(date_str, doc_count), …]`` for the past ``days`` days,
    oldest first. Used to render the dashboard's small column chart."""
    today = datetime.now(UTC).date()
    buckets: dict[str, int] = {}
    for i in range(days):
        d = today - timedelta(days=days - 1 - i)
        buckets[d.isoformat()] = 0
    with session_scope() as session:
        docs = session.exec(select(Document).where(Document.indexed_at.is_not(None))).all()  # type: ignore
        for doc in docs:
            if not doc.indexed_at:
                continue
            d = doc.indexed_at.date().isoformat()
            if d in buckets:
                buckets[d] += 1
    return list(buckets.items())


def docs_per_source(limit: int = 8) -> list[tuple[str, int]]:
    """Return ``[(source_name, doc_count), …]`` for the top sources, ordered
    by count descending."""
    with session_scope() as session:
        sources = session.exec(select(DocumentSource)).all()
        out: list[tuple[str, int]] = []
        for src in sources:
            cnt = len(session.exec(select(Document).where(Document.source_id == src.id)).all())
            out.append((src.name, cnt))
    out.sort(key=lambda x: x[1], reverse=True)
    return out[:limit]


def doc_types_breakdown(limit: int = 6) -> list[tuple[str, int]]:
    """Top auto-classified doc types (rechnung, vertrag, …)."""
    counts: dict[str, int] = {}
    with session_scope() as session:
        docs = session.exec(select(Document)).all()
        for d in docs:
            key = d.doc_type or "unclassified"
            counts[key] = counts.get(key, 0) + 1
    rows = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return rows[:limit]


def overview() -> dict[str, Any]:
    return {
        "per_day": docs_indexed_per_day(),
        "per_source": docs_per_source(),
        "doc_types": doc_types_breakdown(),
    }
