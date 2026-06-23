"""Subscription screen: rendering, the /subscription command, trial activation."""

import math
from datetime import UTC, datetime

from aiogram import F, Router, html
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from src.core.database import async_session_maker
from src.models import Server, Subscription, User
from src.services import (
    ServerAccessService,
    SubscriptionService,
    UserService,
    get_user_language,
    t,
)

from .keyboards import subscription_detail_keyboard, subscription_keyboard
from .privacy import privacy_gate_keyboard, require_privacy

router = Router()


async def render_subscription_info(tg_id: int) -> tuple[str, str | None, str, bool, bool]:
    async with async_session_maker() as session:
        user_result = await session.execute(select(User).where(User.tg_id == tg_id))
        user = user_result.scalar_one_or_none()
        language = user.language if user else "ru"
        if not user or user.is_banned:
            return t(language, "access_unavailable"), None, language, False, False

        now = datetime.now(UTC).replace(tzinfo=None)
        sub_result = await session.execute(
            select(Subscription).where(
                Subscription.user_id == user.id,
                Subscription.is_active == True,  # noqa: E712
                Subscription.expires_at > now,
            )
        )
        sub = sub_result.scalar_one_or_none()

        if not sub:
            return t(language, "no_active_subscription"), None, language, not user.trial_used, False

        accessible_servers = await ServerAccessService.get_accessible_servers_for_user(
            session,
            user.id,
            subscription_group=None,
        )
        subscription_link = SubscriptionService.build_subscription_url(sub.sub_token) if accessible_servers else None

        is_lifetime = SubscriptionService.is_lifetime_subscription(sub)
        if is_lifetime:
            details_block = f"{t(language, 'subscription_lifetime')}\n\n"
        else:
            remaining_seconds = max((sub.expires_at - now).total_seconds(), 0)
            days_left = math.ceil(remaining_seconds / 86400) if remaining_seconds else 0
            details_block = (
                f"{t(language, 'days_left', days=days_left)}\n"
                f"{t(language, 'expires_at', expires_at=sub.expires_at.strftime('%Y-%m-%d %H:%M'))}\n\n"
            )

        text = f"{html.bold(t(language, 'subscription_active_title'))}\n\n{details_block}"
        if subscription_link:
            # Show the link inline instead of hiding it behind a button.
            text += (
                f"{t(language, 'subscription_link', link=html.code(subscription_link))}\n\n"
                f"{t(language, 'v2ray_detail_hint')}"
            )
        else:
            text += t(language, "subscription_hint")
        return text, subscription_link, language, False, is_lifetime


async def render_subscription_screen(tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    text, subscription_link, language, show_trial, is_lifetime = await render_subscription_info(tg_id)
    has_mtproxy = False
    if subscription_link is not None:
        async with async_session_maker() as session:
            count = await session.scalar(
                select(func.count(Server.id)).where(
                    Server.is_active == True,  # noqa: E712
                    Server.mtproxy_secret.isnot(None),
                    Server.mtproxy_port.isnot(None),
                )
            )
            has_mtproxy = (count or 0) > 0
    markup = subscription_keyboard(
        language,
        has_active_subscription=subscription_link is not None,
        show_trial=show_trial,
        has_v2ray=subscription_link is not None,
        is_lifetime=is_lifetime,
        has_mtproxy=has_mtproxy,
    )
    return text, markup


async def render_v2ray_detail_screen(tg_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    _, subscription_link, language, _, _ = await render_subscription_info(tg_id)
    if not subscription_link:
        return None

    lines = [f"{html.bold(t(language, 'v2ray_title'))}", ""]
    lines.append(t(language, "subscription_link", link=html.code(subscription_link)))
    lines.append("")
    lines.append(t(language, "v2ray_detail_hint"))
    body = "\n".join(lines)
    return body, subscription_detail_keyboard(language, "v2ray")


async def render_instruction_text(tg_id: int, kind: str) -> str:
    language = await get_user_language(tg_id)
    return t(language, "setup_text_v2ray")


@router.message(Command("subscription"))
async def cmd_subscription(message: Message):
    if not message.from_user:
        return

    if not await UserService.is_privacy_accepted(message.from_user.id):
        language = await get_user_language(message.from_user.id)
        await message.answer(
            t(language, "privacy_gate_text"),
            parse_mode="HTML",
            reply_markup=await privacy_gate_keyboard(language),
        )
        return

    text, markup = await render_subscription_screen(message.from_user.id)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("help_setup:"))
async def cq_help_setup(call: CallbackQuery):
    kind = call.data.split(":", 1)[1] if call.data else "v2ray"
    await call.message.answer(await render_instruction_text(call.from_user.id, kind))  # type: ignore
    await call.answer()


@router.callback_query(F.data == "subscription_open")
async def cq_subscription_open(call: CallbackQuery):
    if not await require_privacy(call):
        return
    text, markup = await render_subscription_screen(call.from_user.id)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)  # type: ignore
    await call.answer()


@router.callback_query(F.data == "subscription_show_v2ray")
async def cq_subscription_show_v2ray(call: CallbackQuery):
    language = await get_user_language(call.from_user.id)
    rendered = await render_v2ray_detail_screen(call.from_user.id)
    if rendered is None:
        await call.answer(
            "Подписка недоступна" if language == "ru" else "Subscription unavailable",
            show_alert=True,
        )
        return
    text, markup = rendered
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)  # type: ignore
    await call.answer()


@router.callback_query(F.data == "tg_proxy_open")
async def cq_tg_proxy_open(call: CallbackQuery):
    if not await require_privacy(call):
        return
    language = await get_user_language(call.from_user.id)

    async with async_session_maker() as session:
        result = await session.execute(
            select(Server)
            .where(
                Server.is_active == True,  # noqa: E712
                Server.mtproxy_secret.isnot(None),
                Server.mtproxy_port.isnot(None),
            )
            .order_by(Server.display_order, Server.name)
        )
        servers = result.scalars().all()

    if not servers:
        text = f"{html.bold(t(language, 'tg_proxy_title'))}\n\n{t(language, 'tg_proxy_none')}"
    else:
        lines = [html.bold(t(language, "tg_proxy_title")), "", t(language, "tg_proxy_hint"), ""]
        for server in servers:
            label = f"{server.flag} {server.name}" if server.flag else server.name
            link = SubscriptionService.build_mtproxy_link(server)
            if not link:
                continue
            lines.append(f"{label}: {html.link(label, link)}")
        text = "\n".join(lines)

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(language, "back_to_subscription"), callback_data="subscription_open")]
    ])
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)  # type: ignore
    await call.answer()


_TRIAL_ALERT_KEYS = {
    "banned": "access_unavailable",
    "already_used": "trial_already_used",
    "active_exists": "trial_active_subscription_exists",
    "failed": "trial_activation_failed",
}


@router.callback_query(F.data == "trial_activate")
async def cq_trial_activate(call: CallbackQuery):
    if not call.from_user:
        await call.answer()
        return
    if not await require_privacy(call):
        return

    language, status = await UserService.activate_trial(
        call.from_user.id, call.from_user.username, call.from_user.language_code
    )
    if status != "started":
        await call.answer(t(language, _TRIAL_ALERT_KEYS[status]), show_alert=True)
        return

    text, markup = await render_subscription_screen(call.from_user.id)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)  # type: ignore
    await call.answer(t(language, "trial_started"), show_alert=True)
