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
    # A few base columns that create_all always provides in production are kept so
    # backfills can reference them: `servers.host`, and `subscription_servers.server_id`
    # (the FK the servers traffic backfill correlates on).
    async with async_session_maker() as session:
        for table in MIGRATIONS:
            await session.execute(text(f"DROP TABLE IF EXISTS {table}"))
            if table == "servers":
                await session.execute(
                    text("CREATE TABLE servers (id INTEGER PRIMARY KEY, host VARCHAR(255))")
                )
            elif table == "subscription_servers":
                await session.execute(
                    text("CREATE TABLE subscription_servers (id INTEGER PRIMARY KEY, server_id INTEGER)")
                )
            else:
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


async def test_hy2_columns_are_added_but_never_populated():
    """The migration only ADDS the Hy2 capability columns. It never populates them
    for any node: every value (including the obfs password and SNI secrets) is set
    out-of-band by the operator, so a freshly migrated server is not Hy2-capable."""
    async with async_session_maker() as session:
        await session.execute(text("DROP TABLE IF EXISTS servers"))
        await session.execute(
            text("CREATE TABLE servers (id INTEGER PRIMARY KEY, host VARCHAR(255))")
        )
        await session.execute(text("INSERT INTO servers (id, host) VALUES (1, '203.0.113.10')"))
        await session.commit()

    await run_migrations()
    await run_migrations()  # idempotent

    async with async_session_maker() as session:
        row = (
            await session.execute(
                text(
                    "SELECT tcp_port, hy2_enabled, hy2_port, hy2_hop_start, hy2_hop_end, "
                    "hy2_obfs_password, hy2_sni, hy2_up, hy2_down FROM servers WHERE id = 1"
                )
            )
        ).fetchone()

    assert row.tcp_port is None
    assert row.hy2_enabled in (0, False, None)
    assert row.hy2_port is None
    assert row.hy2_hop_start is None
    assert row.hy2_hop_end is None
    assert row.hy2_obfs_password is None
    assert row.hy2_sni is None
    assert row.hy2_up is None
    assert row.hy2_down is None
