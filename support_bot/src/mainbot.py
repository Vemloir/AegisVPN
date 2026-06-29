"""Read the user's language from the MAIN bot's sqlite DB (read-only).

The main bot stores `users(tg_id, language)` in a sqlite file that we mount
read-only at ``main_db_path``. We only ever SELECT, never write. Any problem
(file not mounted, table missing, locked) returns None so the caller falls back
to the support bot's own preference / default — the support bot never depends on
the main bot being present (e.g. in local dev)."""

from __future__ import annotations

import logging

import aiosqlite

from .config import settings
from .i18n import normalize_lang

logger = logging.getLogger("support_bot")


async def main_bot_language(tg_id: int) -> str | None:
    try:
        async with aiosqlite.connect(f"file:{settings.main_db_path}?mode=ro", uri=True) as db:
            async with db.execute("SELECT language FROM users WHERE tg_id = ?", (tg_id,)) as cur:
                row = await cur.fetchone()
                return normalize_lang(row[0]) if row and row[0] else None
    except Exception as exc:
        logger.debug("main_bot_language(%s) unavailable: %s", tg_id, exc)
        return None
