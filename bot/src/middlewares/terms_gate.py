"""Global legal-acceptance gate.

Until a user accepts the current TERMS_VERSION (Privacy Policy + Terms of
Service), every message and callback is intercepted and the acceptance gate is
re-shown instead of running the real handler. The ONLY thing allowed through is
the "Принять" accept callback itself, so the user can never deadlock. The
document links are plain Telegraph URLs in the message body and need no handler.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.types import User as TgUser

from src.handlers.user.terms import ACCEPT_CALLBACK, gate_keyboard, gate_text, show_gate
from src.services import UserService, get_user_language


def _is_start_command(message: Message) -> bool:
    text = message.text or ""
    first = text.split(maxsplit=1)[0] if text else ""
    # Matches "/start" and "/start@BotName", with or without deep-link args.
    return first == "/start" or first.startswith("/start@")


class TermsGateMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")

        # No identifiable human (service updates, channel posts, bots) -> pass.
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)

        # Always let the accept tap through so the gate is escapable.
        if isinstance(event, CallbackQuery) and event.data == ACCEPT_CALLBACK:
            return await handler(event, data)

        # Let /start through even when not accepted: cmd_start registers the
        # user (capturing the referral) and then renders the gate itself.
        if isinstance(event, Message) and _is_start_command(event):
            return await handler(event, data)

        if await UserService.is_terms_accepted(tg_user.id):
            return await handler(event, data)

        # Not accepted: re-show the gate instead of the requested action.
        language = await get_user_language(tg_user.id)

        if isinstance(event, Message):
            name = tg_user.first_name
            await show_gate(event, language, name)
            return None

        if isinstance(event, CallbackQuery):
            await event.answer()
            if event.message is not None:
                await event.message.answer(
                    await gate_text(language, tg_user.first_name),
                    parse_mode="HTML",
                    reply_markup=await gate_keyboard(language),
                    disable_web_page_preview=True,
                )
            return None

        # Unknown event type carrying a user (e.g. inline query): block silently.
        return None
