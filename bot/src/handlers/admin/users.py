"""User management: lookup (id / @username / forward), issue, revoke, ban."""

from aiogram import F, Router, html
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from src.core.database import async_session_maker
from src.models import User
from src.services.admin_service import AdminService

from .common import is_admin
from .keyboards import (
    admin_panel_keyboard,
    cancel_keyboard,
    confirmation_keyboard,
    user_conn_limit_keyboard,
    users_lookup_keyboard,
)
from .rendering import render_user_details
from .states import AdminStates

router = Router()


@router.callback_query(F.data == "admin_users")
async def cq_admin_users(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_user_lookup)
    await call.message.edit_text(  # type: ignore
        "Отправьте @username, Telegram ID или перешлите сообщение пользователя.",
        reply_markup=users_lookup_keyboard(),
    )
    await call.answer()


async def _user_from_forward(message: Message) -> tuple[int, str | None] | None:
    """Extract (tg_id, username) from a forwarded message's original sender.
    Returns None if it's not a user-forward or the sender hid their account."""
    origin = getattr(message, "forward_origin", None)
    sender = getattr(origin, "sender_user", None) if origin is not None else None
    if sender is None:
        sender = getattr(message, "forward_from", None)  # legacy fallback
    if sender is None or getattr(sender, "is_bot", False):
        return None
    return sender.id, sender.username


@router.message(AdminStates.waiting_user_lookup)
async def msg_admin_user_lookup(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    # Forwarded message: read the original sender's current id + username and
    # sync the DB (creates the row if missing). Solves lookup after a rename.
    fwd = await _user_from_forward(message)
    if fwd is not None:
        tg_id, username = fwd
        async with async_session_maker() as session:
            result = await session.execute(select(User).where(User.tg_id == tg_id))
            user = result.scalar_one_or_none()
            if user is None:
                session.add(User(tg_id=tg_id, username=username))
            elif user.username != username:
                user.username = username
            await session.commit()
    elif getattr(message, "forward_origin", None) is not None:
        await message.answer(
            "У пользователя скрыт аккаунт при пересылке — найти по пересланному "
            "сообщению нельзя. Попросите его написать боту или дайте Telegram ID."
        )
        return
    else:
        tg_id = await AdminService.resolve_user_tg_id(message.text or "")
        if tg_id is None:
            await message.answer(
                "Пользователь не найден. Пришлите @username (он должен был писать "
                "боту), числовой Telegram ID или перешлите его сообщение."
            )
            return

    rendered = await render_user_details(tg_id)
    if rendered is None:
        await message.answer("Пользователь не найден. Пусть сначала напишет /start боту.")
        return

    await state.clear()
    text, keyboard = rendered
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("admin_user_issue_start:"))
async def cq_admin_user_issue_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    tg_id = int(call.data.split(":", 1)[1])  # type: ignore[arg-type]
    rendered = await render_user_details(tg_id)
    if rendered is None:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    text, _ = rendered
    action = "продлить" if "Подписка: активна" in text else "выдать"
    await state.set_state(AdminStates.waiting_user_issue_days)
    await state.update_data(issue_tg_id=tg_id)
    prompt_text = (
        f"{text}\n\n{html.bold('Введите срок')}\nОтправьте количество дней, на которое нужно {action} подписку."
    )
    await call.message.edit_text(  # type: ignore
        prompt_text,
        parse_mode="HTML",
        reply_markup=cancel_keyboard(f"admin_user_show:{tg_id}"),
    )
    await call.answer()


@router.message(AdminStates.waiting_user_issue_days)
async def msg_admin_user_issue_days(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    tg_id = data.get("issue_tg_id")
    if not tg_id:
        await state.clear()
        await message.answer("Пользователь не выбран.", reply_markup=admin_panel_keyboard())
        return

    try:
        days = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно отправить количество дней числом.")
        return

    if days <= 0:
        await message.answer("Количество дней должно быть больше нуля.")
        return

    rendered = await render_user_details(tg_id)
    if rendered is None:
        await state.clear()
        await message.answer("Пользователь не найден.", reply_markup=admin_panel_keyboard())
        return

    text, _ = rendered
    action = "продлить" if "Подписка: активна" in text else "выдать"
    await state.clear()
    confirm_text = f"{text}\n\n{html.bold('Подтверждение')}\nПодтвердите действие: {action} подписку на {days} дней."
    await message.answer(
        confirm_text,
        parse_mode="HTML",
        reply_markup=confirmation_keyboard(
            confirm_data=f"admin_user_issue_confirm:{tg_id}:{days}",
            cancel_data=f"admin_user_show:{tg_id}",
        ),
    )


@router.callback_query(F.data.startswith("admin_user_issue_lifetime_start:"))
async def cq_admin_user_issue_lifetime_start(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    tg_id = int(call.data.split(":", 1)[1])  # type: ignore[arg-type]
    rendered = await render_user_details(tg_id)
    if rendered is None:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    text, _ = rendered
    confirm_text = f"{text}\n\n{html.bold('Подтверждение')}\nПодтвердите выдачу бессрочной подписки."
    await call.message.edit_text(  # type: ignore
        confirm_text,
        parse_mode="HTML",
        reply_markup=confirmation_keyboard(
            confirm_data=f"admin_user_issue_lifetime_confirm:{tg_id}",
            cancel_data=f"admin_user_show:{tg_id}",
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith("admin_user_issue_confirm:"))
async def cq_admin_user_issue_confirm(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    parts = call.data.split(":")  # type: ignore[union-attr]
    if len(parts) != 3:
        await call.answer("Некорректные параметры", show_alert=True)
        return

    try:
        tg_id = int(parts[1])
        days = int(parts[2])
    except ValueError:
        await call.answer("Некорректные параметры", show_alert=True)
        return

    await call.answer("Обновляю подписку...")
    sub = await AdminService.grant_subscription(tg_id, days)
    if sub is None:
        await call.message.answer("Не удалось выдать подписку")  # type: ignore
        return

    rendered = await render_user_details(tg_id)
    assert rendered is not None
    text, keyboard = rendered
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)  # type: ignore


@router.callback_query(F.data.startswith("admin_user_issue_lifetime_confirm:"))
async def cq_admin_user_issue_lifetime_confirm(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    tg_id = int(call.data.split(":", 1)[1])  # type: ignore[arg-type]

    await call.answer("Обновляю подписку...")
    sub = await AdminService.grant_lifetime_subscription(tg_id)
    if sub is None:
        await call.message.answer("Не удалось выдать бессрочную подписку")  # type: ignore
        return

    rendered = await render_user_details(tg_id)
    assert rendered is not None
    text, keyboard = rendered
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)  # type: ignore


@router.callback_query(F.data.startswith("admin_user_revoke_prompt:"))
async def cq_admin_user_revoke_prompt(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    tg_id = int(call.data.split(":", 1)[1])  # type: ignore[arg-type]
    rendered = await render_user_details(tg_id)
    if rendered is None:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    text, _ = rendered
    confirm_text = f"{text}\n\n{html.bold('Подтверждение')}\nПодтвердите удаление подписки у пользователя."
    await call.message.edit_text(  # type: ignore
        confirm_text,
        parse_mode="HTML",
        reply_markup=confirmation_keyboard(
            confirm_data=f"admin_user_revoke_confirm:{tg_id}",
            cancel_data=f"admin_user_show:{tg_id}",
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith("admin_user_revoke_confirm:"))
async def cq_admin_user_revoke_confirm(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    tg_id = int(call.data.split(":", 1)[1])  # type: ignore[arg-type]
    success = await AdminService.revoke_subscription(tg_id)
    rendered = await render_user_details(tg_id)
    if rendered is None:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    text, keyboard = rendered
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)  # type: ignore
    await call.answer("Подписка удалена" if success else "Активной подписки нет")


@router.callback_query(F.data.startswith("admin_user_toggle_ban:"))
async def cq_admin_user_toggle_ban(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    tg_id = int(call.data.split(":", 1)[1])  # type: ignore[arg-type]
    async with async_session_maker() as session:
        user_result = await session.execute(select(User).where(User.tg_id == tg_id))
        user = user_result.scalar_one_or_none()
    if user is None:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    if user.is_banned:
        updated_user = await AdminService.set_user_ban_status(tg_id, False)
        if updated_user is None:
            await call.answer("Пользователь не найден", show_alert=True)
            return
        rendered = await render_user_details(tg_id)
        assert rendered is not None
        text, keyboard = rendered
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)  # type: ignore
        await call.answer("Пользователь разбанен")
        return

    rendered = await render_user_details(tg_id)
    if rendered is None:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    text, _ = rendered
    confirm_text = (
        f"{text}\n\n{html.bold('Подтверждение')}\nПодтвердите бан пользователя. Активная подписка тоже будет отключена."
    )
    await call.message.edit_text(  # type: ignore
        confirm_text,
        parse_mode="HTML",
        reply_markup=confirmation_keyboard(
            confirm_data=f"admin_user_ban_confirm:{tg_id}",
            cancel_data=f"admin_user_show:{tg_id}",
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith("admin_user_ban_confirm:"))
async def cq_admin_user_ban_confirm(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    tg_id = int(call.data.split(":", 1)[1])  # type: ignore[arg-type]
    updated_user = await AdminService.set_user_ban_status(tg_id, True)
    if updated_user is None:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    rendered = await render_user_details(tg_id)
    assert rendered is not None
    text, keyboard = rendered
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)  # type: ignore
    await call.answer("Пользователь забанен")


async def _apply_conn_limit(call_or_msg, tg_id: int, limit: int | None) -> None:
    """Set the user's conn-limit, push to nodes, and re-render the user card."""
    user, ok, total = await AdminService.set_user_conn_limit(tg_id, limit)
    if user is None:
        await call_or_msg.answer("Пользователь не найден")
        return
    rendered = await render_user_details(tg_id)
    if rendered is None:
        await call_or_msg.answer("Пользователь не найден")
        return
    what = "без лимита (∞)" if limit == 0 else str(limit)
    note = f"Лимит подключений: {what} (разослано на {ok}/{total} нод)"
    text, keyboard = rendered
    body = f"{text}\n\n{html.bold(note)}"
    if isinstance(call_or_msg, CallbackQuery):
        await call_or_msg.message.edit_text(body, parse_mode="HTML", reply_markup=keyboard)  # type: ignore
        await call_or_msg.answer()
    else:
        await call_or_msg.answer(body, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("admin_user_connlimit:"))
async def cq_admin_user_connlimit(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    tg_id = int(call.data.split(":", 1)[1])  # type: ignore[arg-type]
    rendered = await render_user_details(tg_id)
    if rendered is None:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    text, _ = rendered
    prompt = (
        f"{text}\n\n{html.bold('Лимит подключений')}\n"
        "Сколько одновременных подключений (IP) разрешить этому пользователю?\n"
        "«Без лимита» — снять ограничение совсем."
    )
    await call.message.edit_text(prompt, parse_mode="HTML", reply_markup=user_conn_limit_keyboard(tg_id))  # type: ignore
    await call.answer()


@router.callback_query(F.data.startswith("admin_user_connlimit_set:"))
async def cq_admin_user_connlimit_set(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    parts = call.data.split(":")  # type: ignore[union-attr]
    if len(parts) != 3:
        await call.answer("Некорректные параметры", show_alert=True)
        return
    tg_id = int(parts[1])
    limit = int(parts[2])
    await call.answer("Применяю...")
    await _apply_conn_limit(call, tg_id, limit)


@router.callback_query(F.data.startswith("admin_user_connlimit_custom:"))
async def cq_admin_user_connlimit_custom(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    tg_id = int(call.data.split(":", 1)[1])  # type: ignore[arg-type]
    await state.set_state(AdminStates.waiting_user_conn_limit)
    await state.update_data(conn_limit_tg_id=tg_id)
    await call.message.edit_text(  # type: ignore
        f"{html.bold('Лимит подключений')}\nОтправьте число (сколько одновременных IP разрешить, 0 — без лимита).",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(f"admin_user_show:{tg_id}"),
    )
    await call.answer()


@router.message(AdminStates.waiting_user_conn_limit)
async def msg_admin_user_conn_limit(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    tg_id = data.get("conn_limit_tg_id")
    if not tg_id:
        await state.clear()
        await message.answer("Пользователь не выбран.", reply_markup=admin_panel_keyboard())
        return

    try:
        limit = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно отправить число (0 — без лимита).")
        return
    if limit < 0:
        await message.answer("Число не может быть отрицательным (0 — без лимита).")
        return

    await state.clear()
    await _apply_conn_limit(message, tg_id, limit)


@router.callback_query(F.data.startswith("admin_user_show:"))
async def cq_admin_user_show(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    await state.clear()
    tg_id = int(call.data.split(":", 1)[1])  # type: ignore[arg-type]
    rendered = await render_user_details(tg_id)
    if rendered is None:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    text, keyboard = rendered
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)  # type: ignore
    await call.answer()
