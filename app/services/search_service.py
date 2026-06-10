"""Hybrid search service combining SQLite FTS5 and ChromaDB."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

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


def _query_terms(query: str) -> list[str]:
    """Distinct meaningful (>1 char) word tokens from the query, lower-cased."""
    seen: list[str] = []
    for w in re.findall(r"\w+", query.lower()):
        if len(w) > 1 and w not in seen:
            seen.append(w)
    return seen


def _snippet(text: str, query: str, length: int = 220) -> str:
    if not text:
        return ""
    lt = text.lower()
    # Center the snippet on a match so the highlighted term is visible: prefer
    # the full phrase, then fall back to the first individual term that occurs.
    idx = lt.find(query.lower())
    if idx < 0:
        for w in _query_terms(query):
            j = lt.find(w)
            if j >= 0:
                idx = j
                break
    if idx < 0:
        return text[:length] + ("…" if len(text) > length else "")
    start = max(0, idx - length // 3)
    end = min(len(text), idx + length)
    pre = "…" if start > 0 else ""
    post = "…" if end < len(text) else ""
    return f"{pre}{text[start:end]}{post}"


def _lexical_score(text: str, query: str) -> float:
    """Lexical relevance in ~[0, 1.5]: fraction of query terms present in the
    chunk plus a bonus when the full phrase appears verbatim.

    Used as a tie-breaking boost on top of RRF so that results literally
    containing the searched term surface above purely-semantic neighbours —
    the user's "I searched X but the top hit has no X anywhere" complaint.
    """
    if not text:
        return 0.0
    lt = text.lower()
    terms = _query_terms(query)
    if not terms:
        return 0.0
    present = sum(1 for w in terms if w in lt)
    frac = present / len(terms)
    phrase = 0.5 if query.lower().strip() in lt else 0.0
    return frac + phrase


def _normalise_scores(hits: list[SearchHit]) -> None:
    """Rescale hit scores in place to 0..1 relative to the best hit."""
    top = max((h.score for h in hits), default=0.0)
    if top > 0:
        for h in hits:
            h.score = round(h.score / top, 4)


async def hybrid_search(
    query: str,
    *,
    top_k: int = 15,
    document_ids: list[int] | None = None,
    source_ids: list[int] | None = None,
    tags: list[str] | None = None,
    alpha: float = 0.55,  # weight on vector score vs. FTS
    rerank: bool = False,
    collapse_per_doc: bool = False,  # keep only the best chunk per document
    user: User | None = None,  # noqa: F821 — forward ref to avoid circular
) -> list[SearchHit]:
    """Run vector + FTS search and merge the results.

    With ``collapse_per_doc`` (used by the Search page) only the single
    highest-scoring chunk per document survives, so a term that appears many
    times in one PDF can't flood the list with near-identical cards and crowd
    out other documents. RAG retrieval leaves it off — it wants several chunks
    per document for context.
    """
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

    # --- Reciprocal Rank Fusion (RRF) -------------------------------------
    # Vector (cosine) and FTS (bm25) scores live on incomparable scales, so the
    # old linear `alpha*v + (1-alpha)*f` blend was unstable — and the FTS
    # normalisation `1 - score/max_bm` collapsed to 0 whenever results tied or a
    # single row came back, which is exactly why exact keyword matches sank to
    # the bottom. RRF fuses by *rank* instead: scale-free and robust. Each list
    # contributes 1/(K + rank); a result near the top of either list ranks high.
    K_RRF = 60
    combined: dict[int, dict[str, Any]] = {}
    for rank, r in enumerate(vector_results, start=1):
        try:
            cid = int(r["id"])
        except Exception:
            continue
        e = combined.setdefault(cid, {"rrf": 0.0})
        e["rrf"] += 1.0 / (K_RRF + rank)
        e["meta"] = r.get("metadata") or {}
        e["text"] = r.get("text") or ""

    for rank, (cid, _did, _score) in enumerate(fts_results, start=1):
        e = combined.setdefault(int(cid), {"rrf": 0.0})
        e["rrf"] += 1.0 / (K_RRF + rank)

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

            # RRF base fusion + a lexical boost so verbatim term matches win
            # ties against purely-semantic neighbours. Top-of-both-lists RRF is
            # ~0.033, so weighting the lexical signal (≤1.5) by ~0.02 makes a
            # full phrase match worth roughly one fusion rank — enough to lift
            # an exact hit without degenerating into pure keyword search.
            rrf = scores.get("rrf", 0.0)
            score = rrf + 0.02 * _lexical_score(chunk.text, query)
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
    if collapse_per_doc:
        seen_docs: set[int] = set()
        collapsed: list[SearchHit] = []
        for h in hits:
            if h.document_id in seen_docs:
                continue
            seen_docs.add(h.document_id)
            collapsed.append(h)
        hits = collapsed
    hits = hits[:top_k]

    # Normalise to 0..1 BEFORE reranking. The LLM reranker blends
    # ``0.6*model + 0.4*original`` assuming the original score is in [0, 1];
    # raw RRF values are ~0.03, which would otherwise wash the original signal
    # out entirely (and break its "can't do worse than baseline" guarantee).
    # Then renormalise the blended result so the displayed top hit is 1.0 and
    # the UI shows an intuitive relevance figure rather than raw ~0.0x values.
    _normalise_scores(hits)
    if rerank and len(hits) > 1:
        try:
            from app.services.reranker import rerank as _do_rerank

            hits = await _do_rerank(query, hits)
        except Exception as e:
            logger.warning("rerank pipeline failed: {}", e)
        _normalise_scores(hits)
    return hits


def document_facets(hits: list[SearchHit]) -> dict[str, Any]:
    """Count the distinct documents in ``hits`` by source, doc-type and tag.

    Powers the result-driven search facets: instead of a static dump of every
    source/tag/type in the library, the filter sidebar shows only what actually
    appears in the current results, each with a hit count. Returns::

        {"sources": [(source_id, name, count), ...],   # desc by count
         "doc_types": [(doc_type, count), ...],
         "tags": [(tag_name, count), ...],
         "meta": {doc_id: {"source_id", "source_name", "doc_type", "tags"}}}

    ``meta`` lets the caller filter the same hit set client-side (by source/
    type/tag) without re-querying. Counts are per distinct document.
    """
    from collections import Counter

    from app.models import DocumentSource

    doc_ids = list({h.document_id for h in hits})
    meta: dict[int, dict[str, Any]] = {}
    empty = {"sources": [], "doc_types": [], "tags": [], "meta": meta}
    if not doc_ids:
        return empty

    with session_scope() as session:
        docs = session.exec(select(Document).where(Document.id.in_(doc_ids))).all()  # type: ignore
        src_ids = {d.source_id for d in docs if d.source_id is not None}
        src_name: dict[int, str] = {}
        if src_ids:
            for srow in session.exec(
                select(DocumentSource).where(DocumentSource.id.in_(src_ids))  # type: ignore
            ).all():
                src_name[srow.id] = srow.name
        links = session.exec(
            select(DocumentTagLink).where(DocumentTagLink.document_id.in_(doc_ids))  # type: ignore
        ).all()
        tag_ids = {link.tag_id for link in links}
        tag_name: dict[int, str] = {}
        if tag_ids:
            for trow in session.exec(select(Tag).where(Tag.id.in_(tag_ids))).all():  # type: ignore
                tag_name[trow.id] = trow.name
        tags_by_doc: dict[int, set[str]] = {}
        for link in links:
            nm = tag_name.get(link.tag_id)
            if nm:
                tags_by_doc.setdefault(link.document_id, set()).add(nm)
        for d in docs:
            meta[d.id] = {
                "source_id": d.source_id,
                "source_name": src_name.get(d.source_id, ""),
                "doc_type": d.doc_type or "",
                "tags": tags_by_doc.get(d.id, set()),
            }

    src_counter: Counter = Counter()
    dt_counter: Counter = Counter()
    tag_counter: Counter = Counter()
    for did in doc_ids:
        m = meta.get(did)
        if not m:
            continue
        if m["source_id"] is not None:
            src_counter[m["source_id"]] += 1
        if m["doc_type"]:
            dt_counter[m["doc_type"]] += 1
        for tnm in m["tags"]:
            tag_counter[tnm] += 1

    sources = sorted(
        ((sid, src_name.get(sid, str(sid)), c) for sid, c in src_counter.items()),
        key=lambda x: (-x[2], x[1].lower()),
    )
    doc_types = sorted(dt_counter.items(), key=lambda x: (-x[1], x[0]))
    tags = sorted(tag_counter.items(), key=lambda x: (-x[1], x[0].lower()))
    return {"sources": sources, "doc_types": doc_types, "tags": tags, "meta": meta}


def browse_documents(
    *,
    user: User | None = None,  # noqa: F821 — forward ref to avoid circular
    source_ids: list[int] | None = None,
    tags: list[str] | None = None,
    doc_types: list[str] | None = None,
    top_k: int = 50,
) -> list[SearchHit]:
    """List documents matching metadata filters, newest first, as ``SearchHit``s.

    Used for query-less browsing from the Search page (e.g. clicking a tag chip
    links to ``/search?tag=X``). Returns one hit per document so the existing
    results renderer can show + open them; ``score`` is 0 since there's no
    ranking. ACL-filtered when ``user`` is given.
    """
    with session_scope() as session:
        stmt = select(Document)
        if user is not None:
            from app.auth.acl import filter_documents

            stmt = filter_documents(stmt, user)
        docs = list(session.exec(stmt).all())

        if source_ids:
            ss = {int(i) for i in source_ids}
            docs = [d for d in docs if d.source_id in ss]
        if doc_types:
            dd = set(doc_types)
            docs = [d for d in docs if d.doc_type in dd]
        if tags:
            tag_rows = session.exec(select(Tag).where(Tag.name.in_(tags))).all()  # type: ignore
            tag_ids = [t.id for t in tag_rows if t.id is not None]
            doc_ids_with_tag: set[int] = set()
            if tag_ids:
                links = session.exec(
                    select(DocumentTagLink).where(DocumentTagLink.tag_id.in_(tag_ids))  # type: ignore
                ).all()
                doc_ids_with_tag = {link.document_id for link in links}
            docs = [d for d in docs if d.id in doc_ids_with_tag]

        docs.sort(key=lambda d: d.id or 0, reverse=True)
        docs = docs[:top_k]
        if not docs:
            return []

        # Batch-fetch chunks for the selected docs; pick the first chunk per doc
        # for the snippet / open-at-page target.
        doc_ids = [d.id for d in docs if d.id is not None]
        chunks = session.exec(
            select(DocumentChunk).where(DocumentChunk.document_id.in_(doc_ids))  # type: ignore
        ).all()
        first_chunk: dict[int, DocumentChunk] = {}
        for c in chunks:
            cur = first_chunk.get(c.document_id)
            if cur is None or (c.page_from or 0) < (cur.page_from or 0):
                first_chunk[c.document_id] = c

        hits: list[SearchHit] = []
        for d in docs:
            fc = first_chunk.get(d.id)
            snippet = (fc.text[:220] if fc and fc.text else "") or f"{d.filename}"
            hits.append(
                SearchHit(
                    chunk_id=fc.id if fc else 0,
                    document_id=d.id,
                    filename=d.filename,
                    path=d.path,
                    page_from=fc.page_from if fc else 1,
                    page_to=fc.page_to if fc else 1,
                    snippet=snippet,
                    score=0.0,
                    source=getattr(fc.source, "value", str(fc.source)) if fc else "native_text",
                    tags=list(fc.tags or []) if fc else [],
                )
            )
        return hits
