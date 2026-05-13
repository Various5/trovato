from app.services.near_dup import _jaccard, _shingles


def test_shingles_and_jaccard() -> None:
    a = "the quick brown fox jumps over the lazy dog"
    b = "the quick brown fox jumps over the lazy cat"
    sa = _shingles(a, k=3)
    sb = _shingles(b, k=3)
    assert sa and sb
    j = _jaccard(sa, sb)
    assert 0.5 < j < 1.0


def test_shingles_short_text() -> None:
    assert _shingles("two words", k=5) == set()
    assert _jaccard(set(), {1, 2}) == 0.0
