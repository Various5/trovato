from app.ingestion.chunker import chunk_text, count_tokens


def test_count_tokens_nonzero() -> None:
    assert count_tokens("hello world") > 0


def test_chunker_respects_pages() -> None:
    pages = [(1, "alpha " * 200), (2, "beta " * 200), (3, "gamma " * 200)]
    chunks = list(chunk_text(pages, chunk_tokens=120, overlap=20))
    assert chunks, "should produce at least one chunk"
    for c in chunks:
        assert c.page_from <= c.page_to
        assert c.text.strip()


def test_chunker_empty_input() -> None:
    assert list(chunk_text([])) == []
    assert list(chunk_text([(1, "")])) == []
