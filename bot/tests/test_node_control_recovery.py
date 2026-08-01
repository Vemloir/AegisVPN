from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from src.control.state import publish_snapshot
from src.core.database import async_session_maker, engine
from src.models import (
    Base,
    Device,
    NodeSnapshotPage,
    Server,
    Subscription,
    SubscriptionServer,
    User,
)


async def _seed_subscription(*, device_count: int = 2) -> tuple[int, int, set[str]]:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    async with async_session_maker() as session:
        user = User(tg_id=830001)
        server = Server(
            name="Recovery node",
            flag="R",
            host="203.0.113.80",
            port=443,
            public_key="pk",
            short_id="sid",
            agent_url="http://127.0.0.1:8444",
            agent_token="legacy",
            control_mode="pull",
            is_active=True,
        )
        session.add_all([user, server])
        await session.flush()
        subscription = Subscription(
            user_id=user.id,
            sub_token="recovery-sub",
            client_uuid="40000000-0000-0000-0000-000000000000",
            plan_days=30,
            expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30),
            is_active=True,
        )
        session.add(subscription)
        await session.flush()
        session.add(
            SubscriptionServer(
                subscription_id=subscription.id,
                server_id=server.id,
                is_synced=True,
            )
        )
        device_uuids = {f"40000000-0000-0000-0000-{index:012d}" for index in range(1, device_count + 1)}
        session.add_all(
            Device(
                subscription_id=subscription.id,
                uuid=device_uuid,
                ua_fingerprint=f"device-{index}",
                display_name=f"Device {index}",
                is_active=True,
                is_suspended=False,
            )
            for index, device_uuid in enumerate(sorted(device_uuids), start=1)
        )
        await session.commit()
        return (
            server.id,
            subscription.id,
            {subscription.client_uuid, *device_uuids},
        )


async def _snapshot_items(server_id: int, generation: int) -> list[dict]:
    async with async_session_maker() as session:
        pages = (
            (
                await session.execute(
                    select(NodeSnapshotPage)
                    .where(
                        NodeSnapshotPage.server_id == server_id,
                        NodeSnapshotPage.generation == generation,
                    )
                    .order_by(NodeSnapshotPage.page_index)
                )
            )
            .scalars()
            .all()
        )
    return [item for page in pages for item in page.items]


async def test_offline_revoke_publishes_complete_state_without_any_old_uuid():
    server_id, subscription_id, authorized_uuids = await _seed_subscription()
    async with async_session_maker() as session:
        before = await publish_snapshot(session, server_id, page_size=1)
        await session.commit()

    before_items = await _snapshot_items(server_id, before.generation)
    assert {item["uuid"] for item in before_items if item["kind"] == "client"} == authorized_uuids

    # The node is offline: no acknowledgement or individual remove call occurs.
    async with async_session_maker() as session:
        subscription = await session.get(Subscription, subscription_id)
        subscription.is_active = False
        await session.flush()
        after = await publish_snapshot(session, server_id, page_size=1)
        await session.commit()

    reconnect_items = await _snapshot_items(server_id, after.generation)
    reconnect_uuids = {item["uuid"] for item in reconnect_items if item["kind"] == "client"}
    assert after.generation == before.generation + 1
    assert reconnect_uuids.isdisjoint(authorized_uuids)

    # Generation N remains immutable for audit/retry, but cannot become desired
    # again: reconnect always starts from Server.desired_generation (N+1).
    async with async_session_maker() as session:
        server = await session.get(Server, server_id)
    assert server.desired_generation == after.generation
    assert server.applied_generation == 0


async def test_snapshot_pagination_does_not_impose_a_device_count_limit():
    server_id, _, authorized_uuids = await _seed_subscription(device_count=2_500)
    async with async_session_maker() as session:
        snapshot = await publish_snapshot(session, server_id, page_size=137)
        await session.commit()

    items = await _snapshot_items(server_id, snapshot.generation)
    client_uuids = {item["uuid"] for item in items if item["kind"] == "client"}
    assert client_uuids == authorized_uuids
    assert snapshot.item_count == len(authorized_uuids)
    assert snapshot.page_count > 1
