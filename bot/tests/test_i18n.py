from src.services import language_label, pick_language, t
from src.services.i18n import SUPPORTED_LANGUAGES, TEXTS


def test_supported_languages_have_identical_keys():
    ru_keys = set(TEXTS["ru"])
    en_keys = set(TEXTS["en"])
    assert ru_keys == en_keys, f"key mismatch: {ru_keys ^ en_keys}"


def test_every_supported_language_has_a_table():
    for lang in SUPPORTED_LANGUAGES:
        assert lang in TEXTS


def test_t_returns_translation():
    assert t("ru", "buy_vpn") == "Купить VPN"
    assert t("en", "buy_vpn") == "Buy VPN"


def test_t_formats_kwargs():
    assert t("ru", "days_left", days=5) == "Осталось дней: 5"


def test_t_falls_back_to_ru_for_unknown_language():
    assert t("zz", "buy_vpn") == t("ru", "buy_vpn")


def test_pick_language():
    assert pick_language("en-US") == "en"
    assert pick_language("ru") == "ru"
    assert pick_language(None) == "ru"
    assert pick_language("de") == "ru"


def test_language_label():
    assert language_label("ru")
    assert language_label("en")
