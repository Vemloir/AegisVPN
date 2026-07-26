"""AegisVPN support bot — ticket system.

Users create and manage tickets (title -> message, paginated list, view/reply/
close); operators get one consolidated message per update with the full history
and an inline Close button, and answer by replying to it. Long-polling; token
from env. Logic lives in storage/render/pagination (unit-tested); this module
only wires aiogram together.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from .config import settings
from .deps import ADMIN_IDS, storage
from .handlers import router
from .i18n import t
from .leader import run_support_leader

logger = logging.getLogger("support_bot")


def _commands(lang: str) -> list[BotCommand]:
    return [
        BotCommand(command="start", description=t(lang, "cmd_start")),
        BotCommand(command="new", description=t(lang, "cmd_new")),
        BotCommand(command="tickets", description=t(lang, "cmd_tickets")),
    ]


async def main() -> None:
    logging.basicConfig(level=settings.log_level)
    await storage.init()
    bot = Bot(token=settings.support_bot_token.get_secret_value())
    await bot.set_my_commands(_commands("ru"))  # default
    await bot.set_my_commands(_commands("en"), language_code="en")
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    async def poll() -> None:
        logger.info("Support ticket bot starting (polling leader); %d operator(s) configured", len(ADMIN_IDS))
        await dp.start_polling(bot)

    await run_support_leader(
        settings.leader_database_url,
        poll,
        retry_seconds=settings.leader_retry_seconds,
    )


if __name__ == "__main__":
    asyncio.run(main())
