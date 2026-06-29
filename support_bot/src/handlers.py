"""All bot handlers: user ticket flow (create / list / view / reply / close),
language settings, and the operator side (one consolidated message per update,
reply-to relay, inline close). Handlers stay thin — DB in storage.py, text in
i18n.py/render.py, math in pagination.py — so the logic is unit-tested without
Telegram.

User-facing text is localized; a user's language comes from the main bot
(read-only) until they override it here via /settings. Operator-facing
notifications stay Russian (the operators are the RU team)."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .config import settings
from .deps import ADMIN_IDS, storage
from .i18n import normalize_lang, t
from .keyboards import admin_ticket_kb, main_menu_kb, settings_kb, ticket_view_kb, tickets_list_kb
from .mainbot import main_bot_language
from .pagination import Page
from .render import render_admin_history, render_user_thread
from .states import CreateTicket, ReplyTicket

logger = logging.getLogger("support_bot")
router = Router()

TITLE_MAX = 120


# --- language ---------------------------------------------------------------
async def resolve_lang(user_id: int) -> str:
    """Support-bot override if set, else the main bot's language, else ru."""
    override = await storage.get_lang(user_id)
    if override:
        return normalize_lang(override)
    return await main_bot_language(user_id) or "ru"


# --- helpers ----------------------------------------------------------------
async def notify_admins(bot: Bot, ticket_id: int) -> None:
    """One consolidated RU message (full history + inline Close) per operator,
    remembered so their reply-to resolves back to this ticket."""
    ticket = await storage.get_ticket(ticket_id)
    if not ticket:
        return
    messages = await storage.get_messages(ticket_id)
    text = render_admin_history(ticket, messages)
    kb = admin_ticket_kb(ticket_id) if ticket["status"] == "open" else None
    for operator_id in ADMIN_IDS:
        try:
            sent = await bot.send_message(operator_id, text, reply_markup=kb)
            await storage.set_admin_msg(operator_id, sent.message_id, ticket_id)
        except Exception as exc:
            logger.warning("notify operator %s failed: %s", operator_id, exc)


async def build_list(user_id: int, page: int, lang: str):
    total = await storage.count_tickets(user_id)
    pg = Page(page=page, per_page=settings.page_size, total=total)
    tickets = await storage.list_tickets(user_id, pg.offset, pg.per_page) if total else []
    text = t(lang, "list_title") if tickets else t(lang, "list_empty")
    return text, tickets_list_kb(tickets, pg, lang)


# --- /start + menu ----------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    lang = await resolve_lang(message.from_user.id)  # type: ignore[union-attr]
    await message.answer(t(lang, "welcome"), reply_markup=main_menu_kb(lang))


@router.message(Command("new"))
async def cmd_new(message: Message, state: FSMContext) -> None:
    lang = await resolve_lang(message.from_user.id)  # type: ignore[union-attr]
    await state.set_state(CreateTicket.title)
    await message.answer(t(lang, "prompt_title"))


@router.message(Command("tickets"))
async def cmd_tickets(message: Message, state: FSMContext) -> None:
    await state.clear()
    lang = await resolve_lang(message.from_user.id)  # type: ignore[union-attr]
    text, kb = await build_list(message.from_user.id, 0, lang)  # type: ignore[union-attr]
    await message.answer(text, reply_markup=kb)


@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext) -> None:
    await state.clear()
    lang = await resolve_lang(message.from_user.id)  # type: ignore[union-attr]
    await message.answer(t(lang, "settings_title", lang=t(lang, "lang_name")), reply_markup=settings_kb(lang))


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    lang = await resolve_lang(call.from_user.id)
    await call.message.edit_text(t(lang, "welcome"), reply_markup=main_menu_kb(lang))  # type: ignore[union-attr]
    await call.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()


@router.callback_query(F.data.startswith("set_lang:"))
async def cb_set_lang(call: CallbackQuery) -> None:
    lang = normalize_lang(call.data.split(":")[1])
    await storage.set_lang(call.from_user.id, lang)
    await call.message.edit_text(  # type: ignore[union-attr]
        t(lang, "settings_title", lang=t(lang, "lang_name")), reply_markup=settings_kb(lang)
    )
    await call.answer(t(lang, "settings_saved"))


# --- create ticket (FSM: title -> body) ------------------------------------
@router.callback_query(F.data == "t_new")
async def cb_new(call: CallbackQuery, state: FSMContext) -> None:
    lang = await resolve_lang(call.from_user.id)
    await state.set_state(CreateTicket.title)
    await call.message.edit_text(t(lang, "prompt_title"))  # type: ignore[union-attr]
    await call.answer()


@router.message(CreateTicket.title)
async def st_title(message: Message, state: FSMContext) -> None:
    lang = await resolve_lang(message.from_user.id)  # type: ignore[union-attr]
    title = (message.text or "").strip()
    if not title:
        await message.answer(t(lang, "title_empty"))
        return
    await state.update_data(title=title[:TITLE_MAX])
    await state.set_state(CreateTicket.body)
    await message.answer(t(lang, "prompt_body"))


@router.message(CreateTicket.body)
async def st_body(message: Message, state: FSMContext, bot: Bot) -> None:
    lang = await resolve_lang(message.from_user.id)  # type: ignore[union-attr]
    body = (message.text or "").strip()
    if not body:
        await message.answer(t(lang, "body_empty"))
        return
    data = await state.get_data()
    await state.clear()
    u = message.from_user
    ticket_id = await storage.create_ticket(u.id, u.username, u.full_name, data["title"], body)  # type: ignore[union-attr]
    await message.answer(t(lang, "created", id=ticket_id), reply_markup=main_menu_kb(lang))
    await notify_admins(bot, ticket_id)


# --- list / open / reply / close (user) ------------------------------------
@router.callback_query(F.data.startswith("t_list:"))
async def cb_list(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    lang = await resolve_lang(call.from_user.id)
    page = int(call.data.split(":")[1])
    text, kb = await build_list(call.from_user.id, page, lang)
    await call.message.edit_text(text, reply_markup=kb)  # type: ignore[union-attr]
    await call.answer()


@router.callback_query(F.data.startswith("t_open:"))
async def cb_open(call: CallbackQuery) -> None:
    lang = await resolve_lang(call.from_user.id)
    ticket_id = int(call.data.split(":")[1])
    ticket = await storage.get_ticket(ticket_id)
    if not ticket or ticket["user_id"] != call.from_user.id:
        await call.answer(t(lang, "not_found"), show_alert=True)
        return
    messages = await storage.get_messages(ticket_id)
    await call.message.edit_text(render_user_thread(ticket, messages, lang), reply_markup=ticket_view_kb(ticket, lang))  # type: ignore[union-attr]
    await call.answer()


@router.callback_query(F.data.startswith("t_reply:"))
async def cb_reply(call: CallbackQuery, state: FSMContext) -> None:
    lang = await resolve_lang(call.from_user.id)
    ticket_id = int(call.data.split(":")[1])
    ticket = await storage.get_ticket(ticket_id)
    if not ticket or ticket["user_id"] != call.from_user.id:
        await call.answer(t(lang, "not_found"), show_alert=True)
        return
    if ticket["status"] != "open":
        await call.answer(t(lang, "closed_alert"), show_alert=True)
        return
    await state.set_state(ReplyTicket.body)
    await state.update_data(ticket_id=ticket_id)
    await call.message.edit_text(t(lang, "reply_prompt", id=ticket_id))  # type: ignore[union-attr]
    await call.answer()


@router.message(ReplyTicket.body)
async def st_reply(message: Message, state: FSMContext, bot: Bot) -> None:
    lang = await resolve_lang(message.from_user.id)  # type: ignore[union-attr]
    text = (message.text or "").strip()
    if not text:
        await message.answer(t(lang, "reply_empty"))
        return
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    await state.clear()
    ticket = await storage.get_ticket(ticket_id)
    if not ticket or ticket["user_id"] != message.from_user.id:  # type: ignore[union-attr]
        await message.answer(t(lang, "not_found"), reply_markup=main_menu_kb(lang))
        return
    if ticket["status"] != "open":
        await message.answer(t(lang, "closed_alert"), reply_markup=main_menu_kb(lang))
        return
    await storage.add_message(ticket_id, "user", text)
    await message.answer(t(lang, "reply_added", id=ticket_id), reply_markup=main_menu_kb(lang))
    await notify_admins(bot, ticket_id)


@router.callback_query(F.data.startswith("t_close:"))
async def cb_close(call: CallbackQuery, bot: Bot) -> None:
    lang = await resolve_lang(call.from_user.id)
    ticket_id = int(call.data.split(":")[1])
    ticket = await storage.get_ticket(ticket_id)
    if not ticket or ticket["user_id"] != call.from_user.id:
        await call.answer(t(lang, "not_found"), show_alert=True)
        return
    if ticket["status"] == "open":
        await storage.close_ticket(ticket_id)
        ticket = await storage.get_ticket(ticket_id)
        for operator_id in ADMIN_IDS:
            try:
                await bot.send_message(operator_id, f"Пользователь закрыл тикет #{ticket_id}.")
            except Exception:
                pass
    messages = await storage.get_messages(ticket_id)
    await call.message.edit_text(render_user_thread(ticket, messages, lang), reply_markup=ticket_view_kb(ticket, lang))  # type: ignore[union-attr]
    await call.answer(t(lang, "closed_toast"))


# --- operator: inline close + reply-to relay -------------------------------
@router.callback_query(F.data.startswith("a_close:"))
async def cb_admin_close(call: CallbackQuery, bot: Bot) -> None:
    if call.from_user.id not in ADMIN_IDS:
        await call.answer()
        return
    ticket_id = int(call.data.split(":")[1])
    ticket = await storage.get_ticket(ticket_id)
    if not ticket:
        await call.answer("Тикет не найден", show_alert=True)
        return
    if ticket["status"] == "open":
        await storage.close_ticket(ticket_id)
        user_lang = await resolve_lang(ticket["user_id"])
        try:
            await bot.send_message(ticket["user_id"], t(user_lang, "closed_by_support", id=ticket_id))
        except Exception:
            pass
    try:
        await call.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    except Exception:
        pass
    await call.answer(f"Тикет #{ticket_id} закрыт")


@router.message(F.from_user.id.in_(ADMIN_IDS), F.reply_to_message)
async def admin_reply(message: Message, bot: Bot) -> None:
    ticket_id = await storage.ticket_by_admin_msg(message.chat.id, message.reply_to_message.message_id)  # type: ignore[union-attr]
    if ticket_id is None:
        await message.answer("Не удалось определить тикет. Ответьте (reply) на сообщение тикета.")
        return
    ticket = await storage.get_ticket(ticket_id)
    if not ticket:
        await message.answer("Тикет не найден.")
        return
    text = (message.text or message.caption or "").strip()
    if not text:
        await message.answer("Пустой ответ не отправлен.")
        return
    await storage.add_message(ticket_id, "operator", text)
    user_lang = await resolve_lang(ticket["user_id"])
    try:
        await bot.send_message(ticket["user_id"], f"{t(user_lang, 'support_reply', id=ticket_id)}\n\n{text}")
        await message.answer("Отправлено.")
    except Exception as exc:
        logger.warning("deliver to user %s failed: %s", ticket["user_id"], exc)
        await message.answer("Не удалось доставить (пользователь остановил бота).")


# --- fallback (must be registered last) ------------------------------------
@router.message()
async def fallback(message: Message) -> None:
    if message.from_user and message.from_user.id in ADMIN_IDS:
        await message.answer("Чтобы ответить пользователю — сделайте reply на сообщение тикета. Закрыть — кнопкой под ним.")
        return
    lang = await resolve_lang(message.from_user.id) if message.from_user else "ru"
    await message.answer(t(lang, "use_menu"), reply_markup=main_menu_kb(lang))
