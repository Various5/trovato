"""Tests for the FTS5 query builder + search.

The keyword half of hybrid search used to pass the raw query to ``MATCH``, which
FTS5 reads as an implicit AND of every token — so a natural-language question
matched nothing and exact terms like "SIA Norm 103" never surfaced. The builder
now ORs quoted tokens so bm25 ranks the rarest matched terms to the top.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.database import init_db
from app.database.engine import build_fts_match, fts_insert, fts_search


def test_build_fts_match_ors_quoted_terms() -> None:
    q = build_fts_match("in welchem dokument wird die sia norm 103 aufgeführt")
    # Every token is quoted and OR-joined (so it's ANY-match, not implicit AND).
    assert '"sia" OR "norm" OR "103"' in q
    assert " OR " in q and " AND " not in q
    assert q.startswith('"in"')  # tokens kept verbatim (len>1), in order


def test_build_fts_match_drops_noise_and_dedupes() -> None:
    # 1-char tokens dropped; duplicates collapsed; order preserved.
    q = build_fts_match("a a foo foo BAR")
    assert q == '"foo" OR "bar"'


def test_build_fts_match_neutralises_operators() -> None:
    # Punctuation that would otherwise raise an FTS5 syntax error (colon, hyphen,
    # stray quote, parens) is stripped; only quoted word tokens remain.
    q = build_fts_match('foo: bar-baz "qux" (norm)')
    assert q == '"foo" OR "bar" OR "baz" OR "qux" OR "norm"'


def test_build_fts_match_empty() -> None:
    assert build_fts_match("") == ""
    assert build_fts_match("   $$ %% ") == ""


@pytest.mark.skipif(
    not get_settings().effective_db_url.startswith("sqlite"),
    reason="FTS5 is SQLite-only",
)
def test_fts_search_finds_rare_term_in_conversational_query() -> None:
    init_db()
    # Two chunks for the same (fake) document id; only the first mentions the norm.
    fts_insert(910001, 99001, "Die SIA Norm 103 regelt die Honorare der Bauingenieure.", [])
    fts_insert(910002, 99001, "Allgemeine Einleitung ganz ohne relevante Fachbegriffe.", [])

    # Pre-fix this natural-language query matched nothing (implicit AND of all
    # tokens). Now it surfaces the SIA chunk via the OR/bm25 path.
    res = fts_search("in welchem dokument wird die sia norm 103 aufgeführt")
    ids = [cid for cid, _did, _score in res]
    assert 910001 in ids
    # The chunk that actually contains the rare terms ranks above the filler one.
    if 910002 in ids:
        assert ids.index(910001) < ids.index(910002)
