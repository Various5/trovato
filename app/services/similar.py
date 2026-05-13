"""Find documents similar to a given one using the existing embeddings.

Strategy: take a handful of representative chunks (first, middle, last) of the
source document, embed-query each, aggregate hits by ``document_id``, and rank
the resulting docs by accumulated similarity score.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlmodel import select

from app.database import session_scope
from app.llm import get_client
from app.models import Document, DocumentChunk
from app.vectorstore import similarity_search


@dataclass
class SimilarHit:
    document_id: int
    filename: str
    path: str
    score: float
    matched_chunks: int


def _pick_representatives(chunks: list[DocumentChunk], limit: int = 5) -> list[str]:
    """Pick up to ``limit`` representative chunks: first, last, and a few from
    the middle, preferring longer texts."""
    if not chunks:
        return []
    if len(chunks) <= limit:
        return [c.text for c in chunks if c.text.strip()]
    indices = {0, len(chunks) - 1}
    step = max(1, len(chunks) // (limit - 1))
    for i in range(step, len(chunks) - 1, step):
        indices.add(i)
        if len(indices) >= limit:
            break
    picked = [chunks[i] for i in sorted(indices)]
    return [c.text for c in picked if c.text.strip()][:limit]


async def find_similar(document_id: int, *, top_k: int = 10) -> list[SimilarHit]:
    with session_scope() as session:
        doc = session.get(Document, document_id)
        if not doc:
            return []
        chunks = session.exec(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.page_from)
        ).all()

    samples = _pick_representatives(chunks)
    if not samples:
        return []

    client = get_client()
    try:
        vectors = await client.embed(samples)
    except Exception:
        return []

    aggregated: dict[int, dict[str, Any]] = defaultdict(lambda: {"score": 0.0, "count": 0})
    for vec in vectors:
        hits = similarity_search(vec, top_k=top_k * 3)
        for h in hits:
            meta = h.get("metadata") or {}
            did = meta.get("document_id")
            if not did or did == document_id:
                continue
            aggregated[int(did)]["score"] += float(h.get("score") or 0.0)
            aggregated[int(did)]["count"] += 1

    if not aggregated:
        return []

    with session_scope() as session:
        docs = session.exec(
            select(Document).where(Document.id.in_(list(aggregated.keys())))  # type: ignore
        ).all()
        by_id = {d.id: d for d in docs}

    out: list[SimilarHit] = []
    for did, info in aggregated.items():
        d = by_id.get(did)
        if not d:
            continue
        out.append(
            SimilarHit(
                document_id=did,
                filename=d.filename,
                path=d.path,
                score=info["score"] / max(1, info["count"]),
                matched_chunks=info["count"],
            )
        )
    out.sort(key=lambda h: (h.score, h.matched_chunks), reverse=True)
    return out[:top_k]
