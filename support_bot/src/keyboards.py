"""Inline keyboards. No emoji (house style); pagination uses « » guillemets.
User-facing labels are localized; the operator close button stays Russian."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .i18n import t
from .pagination import Page
from .render import ticket_button_label


def main_menu_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(lang, "btn_create"), callback_data="t_new")],
            [InlineKeyboardButton(text=t(lang, "btn_my"), callback_data="t_list:0")],
        ]
    )


def tickets_list_kb(tickets: list[dict], pg: Page, lang: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=ticket_button_label(t_, lang), callback_data=f"t_open:{t_['id']}")] for t_ in tickets
    ]
    nav: list[InlineKeyboardButton] = []
    if pg.has_prev:
        nav.append(InlineKeyboardButton(text=t(lang, "nav_prev"), callback_data=f"t_list:{pg.clamped - 1}"))
    if pg.total_pages > 1:
        nav.append(
            InlineKeyboardButton(text=t(lang, "page", cur=pg.clamped + 1, total=pg.total_pages), callback_data="noop")
        )
    if pg.has_next:
        nav.append(InlineKeyboardButton(text=t(lang, "nav_next"), callback_data=f"t_list:{pg.clamped + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text=t(lang, "btn_back_menu"), callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ticket_view_kb(ticket: dict, lang: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if ticket["status"] == "open":
        rows.append([InlineKeyboardButton(text=t(lang, "btn_write"), callback_data=f"t_reply:{ticket['id']}")])
        rows.append([InlineKeyboardButton(text=t(lang, "btn_close"), callback_data=f"t_close:{ticket['id']}")])
    rows.append([InlineKeyboardButton(text=t(lang, "btn_back_list"), callback_data="t_list:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t(lang, "btn_lang_ru"), callback_data="set_lang:ru"),
                InlineKeyboardButton(text=t(lang, "btn_lang_en"), callback_data="set_lang:en"),
            ],
            [InlineKeyboardButton(text=t(lang, "btn_back_menu"), callback_data="menu")],
        ]
    )


def admin_ticket_kb(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"Закрыть тикет #{ticket_id}", callback_data=f"a_close:{ticket_id}")]]
    )
