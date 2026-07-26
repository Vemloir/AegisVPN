from datetime import UTC, datetime, timedelta

from src.control.state import build_desired_items
from src.core.database import async_session_maker, engine
from src.models import (
    Base,
    Device,
    NodeTelemetry,
    Server,
    Subscription,
    SubscriptionServer,
    User,
)
from src.scheduler import tasks
from src.services import subscription_service as subscription_module
from src.services.admin_service import AdminService
from src.services.server_access_service import ServerAccessService
from src.services.subscription_service import SubscriptionService


async def _seed(mode: str) -> tuple[int, int, int, int]:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    async with async_session_maker() as session:
        user = User(tg_id=830001)
        server = Server(
            name=f"{mode} node",
            flag="N",
            host="203.0.113.50",
            port=443,
            public_key="public-key",
            short_id="short-id",
            agent_url="http://node.invalid:8444",
            agent_token="legacy-token",
            control_mode=mode,
            is_active=True,
        )
        session.add_all([user, server])
        await session.flush()
        subscription = Subscription(
            user_id=user.id,
            sub_token=f"{mode}-subscription",
            client_uuid="40000000-0000-0000-0000-000000000001",
            plan_days=30,
            expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30),
            is_active=True,
        )
        session.add(subscription)
        await session.flush()
        device = Device(
            subscription_id=subscription.id,
            uuid="40000000-0000-0000-0000-000000000002",
            ua_fingerprint="pull-device",
            display_name="Pull device",
            is_active=True,
            is_suspended=False,
        )
        session.add(device)
        await session.commit()
        return server.id, user.id, subscription.id, device.id


async def test_pull_sync_creates_link_and_snapshot_without_agent_call(monkeypatch):
    server_id, _, subscription_id, _ = await _seed("pull")
    calls: list[list[dict]] = []

    async def forbidden_bulk_add(self, clients):
        calls.append(clients)
        return True

    monkeypatch.setattr(
        subscription_module.AgentClient,
        "bulk_add",
        forbidden_bulk_add,
    )

    async with async_session_maker() as session:
        server = await session.get(Server, server_id)
        subscription = await session.get(Subscription, subscription_id)
        await SubscriptionService.sync_subscription_to_servers(
            session,
            subscription,
            [server],
        )
        await session.commit()

    async with async_session_maker() as session:
        link = await session.get(
            SubscriptionServer,
            {"subscription_id": subscription_id, "server_id": server_id},
        )
        items = await build_desired_items(session, server_id)

    assert link is not None
    assert link.is_synced is False
    assert calls == []
    assert [item["kind"] for item in items] == ["client", "client"]


async def test_observe_keeps_push_and_also_publishes(monkeypatch):
    server_id, _, subscription_id, _ = await _seed("observe")
    pushed: list[list[dict]] = []

    async def fake_bulk_add(self, clients):
        pushed.append(clients)
        return True

    monkeypatch.setattr(
        subscription_module.AgentClient,
        "bulk_add",
        fake_bulk_add,
    )

    async with async_session_maker() as session:
        server = await session.get(Server, server_id)
        subscription = await session.get(Subscription, subscription_id)
        await SubscriptionService.sync_subscription_to_servers(
            session,
            subscription,
            [server],
        )
        await session.commit()
        refreshed = await session.get(Server, server_id)

    assert len(pushed) == 1
    assert len(pushed[0]) == 2
    assert refreshed.desired_generation == 1


async def test_pull_suspend_and_revoke_remove_base_and_device_from_desired_state(
    monkeypatch,
):
    server_id, _, subscription_id, device_id = await _seed("pull")
    calls: list[tuple] = []

    async def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        return True

    monkeypatch.setattr(subscription_module.AgentClient, "bulk_add", forbidden)
    monkeypatch.setattr(subscription_module.AgentClient, "remove_client", forbidden)

    async with async_session_maker() as session:
        server = await session.get(Server, server_id)
        subscription = await session.get(Subscription, subscription_id)
        device = await session.get(Device, device_id)
        await SubscriptionService.sync_subscription_to_servers(
            session,
            subscription,
            [server],
        )
        await SubscriptionService.suspend_device(session, subscription, device)
        await session.commit()
        after_suspend = await build_desired_items(session, server_id)

        subscription.is_active = False
        await SubscriptionService.remove_subscription_from_servers(
            session,
            subscription,
        )
        await session.commit()
        after_revoke = await build_desired_items(session, server_id)

    assert [item["uuid"] for item in after_suspend if item["kind"] == "client"] == [
        "40000000-0000-0000-0000-000000000001"
    ]
    assert after_revoke == []
    assert calls == []


async def test_pull_subscription_link_is_built_from_outbound_telemetry(monkeypatch):
    server_id, _, subscription_id, _ = await _seed("pull")

    async def forbidden_get_subscription(self, token, profile="safe"):
        raise AssertionError("pull mode must not fetch /sub from the Agent API")

    monkeypatch.setattr(
        subscription_module.AgentClient,
        "get_subscription",
        forbidden_get_subscription,
    )

    async with async_session_maker() as session:
        session.add(
            SubscriptionServer(
                subscription_id=subscription_id,
                server_id=server_id,
                is_synced=True,
            )
        )
        session.add(
            NodeTelemetry(
                server_id=server_id,
                sequence=1,
                payload={
                    "subscription_templates": [
                        {
                            "profile": "safe",
                            "host": "node.example",
                            "port": 443,
                            "query": [
                                ["type", "xhttp"],
                                ["security", "reality"],
                                ["encryption", "none"],
                                ["sni", "cdn.example"],
                                ["fp", "chrome"],
                                ["pbk", "ignored-agent-key"],
                                ["sid", "ignored-agent-sid"],
                                ["spx", "/"],
                                ["path", "/"],
                                ["mode", "auto"],
                            ],
                        }
                    ]
                },
            )
        )
        await session.commit()
        subscription = await session.get(Subscription, subscription_id)
        pairs = await SubscriptionService._collect_links(
            session,
            subscription.sub_token,
        )

    assert len(pairs) == 1
    _, link = pairs[0]
    assert link.startswith(
        "vless://40000000-0000-0000-0000-000000000001@203.0.113.50:443?"
    )
    assert "pbk=public-key" in link
    assert "sid=short-id" in link


async def test_pull_traffic_uses_outbound_telemetry_not_agent(monkeypatch):
    server_id, user_id, subscription_id, _ = await _seed("pull")
    prefix = f"user_{user_id}_sub_{subscription_id}"
    agent_calls: list[int] = []

    async def forbidden_stats(self):
        agent_calls.append(1)
        return {}

    monkeypatch.setattr(tasks.AgentClient, "get_stats", forbidden_stats)

    async with async_session_maker() as session:
        session.add(
            SubscriptionServer(
                subscription_id=subscription_id,
                server_id=server_id,
                is_synced=True,
            )
        )
        session.add(
            NodeTelemetry(
                server_id=server_id,
                sequence=1,
                payload={
                    "stats": {
                        prefix: {"uplink": 100, "downlink": 200},
                    }
                },
            )
        )
        await session.commit()

    await tasks.poll_traffic()
    async with async_session_maker() as session:
        telemetry = await session.get(NodeTelemetry, server_id)
        telemetry.sequence = 2
        telemetry.payload = {
            "stats": {
                prefix: {"uplink": 160, "downlink": 290},
            }
        }
        await session.commit()
    await tasks.poll_traffic()

    async with async_session_maker() as session:
        subscription = await session.get(Subscription, subscription_id)
    assert (subscription.traffic_up_bytes, subscription.traffic_down_bytes) == (
        60,
        90,
    )
    assert agent_calls == []


async def test_pull_connection_limit_publishes_without_agent_call(monkeypatch):
    server_id, _, subscription_id, _ = await _seed("pull")
    calls: list[tuple] = []

    async def forbidden_limit(*args, **kwargs):
        calls.append((args, kwargs))
        return True

    monkeypatch.setattr(
        "src.services.admin_service.AgentClient.set_conn_limit",
        forbidden_limit,
    )
    async with async_session_maker() as session:
        subscription = await session.get(Subscription, subscription_id)
        user = await session.get(User, subscription.user_id)
        session.add(
            SubscriptionServer(
                subscription_id=subscription_id,
                server_id=server_id,
                is_synced=True,
            )
        )
        tg_id = user.tg_id
        await session.commit()

    returned_user, successful, total = await AdminService.set_user_conn_limit(
        tg_id,
        0,
    )

    async with async_session_maker() as session:
        items = await build_desired_items(session, server_id)
    assert returned_user is not None
    assert (successful, total) == (1, 1)
    assert [item for item in items if item["kind"] == "conn_limit"] == [
        {
            "kind": "conn_limit",
            "user_id": returned_user.id,
            "limit": 0,
        }
    ]
    assert calls == []


async def test_push_access_revoke_removes_base_and_device_credentials(monkeypatch):
    server_id, _, subscription_id, _ = await _seed("push")
    removed: list[str] = []

    async def capture_remove(self, uuid):
        removed.append(uuid)
        return True

    monkeypatch.setattr(
        "src.services.server_access_service.AgentClient.remove_client_best_effort",
        capture_remove,
    )

    async with async_session_maker() as session:
        server = await session.get(Server, server_id)
        subscription = await session.get(Subscription, subscription_id)
        server.access_mode = "private"
        session.add(
            SubscriptionServer(
                subscription_id=subscription_id,
                server_id=server_id,
                is_synced=True,
            )
        )
        await session.flush()
        await ServerAccessService.reconcile_subscription_servers(
            session,
            subscription,
        )
        await session.commit()

    assert set(removed) == {
        "40000000-0000-0000-0000-000000000001",
        "40000000-0000-0000-0000-000000000002",
    }
