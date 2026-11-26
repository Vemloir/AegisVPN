"""Account overview -> copy -> client import, without a Telegram or VPN connection."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from aiohttp.test_utils import make_mocked_request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from src.api.auth import issue_session
from src.api.main import app
from src.core.database import async_session_maker, engine
from src.main import sub_handler
from src.models import Base, Device, Server, Subscription, SubscriptionServer, User
from src.services import subscription_import
from src.services.agent_client import AgentClient
from src.services.subscription_lifecycle import claim_expirations
from src.services.subscription_service import SubscriptionService

TOKEN = "synthetic-account-token"
BASE_UUID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
async def owner():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with async_session_maker() as session:
        user = User(tg_id=770001)
        server = Server(
            name="Testland",
            flag="T",
            host="192.0.2.1",
            port=443,
            public_key="sample",
            short_id="abc",
            agent_url="https://node.invalid",
            agent_token="test",
        )
        session.add_all([user, server])
        await session.flush()
        sub = Subscription(
            user_id=user.id,
            sub_token=TOKEN,
            legacy_sub_token="synthetic-legacy",
            client_uuid=BASE_UUID,
            expires_at=None,
            plan_days=0,
        )
        session.add(sub)
        await session.flush()
        session.add(SubscriptionServer(subscription_id=sub.id, server_id=server.id, is_synced=True))
        await session.commit()
        return user.id


async def account(user_id):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://site.test", cookies={"aegis_session": issue_session(user_id)}
    ) as client:
        response = await client.get("/api/me")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return response.json()["subscription"]


async def fetch(token):
    return await sub_handler(
        make_mocked_request("GET", f"/sub/{token}", headers={"User-Agent": "Happ/1.0"}, match_info={"token": token})
    )


async def test_account_then_import_reuses_device_and_legacy_link(owner, monkeypatch):
    add = AsyncMock(return_value=True)
    monkeypatch.setattr(AgentClient, "add_client", add)

    async def location(_client, uuid, profile):
        return f"vless://{uuid}@192.0.2.1:443?security=reality&pbk=sample&sid=abc&type=xhttp#Testland"

    monkeypatch.setattr(AgentClient, "get_subscription", location)
    overview = await account(owner)
    assert overview == await account(owner)
    assert overview["is_lifetime"] and overview["is_active"]
    assert urlsplit(overview["sub_url"]).path == f"/sub/{TOKEN}"
    async with async_session_maker() as session:
        assert (await session.scalars(select(Device))).all() == []
    add.assert_not_awaited()

    for token in (TOKEN, "synthetic-legacy"):
        response = await fetch(token)
        assert response.status == 200
        assert response.content_type == "application/json"
        async with async_session_maker() as session:
            device = (await session.scalars(select(Device))).one()
            sub = (await session.scalars(select(Subscription))).one()
            assert sub.client_uuid == BASE_UUID != device.uuid
            assert sub.sub_token == TOKEN
        cfg = json.loads(response.text)[0]
        assert cfg["outbounds"][0]["settings"]["vnext"][0]["users"][0]["id"] == device.uuid
    add.assert_awaited_once()

    async with async_session_maker() as session:
        device = (await session.scalars(select(Device))).one()
        device.is_suspended = True
        await session.commit()
    assert (await fetch(TOKEN)).status == 404


async def test_expired_read_is_inert_but_import_revokes_without_enrolling(owner, monkeypatch):
    async with async_session_maker() as session:
        sub = (await session.scalars(select(Subscription))).one()
        sub.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        await session.commit()
    remove = AsyncMock(return_value=True)
    add = AsyncMock(return_value=True)
    monkeypatch.setattr(AgentClient, "remove_client", remove)
    monkeypatch.setattr(AgentClient, "add_client", add)

    overview = await account(owner)
    assert not overview["is_active"] and overview["sub_url"] is None
    async with async_session_maker() as session:
        assert await SubscriptionService.get_subscription_vless_links(session, TOKEN) == ""
        assert (await session.scalars(select(Subscription))).one().is_active
    remove.assert_not_awaited()
    assert (await fetch(TOKEN)).status == 404
    remove.assert_awaited_once_with(BASE_UUID)
    add.assert_not_awaited()
    async with async_session_maker() as session:
        assert (await session.scalars(select(Device))).all() == []
        assert not (await session.scalars(select(Subscription))).one().is_active
    assert (await fetch(TOKEN)).status == 404
    remove.assert_awaited_once()


async def test_import_started_before_renewal_does_not_revoke_renewed_uuid(owner, monkeypatch):
    now = datetime.now(UTC).replace(tzinfo=None)
    async with async_session_maker() as session:
        sub = (await session.scalars(select(Subscription))).one()
        sub.expires_at = now - timedelta(seconds=1)
        subscription_id = sub.id
        await session.commit()

    async def renew_before_claim(session, *, cutoff, subscription_id):
        async with async_session_maker() as other:
            sub = await other.get(Subscription, subscription_id)
            sub.expires_at = now + timedelta(days=30)
            await other.commit()
        return await claim_expirations(session, cutoff=cutoff, subscription_id=subscription_id)

    remove = AsyncMock()
    monkeypatch.setattr(subscription_import, "claim_expirations", renew_before_claim)
    monkeypatch.setattr(AgentClient, "remove_client", remove)
    async with async_session_maker() as session:
        prepared = await subscription_import.prepare_subscription_import(session, TOKEN, "", None, {})
        assert prepared is not None and prepared.subscription.client_uuid == BASE_UUID
        assert prepared.subscription.expires_at > now
    remove.assert_not_awaited()
    async with async_session_maker() as session:
        assert (await session.get(Subscription, subscription_id)).is_active


async def test_expiry_transition_is_rollbackable_and_leaves_lifetime(owner):
    cutoff = datetime.now(UTC).replace(tzinfo=None)
    async with async_session_maker() as session:
        sub = (await session.scalars(select(Subscription))).one()
        assert await claim_expirations(session, cutoff=cutoff) == []
        sub.expires_at = cutoff
        await session.commit()
    async with async_session_maker() as session:
        assert await claim_expirations(session, cutoff=cutoff) == [sub.id]
        assert await claim_expirations(session, cutoff=cutoff) == []
        await session.rollback()
    async with async_session_maker() as session:
        assert (await session.get(Subscription, sub.id)).is_active
