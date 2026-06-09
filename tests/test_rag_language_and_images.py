"""RAG language detection + image-source labelling, and snippet highlighting.

Covers the three user-reported chat issues:
* answers came back in English for German questions,
* the model said "no images" while image sources were present,
* highlighting marked every word of a full-sentence question.
"""

from __future__ import annotations

from app.chat.rag import _build_context_block, _detect_lang, _lang_directive
from app.services.search_service import SearchHit
from app.ui.app_ui import highlight_terms, meaningful_terms


def _hit(source: str, snippet: str = "a swimming pool next to a meadow") -> SearchHit:
    return SearchHit(
        chunk_id=1,
        document_id=7,
        filename="villa.pdf",
        path="/x/villa.pdf",
        page_from=3,
        page_to=3,
        snippet=snippet,
        score=1.0,
        source=source,
        tags=[],
    )


# --- language ---------------------------------------------------------------


def test_detect_lang_german_question():
    assert _detect_lang("in welchen dokumenten hat es bilder von einem pool oder wiese") == "de"


def test_detect_lang_umlaut_forces_german():
    assert _detect_lang("wo wird die Prüfung erwähnt") == "de"


def test_detect_lang_english_question():
    assert _detect_lang("which documents have a photo of a pool") == "en"


def test_lang_directive_names_german():
    d = _lang_directive("in welchem dokument wird die sia norm 103 aufgeführt")
    assert "German" in d


def test_lang_directive_empty_when_unknown():
    assert _lang_directive("123 456 789") == ""


# --- image source labelling -------------------------------------------------


def test_context_block_flags_image_source():
    block, cites = _build_context_block([_hit("image_description")])
    assert "IMAGE" in block
    assert "describes an image" in block
    assert len(cites) == 1


def test_context_block_plain_text_has_no_image_label():
    block, _ = _build_context_block([_hit("native_text")])
    assert "IMAGE" not in block


def test_context_block_labels_table():
    block, _ = _build_context_block([_hit("table")])
    assert "TABLE" in block


# --- highlighting -----------------------------------------------------------


def test_meaningful_terms_drops_stopwords():
    terms = meaningful_terms("in welchen dokumenten hat es bilder von einem pool oder wiese")
    assert "pool" in terms and "wiese" in terms and "bilder" in terms
    # function words must be gone
    for stop in ("in", "es", "von", "hat", "oder", "einem"):
        assert stop not in terms


def test_highlight_only_marks_meaningful_words():
    html = highlight_terms("Der Pool liegt neben einer Wiese", "hat es bilder von einem pool oder wiese")
    assert "<mark class='ldi-mark'>Pool</mark>" in html
    assert "<mark class='ldi-mark'>Wiese</mark>" in html
    # the stopword 'einer' in the source text must NOT be marked
    assert "<mark class='ldi-mark'>einer</mark>" not in html


def test_highlight_escapes_html():
    html = highlight_terms("<script>pool</script>", "pool")
    assert "&lt;script&gt;" in html
