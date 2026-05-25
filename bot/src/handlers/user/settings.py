"""Settings screen: language switch and account deletion."""

from aiogram import F, Router, html
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from src.services import (
    UserService,
    get_user_language,
    language_label,
    set_user_language,
    t,
)

from .keyboards import delete_account_keyboard, language_keyboard, settings_keyboard

router = Router()


async def render_settings_text(tg_id: int) -> tuple[str, str]:
    language = await get_user_language(tg_id)
    text = (
        f"{html.bold(t(language, 'settings_title'))}\n\n"
        f"{t(language, 'settings_language', language_name=language_label(language))}"
    )
    return text, language


@router.message(Command("settings"))
async def cmd_settings(message: Message):
    if not message.from_user:
        return

    await UserService.ensure_user(message.from_user.id, message.from_user.username, message.from_user.language_code)
    text, language = await render_settings_text(message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=settings_keyboard(language))


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
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=settings_keyboard(updated))  # type: ignore
    await call.answer()
