import asyncio
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta

from aiogram import F, Router, html
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from src.core.config import settings
from src.core.database import async_session_maker
from src.models import Payment, Plan, Server, ServerAccessGrant, Subscription, SubscriptionServer, User
from src.services import ServerAccessService, SubscriptionService


def fmt_bytes(n: int | None) -> str:
    n = int(n or 0)
    if n <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.0f} {units[i]}" if (v >= 100 or i == 0) else f"{v:.1f} {units[i]}"

router = Router()


class AdminStates(StatesGroup):
    waiting_user_lookup = State()
    waiting_server_allow_user = State()
    waiting_server_revoke_user = State()
    waiting_plan_price = State()
    waiting_user_issue_days = State()
    waiting_bulk_extend_days = State()


def is_admin(tg_id: int) -> bool:
    return tg_id in settings.admin_ids


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="Серверы", callback_data="admin_servers")],
            [InlineKeyboardButton(text="Тарифы", callback_data="admin_plans")],
            [InlineKeyboardButton(text="Пользователи", callback_data="admin_users")],
            [InlineKeyboardButton(text="Продлить всем активным", callback_data="admin_bulk_extend_active_start")],
            [InlineKeyboardButton(text="Скачать базу данных", callback_data="admin_download_db")],
        ]
    )


def admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад в админку", callback_data="admin_back")]]
    )


def build_duplicate_name_keys(servers: list[Server]) -> set[str]:
    counts = Counter(server.name.strip().casefold() for server in servers if server.name.strip())
    return {name for name, count in counts.items() if count > 1}


def server_access_text(server: Server) -> str:
    if server.access_mode == "public":
        return "Доступен всем"
    return f"Ограниченный доступ ({len(server.access_grants)} пользователей)"


def server_list_keyboard(servers: list[Server]) -> InlineKeyboardMarkup:
    duplicate_name_keys = build_duplicate_name_keys(servers)
    rows = [
        [
            InlineKeyboardButton(
                text=SubscriptionService.format_server_label(server, duplicate_name_keys),
                callback_data=f"admin_server_manage:{server.id}",
            )
        ]
        for server in servers
    ]
    rows.append([InlineKeyboardButton(text="Назад в админку", callback_data="admin_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def server_manage_keyboard(server: Server) -> InlineKeyboardMarkup:
    toggle_text = "Сделать доступным всем" if server.access_mode == "restricted" else "Ограничить доступ"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_text, callback_data=f"admin_server_toggle:{server.id}")],
            [InlineKeyboardButton(text="Разрешить пользователю", callback_data=f"admin_server_allow_start:{server.id}")],
            [InlineKeyboardButton(text="Забрать доступ", callback_data=f"admin_server_revoke_start:{server.id}")],
            [InlineKeyboardButton(text="К списку серверов", callback_data="admin_servers")],
        ]
    )


def users_lookup_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад в админку", callback_data="admin_back")]]
    )


def plan_list_keyboard(plans: list[Plan]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"ID {plan.id}: {plan.days} days - {plan.stars_price} Stars",
                callback_data=f"admin_plan_edit:{plan.id}",
            )
        ]
        for plan in plans
    ]
    rows.append([InlineKeyboardButton(text="Назад в админку", callback_data="admin_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_manage_keyboard(user: User, has_active_subscription: bool) -> InlineKeyboardMarkup:
    issue_text = "Продлить подписку" if has_active_subscription else "Выдать подписку"
    ban_text = "Разбанить" if user.is_banned else "Забанить"
    rows = [
        [InlineKeyboardButton(text=issue_text, callback_data=f"admin_user_issue_start:{user.tg_id}")],
        [InlineKeyboardButton(text="Выдать навсегда", callback_data=f"admin_user_issue_lifetime_start:{user.tg_id}")],
        [InlineKeyboardButton(text="Удалить подписку", callback_data=f"admin_user_revoke_prompt:{user.tg_id}")],
        [InlineKeyboardButton(text=ban_text, callback_data=f"admin_user_toggle_ban:{user.tg_id}")],
        [InlineKeyboardButton(text="Другой пользователь", callback_data="admin_users")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirmation_keyboard(confirm_data: str, cancel_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Подтвердить", callback_data=confirm_data),
                InlineKeyboardButton(text="Отмена", callback_data=cancel_data),
            ]
        ]
    )


def cancel_keyboard(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data=callback_data)]]
    )


async def count_active_non_lifetime_subscriptions() -> int:
    async with async_session_maker() as session:
        now = datetime.now(UTC).replace(tzinfo=None)
        result = await session.execute(
            select(Subscription).where(
                Subscription.is_active == True,
                Subscription.expires_at > now,
            )
        )
        subscriptions = result.scalars().all()
        return sum(1 for sub in subscriptions if not SubscriptionService.is_lifetime_subscription(sub))


async def extend_active_non_lifetime_subscriptions(days: int) -> int:
    async with async_session_maker() as session:
        if days <= 0:
            return 0

        now = datetime.now(UTC).replace(tzinfo=None)
        result = await session.execute(
            select(Subscription).where(
                Subscription.is_active == True,
                Subscription.expires_at > now,
            )
        )
        subscriptions = result.scalars().all()

        updated = 0
        for sub in subscriptions:
            if SubscriptionService.is_lifetime_subscription(sub):
                continue
            sub.expires_at = max(sub.expires_at, now) + timedelta(days=days)
            sub.plan_days = days
            updated += 1

        await session.commit()
        return updated


async def render_server_details(server_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    async with async_session_maker() as session:
        server = await session.get(
            Server,
            server_id,
            options=[selectinload(Server.access_grants).selectinload(ServerAccessGrant.user)],
        )
        if server is None:
            return None

        status = "ON" if server.is_active else "OFF"
        access_lines = []
        if server.access_mode == "public":
            access_lines.append("Доступ: все пользователи")
        else:
            access_lines.append(f"Доступ: только whitelist ({len(server.access_grants)} чел.)")
            if server.access_grants:
                for grant in server.access_grants[:15]:
                    username = f" @{grant.user.username}" if grant.user and grant.user.username else ""
                    access_lines.append(f"- {grant.user.tg_id}{username}")
            else:
                access_lines.append("- Пока никого не добавили")

        text = (
            f"{html.bold(SubscriptionService.format_server_label(server))}\n\n"
            f"Статус: {status}\n"
            f"Хост: {server.host}:{server.port}\n"
            + "\n".join(access_lines)
        )
        return text, server_manage_keyboard(server)


async def resolve_user_tg_id(query: str) -> int | None:
    """Resolve an admin lookup query (numeric Telegram ID or @username) to a
    tg_id. Username match is case-insensitive and ignores a leading '@'."""
    query = query.strip()
    if not query:
        return None
    if query.lstrip("-").isdigit():
        return int(query)
    uname = query.lstrip("@").lower()
    async with async_session_maker() as session:
        result = await session.execute(
            select(User.tg_id).where(func.lower(User.username) == uname)
        )
        row = result.scalar_one_or_none()
        return row


async def render_user_details(tg_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    async with async_session_maker() as session:
        user_result = await session.execute(select(User).where(User.tg_id == tg_id))
        user = user_result.scalar_one_or_none()
        if user is None:
            return None

        now = datetime.now(UTC).replace(tzinfo=None)
        sub_result = await session.execute(
            select(Subscription).where(
                Subscription.user_id == user.id,
                Subscription.is_active == True,
            )
        )
        sub = sub_result.scalar_one_or_none()

        username = f"@{user.username}" if user.username else "без username"
        lines = [
            f"{html.bold('Пользователь')}",
            "",
            f"TG ID: {user.tg_id}",
            f"Username: {username}",
            f"Статус: {'забанен' if user.is_banned else 'активен'}",
            f"Язык: {user.language}",
        ]

        has_active_subscription = False
        if sub and sub.expires_at > now:
            has_active_subscription = True
            if SubscriptionService.is_lifetime_subscription(sub):
                lines.extend(
                    [
                        "Подписка: активна (бессрочная)",
                        "Срок: не ограничен",
                    ]
                )
            else:
                remaining_days = max((sub.expires_at - now).days, 0)
                lines.extend(
                    [
                        "Подписка: активна",
                        f"Истекает: {sub.expires_at.strftime('%Y-%m-%d %H:%M')}",
                        f"Осталось дней: {remaining_days}",
                    ]
                )
        elif sub:
            lines.append("Подписка: есть запись, но уже истекла")
        else:
            lines.append("Подписка: нет")

        if sub is not None:
            # subscription URL
            url = SubscriptionService.build_subscription_url(sub.sub_token)
            lines += ["", f"Ссылка: {html.code(url)}"]

            # per-location traffic + total
            links = (
                await session.execute(
                    select(SubscriptionServer)
                    .options(selectinload(SubscriptionServer.server))
                    .where(SubscriptionServer.subscription_id == sub.id)
                )
            ).scalars().all()
            lines.append("")
            lines.append(html.bold("Трафик"))
            if links:
                for link in sorted(links, key=lambda x: (x.server.name if x.server else "")):
                    name = link.server.name if link.server else f"server {link.server_id}"
                    flag = (link.server.flag + " ") if (link.server and link.server.flag) else ""
                    up = fmt_bytes(link.traffic_up_bytes)
                    down = fmt_bytes(link.traffic_down_bytes)
                    lines.append(f"{flag}{name}: ↑ {up} / ↓ {down}")
            total_up = fmt_bytes(sub.traffic_up_bytes)
            total_down = fmt_bytes(sub.traffic_down_bytes)
            lines.append(f"Всего: ↑ {total_up} / ↓ {total_down}")

        return "\n".join(lines), user_manage_keyboard(user, has_active_subscription)


async def grant_subscription_by_tg_id(tg_id: int, days: int) -> Subscription | None:
    async with async_session_maker() as session:
        user_result = await session.execute(select(User).where(User.tg_id == tg_id))
        user = user_result.scalar_one_or_none()
        if user is None or days <= 0:
            return None

        now = datetime.now(UTC).replace(tzinfo=None)
        sub_result = await session.execute(
            select(Subscription).where(
                Subscription.user_id == user.id,
                Subscription.is_active == True,
            )
        )
        sub = sub_result.scalar_one_or_none()

        if sub and sub.expires_at > now:
            sub.expires_at = max(sub.expires_at, now) + timedelta(days=days)
            sub.plan_days = days
        else:
            if sub:
                sub.is_active = True
                sub.started_at = now
                sub.expires_at = now + timedelta(days=days)
                sub.plan_days = days
                if not sub.sub_token:
                    sub.sub_token = await SubscriptionService.generate_sub_token(session)
                if not sub.client_uuid:
                    sub.client_uuid = str(uuid.uuid4())
            else:
                sub = Subscription(
                    user_id=user.id,
                    sub_token=await SubscriptionService.generate_sub_token(session),
                    client_uuid=str(uuid.uuid4()),
                    plan_days=days,
                    started_at=now,
                    expires_at=now + timedelta(days=days),
                    is_active=True,
                )
                session.add(sub)

        await session.flush()
        servers = await ServerAccessService.get_accessible_servers_for_user(session, user.id)
        await SubscriptionService.sync_subscription_to_servers(session, sub, servers)
        await session.commit()
        await session.refresh(sub)
        return sub


async def grant_lifetime_subscription_by_tg_id(tg_id: int) -> Subscription | None:
    async with async_session_maker() as session:
        user_result = await session.execute(select(User).where(User.tg_id == tg_id))
        user = user_result.scalar_one_or_none()
        if user is None:
            return None

        now = datetime.now(UTC).replace(tzinfo=None)
        sub_result = await session.execute(
            select(Subscription).where(
                Subscription.user_id == user.id,
                Subscription.is_active == True,
            )
        )
        sub = sub_result.scalar_one_or_none()

        if sub:
            sub.is_active = True
            if sub.expires_at <= now:
                sub.started_at = now
            sub.expires_at = SubscriptionService.LIFETIME_EXPIRES_AT
            sub.plan_days = SubscriptionService.LIFETIME_PLAN_DAYS
            if not sub.sub_token:
                sub.sub_token = await SubscriptionService.generate_sub_token(session)
            if not sub.client_uuid:
                sub.client_uuid = str(uuid.uuid4())
        else:
            sub = Subscription(
                user_id=user.id,
                sub_token=await SubscriptionService.generate_sub_token(session),
                client_uuid=str(uuid.uuid4()),
                plan_days=SubscriptionService.LIFETIME_PLAN_DAYS,
                started_at=now,
                expires_at=SubscriptionService.LIFETIME_EXPIRES_AT,
                is_active=True,
            )
            session.add(sub)

        await session.flush()
        servers = await ServerAccessService.get_accessible_servers_for_user(session, user.id)
        await SubscriptionService.sync_subscription_to_servers(session, sub, servers)
        await session.commit()
        await session.refresh(sub)
        return sub


async def revoke_subscription_by_tg_id(tg_id: int) -> bool:
    async with async_session_maker() as session:
        user_result = await session.execute(select(User).where(User.tg_id == tg_id))
        user = user_result.scalar_one_or_none()
        if user is None:
            return False

        sub_result = await session.execute(
            select(Subscription).where(
                Subscription.user_id == user.id,
                Subscription.is_active == True,
            )
        )
        sub = sub_result.scalar_one_or_none()
        if sub is None:
            return False

        sub.is_active = False
        sub.expires_at = datetime.now(UTC).replace(tzinfo=None)
        await SubscriptionService.remove_subscription_from_servers(session, sub)
        await session.commit()
        return True


async def set_user_ban_status(tg_id: int, is_banned: bool) -> User | None:
    async with async_session_maker() as session:
        user_result = await session.execute(select(User).where(User.tg_id == tg_id))
        user = user_result.scalar_one_or_none()
        if user is None:
            return None

        user.is_banned = is_banned
        if is_banned:
            sub_result = await session.execute(
                select(Subscription).where(
                    Subscription.user_id == user.id,
                    Subscription.is_active == True,
                )
            )
            sub = sub_result.scalar_one_or_none()
            if sub:
                sub.is_active = False
                sub.expires_at = datetime.now(UTC).replace(tzinfo=None)
                await SubscriptionService.remove_subscription_from_servers(session, sub)

        await session.commit()
        return user


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    await state.clear()
    await message.answer(
        "Админ-панель.\nВыберите действие кнопками ниже.",
        reply_markup=admin_panel_keyboard(),
    )


@router.callback_query(F.data == "admin_back")
async def cq_admin_back(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    await state.clear()
    await call.message.edit_text(  # type: ignore
        "Админ-панель.\nВыберите действие кнопками ниже.",
        reply_markup=admin_panel_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data == "admin_stats")
async def cq_admin_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    async with async_session_maker() as session:
        users_count = await session.scalar(select(func.count(User.id)))
        active_subs = await session.scalar(select(func.count(Subscription.id)).where(Subscription.is_active == True))
        banned_users = await session.scalar(select(func.count(User.id)).where(User.is_banned == True))
        revenue = await session.scalar(select(func.sum(Payment.stars_amount))) or 0

    text = (
        f"{html.bold('Статистика')}\n\n"
        f"Пользователей: {users_count}\n"
        f"Активных подписок: {active_subs}\n"
        f"Забаненных: {banned_users}\n"
        f"Выручка (Stars): {revenue}"
    )
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=admin_back_keyboard())  # type: ignore
    await call.answer()


@router.callback_query(F.data == "admin_servers")
async def cq_admin_servers(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    await state.clear()
    async with async_session_maker() as session:
        servers = (
            await session.execute(
                select(Server)
                .where(Server.is_active == True)
                .options(selectinload(Server.access_grants))
                .order_by(Server.id)
            )
        ).scalars().all()

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
            inline_keyboard=[[InlineKeyboardButton(text="Назад к серверу", callback_data=f"admin_server_manage:{server_id}")]]
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
            inline_keyboard=[[InlineKeyboardButton(text="Назад к серверу", callback_data=f"admin_server_manage:{server_id}")]]
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


@router.callback_query(F.data == "admin_plans")
async def cq_admin_plans(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    async with async_session_maker() as session:
        plans = (await session.execute(select(Plan).order_by(Plan.id))).scalars().all()

    lines = [f"{html.bold('Тарифы')}", ""]
    for plan in plans:
        status = "ON" if plan.is_active else "OFF"
        lines.append(f"ID: {plan.id} | {plan.days} дней | {plan.stars_price} Stars | {status}")

    await call.message.edit_text(  # type: ignore
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=plan_list_keyboard(plans),
    )
    await call.answer()


@router.callback_query(F.data.startswith("admin_plan_edit:"))
async def cq_admin_plan_edit(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Доступ запрещён", show_alert=True)
        return

    plan_id = int(call.data.split(":", 1)[1])  # type: ignore[arg-type]
    async with async_session_maker() as session:
        plan = await session.get(Plan, plan_id)

    if plan is None:
        await call.answer("Тариф не найден", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_plan_price)
    await state.update_data(plan_id=plan_id)
    await call.message.edit_text(  # type: ignore
        f"Тариф ID {plan.id}: {plan.days} дней.\nТекущая цена: {plan.stars_price} Stars.\n\n"
        "Отправьте новую цену числом.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Назад к тарифам", callback_data="admin_plans")]]
        ),
    )
    await call.answer()


@router.message(AdminStates.waiting_plan_price)
async def msg_admin_plan_price(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    plan_id = data.get("plan_id")
    if not plan_id:
        await state.clear()
        await message.answer("Тариф не выбран.")
        return

    try:
        new_price = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужна цена числом.")
        return

    if new_price <= 0:
        await message.answer("Цена должна быть больше нуля.")
        return

    async with async_session_maker() as session:
        plan = await session.get(Plan, plan_id)
        if plan is None:
            await state.clear()
            await message.answer("Тариф не найден.")
            return

        plan.stars_price = new_price
        await session.commit()
        plans = (await session.execute(select(Plan).order_by(Plan.id))).scalars().all()

    await state.clear()
    lines = [f"{html.bold('Тарифы')}", ""]
    for plan in plans:
        status = "ON" if plan.is_active else "OFF"
        lines.append(f"ID: {plan.id} | {plan.days} дней | {plan.stars_price} Stars | {status}")

    await message.answer("Цена обновлена.")
    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=plan_list_keyboard(plans),
    )


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
        caption=f"Снапшот БД {datetime.now(UTC):%Y-%m-%d %H:%M} UTC ({len(data)//1024} KiB)",
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

    active_count = await count_active_non_lifetime_subscriptions()
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
    updated_count = await extend_active_non_lifetime_subscriptions(days)
    await call.message.edit_text(  # type: ignore
        (
            f"{html.bold('Готово')}\n\n"
            f"Продлено подписок: {updated_count}\n"
            f"Каждая продлена на {days} дней."
        ),
        parse_mode="HTML",
        reply_markup=admin_panel_keyboard(),
    )


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
        tg_id = await resolve_user_tg_id(message.text or "")
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
        f"{text}\n\n"
        f"{html.bold('Введите срок')}\n"
        f"Отправьте количество дней, на которое нужно {action} подписку."
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
    confirm_text = (
        f"{text}\n\n"
        f"{html.bold('Подтверждение')}\n"
        f"Подтвердите действие: {action} подписку на {days} дней."
    )
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
    confirm_text = (
        f"{text}\n\n"
        f"{html.bold('Подтверждение')}\n"
        "Подтвердите выдачу бессрочной подписки."
    )
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
    sub = await grant_subscription_by_tg_id(tg_id, days)
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
    sub = await grant_lifetime_subscription_by_tg_id(tg_id)
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
    confirm_text = (
        f"{text}\n\n"
        f"{html.bold('Подтверждение')}\n"
        "Подтвердите удаление подписки у пользователя."
    )
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
    success = await revoke_subscription_by_tg_id(tg_id)
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
        updated_user = await set_user_ban_status(tg_id, False)
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
        f"{text}\n\n"
        f"{html.bold('Подтверждение')}\n"
        "Подтвердите бан пользователя. Активная подписка тоже будет отключена."
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
    updated_user = await set_user_ban_status(tg_id, True)
    if updated_user is None:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    rendered = await render_user_details(tg_id)
    assert rendered is not None
    text, keyboard = rendered
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)  # type: ignore
    await call.answer("Пользователь забанен")


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
