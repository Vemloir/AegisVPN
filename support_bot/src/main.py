"""AegisVPN support bot — a two-way relay between users and operators.

Long-polling (no inbound port needed). A user's message is copied to every
operator with a who-wrote banner; an operator answers by *replying* to that
forwarded message and the reply is copied back to the user. Media is supported
via copy_message (it carries any content type). Token comes from the env.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import Message

from .config import settings
from .routing import Action, decide, operator_header
from .storage import Storage

logger = logging.getLogger("support_bot")

dp = Dispatcher()
storage = Storage(settings.db_path)
ADMIN_IDS = set(settings.admin_ids)


def _is_start(message: Message) -> bool:
    return bool(message.text and message.text.strip().split()[0] in ("/start", "/start@"))


async def _forward_to_operators(message: Message, bot: Bot) -> None:
    u = message.from_user
    if u is None:
        return
    header = operator_header(full_name=u.full_name, user_id=u.id, username=u.username)
    delivered = 0
    for operator_id in ADMIN_IDS:
        try:
            await bot.send_message(operator_id, header)
            copied = await bot.copy_message(
                chat_id=operator_id, from_chat_id=message.chat.id, message_id=message.message_id
            )
            await storage.put(operator_id, copied.message_id, u.id)
            delivered += 1
        except Exception as exc:  # one operator being unreachable must not drop the ticket for the rest
            logger.warning("Failed to deliver ticket to operator %s: %s", operator_id, exc)
    if delivered:
        await message.answer(settings.received_text)
    else:
        logger.error("Ticket from user %s reached no operator", u.id)


async def _relay_to_user(message: Message, bot: Bot) -> None:
    replied = message.reply_to_message
    if replied is None:
        await message.answer("Чтобы ответить пользователю, ответьте (reply) на его сообщение.")
        return
    user_id = await storage.get_user(message.chat.id, replied.message_id)
    if user_id is None:
        await message.answer("Не удалось определить получателя. Ответьте на конкретное сообщение пользователя.")
        return
    try:
        await bot.copy_message(chat_id=user_id, from_chat_id=message.chat.id, message_id=message.message_id)
        await message.answer("Отправлено.")
    except Exception as exc:
        logger.warning("Failed to relay reply to user %s: %s", user_id, exc)
        await message.answer("Не удалось доставить ответ (пользователь мог остановить бота).")


@dp.message()
async def handle(message: Message, bot: Bot) -> None:
    action = decide(
        is_start_command=_is_start(message),
        is_private_chat=message.chat.type == "private",
        from_user_id=message.from_user.id if message.from_user else None,
        admin_ids=ADMIN_IDS,
        is_reply=message.reply_to_message is not None,
    )
    if action is Action.WELCOME:
        await message.answer(settings.welcome_text)
    elif action is Action.FORWARD_TO_OPERATORS:
        await _forward_to_operators(message, bot)
    elif action is Action.RELAY_TO_USER:
        await _relay_to_user(message, bot)
    elif action is Action.OPERATOR_REPLY_HINT:
        await message.answer("Чтобы ответить пользователю, ответьте (reply) на его сообщение.")
    # Action.IGNORE: do nothing (non-private chats etc.)


async def main() -> None:
    logging.basicConfig(level=settings.log_level)
    await storage.init()
    bot = Bot(token=settings.support_bot_token.get_secret_value())
    logger.info("Support bot starting (polling); %d operator(s) configured", len(ADMIN_IDS))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
