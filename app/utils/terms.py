"""Query-term extraction shared by UI highlighting and the page-match API.

Lives outside the UI layer so API routes (e.g. the on-page highlight rects in
app/api/routes/documents.py) can use the exact same term list as the snippet
highlighter and the viewer's find-in-document — they must agree on what counts
as a match.
"""

from __future__ import annotations

import re

# Function words we never highlight — a natural-language chat question
# ("in welchen dokumenten hat es bilder von einem pool") is mostly these, and
# marking every one of them turns the snippet into a wall of coloured blocks.
# Only the meaningful terms (pool, wiese, sia, norm, 103, …) should light up.
HL_STOPWORDS = {
    # German
    "der",
    "die",
    "das",
    "den",
    "dem",
    "des",
    "ein",
    "eine",
    "einem",
    "einen",
    "einer",
    "eines",
    "und",
    "oder",
    "ist",
    "sind",
    "war",
    "wird",
    "wurde",
    "hat",
    "habe",
    "haben",
    "es",
    "im",
    "an",
    "am",
    "auf",
    "aus",
    "bei",
    "bis",
    "für",
    "von",
    "vom",
    "vor",
    "mit",
    "nach",
    "zu",
    "zum",
    "zur",
    "über",
    "unter",
    "sich",
    "sie",
    "wie",
    "wo",
    "wer",
    "wann",
    "warum",
    "welche",
    "welcher",
    "welchem",
    "welchen",
    "welches",
    "dass",
    "nicht",
    "auch",
    "nur",
    "noch",
    "wenn",
    "dieser",
    "diese",
    "dieses",
    "man",
    "mir",
    "mein",
    "meine",
    "dokument",
    "dokumente",
    "dokumenten",
    "dokuments",
    "gibt",
    "kann",
    "kannst",
    # English
    "the",
    "of",
    "to",
    "and",
    "or",
    "are",
    "were",
    "be",
    "in",
    "on",
    "at",
    "by",
    "for",
    "with",
    "as",
    "from",
    "that",
    "this",
    "these",
    "those",
    "it",
    "its",
    "which",
    "what",
    "where",
    "who",
    "when",
    "why",
    "how",
    "do",
    "does",
    "did",
    "has",
    "have",
    "had",
    "can",
    "could",
    "would",
    "should",
    "about",
    "show",
    "list",
    "find",
    "me",
    "my",
    "you",
    "your",
    "any",
    "all",
    "was",
    "is",
    "a",
    "document",
    "documents",
    "image",
    "images",
    "picture",
    "pictures",
}


def meaningful_terms(query: str) -> list[str]:
    """Distinct, meaningful (>=2 char, non-stopword) lower-cased query tokens.

    A full-sentence chat question is mostly function words; both the snippet
    highlighter and the viewer's find-in-document need the same short list of
    terms that actually matter so they agree on what to mark/match.
    """
    seen: list[str] = []
    for w in re.findall(r"\w+", (query or "").lower()):
        if len(w) >= 2 and w not in HL_STOPWORDS and w not in seen:
            seen.append(w)
    return seen
