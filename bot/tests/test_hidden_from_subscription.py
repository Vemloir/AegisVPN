"""Server.hidden_from_subscription delists a location from the rendered
subscription WITHOUT touching access control or the control-plane
desired-state — existing clients already synced to the node must keep
connecting on their saved config; only the subscription output stops
advertising it as an option.

This is deliberately a different lever from is_active/access_mode: those two
flow through ServerAccessService, which reconciles SubscriptionServer links
and strips the node's live client list (see test_disable_location.py). That
is the wrong tool for "stop offering this location" without disconnecting
whoever is already on it — see the incident this test guards against.
"""

import base64
from datetime import UTC, datetime, timedelta

from src.control.state import build_desired_items
from src.core.database import async_session_maker, engine
from src.models import Base, Server, Subscription, SubscriptionServer, User
from src.services.agent_client import AgentClient
from src.services.server_access_service import ServerAccessService
from src.services.subscription_service import SubscriptionService


async def _seed() -> tuple[int, int, str]:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with async_session_maker() as session:
        user = User(tg_id=940001)
        server = Server(
            name="Germany | Frankfurt",
            flag="🇩🇪",
            host="203.0.113.30",
            port=443,
            public_key="pk",
            short_id="sid",
            agent_url="http://node.invalid:8444",
            agent_token="tok",
            is_active=True,
        )
        session.add_all([user, server])
        await session.flush()
        now = datetime.now(UTC).replace(tzinfo=None)
        sub = Subscription(
            user_id=user.id,
            sub_token="tok-hidden",
            client_uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            plan_days=30,
            expires_at=now + timedelta(days=30),
            is_active=True,
        )
        session.add(sub)
        await session.flush()
        session.add(SubscriptionServer(subscription_id=sub.id, server_id=server.id, is_synced=True))
        await session.commit()
        return server.id, user.id, sub.sub_token


async def _fake_get_subscription(self, token, profile="safe"):
    return (
        "vless://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee@203.0.113.30:443"
        "?type=xhttp&security=reality&encryption=none&sni=www.example.test"
        "&fp=firefox&pbk=PBK&sid=SID&path=%2F#Frankfurt"
    )


async def test_hidden_server_disappears_from_rendered_subscription(monkeypatch):
    monkeypatch.setattr(AgentClient, "get_subscription", _fake_get_subscription)
    server_id, _, token = await _seed()

    async with async_session_maker() as session:
        server = await session.get(Server, server_id)
        encoded = await SubscriptionService.get_subscription_vless_links(session, token)
        assert base64.b64decode(encoded).decode() if encoded else "" != ""

        server.hidden_from_subscription = True
        await session.commit()

        encoded_after = await SubscriptionService.get_subscription_vless_links(session, token)
        assert encoded_after == ""


async def test_hidden_server_keeps_its_subscription_link_and_desired_state(monkeypatch):
    """The whole point: hiding must NOT touch the control-plane's client list —
    that is what actually authenticates an already-saved config on the node."""
    monkeypatch.setattr(AgentClient, "get_subscription", _fake_get_subscription)
    server_id, user_id, _ = await _seed()

    async with async_session_maker() as session:
        server = await session.get(Server, server_id)
        server.hidden_from_subscription = True
        await session.commit()

        link = await session.get(SubscriptionServer, {"subscription_id": 1, "server_id": server_id})
        assert link is not None, "hiding must not delete the SubscriptionServer link"

        accessible = await ServerAccessService.get_accessible_servers_for_user(session, user_id)
        assert server_id in {s.id for s in accessible}, "hiding must not revoke access"

        items = await build_desired_items(session, server_id)
        assert any(item.get("kind") == "client" for item in items), (
            "hiding must not empty the control-plane desired-state — that is what "
            "wipes the node's live Xray client list and breaks already-saved configs"
        )


async def test_unhidden_server_reappears(monkeypatch):
    monkeypatch.setattr(AgentClient, "get_subscription", _fake_get_subscription)
    server_id, _, token = await _seed()

    async with async_session_maker() as session:
        server = await session.get(Server, server_id)
        server.hidden_from_subscription = True
        await session.commit()

    async with async_session_maker() as session:
        encoded = await SubscriptionService.get_subscription_vless_links(session, token)
        assert encoded == ""

    async with async_session_maker() as session:
        server = await session.get(Server, server_id)
        server.hidden_from_subscription = False
        await session.commit()

    async with async_session_maker() as session:
        encoded = await SubscriptionService.get_subscription_vless_links(session, token)
        assert encoded != ""
