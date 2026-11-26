"""Immediate admin Gemini controls; Decodo overrides only the sole admin."""

from urllib.parse import urlsplit

from aiogram import F, Router, html
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.core.database import async_session_maker
from src.models import Server
from src.services.gemini_policy import (
    DECODO_IP_CAPABILITY,
    DECODO_PORTS,
    decodo_port,
    set_admin_proxy,
    set_policy,
    supported,
    validate_policy,
)

from .common import is_admin
from .states import AdminStates

router = Router()


def describe(policy: dict | None) -> str:
    if policy is None:
        return "Текущая настройка на ноде"
    if policy["mode"] == "warp":
        return "WARP / " + ("IPv6" if policy["ip_version"] == "ipv6" else "IPv4")
    return "DNS / " + policy["resolver"]


def button(server_id: int, label: str, action: str):
    return InlineKeyboardButton(text=label, callback_data=f"gemini:{action}:{server_id}")


def supports_decodo_choice(server: Server) -> bool:
    return DECODO_IP_CAPABILITY in (server.control_capabilities or {}).get("features", [])


def decodo_choices(server: Server) -> InlineKeyboardMarkup:
    current = decodo_port(server)
    buttons = [
        button(server.id, f"{'[x] ' if current == port else ''}IP {port - 10000} ({port})", f"dc{port}")
        for port in DECODO_PORTS
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows + [[button(server.id, "Назад", "open")]])


def panel(server: Server) -> tuple[str, InlineKeyboardMarkup]:
    if not supported(server):
        status = "Требуется обновление агента и pull-режим."
    elif server.gemini_policy is None and not server.gemini_admin_proxy:
        status = "Бот ещё не менял доступ на этой ноде."
    elif server.control_last_error:
        status = "Ошибка агента: изменение не подтверждено."
    elif server.gemini_policy_generation and (server.applied_generation or 0) >= server.gemini_policy_generation:
        status = "Применено агентом."
    else:
        status = "Ожидается подтверждение агента."
    policy = server.gemini_policy
    mode = "Decodo" if server.gemini_admin_proxy else (policy["mode"].upper() if policy else "на ноде")
    text = "\n".join(
        [
            "<b>Доступ к Gemini — " + html.quote(server.name) + "</b>",
            status,
            "Изменения сохраняются сразу и могут кратко переподключить клиентов ноды.",
            "Для остальных пользователей: " + html.quote(describe(policy)),
            "Decodo — только для моего аккаунта; датацентровые IP, 1 ГБ/месяц.",
        ]
    )
    rows = [[button(server.id, "Доступ: " + mode, "mode")]]
    if server.gemini_admin_proxy:
        port = decodo_port(server) if supports_decodo_choice(server) else DECODO_PORTS.start
        rows.append([button(server.id, f"IP Decodo: {port - 10000} ({port})", "info")])
    elif policy and policy["mode"] == "warp":
        rows.append([button(server.id, "Тип IP: " + policy["ip_version"].replace("ipv", "IPv"), "family")])
    elif policy:
        label = "DoH" if policy["resolver"].startswith("https://") else "DoT (ранее задан)"
        rows.append([button(server.id, label + ": " + (urlsplit(policy["resolver"]).hostname or ""), "resolver")])
    else:
        rows.append([button(server.id, "Настройка: выберите способ доступа", "mode")])
    rows.extend(
        [
            [button(server.id, "Обновить статус", "open")],
            [InlineKeyboardButton(text="Назад к локации", callback_data=f"admin_server_manage:{server.id}")],
        ]
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def remember(state: FSMContext, server: Server):
    await state.set_state(AdminStates.gemini_edit)
    await state.update_data(
        gemini_server=server.id,
        gemini_expected=server.gemini_policy,
        gemini_expected_admin=bool(server.gemini_admin_proxy),
        gemini_expected_port=decodo_port(server),
    )


async def edit(call: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup):
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


async def ask_resolver(call: CallbackQuery, state: FSMContext, server_id: int):
    await state.set_state(AdminStates.gemini_resolver)
    await edit(
        call,
        "Введите DoH-адрес: https://dns.example/dns-query\n"
        "После отправки DNS будет включён сразу. Нужен резолвер с разблокировкой Gemini.",
        InlineKeyboardMarkup(inline_keyboard=[[button(server_id, "Отмена", "open")]]),
    )


@router.callback_query(F.data.startswith("gemini:"))
async def callback(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return
    try:
        _, action, raw_id = call.data.split(":")
        server_id = int(raw_id)
    except (ValueError, AttributeError):
        await call.answer("Некорректная команда", show_alert=True)
        return
    async with async_session_maker() as session:
        server = await session.get(Server, server_id)
        if server is None:
            await call.answer("Локация не найдена", show_alert=True)
            return
        if action == "open":
            await state.clear()
            await remember(state, server)
        data = await state.get_data()
        if data.get("gemini_server") != server_id or await state.get_state() not in (
            AdminStates.gemini_edit.state,
            AdminStates.gemini_resolver.state,
        ):
            await call.answer("Откройте доступ к Gemini заново", show_alert=True)
            return
        if action == "mode":
            rows = [[button(server_id, "WARP", "warp"), button(server_id, "DNS", "dns")]]
            if "gemini-admin-proxy-v1" in (server.control_capabilities or {}).get("features", []):
                rows.append([button(server_id, "Decodo (только для меня)", "decodo")])
            rows.append([button(server_id, "Назад", "open")])
            await edit(
                call,
                "Выберите доступ к Gemini. Изменение сохраняется сразу.\n"
                "WARP/DNS — для локации; Decodo — только для моего аккаунта.",
                InlineKeyboardMarkup(inline_keyboard=rows),
            )
        elif action == "family":
            await edit(
                call,
                "Тип IP для WARP:",
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [button(server_id, "IPv4", "ipv4"), button(server_id, "IPv6", "ipv6")],
                        [button(server_id, "Назад", "open")],
                    ]
                ),
            )
        elif action in ("info", "decodo") and supports_decodo_choice(server):
            await edit(
                call,
                "Выберите IP Decodo для Gemini. Только для вашего аккаунта.\n"
                "Выбор сохраняется сразу; дождитесь подтверждения агента.",
                decodo_choices(server),
            )
        elif action == "info":
            await call.answer("Для выбора IP Decodo нужно обновить агент.", show_alert=True)
            return
        elif action == "resolver":
            await ask_resolver(call, state, server_id)
        elif action in ("warp", "dns", "ipv4", "ipv6", "decodo") or action in {f"dc{p}" for p in DECODO_PORTS}:
            prefs = server.gemini_preferences or {}
            if action == "dns":
                policy = server.gemini_policy if (server.gemini_policy or {}).get("mode") == "dns" else prefs.get("dns")
                if not policy or not policy.get("resolver", "").startswith("https://"):
                    await ask_resolver(call, state, server_id)
                    await call.answer()
                    return
            else:
                saved_warp = (
                    server.gemini_policy
                    if (server.gemini_policy or {}).get("mode") == "warp"
                    else prefs.get("warp", {})
                )
                policy = {
                    "mode": "warp",
                    "ip_version": action if action.startswith("ipv") else saved_warp.get("ip_version", "ipv4"),
                }
            try:
                if action == "decodo" or action.startswith("dc"):
                    server = await set_admin_proxy(
                        session,
                        server_id,
                        data.get("gemini_expected"),
                        data.get("gemini_expected_admin", False),
                        port=int(action[2:]) if action.startswith("dc") else None,
                        expected_port=data.get("gemini_expected_port"),
                    )
                else:
                    server = await set_policy(
                        session,
                        server_id,
                        policy,
                        data.get("gemini_expected"),
                        expected_admin_proxy=data.get("gemini_expected_admin", False),
                        expected_port=data.get("gemini_expected_port"),
                    )
                await session.commit()
            except ValueError as exc:
                await session.rollback()
                await call.answer(str(exc), show_alert=True)
                return
            await remember(state, server)
            await edit(call, *panel(server))
            await call.answer("Сохранено, отправлено агенту")
            return
        elif action == "open":
            await edit(call, *panel(server))
        else:
            await call.answer("Эта кнопка устарела. Откройте доступ к Gemini заново", show_alert=True)
            return
    await call.answer()


@router.message(AdminStates.gemini_resolver, F.text, ~F.text.startswith("/"))
async def resolver_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    try:
        policy = validate_policy({"mode": "dns", "resolver": message.text.strip()})
        if not policy["resolver"].startswith("https://"):
            raise ValueError("Введите DoH-адрес, начинающийся с https://")
        async with async_session_maker() as session:
            server = await set_policy(
                session,
                data.get("gemini_server", 0),
                policy,
                data.get("gemini_expected"),
                expected_admin_proxy=data.get("gemini_expected_admin", False),
                expected_port=data.get("gemini_expected_port"),
            )
            await session.commit()
            await remember(state, server)
            text, keyboard = panel(server)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
