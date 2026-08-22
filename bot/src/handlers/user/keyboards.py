"""Inline keyboards for the end-user flow."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.services import t


def subscription_keyboard(
    language: str,
    has_active_subscription: bool = False,
    show_trial: bool = False,
    has_v2ray: bool = False,
    is_lifetime: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_active_subscription:
        rows.append([InlineKeyboardButton(text=t(language, "setup_guide"), callback_data="help_setup:v2ray")])
        if not is_lifetime:
            rows.append([InlineKeyboardButton(text=t(language, "renew_vpn"), callback_data="buy_plan")])
    else:
        rows.append([InlineKeyboardButton(text=t(language, "buy_vpn"), callback_data="buy_plan")])
        if show_trial:
            rows.append([InlineKeyboardButton(text=t(language, "trial_vpn"), callback_data="trial_activate")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_detail_keyboard(language: str, kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(language, "setup_guide"), callback_data=f"help_setup:{kind}")],
            [InlineKeyboardButton(text=t(language, "back_to_subscription"), callback_data="subscription_open")],
        ]
    )


def settings_keyboard(
    language: str,
    has_active_subscription: bool = False,
    device_count: int = 0,
) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=t(language, "change_language"), callback_data="settings_language")],
    ]
    if has_active_subscription:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=t(language, "devices_btn", count=device_count),
                    callback_data="devices_open",
                )
            ]
        )
        buttons.append([InlineKeyboardButton(text=t(language, "locations_btn"), callback_data="locations_open")])
        buttons.append(
            [InlineKeyboardButton(text=t(language, "reissue_subscription"), callback_data="reissue_subscription")]
        )
    buttons.append([InlineKeyboardButton(text=t(language, "delete_account"), callback_data="account_delete")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def locations_list_keyboard(language: str, servers: list) -> InlineKeyboardMarkup:
    """One button per active location (flag + name), then back-to-settings."""
    from src.services.subscription_service import SubscriptionService

    rows: list[list[InlineKeyboardButton]] = []
    duplicate_keys: set[str] = set()
    seen: dict[str, int] = {}
    for s in servers:
        # Key on the full display name so identically named nodes get №id.
        key = SubscriptionService.server_display_name(s).casefold()
        seen[key] = seen.get(key, 0) + 1
    duplicate_keys = {k for k, c in seen.items() if c > 1}
    for s in servers:
        label = SubscriptionService.format_server_label(s, duplicate_keys)
        rows.append([InlineKeyboardButton(text=label, callback_data=f"loc:{s.id}")])
    rows.append([InlineKeyboardButton(text=t(language, "back_to_settings"), callback_data="settings_open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def location_no_alt_keyboard(language: str) -> InlineKeyboardMarkup:
    """Back button for a location without alternative transports."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(language, "back_to_locations"), callback_data="locations_open")],
        ]
    )


# Localized labels for one protocol / transport value, used both inside the
# drill-down choosers and to render the current value on the per-location screen.
_PROTOCOL_LABEL_KEYS = {
    "vless": "location_proto_vless",
    "hy2": "location_proto_hy2",
}
_TRANSPORT_LABEL_KEYS = {
    "xhttp": "location_transport_xhttp",
    "tcp": "location_transport_tcp",
}


def location_settings_keyboard(
    language: str,
    server_id: int,
    protocol: str,
    transport: str,
    available_transports: list[str],
) -> InlineKeyboardMarkup:
    """Per-location drill-down screen.

    Shows the current protocol (and, while on VLESS, the current transport) as
    button labels that open the dedicated choosers. No '[current]' markers here —
    the value itself is on the button.
    """
    proto_label = t(language, _PROTOCOL_LABEL_KEYS.get(protocol, "location_proto_vless"))
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=t(language, "location_protocol_value_btn", value=proto_label),
                callback_data=f"loc_proto:{server_id}",
            )
        ],
    ]
    # The transport selector only makes sense for VLESS (the only protocol with a
    # backend); a future non-vless protocol would hide it.
    if protocol == "vless":
        transport_label = t(language, _TRANSPORT_LABEL_KEYS.get(transport, "location_transport_xhttp"))
        rows.append(
            [
                InlineKeyboardButton(
                    text=t(language, "location_transport_value_btn", value=transport_label),
                    callback_data=f"loc_transport:{server_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text=t(language, "back_to_locations"), callback_data="locations_open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def location_protocol_keyboard(
    language: str,
    server_id: int,
    protocol: str,
    hy2_capable: bool = False,
) -> InlineKeyboardMarkup:
    """Protocol chooser. Hy2 is not offered at all on a node that can't serve it —
    a picker with a dead option invites a tap just to learn that, so the row is
    omitted rather than shown disabled."""
    mark = t(language, "location_selected_mark")
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=t(language, "location_proto_vless") + (mark if protocol == "vless" else ""),
                callback_data=f"loc_proto_set:{server_id}:vless",
            )
        ],
    ]
    if hy2_capable:
        rows.append(
            [
                InlineKeyboardButton(
                    text=t(language, "location_proto_hy2") + (mark if protocol == "hy2" else ""),
                    callback_data=f"loc_proto_set:{server_id}:hy2",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text=t(language, "back"), callback_data=f"loc:{server_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def location_transport_keyboard(
    language: str,
    server_id: int,
    transport: str,
    available_transports: list[str],
) -> InlineKeyboardMarkup:
    """Transport chooser (reachable only while protocol == vless). One button per
    transport the server can actually serve; the current one carries a marker."""
    mark = t(language, "location_selected_mark")
    rows: list[list[InlineKeyboardButton]] = []
    for tr in available_transports:
        rows.append(
            [
                InlineKeyboardButton(
                    text=t(language, _TRANSPORT_LABEL_KEYS[tr]) + (mark if transport == tr else ""),
                    callback_data=f"loc_transport_set:{server_id}:{tr}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text=t(language, "back"), callback_data=f"loc:{server_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reissue_subscription_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(language, "reissue_subscription_confirm_btn"), callback_data="reissue_subscription_confirm"
                )
            ],
            [InlineKeyboardButton(text=t(language, "back_to_settings"), callback_data="settings_open")],
        ]
    )


def delete_account_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(language, "delete_account_confirm"), callback_data="account_delete_confirm")],
            [InlineKeyboardButton(text=t(language, "back_to_settings"), callback_data="settings_open")],
        ]
    )


def language_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Русский", callback_data="settings_set_language:ru"),
                InlineKeyboardButton(text="English", callback_data="settings_set_language:en"),
            ],
            [InlineKeyboardButton(text=t(language, "back_to_settings"), callback_data="settings_open")],
        ]
    )


def devices_list_keyboard(language: str, devices: list) -> InlineKeyboardMarkup:
    rows = []
    for device in devices:
        label = device.display_name[:28] + "…" if len(device.display_name) > 28 else device.display_name
        if device.is_suspended:
            label = f"[II] {label}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"devices_detail:{device.id}")])
    rows.append([InlineKeyboardButton(text=t(language, "back_to_settings"), callback_data="settings_open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def device_detail_keyboard(language: str, device_id: int, is_suspended: bool) -> InlineKeyboardMarkup:
    suspend_btn = (
        InlineKeyboardButton(text=t(language, "device_resume_btn"), callback_data=f"devices_resume:{device_id}")
        if is_suspended
        else InlineKeyboardButton(text=t(language, "device_suspend_btn"), callback_data=f"devices_suspend:{device_id}")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [suspend_btn],
            [InlineKeyboardButton(text=t(language, "device_remove_btn"), callback_data=f"devices_remove:{device_id}")],
            [InlineKeyboardButton(text=t(language, "devices_back"), callback_data="devices_open")],
        ]
    )


def device_remove_confirm_keyboard(language: str, device_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(language, "devices_remove_yes"), callback_data=f"devices_remove_confirm:{device_id}"
                )
            ],
            [InlineKeyboardButton(text=t(language, "devices_back"), callback_data="devices_open")],
        ]
    )
