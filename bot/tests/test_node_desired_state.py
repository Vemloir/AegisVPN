import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from src.control.state import build_desired_items, canonical_json, publish_snapshot
from src.core.database import async_session_maker, engine
from src.models import (
    Base,
    Device,
    NodeSnapshot,
    NodeSnapshotPage,
    Server,
    Subscription,
    SubscriptionServer,
    User,
)


async def _seed_state() -> tuple[int, int, int, int]:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    now = datetime.now(UTC).replace(tzinfo=None)
    async with async_session_maker() as session:
        user = User(tg_id=810001, conn_limit=3)
        server = Server(
            name="Control test",
            flag="T",
            host="203.0.113.30",
            port=443,
            public_key="pk",
            short_id="sid",
            agent_url="http://127.0.0.1:8444",
            agent_token="legacy",
            is_active=True,
        )
        session.add_all([user, server])
        await session.flush()

        active = Subscription(
            user_id=user.id,
            sub_token="state-active",
            client_uuid="00000000-0000-0000-0000-000000000001",
            plan_days=30,
            expires_at=now + timedelta(days=30),
            is_active=True,
        )
        expired = Subscription(
            user_id=user.id,
            sub_token="state-expired",
            client_uuid="00000000-0000-0000-0000-000000000099",
            plan_days=30,
            expires_at=now - timedelta(minutes=1),
            is_active=True,
        )
        session.add_all([active, expired])
        await session.flush()
        session.add_all(
            [
                SubscriptionServer(
                    subscription_id=active.id,
                    server_id=server.id,
                    is_synced=False,
                ),
                SubscriptionServer(
                    subscription_id=expired.id,
                    server_id=server.id,
                    is_synced=True,
                ),
            ]
        )
        devices = [
            Device(
                subscription_id=active.id,
                uuid="00000000-0000-0000-0000-000000000002",
                ua_fingerprint="active",
                display_name="Active",
                is_active=True,
                is_suspended=False,
            ),
            Device(
                subscription_id=active.id,
                uuid="00000000-0000-0000-0000-000000000003",
                ua_fingerprint="suspended",
                display_name="Suspended",
                is_active=True,
                is_suspended=True,
            ),
            Device(
                subscription_id=active.id,
                uuid="00000000-0000-0000-0000-000000000004",
                ua_fingerprint="removed",
                display_name="Removed",
                is_active=False,
                is_suspended=False,
            ),
        ]
        session.add_all(devices)
        await session.commit()
        return server.id, user.id, active.id, devices[0].id


async def test_desired_state_contains_only_current_authorization():
    server_id, user_id, sub_id, active_device_id = await _seed_state()

    async with async_session_maker() as session:
        items = await build_desired_items(session, server_id)
        subscription = await session.get(Subscription, sub_id)

    expires_ms = int(subscription.expires_at.replace(tzinfo=UTC).timestamp() * 1000)
    assert items == [
        {
            "kind": "client",
            "uuid": "00000000-0000-0000-0000-000000000001",
            "email": f"user_{user_id}_sub_{sub_id}",
            "expire_ms": expires_ms,
        },
        {
            "kind": "client",
            "uuid": "00000000-0000-0000-0000-000000000002",
            "email": f"user_{user_id}_sub_{sub_id}_dev_{active_device_id}",
            "expire_ms": expires_ms,
        },
        {"kind": "conn_limit", "user_id": user_id, "limit": 3},
    ]


async def test_snapshot_is_stable_paginated_and_changes_generation():
    server_id, _, sub_id, _ = await _seed_state()

    async with async_session_maker() as session:
        first = await publish_snapshot(session, server_id, page_size=2)
        await session.commit()
        repeated = await publish_snapshot(session, server_id, page_size=2)
        await session.commit()

        assert first.generation == repeated.generation == 1
        assert first.item_count == 3
        assert first.page_count == 2

        new_device = Device(
            subscription_id=sub_id,
            uuid="00000000-0000-0000-0000-000000000005",
            ua_fingerprint="new",
            display_name="New",
            is_active=True,
            is_suspended=False,
        )
        session.add(new_device)
        await session.flush()
        changed = await publish_snapshot(session, server_id, page_size=2)
        await session.commit()

        pages = list(
            (
                await session.execute(
                    select(NodeSnapshotPage)
                    .where(
                        NodeSnapshotPage.server_id == server_id,
                        NodeSnapshotPage.generation == changed.generation,
                    )
                    .order_by(NodeSnapshotPage.page_index)
                )
            )
            .scalars()
            .all()
        )

    assert changed.generation == 2
    assert all(len(page.items) <= 2 for page in pages)
    assert all(page.page_digest == hashlib.sha256(canonical_json(page.items)).hexdigest() for page in pages)
    all_items = [item for page in pages for item in page.items]
    assert changed.digest == hashlib.sha256(canonical_json(all_items)).hexdigest()


async def test_concurrent_publishers_serialize_on_sqlite(monkeypatch):
    server_id, _, _, _ = await _seed_state()
    original = build_desired_items
    second_reader = asyncio.Event()
    readers = 0

    async def overlap_readers(session, target_server_id):
        nonlocal readers
        readers += 1
        if readers == 1:
            try:
                await asyncio.wait_for(second_reader.wait(), timeout=0.05)
            except TimeoutError:
                pass
        else:
            second_reader.set()
        return await original(session, target_server_id)

    monkeypatch.setattr("src.control.state.build_desired_items", overlap_readers)

    async def publish_once():
        async with async_session_maker() as session:
            snapshot = await publish_snapshot(session, server_id, page_size=2)
            await session.commit()
            return snapshot.generation

    results = await asyncio.gather(publish_once(), publish_once(), return_exceptions=True)

    assert results == [1, 1]
    async with async_session_maker() as session:
        snapshots = (
            (await session.execute(select(NodeSnapshot).where(NodeSnapshot.server_id == server_id))).scalars().all()
        )
    assert [snapshot.generation for snapshot in snapshots] == [1]
