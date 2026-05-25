"""Keep the stored username fresh on every interaction.

Telegram doesn't let bots resolve an arbitrary @username to an ID, so admin
lookup by username relies on what we have in the DB. If a user changed their
username and hasn't triggered /start since, the DB is stale and they can't be
found by the new name. This middleware upserts the current username (and
keeps the row's username in sync) on ANY update carrying a from_user, so the
DB self-heals the moment the user touches the bot again.
"""
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User as TgUser
from sqlalchemy import select

from src.core.database import async_session_maker
from src.models import User


class IdentitySyncMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is not None and not tg_user.is_bot:
            try:
                async with async_session_maker() as session:
                    result = await session.execute(
                        select(User).where(User.tg_id == tg_user.id)
                    )
                    user = result.scalar_one_or_none()
                    if user is not None and user.username != tg_user.username:
                        user.username = tg_user.username
                        await session.commit()
            except Exception:
                # never block the handler over a username refresh
                pass
        return await handler(event, data)
