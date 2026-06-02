"""Top-level admin panel: entry command, stats, DB download, bulk extend."""

import asyncio
from datetime import UTC, datetime

from aiogram import F, Router, html
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from src.services.admin_service import AdminService

from .common import is_admin
from .keyboards import (
    admin_back_keyboard,
    admin_panel_keyboard,
    cancel_keyboard,
    confirmation_keyboard,
)
from .states import AdminStates

router = Router()

_PANEL_TEXT = "Админ-панель.\nВыберите действие кнопками ниже."


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    await state.clear()
    await message.answer(_PANEL_TEXT, reply_markup=admin_panel_keyboard())


@router.callback_query(F.data == "admin_back")
async def cq_admin_back(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    await state.clear()
    await call.message.edit_text(_PANEL_TEXT, reply_markup=admin_panel_keyboard())  # type: ignore
    await call.answer()


@router.callback_query(F.data == "admin_stats")
async def cq_admin_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    stats = await AdminService.get_stats()
    nodes_text = ""
    if stats.nodes_online:
        lines = []
        for flag, name, count in stats.nodes_online:
            label = f"{flag} {name}"
            value = str(count) if count >= 0 else "н/д"
            lines.append(f"  {label}: {value}")
        nodes_text = "\n\nОнлайн по нодам:\n" + "\n".join(lines)
    text = (
        f"{html.bold('Статистика')}\n\n"
        f"Пользователей: {stats.users}\n"
        f"Активных подписок: {stats.active_subscriptions}\n"
        f"Забаненных: {stats.banned_users}\n"
        f"Выручка (Stars): {stats.revenue_stars}"
        f"{nodes_text}"
    )
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=admin_back_keyboard())  # type: ignore
    await call.answer()


@router.callback_query(F.data == "admin_download_db")
async def cq_admin_download_db(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return
    await call.answer("Готовлю снапшот…")
    from src.scheduler.tasks import _make_sqlite_backup

    try:
        gz = await asyncio.to_thread(_make_sqlite_backup)
    except Exception as exc:
        await call.message.answer(f"Не удалось сделать бэкап: {exc}")  # type: ignore
        return
    if gz is None:
        await call.message.answer("Бэкап недоступен (БД не SQLite или файл отсутствует).")  # type: ignore
        return
    data = gz.read_bytes()
    await call.message.answer_document(  # type: ignore
        BufferedInputFile(data, filename=gz.name),
        caption=f"Снапшот БД {datetime.now(UTC):%Y-%m-%d %H:%M} UTC ({len(data) // 1024} KiB)",
    )


@router.callback_query(F.data == "admin_bulk_extend_active_start")
async def cq_admin_bulk_extend_active_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_bulk_extend_days)
    text = (
        f"{html.bold('Продление всем активным')}\n\n"
        "Отправьте количество дней для продления всех активных небессрочных подписок."
    )
    await call.message.edit_text(  # type: ignore
        text,
        parse_mode="HTML",
        reply_markup=cancel_keyboard("admin_back"),
    )
    await call.answer()


@router.message(AdminStates.waiting_bulk_extend_days)
async def msg_admin_bulk_extend_days(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    try:
        days = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно отправить количество дней числом.")
        return

    if days <= 0:
        await message.answer("Количество дней должно быть больше нуля.")
        return

    active_count = await AdminService.count_active_non_lifetime_subscriptions()
    if active_count == 0:
        await state.clear()
        await message.answer("Активных небессрочных подписок нет.", reply_markup=admin_panel_keyboard())
        return

    await state.clear()
    text = (
        f"{html.bold('Подтверждение')}\n\n"
        f"Будут продлены все активные небессрочные подписки.\n"
        f"Подписок к продлению: {active_count}\n"
        f"Срок продления: {days} дней"
    )
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=confirmation_keyboard(
            confirm_data=f"admin_bulk_extend_active_confirm:{days}",
            cancel_data="admin_back",
        ),
    )


@router.callback_query(F.data.startswith("admin_bulk_extend_active_confirm:"))
async def cq_admin_bulk_extend_active_confirm(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    await state.clear()
    try:
        days = int(call.data.split(":", 1)[1])  # type: ignore[arg-type]
    except (TypeError, ValueError):
        await call.answer("Некорректное количество дней", show_alert=True)
        return

    await call.answer("Продлеваю подписки...")
    updated_count = await AdminService.extend_active_non_lifetime_subscriptions(days)
    await call.message.edit_text(  # type: ignore
        (f"{html.bold('Готово')}\n\nПродлено подписок: {updated_count}\nКаждая продлена на {days} дней."),
        parse_mode="HTML",
        reply_markup=admin_panel_keyboard(),
    )
