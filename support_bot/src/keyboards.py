"""Inline keyboards. No emoji (house style); pagination uses « » guillemets."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .pagination import Page
from .render import ticket_button_label


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Создать тикет", callback_data="t_new")],
            [InlineKeyboardButton(text="Мои тикеты", callback_data="t_list:0")],
        ]
    )


def tickets_list_kb(tickets: list[dict], pg: Page) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=ticket_button_label(t), callback_data=f"t_open:{t['id']}")] for t in tickets
    ]
    nav: list[InlineKeyboardButton] = []
    if pg.has_prev:
        nav.append(InlineKeyboardButton(text="« Пред.", callback_data=f"t_list:{pg.clamped - 1}"))
    if pg.total_pages > 1:
        nav.append(InlineKeyboardButton(text=f"стр. {pg.clamped + 1}/{pg.total_pages}", callback_data="noop"))
    if pg.has_next:
        nav.append(InlineKeyboardButton(text="След. »", callback_data=f"t_list:{pg.clamped + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="Назад в меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ticket_view_kb(ticket: dict) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if ticket["status"] == "open":
        rows.append([InlineKeyboardButton(text="Написать сообщение", callback_data=f"t_reply:{ticket['id']}")])
        rows.append([InlineKeyboardButton(text="Закрыть тикет", callback_data=f"t_close:{ticket['id']}")])
    rows.append([InlineKeyboardButton(text="Назад к списку", callback_data="t_list:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_ticket_kb(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"Закрыть тикет #{ticket_id}", callback_data=f"a_close:{ticket_id}")]]
    )
