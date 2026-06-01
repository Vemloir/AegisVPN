"""Settings screen: language switch and account deletion."""

import asyncio
from datetime import UTC, datetime, timedelta
from aiogram import F, Router, html
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from sqlalchemy import select

from src.core.database import async_session_maker
from src.models import Subscription
from src.services import (
    ServerAccessService,
    SubscriptionService,
    UserService,
    get_user_language,
    language_label,
    set_user_language,
    t,
)

from .keyboards import delete_account_keyboard, language_keyboard, settings_keyboard

router = Router()


async def _has_active_subscription(user_id: int) -> bool:
    async with async_session_maker() as session:
        result = await session.execute(
            select(Subscription).where(
                Subscription.user_id == user_id,
                Subscription.is_active == True,
            )
        )
        return result.scalar_one_or_none() is not None


async def render_settings_text(tg_id: int) -> tuple[str, str, bool]:
    language = await get_user_language(tg_id)
    has_active = await _has_active_subscription(tg_id)
    text = (
        f"{html.bold(t(language, 'settings_title'))}\n\n"
        f"{t(language, 'settings_language', language_name=language_label(language))}"
    )
    return text, language, has_active


@router.message(Command("settings"))
async def cmd_settings(message: Message):
    if not message.from_user:
        return

    await UserService.ensure_user(message.from_user.id, message.from_user.username, message.from_user.language_code)
    text, language, has_active = await render_settings_text(message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=settings_keyboard(language, has_active))


@router.callback_query(F.data == "settings_open")
async def cq_settings_open(call: CallbackQuery):
    text, language, has_active = await render_settings_text(call.from_user.id)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=settings_keyboard(language, has_active))  # type: ignore
    await call.answer()


@router.callback_query(F.data == "settings_language")
async def cq_settings_language(call: CallbackQuery):
    language = await get_user_language(call.from_user.id)
    await call.message.edit_text(  # type: ignore
        t(language, "language_select"),
        reply_markup=language_keyboard(language),
    )
    await call.answer()


@router.callback_query(F.data == "account_delete")
async def cq_account_delete(call: CallbackQuery):
    language = await get_user_language(call.from_user.id)
    await call.message.edit_text(  # type: ignore
        t(language, "delete_account_warning"),
        parse_mode="HTML",
        reply_markup=delete_account_keyboard(language),
    )
    await call.answer()


@router.callback_query(F.data == "account_delete_confirm")
async def cq_account_delete_confirm(call: CallbackQuery):
    if not call.from_user:
        await call.answer()
        return
    language = await UserService.delete_account(call.from_user.id)
    if language is None:
        await call.answer()
        return

    await call.message.edit_text(t(language, "delete_account_done"), parse_mode="HTML")  # type: ignore
    await call.answer()


@router.callback_query(F.data.startswith("settings_set_language:"))
async def cq_settings_set_language(call: CallbackQuery):
    language = call.data.split(":", 1)[1]  # type: ignore[union-attr]
    updated = await set_user_language(call.from_user.id, language)
    if updated is None:
        await call.answer("User not found", show_alert=True)
        return

    text = (
        f"{html.bold(t(updated, 'settings_title'))}\n\n"
        f"{t(updated, 'language_updated')}\n"
        f"{t(updated, 'settings_language', language_name=language_label(updated))}"
    )
    has_active = await _has_active_subscription(call.from_user.id)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=settings_keyboard(updated, has_active))  # type: ignore
    await call.answer()


@router.callback_query(F.data == "reissue_subscription")
async def cq_reissue_subscription(call: CallbackQuery):
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    language = await get_user_language(call.from_user.id)
    await call.message.edit_text(  # type: ignore
        t(language, "reissue_subscription_confirm"),
        parse_mode="HTML",
        reply_markup=InlineKeyboardBuilder.from_button(
            InlineKeyboardButton(text=t(language, "reissue_subscription"), callback_data="reissue_subscription_confirm")
        )
        .row([InlineKeyboardButton(text=t(language, "back"), callback_data="settings_open")])
        .as_markup(),
    )
    await call.answer()


@router.callback_query(F.data == "reissue_subscription_confirm")
async def cq_reissue_subscription_confirm(call: CallbackQuery):
    if not call.from_user:
        await call.answer()
        return

    language = await get_user_language(call.from_user.id)
    await call.message.edit_text(t(language, "reissue_subscription_in_progress"))  # type: ignore
    await call.answer()

    async with async_session_maker() as session:
        result = await session.execute(
            select(Subscription).where(
                Subscription.user_id == call.from_user.id,
                Subscription.is_active == True,
            )
        )
        old_sub = result.scalar_one_or_none()

        if not old_sub:
            await call.message.edit_text(t(language, "reissue_subscription_no_active"))  # type: ignore
            await call.answer()
            return

        # Deactivate old subscription
        old_sub.is_active = False
        await SubscriptionService.remove_subscription_from_servers(session, old_sub)

        # Create new subscription with the SAME client_uuid
        new_token = await SubscriptionService.generate_sub_token(session)
        new_sub = Subscription(
            user_id=call.from_user.id,
            sub_token=new_token,
            client_uuid=old_sub.client_uuid,
            plan_days=old_sub.plan_days,
            started_at=datetime.now(UTC).replace(tzinfo=None),
            expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=old_sub.plan_days),
            is_active=True,
        )
        session.add(new_sub)
        await session.flush()

        # Sync to servers
        servers = await ServerAccessService.get_accessible_servers_for_user(session, call.from_user.id)
        await SubscriptionService.sync_subscription_to_servers(session, new_sub, servers)
        await session.commit()

        link = html.code(SubscriptionService.build_subscription_url(new_token))
        success_text = t(language, "reissue_subscription_success", link=link)
        await call.message.edit_text(success_text, parse_mode="HTML")  # type: ignore
        await call.answer()
