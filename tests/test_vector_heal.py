"""Vector-store self-heal when the embedding model's dimension changes.

Switching the embedding model (e.g. bge-m3 @ 1024 → nomic-embed @ 768) leaves
Chroma rejecting every upsert/query because a collection pins its dimension at
first write. ``heal_vector_store_if_model_changed`` must detect that — via the
exact query path production uses — and rebuild.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.services.indexer as idx
import app.vectorstore as vs


class _FakeClient:
    def __init__(self, dim: int):
        self._dim = dim

    async def embed(self, texts):
        return [[0.0] * self._dim for _ in texts]


@pytest.fixture
def patched(monkeypatch):
    state = {"reset": 0, "meta": {}, "reembedded": 0}

    monkeypatch.setattr(idx, "get_settings", lambda: SimpleNamespace(embedding_model="nomic"))
    monkeypatch.setattr(vs, "collection_dim", lambda: 1024)

    def _reset():
        state["reset"] += 1

    def _write(model, dim):
        state["meta"] = {"model": model, "dim": dim}

    async def _reembed(controller=None):
        state["reembedded"] += 1
        return 5

    monkeypatch.setattr(vs, "reset_collection", _reset)
    monkeypatch.setattr(vs, "write_embed_meta", _write)
    monkeypatch.setattr(vs, "read_embed_meta", lambda: {"model": "bge-m3", "dim": 1024})
    monkeypatch.setattr(idx, "reembed_all_documents", _reembed)
    monkeypatch.setattr(idx, "get_client", lambda: _FakeClient(768))
    return state


async def test_heal_rebuilds_on_dimension_mismatch(patched, monkeypatch):
    # Non-empty collection; a 768-dim query is rejected → rebuild.
    monkeypatch.setattr(vs, "collection_size", lambda: 42)
    monkeypatch.setattr(vs, "query_dim_ok", lambda emb: False)

    n = await idx.heal_vector_store_if_model_changed()

    assert n == 5
    assert patched["reset"] == 1
    assert patched["reembedded"] == 1
    assert patched["meta"] == {"model": "nomic", "dim": 768}


async def test_heal_noop_when_query_compatible(patched, monkeypatch):
    monkeypatch.setattr(vs, "collection_size", lambda: 42)
    monkeypatch.setattr(vs, "query_dim_ok", lambda emb: True)

    n = await idx.heal_vector_store_if_model_changed()

    assert n == 0
    assert patched["reset"] == 0
    assert patched["reembedded"] == 0
    assert patched["meta"] == {"model": "nomic", "dim": 768}


async def test_heal_records_identity_on_fresh_collection(patched, monkeypatch):
    # Truly fresh collection: count 0 AND the probe query is accepted → no-op.
    monkeypatch.setattr(vs, "collection_size", lambda: 0)
    monkeypatch.setattr(vs, "query_dim_ok", lambda emb: True)

    n = await idx.heal_vector_store_if_model_changed()

    assert n == 0
    assert patched["reset"] == 0
    assert patched["meta"] == {"model": "nomic", "dim": 768}


async def test_heal_rebuilds_empty_but_dimension_pinned_collection(patched, monkeypatch):
    # The real bug: all vectors were deleted (count 0) but the collection kept
    # its dimension pinned, so queries still get rejected → must still rebuild.
    monkeypatch.setattr(vs, "collection_size", lambda: 0)
    monkeypatch.setattr(vs, "query_dim_ok", lambda emb: False)

    n = await idx.heal_vector_store_if_model_changed()

    assert n == 5
    assert patched["reset"] == 1
    assert patched["reembedded"] == 1
    assert patched["meta"] == {"model": "nomic", "dim": 768}


async def test_heal_never_wipes_on_inconclusive_error(patched, monkeypatch):
    # Unrelated Chroma error (not a dimension mismatch) → leave the index alone.
    monkeypatch.setattr(vs, "collection_size", lambda: 42)
    monkeypatch.setattr(vs, "query_dim_ok", lambda emb: None)

    n = await idx.heal_vector_store_if_model_changed()

    assert n == 0
    assert patched["reset"] == 0
    assert patched["reembedded"] == 0


async def test_heal_skips_when_lmstudio_unreachable(patched, monkeypatch):
    class _Down:
        async def embed(self, texts):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(idx, "get_client", lambda: _Down())
    monkeypatch.setattr(vs, "collection_size", lambda: 42)
    monkeypatch.setattr(vs, "query_dim_ok", lambda emb: False)

    n = await idx.heal_vector_store_if_model_changed()

    assert n == 0
    assert patched["reset"] == 0  # never wipe vectors on a transient probe failure
