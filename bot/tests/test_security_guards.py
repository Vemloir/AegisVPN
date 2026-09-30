from types import SimpleNamespace

from src.core.config import settings
from src.handlers.admin import _AdminOnly


async def test_admin_router_rejects_non_admins(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids", [111])
    guard = _AdminOnly()
    assert await guard(SimpleNamespace(from_user=SimpleNamespace(id=111))) is True
    assert await guard(SimpleNamespace(from_user=SimpleNamespace(id=222))) is False
    assert await guard(SimpleNamespace(from_user=None)) is False


async def test_command_during_admin_input_reaches_its_handler(monkeypatch):
    from datetime import datetime

    from aiogram import Bot, Dispatcher, Router
    from aiogram.filters import Command
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import Chat, Message, Update, User

    from src.handlers.admin import AdminStates
    from src.handlers.admin import router as admin_router

    monkeypatch.setattr(settings, "admin_ids", [111])
    seen: list[str] = []
    user_router = Router()

    @user_router.message(Command("start"))
    async def start(message: Message) -> None:
        seen.append(message.text or "")

    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.include_router(admin_router)
    dp.include_router(user_router)
    bot = Bot("42:TEST")
    key = StorageKey(bot_id=bot.id, chat_id=111, user_id=111)
    await storage.set_state(key, AdminStates.waiting_user_lookup)

    message = Message(
        message_id=1,
        date=datetime.now(),
        chat=Chat(id=111, type="private"),
        from_user=User(id=111, is_bot=False, first_name="Admin"),
        text="/start",
    )
    await dp.feed_update(bot, Update(update_id=1, message=message))

    assert seen == ["/start"]
    assert await storage.get_state(key) is None
    await bot.session.close()
