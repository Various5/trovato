"""Near-duplicate detection using k-shingles + Jaccard similarity.

Fingerprints each document by hashing 5-word shingles of its first ~12 KB of
text and the last ~4 KB (to catch documents that share content but differ at
the edges). Pairwise Jaccard above ``threshold`` flags a pair as near-dup.

This is O(N²) in the worst case but cheap enough for a few thousand docs and
needs no extra dependencies. For larger corpora switch to MinHash + LSH.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlmodel import select

from app.database import session_scope
from app.models import Document, DocumentChunk

_WORD_RX = re.compile(r"\w+", re.UNICODE)


def _shingles(text: str, k: int = 5) -> set[int]:
    words = _WORD_RX.findall(text.lower())
    if len(words) < k:
        return set()
    return {hash(" ".join(words[i : i + k])) for i in range(len(words) - k + 1)}


def _fingerprint_from_texts(texts: list[str]) -> set[int]:
    text = "\n".join(texts)
    head = text[:12_000]
    tail = text[-4_000:] if len(text) > 16_000 else ""
    return _shingles(head) | _shingles(tail)


def _doc_fingerprint(document_id: int) -> set[int]:
    """Fingerprint a single document (kept for ad-hoc callers/tests).

    The batch path in :func:`find_near_duplicates` avoids the per-doc session.
    """
    with session_scope() as session:
        chunks = session.exec(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.page_from)
            .limit(60)
        ).all()
        return _fingerprint_from_texts([c.text for c in chunks])


def _jaccard(a: set[int], b: set[int]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


@dataclass
class NearDupPair:
    a_id: int
    a_filename: str
    b_id: int
    b_filename: str
    similarity: float


def find_near_duplicates(threshold: float = 0.7, *, limit_docs: int = 500) -> list[NearDupPair]:
    """Return pairs with Jaccard ≥ ``threshold``."""
    with session_scope() as session:
        docs = session.exec(select(Document).limit(limit_docs)).all()
        items = [(d.id, d.filename) for d in docs if d.id is not None]
        doc_ids = [did for did, _ in items]

        # Batch-fetch chunk text for every doc in one query instead of opening a
        # session per document (was up to `limit_docs` round-trips on each
        # Diagnostics load). Keep the first 60 chunks per doc, ordered by page.
        texts_by_doc: dict[int, list[str]] = {did: [] for did in doc_ids}
        if doc_ids:
            rows = session.exec(
                select(DocumentChunk.document_id, DocumentChunk.text)
                .where(DocumentChunk.document_id.in_(doc_ids))  # type: ignore[attr-defined]
                .order_by(DocumentChunk.document_id, DocumentChunk.page_from)
            ).all()
            for did, text in rows:
                bucket = texts_by_doc.get(did)
                if bucket is not None and len(bucket) < 60:
                    bucket.append(text or "")

    fingerprints: dict[int, set[int]] = {
        did: _fingerprint_from_texts(texts_by_doc.get(did, [])) for did in doc_ids
    }

    pairs: list[NearDupPair] = []
    for i, (a_id, a_name) in enumerate(items):
        a_fp = fingerprints.get(a_id) or set()
        if not a_fp:
            continue
        for b_id, b_name in items[i + 1 :]:
            b_fp = fingerprints.get(b_id) or set()
            if not b_fp:
                continue
            sim = _jaccard(a_fp, b_fp)
            if sim >= threshold:
                pairs.append(
                    NearDupPair(
                        a_id=a_id,
                        a_filename=a_name,
                        b_id=b_id,
                        b_filename=b_name,
                        similarity=sim,
                    )
                )
    pairs.sort(key=lambda p: p.similarity, reverse=True)
    return pairs
