"""Library-wide retrieval: broad-query detection, dynamic plan, and the
document-diversified context builder with one citation per document.
"""

from __future__ import annotations

from app.chat.rag import _build_context_block, _is_broad_query, _retrieval_plan
from app.services.search_service import SearchHit


def _hit(doc_id: int, page: int, snippet: str, *, source: str = "native_text") -> SearchHit:
    return SearchHit(
        chunk_id=doc_id * 100 + page,
        document_id=doc_id,
        filename=f"doc{doc_id}.pdf",
        path=f"/x/doc{doc_id}.pdf",
        page_from=page,
        page_to=page,
        snippet=snippet,
        score=1.0,
        source=source,
        tags=[],
    )


# --- intent ----------------------------------------------------------------


def test_broad_query_detection_en():
    assert _is_broad_query("which documents mention the budget")
    assert _is_broad_query("compare all the contracts")
    assert _is_broad_query("how many reports discuss safety")
    assert not _is_broad_query("what is the SIA norm 103")


def test_broad_query_detection_de():
    assert _is_broad_query("welche dokumente erwähnen den pool")
    assert _is_broad_query("vergleiche alle berichte")
    assert not _is_broad_query("wo wird die SIA Norm 103 aufgeführt")


def test_retrieval_plan_widens_for_broad():
    # Broad queries widen the candidate pool but keep a 2-chunk-per-doc floor so
    # a cited document's answer chunk isn't dropped (was 1, which lost answers).
    eff, per_doc = _retrieval_plan("which documents have a pool", 15)
    assert eff >= 40 and per_doc == 2
    eff2, per_doc2 = _retrieval_plan("where is the pool plan", 15)
    assert eff2 == 15 and per_doc2 == 3


# --- document-diversified context + citations ------------------------------


def test_per_chunk_citation_carries_its_own_page():
    hits = [
        _hit(1, 3, "pool A"),
        _hit(1, 15, "pool B"),
        _hit(1, 22, "pool C"),
        _hit(2, 1, "meadow"),
    ]
    block, cites = _build_context_block(hits, max_chars=10000, max_per_doc=3)
    # ONE citation per CHUNK so each links to its OWN page (the user wants a
    # citation click to land on the exact page the fact came from, not p.1).
    assert [c.n for c in cites] == [1, 2, 3, 4]
    assert [c.document_id for c in cites] == [1, 1, 1, 2]
    assert [c.page_from for c in cites] == [3, 15, 22, 1]
    # Document's chunks get consecutive numbers; the file is named per source.
    assert block.count("doc1.pdf") == 3
    assert block.count("doc2.pdf") == 1


def test_max_per_doc_caps_chunks_for_breadth():
    hits = [_hit(1, p, f"chunk {p}") for p in range(1, 6)] + [_hit(2, 1, "other")]
    block, cites = _build_context_block(hits, max_chars=10000, max_per_doc=1)
    # Only the best chunk of doc 1 survives the cap → its own page, page 1.
    assert cites[0].page_from == 1 and cites[0].page_to == 1
    assert "chunk 1" in block and "chunk 2" not in block
    # doc 1 (capped to 1) + doc 2 → exactly two citations.
    assert [c.n for c in cites] == [1, 2]


def test_image_label_preserved_in_grouped_block():
    block, _ = _build_context_block([_hit(1, 4, "a pool", source="image_description")])
    assert "IMAGE" in block


def test_context_uses_full_chunk_text_not_snippet():
    # RAG-1 regression: the model must receive the full chunk body, not the
    # ~220-char UI highlight preview — feeding the snippet caused "correct
    # source cited as [1] but the answer is wrong/missing".
    h = _hit(1, 1, "short highlight preview")
    h.text = "FULL CHUNK BODY containing the actual answer the model needs"
    block, _ = _build_context_block([h], max_chars=10000, max_per_doc=3)
    assert "FULL CHUNK BODY containing the actual answer" in block
    assert "short highlight preview" not in block


def test_context_falls_back_to_snippet_when_text_missing():
    # Hits without a populated `text` (e.g. browse results) still render.
    block, _ = _build_context_block([_hit(1, 1, "only the snippet")], max_chars=10000)
    assert "only the snippet" in block
