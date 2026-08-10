"""Drill-down per-location keyboards: the per-location screen + the protocol and
transport choosers, and the callbacks they emit.

The screen shows the current protocol/transport AS button labels (no markers);
the choosers mark the current option with a localized suffix. Hysteria2 is
selectable only on a capable node and disabled everywhere else."""

from src.handlers.user.keyboards import (
    location_protocol_keyboard,
    location_settings_keyboard,
    location_transport_keyboard,
)


def _callbacks(kb) -> list[str]:
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def _texts(kb) -> list[str]:
    return [btn.text for row in kb.inline_keyboard for btn in row]


# --- per-location screen -----------------------------------------------------


def test_location_screen_vless_shows_protocol_and_transport_then_back():
    kb = location_settings_keyboard("ru", 7, "vless", "tcp", ["xhttp", "tcp"])
    cbs = _callbacks(kb)
    # Protocol button -> proto chooser, transport button -> transport chooser,
    # then back to the locations list.
    assert cbs == ["loc_proto:7", "loc_transport:7", "locations_open"]
    texts = _texts(kb)
    # The current values are rendered ON the buttons (no [current] markers here).
    assert "VLESS" in texts[0]
    assert "TCP + VISION" in texts[1]
    assert "[текущий]" not in texts[0] and "(текущий)" not in texts[0]


def test_location_screen_xhttp_transport_label_shows_xhttp():
    kb = location_settings_keyboard("ru", 7, "vless", "xhttp", ["xhttp", "tcp"])
    assert "XHTTP" in _texts(kb)[1]


def test_location_screen_hides_transport_button_off_vless():
    # A future non-vless protocol has no transport selector.
    kb = location_settings_keyboard("ru", 7, "hy2", "xhttp", ["xhttp", "tcp"])
    assert _callbacks(kb) == ["loc_proto:7", "locations_open"]


# --- protocol chooser --------------------------------------------------------


def test_protocol_chooser_enables_hy2_only_on_capable_node():
    capable = location_protocol_keyboard("ru", 7, "vless", hy2_capable=True)
    assert _callbacks(capable) == [
        "loc_proto_set:7:vless",
        "loc_proto_set:7:hy2",
        "loc:7",
    ]
    assert "(текущий)" in _texts(capable)[0]

    unavailable = location_protocol_keyboard("ru", 7, "vless", hy2_capable=False)
    assert _callbacks(unavailable) == [
        "loc_proto_set:7:vless",
        "loc_hy2:7",
        "loc:7",
    ]


# --- transport chooser -------------------------------------------------------


def test_transport_chooser_lists_available_with_marker_and_back():
    kb = location_transport_keyboard("ru", 7, "tcp", ["tcp", "xhttp"])
    cbs = _callbacks(kb)
    assert cbs == ["loc_transport_set:7:tcp", "loc_transport_set:7:xhttp", "loc:7"]
    texts = _texts(kb)
    assert "TCP + VISION" in texts[0] and "(текущий)" in texts[0]
    # The non-selected option has no marker.
    assert "(текущий)" not in texts[1]


def test_transport_chooser_omits_tcp_without_capability():
    kb = location_transport_keyboard("ru", 7, "xhttp", ["xhttp"])
    assert _callbacks(kb) == ["loc_transport_set:7:xhttp", "loc:7"]
