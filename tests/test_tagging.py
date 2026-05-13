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
