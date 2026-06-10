from app.services import tagging
from app.services.tagging import auto_tags, detect_doc_type, detect_language


def test_doc_type_invoice() -> None:
    assert detect_doc_type("Rechnungsnummer 1234 Betrag 100 EUR") == "rechnung"


def test_detect_language_de() -> None:
    assert detect_language("Das ist ein deutscher Satz und der nächste folgt.") == "de"


def test_auto_tags_includes_lang() -> None:
    # The language detector uses small surface markers ("und", "der", "ist", …)
    # to distinguish DE from EN, so the sample text needs them explicitly.
    sample = (
        "Die Rechnung ist überfällig und der Betrag beträgt 100 EUR. "
        "Der Kunde Mustermann hat die Zahlung noch nicht geleistet."
    )
    tags = auto_tags(sample)
    assert any(t.startswith("lang:") for t in tags)


def test_doc_type_needs_two_keyword_hits() -> None:
    # One incidental keyword must NOT confidently assign a doc-type.
    assert detect_doc_type("Please see the attached report.") is None
    # Two hits clear the threshold.
    assert detect_doc_type("Quarterly report — findings and executive summary follow.") == "bericht"


def test_auto_tags_are_all_namespaced_and_denoised() -> None:
    sample = (
        "Rechnung Nr. 5 — Rechnungsbetrag 100 EUR, IBAN DE12 3456 7890. "
        "Datum 2026-01-01. Acme Holding GmbH."
    )
    tags = auto_tags(sample)
    # Doc-type is NOT emitted as a tag — it lives on Document.doc_type instead.
    assert "rechnung" not in tags
    # Every auto-tag is namespaced (no bare topic pollution).
    assert all(":" in t for t in tags), tags
    # Sensitivity is namespaced under sensitive:.
    assert "sensitive:finanzen" in tags
    # The near-universal flags are gone; the selective org flag survives.
    assert "has:dates" not in tags
    assert "has:amounts" not in tags
    assert "has:org" in tags


def test_finanzen_no_longer_fires_on_bare_words() -> None:
    # The word "total" alone must not tag a document as financial anymore.
    assert "sensitive:finanzen" not in auto_tags("The total summary of our amount of work.")


# --- LLM topic tagger -------------------------------------------------------
def test_parse_topics_plain_json() -> None:
    assert tagging.parse_topics('["alpha", "beta", "gamma"]') == ["alpha", "beta", "gamma"]


def test_parse_topics_code_fence_and_prose() -> None:
    assert tagging.parse_topics('```json\n["x1", "y2"]\n```') == ["x1", "y2"]
    assert tagging.parse_topics('Sure! ["foo", "bar"] — hope that helps.') == ["foo", "bar"]


def test_parse_topics_bullets_fallback() -> None:
    assert tagging.parse_topics("- alpha\n- beta\n* gamma") == ["alpha", "beta", "gamma"]


def test_parse_topics_filters_colon_dups_and_length() -> None:
    out = tagging.parse_topics('["lang:de", "Foo", "foo", "' + "z" * 50 + '", "ok"]')
    assert out == ["Foo", "ok"]  # colon dropped, case-insensitive dup dropped, 50-char dropped


def test_parse_topics_respects_max() -> None:
    assert tagging.parse_topics('["aa","bb","cc","dd","ee"]', max_tags=3) == ["aa", "bb", "cc"]


class _FakeChat:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def chat(self, messages, **kw) -> str:
        return self.reply


async def test_llm_topics_parses_reply() -> None:
    out = await tagging.llm_topics("a long document " * 20, client=_FakeChat('["kostenplanung", "SIA 416"]'))
    assert out == ["kostenplanung", "SIA 416"]


async def test_llm_topics_skips_short_text() -> None:
    assert await tagging.llm_topics("tiny", client=_FakeChat('["x"]')) == []


async def test_llm_topics_is_error_safe() -> None:
    class _Boom:
        async def chat(self, *a, **k):
            raise RuntimeError("offline")

    assert await tagging.llm_topics("long text " * 20, client=_Boom()) == []
