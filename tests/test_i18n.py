from app.utils.i18n import SUPPORTED_LANGUAGES, t


def test_t_returns_translation() -> None:
    assert t("nav.dashboard", "en") == "Dashboard"
    assert t("nav.dashboard", "de") == "Übersicht"


def test_t_falls_back_to_en() -> None:
    # Unknown language → English
    assert t("nav.dashboard", "xx") == "Dashboard"


def test_t_returns_key_when_missing() -> None:
    assert t("nonexistent.key.zzz", "en") == "nonexistent.key.zzz"


def test_all_supported_languages_have_navs() -> None:
    keys = ["nav.dashboard", "nav.documents", "nav.search", "nav.chat", "nav.settings"]
    for lang in SUPPORTED_LANGUAGES:
        for k in keys:
            assert t(k, lang) != k, f"missing {k} for {lang}"
