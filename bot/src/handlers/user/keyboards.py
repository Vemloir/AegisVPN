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
    has_mtproxy: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_active_subscription:
        top_row = [InlineKeyboardButton(text=t(language, "vpn_sub_btn"), callback_data="subscription_show_v2ray")]
        if has_mtproxy:
            top_row.append(InlineKeyboardButton(text=t(language, "tg_proxy_btn"), callback_data="tg_proxy_open"))
        rows.append(top_row)
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
        buttons.append([
            InlineKeyboardButton(
                text=t(language, "devices_btn", count=device_count),
                callback_data="devices_open",
            )
        ])
        buttons.append([InlineKeyboardButton(text=t(language, "locations_btn"), callback_data="locations_open")])
        buttons.append([InlineKeyboardButton(text=t(language, "reissue_subscription"), callback_data="reissue_subscription")])
    buttons.append([InlineKeyboardButton(text=t(language, "delete_account"), callback_data="account_delete")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def locations_list_keyboard(language: str, servers: list) -> InlineKeyboardMarkup:
    """One button per active location (flag + name), then back-to-settings."""
    from src.services.subscription_service import SubscriptionService

    rows: list[list[InlineKeyboardButton]] = []
    duplicate_keys: set[str] = set()
    seen: dict[str, int] = {}
    for s in servers:
        key = (s.name or "").strip().casefold()
        if key:
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


def location_settings_keyboard(
    language: str,
    server_id: int,
    protocol: str,
    transport: str,
    available_transports: list[str],
) -> InlineKeyboardMarkup:
    """Protocol + transport selectors for one location.

    The current choice is marked with a localized text tag (no emoji). Hysteria2
    is shown but disabled — tapping it only flashes a 'coming soon' answer and
    never persists, so the bot can never be coerced into emitting a Hy2 config.
    """
    mark = t(language, "location_current_mark")

    def label(key: str, selected: bool) -> str:
        return t(language, key) + (mark if selected else "")

    rows: list[list[InlineKeyboardButton]] = []
    # Protocol row(s).
    # Selecting "VLESS" keeps the current transport (or xhttp when switching back
    # from a future non-vless protocol).
    vless_transport = transport if protocol == "vless" else "xhttp"
    rows.append([
        InlineKeyboardButton(
            text=label("location_proto_vless", protocol == "vless"),
            callback_data=f"loc_set:{server_id}:vless:{vless_transport}",
        )
    ])
    rows.append([
        InlineKeyboardButton(
            text=label("location_proto_hy2", protocol == "hy2"),
            # Disabled: routes to a no-op handler that only shows "coming soon".
            callback_data=f"loc_hy2:{server_id}",
        )
    ])
    # Transport row (VLESS only) — one button per available transport.
    transport_keys = {
        "xhttp": "location_transport_xhttp",
        "tcp": "location_transport_tcp",
    }
    for tr in available_transports:
        rows.append([
            InlineKeyboardButton(
                text=label(transport_keys[tr], protocol == "vless" and transport == tr),
                callback_data=f"loc_set:{server_id}:vless:{tr}",
            )
        ])
    rows.append([
        InlineKeyboardButton(text=t(language, "location_reset_btn"), callback_data=f"loc_reset:{server_id}")
    ])
    rows.append([
        InlineKeyboardButton(text=t(language, "back_to_locations"), callback_data="locations_open")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reissue_subscription_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(language, "reissue_subscription_confirm_btn"), callback_data="reissue_subscription_confirm")],
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
            [InlineKeyboardButton(text=t(language, "devices_remove_yes"), callback_data=f"devices_remove_confirm:{device_id}")],
            [InlineKeyboardButton(text=t(language, "devices_back"), callback_data="devices_open")],
        ]
    )
