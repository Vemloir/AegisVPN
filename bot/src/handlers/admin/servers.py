"""Server management: list, access mode toggle, per-user access grants."""

from aiogram import F, Router, html
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.core.database import async_session_maker
from src.models import Server, User
from src.services import ServerAccessService, SubscriptionService

from .common import is_admin
from .keyboards import build_duplicate_name_keys, server_access_text, server_list_keyboard
from .rendering import render_server_details
from .states import AdminStates

router = Router()


@router.callback_query(F.data == "admin_servers")
async def cq_admin_servers(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    await state.clear()
    async with async_session_maker() as session:
        # Show inactive locations too — otherwise a location taken offline
        # vanishes from the panel and can never be re-enabled. Status (ON/OFF) is
        # rendered per row below.
        servers = (
            (
                await session.execute(
                    select(Server)
                    .options(selectinload(Server.access_grants))
                    .order_by(Server.is_active.desc(), Server.id)
                )
            )
            .scalars()
            .all()
        )

    duplicate_name_keys = build_duplicate_name_keys(servers)
    lines = [f"{html.bold('Серверы')}", ""]
    for server in servers:
        status = "ON" if server.is_active else "OFF"
        label = SubscriptionService.format_server_label(server, duplicate_name_keys)
        lines.append(f"{status} {label} | {server_access_text(server)}")

    await call.message.edit_text(  # type: ignore
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=server_list_keyboard(servers),
    )
    await call.answer()


@router.callback_query(F.data.startswith("admin_server_manage:"))
async def cq_admin_server_manage(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    await state.clear()
    server_id = int(call.data.split(":", 1)[1])  # type: ignore[arg-type]
    rendered = await render_server_details(server_id)
    if rendered is None:
        await call.answer("Сервер не найден", show_alert=True)
        return

    text, keyboard = rendered
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)  # type: ignore
    await call.answer()


@router.callback_query(F.data.startswith("admin_server_toggle:"))
async def cq_admin_server_toggle(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    server_id = int(call.data.split(":", 1)[1])  # type: ignore[arg-type]
    async with async_session_maker() as session:
        server = await session.get(Server, server_id)
        if server is None:
            await call.answer("Сервер не найден", show_alert=True)
            return

        new_mode = "public" if server.access_mode == "restricted" else "restricted"
        await ServerAccessService.set_server_access_mode(session, server, new_mode)
        await session.commit()

    await state.clear()
    rendered = await render_server_details(server_id)
    assert rendered is not None
    text, keyboard = rendered
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)  # type: ignore
    await call.answer("Настройки доступа обновлены")


@router.callback_query(F.data.startswith("admin_server_active_toggle:"))
async def cq_admin_server_active_toggle(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    server_id = int(call.data.split(":", 1)[1])  # type: ignore[arg-type]
    async with async_session_maker() as session:
        server = await session.get(Server, server_id)
        if server is None:
            await call.answer("Сервер не найден", show_alert=True)
            return

        target = not server.is_active
        await ServerAccessService.set_server_active(session, server, target)
        await session.commit()

    await state.clear()
    rendered = await render_server_details(server_id)
    assert rendered is not None
    text, keyboard = rendered
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)  # type: ignore
    await call.answer(
        "Локация включена" if target else "Локация отключена — убрана у всех пользователей"
    )


@router.callback_query(F.data.startswith("admin_server_allow_start:"))
async def cq_admin_server_allow_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    server_id = int(call.data.split(":", 1)[1])  # type: ignore[arg-type]
    await state.set_state(AdminStates.waiting_server_allow_user)
    await state.update_data(server_id=server_id)
    await call.message.edit_text(  # type: ignore
        "Отправьте Telegram ID пользователя, которому нужно разрешить доступ к серверу.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Назад к серверу", callback_data=f"admin_server_manage:{server_id}")]
            ]
        ),
    )
    await call.answer()


@router.message(AdminStates.waiting_server_allow_user)
async def msg_admin_server_allow_user(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    server_id = data.get("server_id")
    if not server_id:
        await state.clear()
        await message.answer("Сервер не выбран.")
        return

    try:
        tg_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужен числовой Telegram ID.")
        return

    async with async_session_maker() as session:
        server = await session.get(Server, server_id)
        user_result = await session.execute(select(User).where(User.tg_id == tg_id))
        user = user_result.scalar_one_or_none()

        if server is None or user is None:
            await message.answer("Не удалось найти сервер или пользователя.")
            return

        created = await ServerAccessService.grant_user_access(session, server, user)
        await session.commit()

    await state.clear()
    rendered = await render_server_details(server_id)
    assert rendered is not None
    text, keyboard = rendered
    await message.answer("Доступ выдан." if created else "Доступ уже был выдан.")
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("admin_server_revoke_start:"))
async def cq_admin_server_revoke_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    server_id = int(call.data.split(":", 1)[1])  # type: ignore[arg-type]
    await state.set_state(AdminStates.waiting_server_revoke_user)
    await state.update_data(server_id=server_id)
    await call.message.edit_text(  # type: ignore
        "Отправьте Telegram ID пользователя, у которого нужно забрать доступ к серверу.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Назад к серверу", callback_data=f"admin_server_manage:{server_id}")]
            ]
        ),
    )
    await call.answer()


@router.message(AdminStates.waiting_server_revoke_user)
async def msg_admin_server_revoke_user(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    server_id = data.get("server_id")
    if not server_id:
        await state.clear()
        await message.answer("Сервер не выбран.")
        return

    try:
        tg_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужен числовой Telegram ID.")
        return

    async with async_session_maker() as session:
        server = await session.get(Server, server_id)
        user_result = await session.execute(select(User).where(User.tg_id == tg_id))
        user = user_result.scalar_one_or_none()

        if server is None or user is None:
            await message.answer("Не удалось найти сервер или пользователя.")
            return

        removed = await ServerAccessService.revoke_user_access(session, server, user)
        await session.commit()

    await state.clear()
    rendered = await render_server_details(server_id)
    assert rendered is not None
    text, keyboard = rendered
    await message.answer("Доступ забран." if removed else "У пользователя и так не было доступа.")
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
