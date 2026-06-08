"""Lightweight, idempotent schema migrations applied on startup.

The project deliberately does not use Alembic: the schema only ever grows by
nullable/defaulted columns, which both SQLite and PostgreSQL can add in place.
Each :class:`Column` is added only if it is missing, so this is safe to run on
every boot and against a database at any prior version.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.database import async_session_maker


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    sqlite_ddl: str
    pg_ddl: str
    post_sql: str | None = None  # extra statement run right after the column is added


# Ordered table -> columns that may be missing on older databases.
MIGRATIONS: dict[str, list[Column]] = {
    "users": [
        Column("language", "VARCHAR(8) DEFAULT 'ru'", "VARCHAR(8) DEFAULT 'ru'"),
        Column("trial_used", "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT FALSE"),
        Column("privacy_accepted", "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT FALSE"),
    ],
    "subscriptions": [
        Column("legacy_sub_token", "VARCHAR(255)", "VARCHAR(255)"),
        Column("traffic_up_bytes", "BIGINT DEFAULT 0", "BIGINT DEFAULT 0"),
        Column("traffic_down_bytes", "BIGINT DEFAULT 0", "BIGINT DEFAULT 0"),
    ],
    "subscription_servers": [
        Column("traffic_last_up", "BIGINT DEFAULT 0", "BIGINT DEFAULT 0"),
        Column("traffic_last_down", "BIGINT DEFAULT 0", "BIGINT DEFAULT 0"),
        Column("traffic_up_bytes", "BIGINT DEFAULT 0", "BIGINT DEFAULT 0"),
        Column("traffic_down_bytes", "BIGINT DEFAULT 0", "BIGINT DEFAULT 0"),
    ],
    "devices": [
        Column("is_suspended", "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT FALSE"),
        Column("last_server_id", "INTEGER", "INTEGER"),
        Column("traffic_up_bytes", "BIGINT DEFAULT 0", "BIGINT DEFAULT 0"),
        Column("traffic_down_bytes", "BIGINT DEFAULT 0", "BIGINT DEFAULT 0"),
        Column("os_label", "VARCHAR(64)", "VARCHAR(64)"),
        Column("build_number", "VARCHAR(32)", "VARCHAR(32)"),
        Column("added_location", "VARCHAR(128)", "VARCHAR(128)"),
        Column("added_country_code", "VARCHAR(2)", "VARCHAR(2)"),
    ],
    "servers": [
        Column(
            "subscription_group",
            "VARCHAR(16) DEFAULT 'safe'",
            "VARCHAR(16) DEFAULT 'safe'",
            post_sql="UPDATE servers SET subscription_group = 'safe' WHERE subscription_group IS NULL",
        ),
        Column("display_order", "INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
        Column("static_uri", "VARCHAR(512)", "VARCHAR(512)"),
        Column("mtproxy_secret", "VARCHAR(64)", "VARCHAR(64)"),
    ],
}


def _is_sqlite() -> bool:
    return settings.db_url.startswith("sqlite+aiosqlite")


async def _existing_columns(session: AsyncSession, table: str, is_sqlite: bool) -> set[str]:
    if is_sqlite:
        result = await session.execute(text(f"PRAGMA table_info({table})"))
        return {row[1] for row in result.fetchall()}
    result = await session.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = :table"),
        {"table": table},
    )
    return {row[0] for row in result.fetchall()}


async def run_migrations() -> None:
    """Add any columns missing from the live schema. Idempotent."""
    # Ensure all model-defined tables exist (create_all is safe; it never drops or modifies).
    from src.core.database import engine
    from src.models import Base  # noqa: F401 — triggers all model imports

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    is_sqlite = _is_sqlite()
    async with async_session_maker() as session:
        for table, columns in MIGRATIONS.items():
            existing = await _existing_columns(session, table, is_sqlite)
            for column in columns:
                if column.name in existing:
                    continue
                ddl = column.sqlite_ddl if is_sqlite else column.pg_ddl
                await session.execute(text(f"ALTER TABLE {table} ADD COLUMN {column.name} {ddl}"))
                if column.post_sql:
                    await session.execute(text(column.post_sql))
        await session.commit()
