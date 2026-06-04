"""Tests for hybrid-search scoring: RRF fusion + lexical (exact-term) boost.

Regression guard for the user-reported bug "I search X, the top hit contains no
X anywhere": the old linear alpha-blend with a degenerate FTS normalisation let
purely-semantic neighbours outrank verbatim keyword matches. RRF + a lexical
boost must put the document that actually contains the term on top.
"""

from __future__ import annotations

from app.database import init_db, session_scope
from app.models import (
    ChunkSource,
    Document,
    DocumentSource,
    DocumentStatus,
    SourceType,
    Visibility,
)
from app.services import search_service
from app.services.search_service import (
    _lexical_score,
    _query_terms,
    _snippet,
    hybrid_search,
)


# --- pure helpers ---------------------------------------------------------


def test_query_terms_dedups_and_drops_single_chars() -> None:
    assert _query_terms("Grundriss a Grundriss Erdgeschoss") == ["grundriss", "erdgeschoss"]


def test_lexical_score_rewards_presence_and_phrase() -> None:
    # both terms present + full phrase verbatim → frac 1.0 + phrase 0.5
    assert _lexical_score("Der Grundriss Erdgeschoss zeigt…", "grundriss erdgeschoss") == 1.5
    # one of two terms present, no phrase → 0.5
    assert _lexical_score("nur grundriss hier", "grundriss erdgeschoss") == 0.5
    # nothing present → 0
    assert _lexical_score("etwas ganz anderes", "grundriss") == 0.0


def test_snippet_centers_on_a_matching_term_without_full_phrase() -> None:
    text = "a" * 400 + " grundriss " + "b" * 400
    snip = _snippet(text, "missing grundriss", length=120)
    assert "grundriss" in snip
    assert len(snip) < len(text)


# --- fusion (integration with mocked vector + FTS) ------------------------


def _seed_two_docs() -> tuple[int, int]:
    """Two indexed docs, one chunk each. Return (chunk_id_with_term, other)."""
    init_db()
    with session_scope() as session:
        src = DocumentSource(
            name="score-src",
            type=SourceType.local,
            path="/tmp/score",
            owner_id=None,
            visibility=Visibility.shared,
        )
        session.add(src)
        session.flush()
        d_term = Document(
            source_id=src.id,
            path="/tmp/score/term.pdf",
            filename="term.pdf",
            content_hash="score-term",
            status=DocumentStatus.indexed,
            page_count=1,
            visibility=Visibility.shared,
        )
        d_other = Document(
            source_id=src.id,
            path="/tmp/score/other.pdf",
            filename="other.pdf",
            content_hash="score-other",
            status=DocumentStatus.indexed,
            page_count=1,
            visibility=Visibility.shared,
        )
        session.add(d_term)
        session.add(d_other)
        session.flush()
        from app.models import DocumentChunk

        c_term = DocumentChunk(
            document_id=d_term.id,
            page_from=1,
            page_to=1,
            text="Der Grundriss des Erdgeschosses ist hier abgebildet.",
            source=ChunkSource.native_text,
            token_count=8,
        )
        c_other = DocumentChunk(
            document_id=d_other.id,
            page_from=1,
            page_to=1,
            text="Allgemeine Baubeschreibung ohne das gesuchte Wort.",
            source=ChunkSource.native_text,
            token_count=7,
        )
        session.add(c_term)
        session.add(c_other)
        session.flush()
        return c_term.id, c_other.id


async def test_exact_term_match_outranks_semantic_neighbour(monkeypatch) -> None:
    cid_term, cid_other = _seed_two_docs()

    class _FakeClient:
        async def embed(self, texts):
            return [[0.1, 0.2, 0.3]]

    # Vector search prefers the *other* chunk (no term) — simulating a close
    # semantic neighbour ranked first by embeddings.
    def _fake_similarity(emb, *, top_k=10, where=None):
        return [
            {"id": str(cid_other), "score": 0.95, "metadata": {}, "text": ""},
            {"id": str(cid_term), "score": 0.80, "metadata": {}, "text": ""},
        ]

    # FTS only matches the chunk that actually contains the term.
    def _fake_fts(query, limit=25):
        return [(cid_term, 0, -3.2)]

    monkeypatch.setattr(search_service, "get_client", lambda: _FakeClient())
    monkeypatch.setattr(search_service, "similarity_search", _fake_similarity)
    monkeypatch.setattr(search_service, "fts_search", _fake_fts)

    hits = await hybrid_search("grundriss", top_k=10)

    assert hits, "expected results"
    assert hits[0].chunk_id == cid_term, "verbatim term match must rank first"
    assert "Grundriss" in hits[0].snippet
    # Normalised display score: top hit is 1.0, runner-up strictly lower.
    assert hits[0].score == 1.0
    assert hits[1].score < 1.0
