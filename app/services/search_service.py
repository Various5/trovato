"""Hybrid search service combining SQLite FTS5 and ChromaDB."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from sqlmodel import select

from app.database import session_scope
from app.database.engine import fts_search
from app.llm import get_client
from app.models import Document, DocumentChunk, DocumentTagLink, Tag
from app.utils.logging import logger
from app.vectorstore import similarity_search


@dataclass
class SearchHit:
    chunk_id: int
    document_id: int
    filename: str
    path: str
    page_from: int
    page_to: int
    snippet: str
    score: float
    source: str  # native_text | ocr_text | image_description | table
    tags: list[str]


def _snippet(text: str, query: str, length: int = 220) -> str:
    if not text:
        return ""
    lt = text.lower()
    q = query.lower()
    idx = lt.find(q)
    if idx < 0:
        return text[:length] + ("…" if len(text) > length else "")
    start = max(0, idx - length // 3)
    end = min(len(text), idx + length)
    pre = "…" if start > 0 else ""
    post = "…" if end < len(text) else ""
    return f"{pre}{text[start:end]}{post}"


async def hybrid_search(
    query: str,
    *,
    top_k: int = 15,
    document_ids: Optional[list[int]] = None,
    source_ids: Optional[list[int]] = None,
    tags: Optional[list[str]] = None,
    alpha: float = 0.55,  # weight on vector score vs. FTS
    rerank: bool = False,
    user: Optional["User"] = None,  # noqa: F821 — forward ref to avoid circular
) -> list[SearchHit]:
    """Run vector + FTS search and merge the results."""
    query = (query or "").strip()
    if not query:
        return []

    vector_results: list[dict[str, Any]] = []
    try:
        client = get_client()
        emb = await client.embed([query])
        if emb:
            where: dict[str, Any] | None = None
            if document_ids:
                where = {"document_id": {"$in": [int(i) for i in document_ids]}}
            elif source_ids:
                where = {"source_id": {"$in": [int(i) for i in source_ids]}}
            vector_results = similarity_search(emb[0], top_k=top_k * 2, where=where)
    except Exception as e:
        logger.warning("vector search failed: {}", e)

    fts_results = fts_search(query, limit=top_k * 2)

    # Combine
    combined: dict[int, dict[str, Any]] = {}
    for r in vector_results:
        try:
            cid = int(r["id"])
        except Exception:
            continue
        combined.setdefault(cid, {})["vector_score"] = float(r.get("score") or 0.0)
        combined[cid]["meta"] = r.get("metadata") or {}
        combined[cid]["text"] = r.get("text") or ""

    if fts_results:
        max_bm = max(score for _, _, score in fts_results) or 1.0
        for cid, _did, score in fts_results:
            combined.setdefault(cid, {})
            # BM25 lower is better → invert
            combined[cid]["fts_score"] = 1.0 - (score / max_bm)

    if not combined:
        return []

    chunk_ids = list(combined.keys())
    with session_scope() as session:
        chunks = session.exec(
            select(DocumentChunk).where(DocumentChunk.id.in_(chunk_ids))  # type: ignore
        ).all()
        chunk_by_id = {c.id: c for c in chunks}
        doc_ids_needed = {c.document_id for c in chunks}
        docs_stmt = select(Document).where(Document.id.in_(list(doc_ids_needed)))  # type: ignore
        if user is not None:
            from app.auth.acl import filter_documents

            docs_stmt = filter_documents(docs_stmt, user)
        docs = session.exec(docs_stmt).all()
        doc_by_id = {d.id: d for d in docs}

        tag_filter_ids: set[int] = set()
        if tags:
            tag_rows = session.exec(select(Tag).where(Tag.name.in_(tags))).all()  # type: ignore
            tag_ids = [t.id for t in tag_rows if t.id is not None]
            if tag_ids:
                links = session.exec(
                    select(DocumentTagLink).where(DocumentTagLink.tag_id.in_(tag_ids))  # type: ignore
                ).all()
                tag_filter_ids = {l.document_id for l in links}

        hits: list[SearchHit] = []
        for cid, scores in combined.items():
            chunk = chunk_by_id.get(cid)
            if not chunk:
                continue
            doc = doc_by_id.get(chunk.document_id)
            if not doc:
                continue
            if document_ids and doc.id not in set(document_ids):
                continue
            if source_ids and doc.source_id not in set(source_ids):
                continue
            if tag_filter_ids and doc.id not in tag_filter_ids:
                continue

            v = scores.get("vector_score", 0.0)
            f = scores.get("fts_score", 0.0)
            score = alpha * v + (1 - alpha) * f
            hits.append(
                SearchHit(
                    chunk_id=cid,
                    document_id=doc.id,
                    filename=doc.filename,
                    path=doc.path,
                    page_from=chunk.page_from,
                    page_to=chunk.page_to,
                    snippet=_snippet(chunk.text, query),
                    score=score,
                    source=getattr(chunk.source, "value", str(chunk.source)),
                    tags=list(chunk.tags or []),
                )
            )

    hits.sort(key=lambda h: h.score, reverse=True)
    hits = hits[:top_k]
    if rerank and len(hits) > 1:
        try:
            from app.services.reranker import rerank as _do_rerank

            hits = await _do_rerank(query, hits)
        except Exception as e:
            logger.warning("rerank pipeline failed: {}", e)
    return hits
