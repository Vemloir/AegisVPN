"""Persistent mapping: a message in an operator's chat -> the user it came from.

When a user writes to the support bot, the bot copies that message into each
operator's chat and records (operator_chat_id, copied_message_id) -> user_id.
When an operator *replies* to that copied message, we look the user back up and
relay the reply. Kept in sqlite so the mapping survives a bot restart (an
operator answering a ticket from yesterday must still reach the right user).
"""

from __future__ import annotations

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS msg_map (
    operator_chat_id INTEGER NOT NULL,
    operator_msg_id  INTEGER NOT NULL,
    user_id          INTEGER NOT NULL,
    PRIMARY KEY (operator_chat_id, operator_msg_id)
);
"""


class Storage:
    def __init__(self, path: str) -> None:
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(_SCHEMA)
            await db.commit()

    async def put(self, operator_chat_id: int, operator_msg_id: int, user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO msg_map (operator_chat_id, operator_msg_id, user_id) VALUES (?, ?, ?)",
                (operator_chat_id, operator_msg_id, user_id),
            )
            await db.commit()

    async def get_user(self, operator_chat_id: int, operator_msg_id: int) -> int | None:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT user_id FROM msg_map WHERE operator_chat_id = ? AND operator_msg_id = ?",
                (operator_chat_id, operator_msg_id),
            ) as cur:
                row = await cur.fetchone()
                return int(row[0]) if row else None
