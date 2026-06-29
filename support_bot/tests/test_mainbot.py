import aiosqlite

from src import mainbot
from src.config import settings


async def _make_main_db(path: str, rows: list[tuple[int, str]]) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute("CREATE TABLE users (tg_id INTEGER PRIMARY KEY, language TEXT)")
        await db.executemany("INSERT INTO users (tg_id, language) VALUES (?, ?)", rows)
        await db.commit()


async def test_reads_language_from_main_db(tmp_path, monkeypatch):
    db = tmp_path / "main.db"
    await _make_main_db(str(db), [(42, "en"), (7, "ru")])
    monkeypatch.setattr(settings, "main_db_path", str(db))
    assert await mainbot.main_bot_language(42) == "en"
    assert await mainbot.main_bot_language(7) == "ru"


async def test_unknown_user_is_none(tmp_path, monkeypatch):
    db = tmp_path / "main.db"
    await _make_main_db(str(db), [(42, "en")])
    monkeypatch.setattr(settings, "main_db_path", str(db))
    assert await mainbot.main_bot_language(999) is None


async def test_missing_db_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "main_db_path", str(tmp_path / "nope.db"))
    assert await mainbot.main_bot_language(1) is None


async def test_bad_language_value_normalized(tmp_path, monkeypatch):
    db = tmp_path / "main.db"
    await _make_main_db(str(db), [(42, "de")])  # unsupported -> ru
    monkeypatch.setattr(settings, "main_db_path", str(db))
    assert await mainbot.main_bot_language(42) == "ru"
