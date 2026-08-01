import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from src.core.database import async_session_maker, engine
from src.models import Base, Device, Server, Subscription, SubscriptionServer, User
from src.scheduler import tasks


async def _reset_schema() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)


async def test_expiry_commits_before_remote_revocation_and_revokes_devices(monkeypatch):
    await _reset_schema()
    async with async_session_maker() as session:
        user = User(tg_id=991001, username="expired", language="ru")
        session.add(user)
        await session.flush()
        subscription = Subscription(
            user_id=user.id,
            sub_token="expired-network-phase",
            client_uuid="91000000-0000-0000-0000-000000000001",
            plan_days=30,
            expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1),
        )
        session.add(subscription)
        server = Server(
            name="Push",
            flag="P",
            host="192.0.2.10",
            port=443,
            public_key="pk",
            short_id="sid",
            agent_url="https://agent.invalid",
            agent_token="token",
            control_mode="push",
        )
        session.add(server)
        await session.flush()
        device_uuid = "91000000-0000-0000-0000-000000000002"
        session.add(
            Device(
                subscription_id=subscription.id,
                uuid=device_uuid,
                ua_fingerprint="fingerprint",
                display_name="Phone",
            )
        )
        session.add(
            SubscriptionServer(
                subscription_id=subscription.id,
                server_id=server.id,
                is_synced=True,
            )
        )
        subscription_id = subscription.id
        await session.commit()

    removed: list[str] = []

    async def remove_client(_client, client_uuid):
        async with async_session_maker() as session:
            persisted = await session.get(Subscription, subscription_id)
            assert persisted.is_active is False
        removed.append(client_uuid)
        return True

    class Bot:
        async def send_message(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(tasks.AgentClient, "remove_client", remove_client)

    await tasks.check_expired_subscriptions(Bot())

    assert set(removed) == {
        "91000000-0000-0000-0000-000000000001",
        device_uuid,
    }


async def test_unsynced_push_retries_are_bounded_and_concurrent(monkeypatch):
    await _reset_schema()
    async with async_session_maker() as session:
        user = User(tg_id=991002, username="retry", language="ru")
        session.add(user)
        await session.flush()
        subscription = Subscription(
            user_id=user.id,
            sub_token="retry-network-phase",
            client_uuid="92000000-0000-0000-0000-000000000001",
            plan_days=30,
            expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1),
        )
        session.add(subscription)
        for index in range(2):
            server = Server(
                name=f"Push {index}",
                flag="P",
                host=f"192.0.2.{20 + index}",
                port=443,
                public_key=f"pk-{index}",
                short_id=f"sid-{index}",
                agent_url=f"https://agent-{index}.invalid",
                agent_token=f"token-{index}",
                control_mode="push",
            )
            session.add(server)
            await session.flush()
            session.add(
                SubscriptionServer(
                    subscription_id=subscription.id,
                    server_id=server.id,
                    is_synced=False,
                )
            )
        await session.commit()

    active = 0
    peak = 0

    async def add_client(_client, _client_uuid, _email):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return True

    monkeypatch.setattr(tasks.AgentClient, "add_client", add_client)

    await tasks.retry_unsynced_servers()

    async with async_session_maker() as session:
        links = (await session.scalars(select(SubscriptionServer))).all()
    assert peak == 2
    assert all(link.is_synced for link in links)
