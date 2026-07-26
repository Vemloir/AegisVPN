"""Admin server card keyboard: the location ON/OFF (is_active) toggle and the
access-mode (public/restricted) toggle. Disabling a location is the clean
decommission path for a node we stopped paying for — it must be reachable from
the card and labelled by current state."""

from src.handlers.admin.keyboards import server_list_keyboard, server_manage_keyboard
from src.models import Server


def _server(**kwargs) -> Server:
    base = {
        "id": 5,
        "name": "Testland",
        "flag": "\U0001F1EC\U0001F1F7",
        "host": "h",
        "port": 443,
        "public_key": "p",
        "short_id": "s",
        "access_mode": "public",
        "is_active": True,
    }
    base.update(kwargs)
    return Server(**base)


def _callbacks(kb) -> list[str]:
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _texts(kb) -> list[str]:
    return [btn.text for row in kb.inline_keyboard for btn in row]


def test_active_server_card_offers_disable_toggle():
    kb = server_manage_keyboard(_server(is_active=True))
    cbs = _callbacks(kb)
    # The active toggle is the first, most prominent action.
    assert cbs[0] == "admin_server_active_toggle:5"
    assert _texts(kb)[0] == "Отключить локацию"
    # The access-mode toggle is still present and distinct.
    assert "admin_server_toggle:5" in cbs


def test_inactive_server_card_offers_enable_toggle():
    kb = server_manage_keyboard(_server(is_active=False))
    assert _callbacks(kb)[0] == "admin_server_active_toggle:5"
    assert _texts(kb)[0] == "Включить локацию"


def test_access_toggle_label_follows_mode():
    assert _texts(server_manage_keyboard(_server(access_mode="public")))[1] == "Ограничить доступ"
    assert _texts(server_manage_keyboard(_server(access_mode="restricted")))[1] == "Сделать доступным всем"


def test_server_list_highlights_disabled_only():
    active = _server(id=1, name="Finland", flag="\U0001F1EB\U0001F1EE", is_active=True)
    off = _server(id=2, name="Testland", is_active=False)
    texts = _texts(server_list_keyboard([active, off]))
    # The active location is clean; the disabled one is marked OFF on the button.
    assert any(t.endswith("Finland") and not t.startswith("OFF") for t in texts)
    assert any(t.startswith("OFF · ") and "Testland" in t for t in texts)
    # Last button is the back action, unmarked.
    assert texts[-1] == "Назад в админку"
