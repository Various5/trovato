"""Property-based tests for the chunker.

Skipped if ``hypothesis`` is not installed (it's a dev-only optional dep).
"""

from __future__ import annotations

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st

from app.ingestion.chunker import chunk_text, count_tokens  # noqa: E402

_text_strategy = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126, blacklist_characters=""),
    min_size=0,
    max_size=400,
).map(lambda s: " ".join(s.split()))


@st.composite
def _sorted_pages(draw) -> list[tuple[int, str]]:
    """Pages are always supplied in increasing page-number order by real callers
    (the PDF extractor yields page 1, 2, 3, …). Encode that as an invariant in
    the strategy itself."""
    n = draw(st.integers(min_value=0, max_value=8))
    if n == 0:
        return []
    page_numbers = sorted(
        draw(st.lists(st.integers(min_value=1, max_value=20), min_size=n, max_size=n, unique=True))
    )
    return [(p, draw(_text_strategy)) for p in page_numbers]


@settings(max_examples=40, deadline=2000, suppress_health_check=[HealthCheck.too_slow])
@given(
    pages=_sorted_pages(),
    chunk_tokens=st.integers(min_value=20, max_value=400),
    overlap=st.integers(min_value=0, max_value=30),
)
def test_chunker_invariants(pages: list[tuple[int, str]], chunk_tokens: int, overlap: int) -> None:
    if overlap >= chunk_tokens:
        return  # invalid combination — skip
    chunks = list(chunk_text(pages, chunk_tokens=chunk_tokens, overlap=overlap))
    used_pages = {p for p, _ in pages}
    for c in chunks:
        # Page span monotone (input is sorted ascending)
        assert c.page_from <= c.page_to
        # Pages come from the input
        assert c.page_from in used_pages
        assert c.page_to in used_pages
        # Text non-empty + token count reasonable
        assert c.text.strip()
        assert c.token_count > 0


def test_count_tokens_increasing() -> None:
    a = count_tokens("hello")
    b = count_tokens("hello hello hello hello")
    assert b >= a
