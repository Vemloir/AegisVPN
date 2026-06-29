"""Ticket persistence (sqlite via aiosqlite).

Three tables:
- tickets          one row per ticket (status open|closed);
- ticket_messages  the full conversation, ordered by id;
- admin_msg_map    (operator_chat, operator_msg) -> ticket, so an operator's
                   reply-to resolves back to the ticket it answers.
All survive a bot restart (the volume-mounted DB).
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    username   TEXT,
    full_name  TEXT NOT NULL,
    title      TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL,
    sender     TEXT NOT NULL,        -- 'user' | 'operator'
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_msg_map (
    operator_chat_id INTEGER NOT NULL,
    operator_msg_id  INTEGER NOT NULL,
    ticket_id        INTEGER NOT NULL,
    PRIMARY KEY (operator_chat_id, operator_msg_id)
);
CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id);
CREATE INDEX IF NOT EXISTS idx_msgs_ticket ON ticket_messages(ticket_id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Storage:
    def __init__(self, path: str) -> None:
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def create_ticket(
        self, user_id: int, username: str | None, full_name: str, title: str, first_message: str
    ) -> int:
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT INTO tickets (user_id, username, full_name, title, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'open', ?, ?)",
                (user_id, username, full_name, title, now, now),
            )
            ticket_id = int(cur.lastrowid)
            await db.execute(
                "INSERT INTO ticket_messages (ticket_id, sender, text, created_at) VALUES (?, 'user', ?, ?)",
                (ticket_id, first_message, now),
            )
            await db.commit()
            return ticket_id

    async def add_message(self, ticket_id: int, sender: str, text: str) -> None:
        now = _now()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO ticket_messages (ticket_id, sender, text, created_at) VALUES (?, ?, ?, ?)",
                (ticket_id, sender, text, now),
            )
            await db.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now, ticket_id))
            await db.commit()

    async def get_ticket(self, ticket_id: int) -> dict | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def get_messages(self, ticket_id: int) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM ticket_messages WHERE ticket_id = ? ORDER BY id", (ticket_id,)
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def count_tickets(self, user_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT COUNT(*) FROM tickets WHERE user_id = ?", (user_id,)) as cur:
                return int((await cur.fetchone())[0])

    async def list_tickets(self, user_id: int, offset: int, limit: int) -> list[dict]:
        # Open tickets first, then most-recently-updated.
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM tickets WHERE user_id = ?"
                " ORDER BY (status = 'open') DESC, updated_at DESC, id DESC LIMIT ? OFFSET ?",
                (user_id, limit, offset),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def close_ticket(self, ticket_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE tickets SET status = 'closed', updated_at = ? WHERE id = ?", (_now(), ticket_id)
            )
            await db.commit()

    async def set_admin_msg(self, operator_chat_id: int, operator_msg_id: int, ticket_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO admin_msg_map (operator_chat_id, operator_msg_id, ticket_id)"
                " VALUES (?, ?, ?)",
                (operator_chat_id, operator_msg_id, ticket_id),
            )
            await db.commit()

    async def ticket_by_admin_msg(self, operator_chat_id: int, operator_msg_id: int) -> int | None:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT ticket_id FROM admin_msg_map WHERE operator_chat_id = ? AND operator_msg_id = ?",
                (operator_chat_id, operator_msg_id),
            ) as cur:
                row = await cur.fetchone()
                return int(row[0]) if row else None
