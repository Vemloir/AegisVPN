import math
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiogram import F, Router, html
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from src.core.config import settings
from src.core.database import async_session_maker
from src.core.logger import logger
from src.models import Subscription, User
from src.services import (
    ServerAccessService,
    SubscriptionService,
    get_user_language,
    language_label,
    set_user_language,
    t,
)
from src.services.telegraph import get_privacy_url

router = Router()

_PRIVACY_DIR = Path(__file__).resolve().parent.parent / "privacy"


def pick_language(code: str | None) -> str:
    """Map a Telegram language_code to a supported UI language."""
    return "en" if (code or "").lower().startswith("en") else "ru"


def load_privacy_text(language: str) -> str:
    lang = language if language in ("ru", "en") else "ru"
    path = _PRIVACY_DIR / f"privacy_{lang}.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return "Privacy policy is currently unavailable."


async def privacy_button(language: str) -> InlineKeyboardButton:
    """A Telegraph URL button if publishing succeeds, else a text fallback."""
    title = "Политика конфиденциальности AegisVPN" if language == "ru" else "AegisVPN Privacy Policy"
    url = await get_privacy_url(language, load_privacy_text(language), title)
    if url:
        return InlineKeyboardButton(text=t(language, "privacy_button"), url=url)
    return InlineKeyboardButton(text=t(language, "privacy_button"), callback_data="privacy_show")


async def privacy_gate_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [await privacy_button(language)],
            [InlineKeyboardButton(text=t(language, "privacy_accept_button"), callback_data="privacy_accept")],
        ]
    )


async def is_privacy_accepted(tg_id: int) -> bool:
    async with async_session_maker() as session:
        result = await session.execute(select(User.privacy_accepted).where(User.tg_id == tg_id))
        return bool(result.scalar_one_or_none())


async def require_privacy(call: CallbackQuery) -> bool:
    """Returns True if the user may proceed; otherwise answers an alert."""
    if await is_privacy_accepted(call.from_user.id):
        return True
    language = await get_user_language(call.from_user.id)
    await call.answer(t(language, "privacy_required"), show_alert=True)
    return False


def subscription_keyboard(
    language: str,
    has_active_subscription: bool = False,
    show_trial: bool = False,
    has_v2ray: bool = False,
    is_lifetime: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    # The subscription link is now shown inline on the screen; offer the setup
    # guide directly instead of a button that just revealed the link.
    if has_v2ray:
        rows.append([InlineKeyboardButton(text=t(language, "setup_guide"), callback_data="help_setup:v2ray")])
    # No point offering "renew" to a lifetime subscription.
    if not (has_active_subscription and is_lifetime):
        action_text = t(language, "renew_vpn") if has_active_subscription else t(language, "buy_vpn")
        rows.append([InlineKeyboardButton(text=action_text, callback_data="buy_plan")])
    if show_trial and not has_active_subscription:
        rows.append([InlineKeyboardButton(text=t(language, "trial_vpn"), callback_data="trial_activate")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_detail_keyboard(language: str, kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(language, "setup_guide"), callback_data=f"help_setup:{kind}")],
            [InlineKeyboardButton(text=t(language, "back_to_subscription"), callback_data="subscription_open")],
        ]
    )


def settings_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(language, "change_language"), callback_data="settings_language")],
            [InlineKeyboardButton(text=t(language, "delete_account"), callback_data="account_delete")],
        ]
    )


def delete_account_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(language, "delete_account_confirm"), callback_data="account_delete_confirm")],
            [InlineKeyboardButton(text=t(language, "back_to_settings"), callback_data="settings_open")],
        ]
    )


def language_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Русский", callback_data="settings_set_language:ru"),
                InlineKeyboardButton(text="English", callback_data="settings_set_language:en"),
            ],
            [InlineKeyboardButton(text=t(language, "back_to_settings"), callback_data="settings_open")],
        ]
    )


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
                Subscription.is_active == True,
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
        subscription_link = (
            SubscriptionService.build_subscription_url(
                sub.sub_token,
            )
            if accessible_servers else None
        )

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

        text = (
            f"{html.bold(t(language, 'subscription_active_title'))}\n\n"
            f"{details_block}"
        )
        if subscription_link:
            # Show the link inline instead of hiding it behind a button.
            text += (
                f"{t(language, 'subscription_link', link=html.code(subscription_link))}\n\n"
                f"{t(language, 'v2ray_detail_hint')}"
            )
        else:
            text += t(language, "subscription_hint")
        return text, subscription_link, language, False, is_lifetime


async def subscription_state(tg_id: int) -> tuple[bool, bool]:
    """Returns (has_active_subscription, is_lifetime)."""
    async with async_session_maker() as session:
        user_result = await session.execute(select(User).where(User.tg_id == tg_id))
        user = user_result.scalar_one_or_none()
        if not user or user.is_banned:
            return False, False

        now = datetime.now(UTC).replace(tzinfo=None)
        sub_result = await session.execute(
            select(Subscription).where(
                Subscription.user_id == user.id,
                Subscription.is_active == True,
                Subscription.expires_at > now,
            )
        )
        sub = sub_result.scalar_one_or_none()
        if not sub:
            return False, False
        return True, SubscriptionService.is_lifetime_subscription(sub)


async def has_active_subscription(tg_id: int) -> bool:
    active, _ = await subscription_state(tg_id)
    return active


async def render_settings_text(tg_id: int) -> tuple[str, str]:
    language = await get_user_language(tg_id)
    text = (
        f"{html.bold(t(language, 'settings_title'))}\n\n"
        f"{t(language, 'settings_language', language_name=language_label(language))}"
    )
    return text, language


async def render_instruction_text(tg_id: int, kind: str) -> str:
    language = await get_user_language(tg_id)
    return t(language, "setup_text_v2ray")


async def render_subscription_screen(tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    text, subscription_link, language, show_trial, is_lifetime = await render_subscription_info(tg_id)
    markup = subscription_keyboard(
        language,
        has_active_subscription=subscription_link is not None,
        show_trial=show_trial,
        has_v2ray=subscription_link is not None,
        is_lifetime=is_lifetime,
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


@router.message(CommandStart())
async def cmd_start(message: Message):
    if not message.from_user:
        return

    tg_id = message.from_user.id
    username = message.from_user.username

    referrer_id = None
    args = message.text.split()[1:] if message.text else []
    if args and args[0].startswith("ref_"):
        try:
            referrer_id = int(args[0].replace("ref_", ""))
        except ValueError:
            referrer_id = None

    async with async_session_maker() as session:
        result = await session.execute(select(User).where(User.tg_id == tg_id))
        user = result.scalar_one_or_none()

        if not user:
            if referrer_id == tg_id:
                referrer_id = None
            user = User(
                tg_id=tg_id,
                username=username,
                referrer_id=referrer_id,
                language=pick_language(message.from_user.language_code),
            )
            session.add(user)
        else:
            user.username = username

        await session.commit()

        if user.is_banned:
            await message.answer("You are banned." if user.language == "en" else "Вы заблокированы.")
            return

        language = user.language
        can_use_trial = not user.trial_used
        privacy_ok = user.privacy_accepted

    # Privacy gate: a user must accept the policy before using the bot.
    if not privacy_ok:
        await message.answer(
            t(language, "privacy_gate_text"),
            parse_mode="HTML",
            reply_markup=await privacy_gate_keyboard(language),
        )
        return

    active_sub, is_lifetime = await subscription_state(tg_id)
    first_name = html.bold(message.from_user.first_name)

    await message.answer(
        t(language, "start_text", name=first_name),
        parse_mode="HTML",
        reply_markup=subscription_keyboard(
            language,
            has_active_subscription=active_sub,
            show_trial=can_use_trial and not active_sub,
            is_lifetime=is_lifetime,
        ),
    )


@router.callback_query(F.data == "privacy_show")
async def cq_privacy_show(call: CallbackQuery):
    language = await get_user_language(call.from_user.id)
    await call.message.answer(load_privacy_text(language), parse_mode="HTML")  # type: ignore
    await call.answer()


@router.callback_query(F.data == "privacy_accept")
async def cq_privacy_accept(call: CallbackQuery):
    if not call.from_user:
        await call.answer()
        return
    async with async_session_maker() as session:
        result = await session.execute(select(User).where(User.tg_id == call.from_user.id))
        user = result.scalar_one_or_none()
        if user is None:
            await call.answer()
            return
        user.privacy_accepted = True
        language = user.language
        can_use_trial = not user.trial_used
        await session.commit()

    active_sub, is_lifetime = await subscription_state(call.from_user.id)
    first_name = html.bold(call.from_user.first_name)
    await call.message.edit_text(  # type: ignore
        t(language, "start_text", name=first_name),
        parse_mode="HTML",
        reply_markup=subscription_keyboard(
            language,
            has_active_subscription=active_sub,
            show_trial=can_use_trial and not active_sub,
            is_lifetime=is_lifetime,
        ),
    )
    await call.answer(t(language, "privacy_accepted_msg"), show_alert=True)


@router.message(Command("help"))
async def cmd_help(message: Message):
    if not message.from_user:
        return

    language = await get_user_language(message.from_user.id)
    text = t(language, "help_text")
    if message.from_user.id in settings.admin_ids:
        admin_line = "/admin - admin panel\n" if language == "en" else "/admin - админ-панель\n"
        text += f"\nAdmin:\n{admin_line}" if language == "en" else f"\nАдмин:\n{admin_line}"
    help_markup = InlineKeyboardMarkup(inline_keyboard=[[await privacy_button(language)]])
    await message.answer(text, reply_markup=help_markup)


@router.message(Command("subscription"))
async def cmd_subscription(message: Message):
    if not message.from_user:
        return

    if not await is_privacy_accepted(message.from_user.id):
        language = await get_user_language(message.from_user.id)
        await message.answer(
            t(language, "privacy_gate_text"),
            parse_mode="HTML",
            reply_markup=await privacy_gate_keyboard(language),
        )
        return

    text, markup = await render_subscription_screen(message.from_user.id)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.message(Command("settings"))
async def cmd_settings(message: Message):
    if not message.from_user:
        return

    async with async_session_maker() as session:
        result = await session.execute(select(User).where(User.tg_id == message.from_user.id))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(
                tg_id=message.from_user.id,
                username=message.from_user.username,
                referrer_id=None,
                language=pick_language(message.from_user.language_code),
            )
            session.add(user)
            await session.commit()

    text, language = await render_settings_text(message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=settings_keyboard(language))


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
        await call.answer("Подписка недоступна" if language == "ru" else "Subscription unavailable", show_alert=True)
        return
    text, markup = rendered
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)  # type: ignore
    await call.answer()


@router.callback_query(F.data == "trial_activate")
async def cq_trial_activate(call: CallbackQuery):
    if not call.from_user:
        await call.answer()
        return
    if not await require_privacy(call):
        return

    async with async_session_maker() as session:
        result = await session.execute(select(User).where(User.tg_id == call.from_user.id))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(
                tg_id=call.from_user.id,
                username=call.from_user.username,
                referrer_id=None,
                language=pick_language(call.from_user.language_code),
            )
            session.add(user)
            await session.flush()
        else:
            user.username = call.from_user.username

        language = user.language
        if user.is_banned:
            await call.answer(t(language, "access_unavailable"), show_alert=True)
            return

        if user.trial_used:
            await call.answer(t(language, "trial_already_used"), show_alert=True)
            return

        now = datetime.now(UTC).replace(tzinfo=None)
        sub_result = await session.execute(
            select(Subscription).where(
                Subscription.user_id == user.id,
                Subscription.is_active == True,
                Subscription.expires_at > now,
            )
        )
        active_sub = sub_result.scalar_one_or_none()
        if active_sub is not None:
            await call.answer(t(language, "trial_active_subscription_exists"), show_alert=True)
            return

        sub = Subscription(
            user_id=user.id,
            sub_token=await SubscriptionService.generate_sub_token(session),
            client_uuid=str(uuid.uuid4()),
            plan_days=3,
            started_at=now,
            expires_at=now + timedelta(days=3),
            is_active=True,
        )
        session.add(sub)
        user.trial_used = True

        try:
            await session.flush()
            active_servers = await ServerAccessService.get_accessible_servers_for_user(session, user.id)
            await SubscriptionService.sync_subscription_to_servers(session, sub, active_servers)
            await session.commit()
        except Exception as exc:
            await session.rollback()
            logger.error("Failed to activate trial for %s: %s", call.from_user.id, exc)
            await call.answer(t(language, "trial_activation_failed"), show_alert=True)
            return

    text, markup = await render_subscription_screen(call.from_user.id)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)  # type: ignore
    await call.answer(t(language, "trial_started"), show_alert=True)


@router.callback_query(F.data == "settings_open")
async def cq_settings_open(call: CallbackQuery):
    text, language = await render_settings_text(call.from_user.id)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=settings_keyboard(language))  # type: ignore
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
    language = await get_user_language(call.from_user.id)
    async with async_session_maker() as session:
        result = await session.execute(select(User).where(User.tg_id == call.from_user.id))
        user = result.scalar_one_or_none()
        if user is None:
            await call.answer()
            return
        # revoke access on all nodes for every subscription, then delete the
        # user (subscriptions cascade-delete with the row)
        subs_result = await session.execute(
            select(Subscription).where(Subscription.user_id == user.id)
        )
        for sub in subs_result.scalars().all():
            await SubscriptionService.remove_subscription_from_servers(session, sub)
        await session.delete(user)
        await session.commit()

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
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=settings_keyboard(updated))  # type: ignore
    await call.answer()
