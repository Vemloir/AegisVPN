"""Privacy policy gate: publishing, acceptance, and the access guard."""

from pathlib import Path

from aiogram import F, Router, html
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from src.services import UserService, get_user_language, t
from src.services.telegraph import get_privacy_url

from .keyboards import subscription_keyboard

router = Router()

_PRIVACY_DIR = Path(__file__).resolve().parents[2] / "privacy"


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


async def require_privacy(call: CallbackQuery) -> bool:
    """Returns True if the user may proceed; otherwise answers an alert."""
    if await UserService.is_privacy_accepted(call.from_user.id):
        return True
    language = await get_user_language(call.from_user.id)
    await call.answer(t(language, "privacy_required"), show_alert=True)
    return False


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

    accepted = await UserService.accept_privacy(call.from_user.id)
    if accepted is None:
        await call.answer()
        return
    language, can_use_trial = accepted

    active_sub, is_lifetime = await UserService.subscription_state(call.from_user.id)
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
