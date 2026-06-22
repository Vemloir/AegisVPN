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
    # `servers` additionally keeps `host`, which create_all always provides in
    # production and the Greece transport backfill (post_sql) references.
    async with async_session_maker() as session:
        for table in MIGRATIONS:
            await session.execute(text(f"DROP TABLE IF EXISTS {table}"))
            if table == "servers":
                await session.execute(
                    text("CREATE TABLE servers (id INTEGER PRIMARY KEY, host VARCHAR(255))")
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


async def test_greece_hy2_backfill_only_greece_and_not_the_secret():
    """The Hy2 capability backfill targets ONLY the Greece node (host 45.142.31.13)
    and leaves the obfs password NULL (a secret the operator sets out-of-band)."""
    async with async_session_maker() as session:
        await session.execute(text("DROP TABLE IF EXISTS servers"))
        await session.execute(
            text("CREATE TABLE servers (id INTEGER PRIMARY KEY, host VARCHAR(255))")
        )
        await session.execute(
            text("INSERT INTO servers (id, host) VALUES (1, '45.142.31.13'), (2, '1.2.3.4')")
        )
        await session.commit()

    await run_migrations()
    await run_migrations()  # idempotent

    async with async_session_maker() as session:
        gr = (
            await session.execute(
                text(
                    "SELECT hy2_enabled, hy2_port, hy2_hop_start, hy2_hop_end, "
                    "hy2_obfs_password, hy2_up, hy2_down FROM servers WHERE id = 1"
                )
            )
        ).fetchone()
        other = (
            await session.execute(
                text("SELECT hy2_enabled, hy2_port, hy2_obfs_password FROM servers WHERE id = 2")
            )
        ).fetchone()

    assert gr.hy2_enabled in (1, True)
    assert gr.hy2_port == 36500
    assert gr.hy2_hop_start == 20000
    assert gr.hy2_hop_end == 50000
    assert gr.hy2_up == "100 mbps"
    assert gr.hy2_down == "100 mbps"
    # The obfs password is a secret and is deliberately NOT backfilled.
    assert gr.hy2_obfs_password is None
    # A non-Greece node is untouched.
    assert other.hy2_enabled in (0, False, None)
    assert other.hy2_port is None
    assert other.hy2_obfs_password is None
