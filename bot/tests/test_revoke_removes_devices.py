"""remove_subscription_from_servers must pull the sub's UUID AND every device
UUID from each node. A device connects with its own UUID, so removing only the
sub's UUID left every device authenticated — revoke/expiry didn't cut access.
"""

from datetime import UTC, datetime, timedelta

from src.core.database import async_session_maker, engine
from src.models import Device, Server, Subscription, SubscriptionServer, User
from src.models.base import Base
from src.services import SubscriptionService
from src.services import subscription_service as svc


async def _seed(key: int) -> tuple[int, str, list[str]]:
    """Seed user/sub/server + two devices. Returns (sub_id, sub_uuid, device_uuids)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with async_session_maker() as session:
        user = User(tg_id=910000 + key)
        session.add(user)
        await session.flush()

        sub_uuid = f"{key:08d}-0000-0000-0000-000000000000"
        sub = Subscription(
            user_id=user.id,
            sub_token=f"tok-revoke-{key}",
            client_uuid=sub_uuid,
            plan_days=30,
            expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30),
            is_active=True,
        )
        session.add(sub)
        await session.flush()

        device_uuids = [
            f"{key:08d}-1111-1111-1111-111111111111",
            f"{key:08d}-2222-2222-2222-222222222222",
        ]
        for i, du in enumerate(device_uuids):
            session.add(
                Device(
                    subscription_id=sub.id,
                    uuid=du,
                    ua_fingerprint=f"fp-{key}-{i}",
                    display_name=f"Device {i}",
                    is_active=(i == 0),  # one active, one inactive — both must be pulled
                )
            )

        server = Server(
            name="N",
            flag="N",
            host="203.0.113.20",
            port=443,
            public_key="pk",
            short_id="sid",
            agent_url="http://127.0.0.1:8444",
            agent_token="tok",
            is_active=True,
        )
        session.add(server)
        await session.flush()
        session.add(SubscriptionServer(subscription_id=sub.id, server_id=server.id, is_synced=True))
        await session.commit()
        return sub.id, sub_uuid, device_uuids


async def test_revoke_removes_sub_and_all_device_uuids(monkeypatch):
    sub_id, sub_uuid, device_uuids = await _seed(1)

    removed: list[str] = []

    async def fake_remove_client(self, uuid):
        removed.append(uuid)
        return True

    monkeypatch.setattr(svc.AgentClient, "remove_client", fake_remove_client)

    async with async_session_maker() as session:
        sub = await session.get(Subscription, sub_id)
        await SubscriptionService.remove_subscription_from_servers(session, sub)

    assert set(removed) == {sub_uuid, *device_uuids}
