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
