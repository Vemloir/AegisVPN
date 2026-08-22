"""The synthetic "Автовыбор" xray-JSON entry: one leastLoad balancer over every
location's proxy outbound, so the client (not the bot) measures real per-user
RTT and picks the fastest node. Server-rendered subscriptions cannot do that
themselves — this only assembles the candidate list and lets xray-core's own
burstObservatory/balancer do the actual selection client-side.
"""

import json
from datetime import UTC, datetime, timedelta

from src.core.database import async_session_maker, engine
from src.models import Server, Subscription, SubscriptionServer, User
from src.models.base import Base
from src.services import SubscriptionService
from src.services.agent_client import AgentClient


def _node(**overrides) -> Server:
    fields = {
        "name": "Testland",
        "flag": "\U0001f1ec\U0001f1f7",
        "host": "203.0.113.10",
        "port": 443,
        "public_key": "PBK",
        "short_id": "SID",
        "agent_url": "http://x",
        "agent_token": "t",
    }
    fields.update(overrides)
    return Server(**fields)


async def _seed_two_node_sub(monkeypatch) -> str:
    async def fake_get_subscription(self, token, profile="safe"):
        host = self.base_url.split("//", 1)[1].split(":", 1)[0]
        return (
            f"vless://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee@{host}:443"
            "?type=xhttp&security=reality&encryption=none&sni=www.example.test"
            f"&fp=firefox&pbk=PBK&sid=SID&path=%2F#{host}"
        )

    monkeypatch.setattr(AgentClient, "get_subscription", fake_get_subscription)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with async_session_maker() as session:
        user = User(tg_id=930001)
        session.add(user)
        await session.flush()
        server_a = _node(name="Germany | Frankfurt", host="203.0.113.30", agent_url="http://203.0.113.30")
        server_b = _node(name="Finland | Helsinki", host="203.0.113.40", agent_url="http://203.0.113.40")
        session.add_all([server_a, server_b])
        await session.flush()
        now = datetime.now(UTC).replace(tzinfo=None)
        sub = Subscription(
            user_id=user.id,
            sub_token="tok-auto",
            client_uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            plan_days=30,
            started_at=now,
            expires_at=now + timedelta(days=30),
            is_active=True,
        )
        session.add(sub)
        await session.flush()
        session.add_all(
            [
                SubscriptionServer(subscription_id=sub.id, server_id=server_a.id, is_synced=True),
                SubscriptionServer(subscription_id=sub.id, server_id=server_b.id, is_synced=True),
            ]
        )
        await session.commit()
        return sub.sub_token


async def test_autoselect_entry_bundles_every_location(monkeypatch):
    token = await _seed_two_node_sub(monkeypatch)
    async with async_session_maker() as session:
        kind, body = await SubscriptionService.build_xray_json_subscription(session, token)
    assert kind == "json"
    configs = json.loads(body)
    assert len(configs) == 3  # Germany, Finland, + the auto-select bundle

    auto = next(cfg for cfg in configs if cfg["remarks"] == "\U0001f1fa\U0001f1f3 Автовыбор")
    proxy_tags = [ob["tag"] for ob in auto["outbounds"] if ob["protocol"] == "vless"]
    assert proxy_tags == ["proxy", "proxy-2"]
    assert auto["burstObservatory"]["subjectSelector"] == ["proxy"]
    assert auto["routing"]["balancers"][0]["selector"] == ["proxy"]
    assert auto["routing"]["rules"][-1]["balancerTag"] == "auto"


async def test_autoselect_drops_an_overloaded_outlier(monkeypatch):
    async def fake_get_subscription(self, token, profile="safe"):
        host = self.base_url.split("//", 1)[1].split(":", 1)[0]
        return (
            f"vless://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee@{host}:443"
            "?type=xhttp&security=reality&encryption=none&sni=www.example.test"
            f"&fp=firefox&pbk=PBK&sid=SID&path=%2F#{host}"
        )

    monkeypatch.setattr(AgentClient, "get_subscription", fake_get_subscription)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with async_session_maker() as session:
        user = User(tg_id=930003)
        session.add(user)
        await session.flush()
        quiet_a = _node(
            name="Germany | Frankfurt",
            host="203.0.113.30",
            agent_url="http://203.0.113.30",
            last_seen_online_clients=10,
        )
        quiet_b = _node(
            name="Finland | Helsinki",
            host="203.0.113.40",
            agent_url="http://203.0.113.40",
            last_seen_online_clients=12,
        )
        overloaded = _node(
            name="Poland | Warsaw",
            host="203.0.113.50",
            agent_url="http://203.0.113.50",
            last_seen_online_clients=500,
        )
        session.add_all([quiet_a, quiet_b, overloaded])
        await session.flush()
        now = datetime.now(UTC).replace(tzinfo=None)
        sub = Subscription(
            user_id=user.id,
            sub_token="tok-auto-load",
            client_uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            plan_days=30,
            started_at=now,
            expires_at=now + timedelta(days=30),
            is_active=True,
        )
        session.add(sub)
        await session.flush()
        session.add_all(
            [
                SubscriptionServer(subscription_id=sub.id, server_id=quiet_a.id, is_synced=True),
                SubscriptionServer(subscription_id=sub.id, server_id=quiet_b.id, is_synced=True),
                SubscriptionServer(subscription_id=sub.id, server_id=overloaded.id, is_synced=True),
            ]
        )
        await session.commit()

    async with async_session_maker() as session:
        kind, body = await SubscriptionService.build_xray_json_subscription(session, "tok-auto-load")
    assert kind == "json"
    configs = json.loads(body)
    assert len(configs) == 4  # 3 locations + the auto-select bundle

    auto = next(cfg for cfg in configs if cfg["remarks"] == "\U0001f1fa\U0001f1f3 Автовыбор")
    proxy_tags = [ob["tag"] for ob in auto["outbounds"] if ob["protocol"] == "vless"]
    # Only the two quiet nodes make it into the balancer — Warsaw's 500 online
    # clients is far above the group average, so it's excluded as an outlier.
    assert len(proxy_tags) == 2


async def test_autoselect_omitted_for_a_single_location(monkeypatch):
    async def fake_get_subscription(self, token, profile="safe"):
        return (
            "vless://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee@203.0.113.10:443"
            "?type=xhttp&security=reality&encryption=none&sni=www.example.test"
            "&fp=firefox&pbk=PBK&sid=SID&path=%2F#Testland"
        )

    monkeypatch.setattr(AgentClient, "get_subscription", fake_get_subscription)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with async_session_maker() as session:
        user = User(tg_id=930002)
        session.add(user)
        await session.flush()
        server = _node()
        session.add(server)
        await session.flush()
        now = datetime.now(UTC).replace(tzinfo=None)
        sub = Subscription(
            user_id=user.id,
            sub_token="tok-auto-single",
            client_uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            plan_days=30,
            started_at=now,
            expires_at=now + timedelta(days=30),
            is_active=True,
        )
        session.add(sub)
        await session.flush()
        session.add(SubscriptionServer(subscription_id=sub.id, server_id=server.id, is_synced=True))
        await session.commit()

    async with async_session_maker() as session:
        kind, body = await SubscriptionService.build_xray_json_subscription(session, "tok-auto-single")
    assert kind == "json"
    configs = json.loads(body)
    assert len(configs) == 1
