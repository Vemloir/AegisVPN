from src.i18n import TRANSLATIONS, normalize_lang, t


def test_key_parity_ru_en():
    assert set(TRANSLATIONS["ru"]) == set(TRANSLATIONS["en"])


def test_normalize():
    assert normalize_lang("en") == "en"
    assert normalize_lang("ru") == "ru"
    assert normalize_lang(None) == "ru"
    assert normalize_lang("de") == "ru"


def test_lookup_and_format():
    assert t("en", "btn_create") == "Create ticket"
    assert t("ru", "btn_create") == "Создать тикет"
    assert t("en", "created", id=5) == "Ticket #5 created. Support will reply here."


def test_unknown_lang_falls_back_to_ru():
    assert t("de", "btn_my") == TRANSLATIONS["ru"]["btn_my"]


def test_unknown_key_returns_key():
    assert t("ru", "does_not_exist") == "does_not_exist"
