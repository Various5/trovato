"""ChromaDB persistent vector store wrapper."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.utils.logging import logger

COLLECTION_NAME = "documents"


@lru_cache(maxsize=1)
def get_chroma():
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    s = get_settings()
    client = chromadb.PersistentClient(
        path=str(s.chroma_path),
        settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
    )
    return client


def _get_or_create_collection():
    client = get_chroma()
    return client.get_or_create_collection(name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"})


def ensure_ready() -> None:
    """Initialize the Chroma client + collection once, single-threaded.

    Call this before fanning indexing work out across worker threads: the very
    first ``PersistentClient`` construction validates the tenant/database, and
    several threads hitting that init simultaneously can race with
    "Could not connect to tenant default_tenant". Warming it once up front means
    the workers only ever upsert into an already-live collection.
    """
    try:
        _get_or_create_collection()
    except Exception as e:
        logger.warning("chroma warm-up failed: {}", e)


def add_chunks(
    *,
    ids: list[str],
    embeddings: list[list[float]],
    documents: list[str],
    metadatas: list[dict[str, Any]],
) -> None:
    if not ids:
        return
    if not (len(ids) == len(embeddings) == len(documents) == len(metadatas)):
        raise ValueError("ids/embeddings/documents/metadatas length mismatch")
    col = _get_or_create_collection()
    col.upsert(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)


def delete_for_document(document_id: int) -> None:
    col = _get_or_create_collection()
    try:
        col.delete(where={"document_id": document_id})
    except Exception as e:
        logger.debug("chroma delete skipped: {}", e)


def similarity_search(
    query_embedding: list[float],
    *,
    top_k: int = 10,
    where: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    col = _get_or_create_collection()
    try:
        res = col.query(query_embeddings=[query_embedding], n_results=top_k, where=where)
    except Exception as e:
        logger.warning("chroma query failed: {}", e)
        return []
    out: list[dict[str, Any]] = []
    ids = res.get("ids", [[]])[0]
    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    dists = res.get("distances", [[]])[0]
    for i, _id in enumerate(ids):
        out.append(
            {
                "id": _id,
                "text": docs[i] if i < len(docs) else "",
                "metadata": metas[i] if i < len(metas) else {},
                "distance": dists[i] if i < len(dists) else None,
                "score": 1.0 - float(dists[i]) if i < len(dists) and dists[i] is not None else 0.0,
            }
        )
    return out


def collection_size() -> int:
    try:
        return _get_or_create_collection().count()
    except Exception:
        return 0


def collection_dim() -> int | None:
    """Dimension of the vectors currently stored in the collection, or ``None``
    if it's empty / can't be determined. A Chroma collection pins its dimension
    at first write, so this is how we detect that the embedding model changed
    under us (e.g. bge-m3 @ 1024 → nomic-embed @ 768) and the vectors no longer
    match what the model now produces."""
    try:
        col = _get_or_create_collection()
        # Explicitly request embeddings — peek() doesn't include them on every
        # Chroma version, which would make an occupied collection look empty.
        res = col.get(limit=1, include=["embeddings"])
        embs = res.get("embeddings")
        if embs is not None and len(embs) and embs[0] is not None:
            return len(embs[0])
    except Exception as e:
        logger.debug("collection_dim probe failed: {}", e)
    return None


def _meta_path() -> Path:
    return Path(get_settings().chroma_path) / "embed_meta.json"


def read_embed_meta() -> dict[str, Any]:
    """The embedding model + dimension the current vectors were built with."""
    try:
        return json.loads(_meta_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_embed_meta(model: str, dim: int) -> None:
    try:
        p = _meta_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"model": model, "dim": dim}), encoding="utf-8")
    except Exception as e:
        logger.debug("write_embed_meta failed: {}", e)


def reset_collection() -> None:
    client = get_chroma()
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    # Recreate eagerly so the next writer upserts into a live, empty collection
    # at the new dimension instead of racing on first-use creation.
    try:
        _get_or_create_collection()
    except Exception as e:
        logger.debug("reset_collection recreate skipped: {}", e)
