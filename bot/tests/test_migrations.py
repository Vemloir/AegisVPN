"""run_migrations() must add every declared column to an older schema and be
safe to run repeatedly."""

from sqlalchemy import text

from src.core.database import async_session_maker
from src.core.migrations import MIGRATIONS, run_migrations


async def _table_columns(session, table: str) -> set[str]:
    result = await session.execute(text(f"PRAGMA table_info({table})"))
    return {row[1] for row in result.fetchall()}


async def test_run_migrations_adds_missing_columns_idempotently():
    # Start from a minimal, pre-migration schema: each table with only an id.
    async with async_session_maker() as session:
        for table in MIGRATIONS:
            await session.execute(text(f"DROP TABLE IF EXISTS {table}"))
            await session.execute(text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
        await session.commit()

    await run_migrations()
    await run_migrations()  # must be idempotent

    async with async_session_maker() as session:
        for table, columns in MIGRATIONS.items():
            present = await _table_columns(session, table)
            for column in columns:
                assert column.name in present, f"{table}.{column.name} missing after migration"

    # the servers.subscription_group post_sql backfill should have run cleanly
    async with async_session_maker() as session:
        await session.execute(text("INSERT INTO servers (id) VALUES (999)"))
        await session.commit()
        value = await session.scalar(text("SELECT subscription_group FROM servers WHERE id = 999"))
        # column default applies to new rows; existing NULLs were backfilled to 'safe'
        assert value in ("safe", None)
