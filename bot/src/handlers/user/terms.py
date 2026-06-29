"""Mandatory legal-acceptance gate: Privacy Policy + Terms of Service.

A user must accept the current TERMS_VERSION (both documents at once) before
using the bot. The gate is enforced globally by ``TermsGateMiddleware``; this
module owns the gate message, the single "Принять" button, the accept
callback, and resolving both Telegraph URLs.
"""

from pathlib import Path

from aiogram import F, Router, html
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.services import UserService, get_user_language, t
from src.services.telegraph import get_privacy_url, get_tos_url

from .keyboards import subscription_keyboard

router = Router()

ACCEPT_CALLBACK = "terms_accept"

GITHUB_URL = "https://github.com/demented484/AegisVPN"
NEWS_URL = "https://t.me/AegisVPNnews"
SUPPORT_URL = "https://t.me/AegisVPNsupportBot"

_DOCS_DIR = Path(__file__).resolve().parents[2] / "privacy"

# The Terms of Service is a single Russian legal document (public offer). There
# is no separate EN file, so the ToS URL is the same RU page for every UI
# language; only the link label is localized.
_TOS_TITLE = "Пользовательское соглашение AegisVPN"


def _load_doc(name: str) -> str:
    try:
        return (_DOCS_DIR / name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def load_privacy_text(language: str) -> str:
    lang = language if language in ("ru", "en") else "ru"
    text = _load_doc(f"privacy_{lang}.md")
    return text or "Privacy policy is currently unavailable."


def load_tos_text() -> str:
    return _load_doc("tos_ru.md") or "Пользовательское соглашение временно недоступно."


async def resolve_privacy_url(language: str) -> str | None:
    title = "Политика конфиденциальности AegisVPN" if language == "ru" else "AegisVPN Privacy Policy"
    return await get_privacy_url(language, load_privacy_text(language), title)


async def resolve_tos_url() -> str | None:
    return await get_tos_url("ru", load_tos_text(), _TOS_TITLE)


def _doc_link(url: str | None, label: str) -> str:
    """Telegraph HTML hyperlink, or a plain label if publishing failed."""
    if url:
        return f'<a href="{url}">{label}</a>'
    return label


async def doc_links_block(language: str) -> str:
    """The two documents as HTML hyperlinks, each on its own line."""
    privacy_url = await resolve_privacy_url(language)
    tos_url = await resolve_tos_url()
    privacy_link = _doc_link(privacy_url, t(language, "doc_privacy_label"))
    tos_link = _doc_link(tos_url, t(language, "doc_tos_label"))
    return f"{privacy_link}\n{tos_link}"


async def gate_text(language: str, name: str | None = None) -> str:
    # The two documents are now URL BUTTONS (see gate_keyboard), so the body is
    # just the greeting + intro — no inline doc links.
    greeting = t(language, "terms_gate_greeting", name=html.bold(name or ""))
    return f"{greeting}\n\n{t(language, 'terms_gate_intro')}"


async def gate_keyboard(language: str) -> InlineKeyboardMarkup:
    """Two document URL buttons (Privacy + ToS) on one row, then Accept.

    The Telegraph URLs are resolved (and published-on-demand) here; if either
    fails to resolve we simply omit that button rather than crash the gate — the
    Accept button is always present so the user can never deadlock.
    """
    privacy_url = await resolve_privacy_url(language)
    tos_url = await resolve_tos_url()

    doc_row: list[InlineKeyboardButton] = []
    if privacy_url:
        doc_row.append(InlineKeyboardButton(text=t(language, "doc_privacy_label"), url=privacy_url))
    if tos_url:
        doc_row.append(InlineKeyboardButton(text=t(language, "doc_tos_label"), url=tos_url))

    rows: list[list[InlineKeyboardButton]] = []
    if doc_row:
        rows.append(doc_row)
    rows.append([InlineKeyboardButton(text=t(language, "terms_accept_button"), callback_data=ACCEPT_CALLBACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_gate(message: Message, language: str, name: str | None = None) -> None:
    """Render the acceptance gate as a single HTML message with doc buttons."""
    await message.answer(
        await gate_text(language, name),
        parse_mode="HTML",
        reply_markup=await gate_keyboard(language),
        disable_web_page_preview=True,
    )


@router.message(Command("info"))
async def cmd_info(message: Message):
    if not message.from_user:
        return
    language = await get_user_language(message.from_user.id)
    github_link = f'<a href="{GITHUB_URL}">{t(language, "info_github_label")}</a>'
    text = (
        f"{t(language, 'info_about')}\n\n"
        f"{await doc_links_block(language)}\n\n"
        f"{github_link}"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(language, "info_support_button"), url=SUPPORT_URL)],
            [InlineKeyboardButton(text=t(language, "info_news_button"), url=NEWS_URL)],
        ]
    )
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)


@router.callback_query(F.data == ACCEPT_CALLBACK)
async def cq_terms_accept(call: CallbackQuery):
    if not call.from_user:
        await call.answer()
        return

    language, can_use_trial = await UserService.accept_terms(
        call.from_user.id, call.from_user.username, call.from_user.language_code
    )

    active_sub, is_lifetime = await UserService.subscription_state(call.from_user.id)
    first_name = html.bold(call.from_user.first_name)
    await call.message.edit_text(  # type: ignore[union-attr]
        t(language, "start_text", name=first_name),
        parse_mode="HTML",
        reply_markup=subscription_keyboard(
            language,
            has_active_subscription=active_sub,
            show_trial=can_use_trial and not active_sub,
            is_lifetime=is_lifetime,
        ),
    )
    await call.answer(t(language, "terms_accepted_msg"), show_alert=True)
