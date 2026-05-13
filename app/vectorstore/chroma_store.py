"""ChromaDB persistent vector store wrapper."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Optional

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
    return client.get_or_create_collection(
        name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
    )


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
    where: Optional[dict[str, Any]] = None,
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


def reset_collection() -> None:
    client = get_chroma()
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
