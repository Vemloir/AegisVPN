"""All bot handlers: user ticket flow (create / list / view / reply / close) and
the operator side (one consolidated message per update, reply-to relay, inline
close). Handlers stay thin — DB in storage.py, text in render.py, math in
pagination.py — so the logic is unit-tested without Telegram."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .config import settings
from .deps import ADMIN_IDS, storage
from .keyboards import admin_ticket_kb, main_menu_kb, ticket_view_kb, tickets_list_kb
from .pagination import Page
from .render import render_admin_history, render_user_thread
from .states import CreateTicket, ReplyTicket

logger = logging.getLogger("support_bot")
router = Router()

TITLE_MAX = 120


# --- helpers ---------------------------------------------------------------
async def notify_admins(bot: Bot, ticket_id: int) -> None:
    """Send ONE consolidated message (full history + inline Close) to each
    operator and remember it so their reply-to resolves back to this ticket."""
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


async def _show_ticket(message: Message, ticket: dict) -> None:
    messages = await storage.get_messages(ticket["id"])
    await message.edit_text(render_user_thread(ticket, messages), reply_markup=ticket_view_kb(ticket))


async def _show_list(message: Message, user_id: int, page: int) -> None:
    total = await storage.count_tickets(user_id)
    pg = Page(page=page, per_page=settings.page_size, total=total)
    tickets = await storage.list_tickets(user_id, pg.offset, pg.per_page) if total else []
    text = "Ваши тикеты:" if tickets else "У вас пока нет тикетов. Нажмите «Создать тикет»."
    await message.edit_text(text, reply_markup=tickets_list_kb(tickets, pg))


# --- /start + menu ---------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(settings.welcome_text, reply_markup=main_menu_kb())


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text(settings.welcome_text, reply_markup=main_menu_kb())  # type: ignore[union-attr]
    await call.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()


# --- create ticket (FSM: title -> body) ------------------------------------
@router.callback_query(F.data == "t_new")
async def cb_new(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CreateTicket.title)
    await call.message.edit_text("Введите короткое название тикета (тему):")  # type: ignore[union-attr]
    await call.answer()


@router.message(CreateTicket.title)
async def st_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title:
        await message.answer("Название не может быть пустым. Введите тему тикета:")
        return
    await state.update_data(title=title[:TITLE_MAX])
    await state.set_state(CreateTicket.body)
    await message.answer("Теперь опишите ваше обращение одним сообщением:")


@router.message(CreateTicket.body)
async def st_body(message: Message, state: FSMContext, bot: Bot) -> None:
    body = (message.text or "").strip()
    if not body:
        await message.answer("Сообщение не может быть пустым. Опишите обращение:")
        return
    data = await state.get_data()
    await state.clear()
    u = message.from_user
    ticket_id = await storage.create_ticket(u.id, u.username, u.full_name, data["title"], body)  # type: ignore[union-attr]
    await message.answer(f"Тикет #{ticket_id} создан. Поддержка ответит здесь.", reply_markup=main_menu_kb())
    await notify_admins(bot, ticket_id)


# --- list / open / reply / close (user) ------------------------------------
@router.callback_query(F.data.startswith("t_list:"))
async def cb_list(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    page = int(call.data.split(":")[1])
    await _show_list(call.message, call.from_user.id, page)  # type: ignore[arg-type]
    await call.answer()


@router.callback_query(F.data.startswith("t_open:"))
async def cb_open(call: CallbackQuery) -> None:
    ticket_id = int(call.data.split(":")[1])
    ticket = await storage.get_ticket(ticket_id)
    if not ticket or ticket["user_id"] != call.from_user.id:
        await call.answer("Тикет не найден", show_alert=True)
        return
    await _show_ticket(call.message, ticket)  # type: ignore[arg-type]
    await call.answer()


@router.callback_query(F.data.startswith("t_reply:"))
async def cb_reply(call: CallbackQuery, state: FSMContext) -> None:
    ticket_id = int(call.data.split(":")[1])
    ticket = await storage.get_ticket(ticket_id)
    if not ticket or ticket["user_id"] != call.from_user.id:
        await call.answer("Тикет не найден", show_alert=True)
        return
    if ticket["status"] != "open":
        await call.answer("Тикет закрыт", show_alert=True)
        return
    await state.set_state(ReplyTicket.body)
    await state.update_data(ticket_id=ticket_id)
    await call.message.edit_text(f"Тикет #{ticket_id}: введите сообщение для поддержки:")  # type: ignore[union-attr]
    await call.answer()


@router.message(ReplyTicket.body)
async def st_reply(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Сообщение не может быть пустым:")
        return
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    await state.clear()
    ticket = await storage.get_ticket(ticket_id)
    if not ticket or ticket["user_id"] != message.from_user.id:  # type: ignore[union-attr]
        await message.answer("Тикет не найден.", reply_markup=main_menu_kb())
        return
    if ticket["status"] != "open":
        await message.answer("Тикет закрыт.", reply_markup=main_menu_kb())
        return
    await storage.add_message(ticket_id, "user", text)
    await message.answer(f"Сообщение добавлено в тикет #{ticket_id}.", reply_markup=main_menu_kb())
    await notify_admins(bot, ticket_id)


@router.callback_query(F.data.startswith("t_close:"))
async def cb_close(call: CallbackQuery, bot: Bot) -> None:
    ticket_id = int(call.data.split(":")[1])
    ticket = await storage.get_ticket(ticket_id)
    if not ticket or ticket["user_id"] != call.from_user.id:
        await call.answer("Тикет не найден", show_alert=True)
        return
    if ticket["status"] == "open":
        await storage.close_ticket(ticket_id)
        ticket = await storage.get_ticket(ticket_id)
        for operator_id in ADMIN_IDS:
            try:
                await bot.send_message(operator_id, f"Пользователь закрыл тикет #{ticket_id}.")
            except Exception:
                pass
    await _show_ticket(call.message, ticket)  # type: ignore[arg-type]
    await call.answer("Тикет закрыт")


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
        try:
            await bot.send_message(ticket["user_id"], f"Ваш тикет #{ticket_id} закрыт поддержкой.")
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
    try:
        await bot.send_message(ticket["user_id"], f"Ответ поддержки по тикету #{ticket_id}:\n\n{text}")
        await message.answer("Отправлено.")
    except Exception as exc:
        logger.warning("deliver to user %s failed: %s", ticket["user_id"], exc)
        await message.answer("Не удалось доставить (пользователь остановил бота).")


# --- fallback (must be registered last) ------------------------------------
@router.message()
async def fallback(message: Message) -> None:
    if message.from_user and message.from_user.id in ADMIN_IDS:
        await message.answer("Чтобы ответить пользователю — сделайте reply на сообщение тикета. Закрыть — кнопкой под ним.")
    else:
        await message.answer("Используйте меню ниже.", reply_markup=main_menu_kb())
