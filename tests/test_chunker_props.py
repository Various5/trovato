"""Property-based tests for the chunker.

Skipped if ``hypothesis`` is not installed (it's a dev-only optional dep).
"""

from __future__ import annotations

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402

from app.ingestion.chunker import chunk_text, count_tokens  # noqa: E402


_text_strategy = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126, blacklist_characters=""),
    min_size=0,
    max_size=400,
).map(lambda s: " ".join(s.split()))


@settings(max_examples=40, deadline=2000, suppress_health_check=[HealthCheck.too_slow])
@given(
    pages=st.lists(
        st.tuples(st.integers(min_value=1, max_value=20), _text_strategy),
        min_size=0,
        max_size=8,
    ),
    chunk_tokens=st.integers(min_value=20, max_value=400),
    overlap=st.integers(min_value=0, max_value=30),
)
def test_chunker_invariants(pages: list[tuple[int, str]], chunk_tokens: int, overlap: int) -> None:
    if overlap >= chunk_tokens:
        return  # invalid combination — skip
    chunks = list(chunk_text(pages, chunk_tokens=chunk_tokens, overlap=overlap))
    for c in chunks:
        # Page span monotone
        assert c.page_from <= c.page_to
        # Pages come from the input
        used_pages = {p for p, _ in pages}
        assert c.page_from in used_pages
        assert c.page_to in used_pages
        # Text non-empty + token count reasonable
        assert c.text.strip()
        assert c.token_count > 0


def test_count_tokens_increasing() -> None:
    a = count_tokens("hello")
    b = count_tokens("hello hello hello hello")
    assert b >= a
