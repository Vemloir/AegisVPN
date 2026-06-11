"""Render admin cards (text + keyboard) for servers and users."""

from datetime import UTC, datetime

from aiogram import html
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.core.database import async_session_maker
from src.models import Server, ServerAccessGrant, Subscription, SubscriptionServer, User
from src.services import SubscriptionService

from .common import fmt_bytes
from .keyboards import server_manage_keyboard, user_manage_keyboard


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
            f"Хост: {server.host}:{server.port}\n" + "\n".join(access_lines)
        )
        return text, server_manage_keyboard(server)


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
                Subscription.is_active == True,  # noqa: E712
            )
        )
        sub = sub_result.scalar_one_or_none()

        username = f"@{user.username}" if user.username else "без username"
        if user.conn_limit is None:
            conn_limit_text = "по умолчанию"
        elif user.conn_limit == 0:
            conn_limit_text = "без лимита"
        else:
            conn_limit_text = str(user.conn_limit)
        lines = [
            f"{html.bold('Пользователь')}",
            "",
            f"TG ID: {user.tg_id}",
            f"Username: {username}",
            f"Статус: {'забанен' if user.is_banned else 'активен'}",
            f"Язык: {user.language}",
            f"Лимит подключений: {conn_limit_text}",
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
                (
                    await session.execute(
                        select(SubscriptionServer)
                        .options(selectinload(SubscriptionServer.server))
                        .where(SubscriptionServer.subscription_id == sub.id)
                    )
                )
                .scalars()
                .all()
            )
            lines.append("")
            lines.append(html.bold("Трафик"))
            if links:
                for link in sorted(links, key=lambda x: x.server.name if x.server else ""):
                    name = link.server.name if link.server else f"server {link.server_id}"
                    flag = (link.server.flag + " ") if (link.server and link.server.flag) else ""
                    up = fmt_bytes(link.traffic_up_bytes)
                    down = fmt_bytes(link.traffic_down_bytes)
                    lines.append(f"{flag}{name}: ↑ {up} / ↓ {down}")
            total_up = fmt_bytes(sub.traffic_up_bytes)
            total_down = fmt_bytes(sub.traffic_down_bytes)
            lines.append(f"Всего: ↑ {total_up} / ↓ {total_down}")

        return "\n".join(lines), user_manage_keyboard(user, has_active_subscription)
