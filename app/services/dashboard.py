"""Aggregations for the dashboard's mini charts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func
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
        # One grouped count instead of a query (and full materialisation) per source.
        rows = session.exec(
            select(Document.source_id, func.count()).group_by(Document.source_id)  # type: ignore[arg-type]
        ).all()
        counts = dict(rows)
        out = [(src.name, counts.get(src.id, 0)) for src in sources]
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


def wow_trend() -> dict[str, Any]:
    """Week-over-week comparison: documents indexed this week vs prior week."""
    today = datetime.now(UTC).date()
    this_week_start = today - timedelta(days=6)
    prev_week_start = today - timedelta(days=13)
    prev_week_end = today - timedelta(days=7)
    this_week = 0
    prev_week = 0
    most_active: tuple[str, int] = ("", 0)
    with session_scope() as session:
        docs = session.exec(select(Document).where(Document.indexed_at.is_not(None))).all()  # type: ignore[attr-defined]
        per_day: dict[str, int] = {}
        for d in docs:
            if not d.indexed_at:
                continue
            day = d.indexed_at.date()
            per_day[day.isoformat()] = per_day.get(day.isoformat(), 0) + 1
            if this_week_start <= day <= today:
                this_week += 1
            elif prev_week_start <= day <= prev_week_end:
                prev_week += 1
        for k, v in per_day.items():
            if v > most_active[1]:
                most_active = (k, v)
    pct_change: float | None
    if prev_week == 0:
        pct_change = None if this_week == 0 else 100.0
    else:
        pct_change = ((this_week - prev_week) / prev_week) * 100.0
    return {
        "this_week": this_week,
        "prev_week": prev_week,
        "pct_change": pct_change,
        "most_active_day": most_active[0],
        "most_active_count": most_active[1],
    }


def overview() -> dict[str, Any]:
    return {
        "per_day": docs_indexed_per_day(),
        "per_source": docs_per_source(),
        "doc_types": doc_types_breakdown(),
        "trend": wow_trend(),
    }
