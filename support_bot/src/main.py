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

from .config import settings
from .deps import ADMIN_IDS, storage
from .handlers import router

logger = logging.getLogger("support_bot")


async def main() -> None:
    logging.basicConfig(level=settings.log_level)
    await storage.init()
    bot = Bot(token=settings.support_bot_token.get_secret_value())
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Support ticket bot starting (polling); %d operator(s) configured", len(ADMIN_IDS))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
