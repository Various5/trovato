from app.services.tagging import auto_tags, detect_doc_type, detect_language


def test_doc_type_invoice() -> None:
    assert detect_doc_type("Rechnungsnummer 1234 Betrag 100 EUR") == "rechnung"


def test_detect_language_de() -> None:
    assert detect_language("Das ist ein deutscher Satz und der nächste folgt.") == "de"


def test_auto_tags_includes_lang() -> None:
    tags = auto_tags("Rechnung Betrag 100 EUR Kunde Mustermann")
    assert any(t.startswith("lang:") for t in tags)
