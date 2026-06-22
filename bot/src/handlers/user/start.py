"""Entry points: /start (with referral + legal-acceptance gate) and /help."""

from aiogram import Router, html
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from src.core.config import settings
from src.services import UserService, get_user_language, t

from .keyboards import subscription_keyboard
from .terms import show_gate

router = Router()


def _parse_referrer(text: str | None, tg_id: int) -> int | None:
    args = text.split()[1:] if text else []
    if args and args[0].startswith("ref_"):
        try:
            return int(args[0].replace("ref_", ""))
        except ValueError:
            return None
    return None


@router.message(CommandStart())
async def cmd_start(message: Message):
    if not message.from_user:
        return

    tg_id = message.from_user.id
    referrer_id = _parse_referrer(message.text, tg_id)

    language, can_use_trial, terms_ok, is_banned = await UserService.register_or_update_on_start(
        tg_id, message.from_user.username, message.from_user.language_code, referrer_id
    )

    if is_banned:
        await message.answer("You are banned." if language == "en" else "Вы заблокированы.")
        return

    # Legal-acceptance gate: a user must accept the current Privacy Policy +
    # Terms of Service before using the bot. (The global TermsGateMiddleware
    # enforces this for every other interaction; /start renders it here so the
    # referral above is still captured for brand-new users.)
    if not terms_ok:
        await show_gate(message, language, message.from_user.first_name)
        return

    active_sub, is_lifetime = await UserService.subscription_state(tg_id)
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


@router.message(Command("help"))
async def cmd_help(message: Message):
    if not message.from_user:
        return

    language = await get_user_language(message.from_user.id)
    # Privacy/ToS content lives in /info now; /help only lists commands.
    text = t(language, "help_text")
    if message.from_user.id in settings.admin_ids:
        admin_line = "/admin - admin panel\n" if language == "en" else "/admin - админ-панель\n"
        text += f"\nAdmin:\n{admin_line}" if language == "en" else f"\nАдмин:\n{admin_line}"
    await message.answer(text)
