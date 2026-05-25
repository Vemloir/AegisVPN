"""Inline keyboards for the end-user flow."""

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
    # The subscription link is now shown inline on the screen; offer the setup
    # guide directly instead of a button that just revealed the link.
    if has_v2ray:
        rows.append([InlineKeyboardButton(text=t(language, "setup_guide"), callback_data="help_setup:v2ray")])
    # No point offering "renew" to a lifetime subscription.
    if not (has_active_subscription and is_lifetime):
        action_text = t(language, "renew_vpn") if has_active_subscription else t(language, "buy_vpn")
        rows.append([InlineKeyboardButton(text=action_text, callback_data="buy_plan")])
    if show_trial and not has_active_subscription:
        rows.append([InlineKeyboardButton(text=t(language, "trial_vpn"), callback_data="trial_activate")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_detail_keyboard(language: str, kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(language, "setup_guide"), callback_data=f"help_setup:{kind}")],
            [InlineKeyboardButton(text=t(language, "back_to_subscription"), callback_data="subscription_open")],
        ]
    )


def settings_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(language, "change_language"), callback_data="settings_language")],
            [InlineKeyboardButton(text=t(language, "delete_account"), callback_data="account_delete")],
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
