"""A lifetime subscription has no expiry (NULL), end to end: node snapshots,
running-subscription queries and the SQLite migration that allows the NULL."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from src.control.state import _PRE_LIFETIME_AGENT_EXPIRE_MS, build_desired_items
from src.core.database import async_session_maker, engine
from src.core.migrations import ONE_SHOT_DATA_MIGRATIONS, make_subscription_expiry_nullable
from src.models import Base, Server, Subscription, SubscriptionServer, User

BACKFILL_SQL = dict(ONE_SHOT_DATA_MIGRATIONS)["lifetime_subscriptions_without_expiry_2026_10_13"]


async def _seed(capabilities: dict | None) -> tuple[int, int]:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    async with async_session_maker() as session:
        user = User(tg_id=820001)
        server = Server(
            name="Lifetime test",
            flag="T",
            host="203.0.113.40",
            port=443,
            public_key="pk",
            short_id="sid",
            agent_url="http://127.0.0.1:8444",
            agent_token="legacy",
            is_active=True,
            control_capabilities=capabilities,
        )
        session.add_all([user, server])
        await session.flush()
        sub = Subscription(
            user_id=user.id,
            sub_token="lifetime",
            client_uuid="00000000-0000-0000-0000-0000000000aa",
            plan_days=0,
            expires_at=None,
            is_active=True,
        )
        session.add(sub)
        await session.flush()
        session.add(SubscriptionServer(subscription_id=sub.id, server_id=server.id, is_synced=True))
        await session.commit()
        return server.id, sub.id


async def test_lifetime_subscription_reaches_a_capable_node_without_expiry():
    server_id, _ = await _seed({"features": ["cascade-v2", "lifetime-v1"]})
    async with async_session_maker() as session:
        items = await build_desired_items(session, server_id)
    clients = [item for item in items if item["kind"] == "client"]
    assert clients and all(item["expire_ms"] is None for item in clients)


async def test_older_agents_get_an_integer_they_can_validate():
    # Until a node reports lifetime-v1 its agent requires an integer expiry.
    server_id, _ = await _seed({"features": ["cascade-v2"]})
    async with async_session_maker() as session:
        items = await build_desired_items(session, server_id)
    clients = [item for item in items if item["kind"] == "client"]
    assert clients and all(item["expire_ms"] == _PRE_LIFETIME_AGENT_EXPIRE_MS for item in clients)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    assert _PRE_LIFETIME_AGENT_EXPIRE_MS > now_ms


async def test_lifetime_subscription_counts_as_running():
    _, sub_id = await _seed(None)
    far_future = datetime(9999, 12, 31)
    async with async_session_maker() as session:
        running = (
            await session.execute(select(Subscription.id).where(Subscription.unexpired_at(far_future)))
        ).scalars()
        assert list(running) == [sub_id]
        # And never caught by the expiry sweep's "expires_at <= now".
        expired = (await session.execute(select(Subscription.id).where(Subscription.expires_at <= far_future))).all()
        assert expired == []


# --- the SQLite migration ------------------------------------------------------

_OLD_SCHEMA = [
    "CREATE TABLE users (id INTEGER NOT NULL, CONSTRAINT pk_users PRIMARY KEY (id))",
    """CREATE TABLE subscriptions (
	id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	plan_days INTEGER NOT NULL,
	expires_at DATETIME NOT NULL,
	is_active BOOLEAN NOT NULL, traffic_up_bytes BIGINT DEFAULT 0,
	CONSTRAINT pk_subscriptions PRIMARY KEY (id),
	CONSTRAINT fk_subscriptions_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
)""",
    "CREATE INDEX ix_subscriptions_expires_at ON subscriptions (expires_at)",
    "CREATE INDEX ix_subscriptions_user_id ON subscriptions (user_id)",
    """CREATE TABLE devices (
	id INTEGER NOT NULL,
	subscription_id INTEGER NOT NULL,
	CONSTRAINT pk_devices PRIMARY KEY (id),
	CONSTRAINT fk_devices_subscription_id_subscriptions FOREIGN KEY(subscription_id)
		REFERENCES subscriptions (id) ON DELETE CASCADE
)""",
]


@pytest.fixture
async def old_sqlite(tmp_path):
    database = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")

    @event.listens_for(database.sync_engine, "connect")
    def _foreign_keys(connection, _record) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    async with database.begin() as conn:
        for statement in _OLD_SCHEMA:
            await conn.exec_driver_sql(statement)
        await conn.exec_driver_sql("INSERT INTO users (id) VALUES (1)")
        await conn.exec_driver_sql(
            "INSERT INTO subscriptions (id, user_id, plan_days, expires_at, is_active, traffic_up_bytes) VALUES "
            "(1, 1, 0, '2099-12-31 23:59:59.000000', 1, 7), (2, 1, 30, '2026-11-01 00:00:00.000000', 1, 9)"
        )
        await conn.exec_driver_sql("INSERT INTO devices (id, subscription_id) VALUES (10, 1), (11, 1), (12, 2)")
    yield database
    await database.dispose()


async def test_sqlite_rebuild_allows_null_and_keeps_every_child_row(old_sqlite):
    await make_subscription_expiry_nullable(old_sqlite, is_sqlite=True)
    await make_subscription_expiry_nullable(old_sqlite, is_sqlite=True)  # idempotent

    async with old_sqlite.begin() as conn:
        table_sql = (await conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name = 'subscriptions'")).scalar()
        assert "expires_at DATETIME NOT NULL" not in table_sql
        # Dropping the old table must not have cascaded into the devices.
        assert (await conn.exec_driver_sql("SELECT count(*) FROM devices")).scalar() == 3
        rows = (await conn.exec_driver_sql("SELECT id, traffic_up_bytes FROM subscriptions ORDER BY id")).all()
        assert [tuple(row) for row in rows] == [(1, 7), (2, 9)]
        indexes = {
            row[0]
            for row in (
                await conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'subscriptions'")
            ).all()
        }
        assert {"ix_subscriptions_expires_at", "ix_subscriptions_user_id"} <= indexes
        await conn.exec_driver_sql(BACKFILL_SQL)
        lifetime = (await conn.exec_driver_sql("SELECT id FROM subscriptions WHERE expires_at IS NULL")).all()
        assert [row[0] for row in lifetime] == [1]
        assert (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar() == 1
        assert (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all() == []
        # Foreign keys still bite after the swap: deleting a subscription cascades.
        await conn.exec_driver_sql("DELETE FROM subscriptions WHERE id = 2")
        assert (await conn.exec_driver_sql("SELECT count(*) FROM devices")).scalar() == 2


async def test_backfill_clears_only_the_old_sentinel():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    now = datetime.now(UTC).replace(tzinfo=None)
    async with async_session_maker() as session:
        user = User(tg_id=820002)
        session.add(user)
        await session.flush()
        for token, uuid_tail, expires in (
            ("sentinel", "b1", datetime(2099, 12, 31, 23, 59, 59)),
            ("revoked", "b2", now - timedelta(days=1)),
            ("regular", "b3", now + timedelta(days=30)),
        ):
            session.add(
                Subscription(
                    user_id=user.id,
                    sub_token=token,
                    client_uuid=f"00000000-0000-0000-0000-0000000000{uuid_tail}",
                    plan_days=0 if token != "regular" else 30,
                    expires_at=expires,
                    is_active=True,
                )
            )
        await session.commit()
        await session.execute(text(BACKFILL_SQL))
        await session.commit()
        lifetime = (await session.execute(select(Subscription.sub_token).where(Subscription.expires_at.is_(None)))).all()
        assert [row[0] for row in lifetime] == ["sentinel"]
