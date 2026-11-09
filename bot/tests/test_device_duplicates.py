"""Legacy duplicate UA rows must not break subscription fetches or revoke UUIDs."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiohttp.test_utils import make_mocked_request
from sqlalchemy import select

from src.core.database import async_session_maker, engine
from src.main import subscription_response
from src.models import Device, Subscription, User
from src.models.base import Base
from src.services import SubscriptionService

UA = "Happ/4.6.1/Linux/2610271019666"
TOKEN = "duplicate-linux-test-token"


async def _device_identities(session):
    rows = (await session.execute(select(Device).order_by(Device.id))).scalars().all()
    return [(row.id, row.uuid, row.subscription_id, row.ua_fingerprint, row.is_active, row.is_suspended) for row in rows]


async def _seed_duplicates(*, suspended: bool = False):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    async with async_session_maker() as session:
        users = [User(tg_id=991001), User(tg_id=991002)]
        session.add_all(users)
        await session.flush()
        subscriptions = [
            Subscription(
                user_id=user.id,
                sub_token=TOKEN if index == 0 else "other-subscription-token",
                client_uuid=str(uuid4()),
                plan_days=30,
                expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30),
                is_active=True,
            )
            for index, user in enumerate(users)
        ]
        session.add_all(subscriptions)
        await session.flush()
        fingerprint = SubscriptionService.fingerprint_ua(UA)
        # Older rows from another subscription or inactive records must not win.
        for subscription_id, active in ((subscriptions[0].id, False), (subscriptions[1].id, True)):
            session.add(
                Device(
                    subscription_id=subscription_id,
                    uuid=str(uuid4()),
                    ua_fingerprint=fingerprint,
                    display_name="Linux",
                    is_active=active,
                    is_suspended=False,
                )
            )
            await session.flush()
        devices = [
            Device(
                subscription_id=subscriptions[0].id,
                uuid=str(uuid4()),
                ua_fingerprint=fingerprint,
                display_name="Linux",
                is_active=True,
                is_suspended=suspended if index == 0 else False,
            )
            for index in range(15)
        ]
        session.add_all(devices)
        await session.flush()
        canonical = (devices[0].id, devices[0].uuid)
        identities = await _device_identities(session)
        await session.commit()
        return subscriptions[0].id, canonical, identities


@pytest.mark.parametrize("suspended", [False, True])
async def test_duplicate_ua_reuses_stable_device_without_changing_identities(monkeypatch, suspended):
    sub_id, canonical, identities = await _seed_duplicates(suspended=suspended)
    sync = AsyncMock(side_effect=AssertionError("Existing devices must not be resynchronized"))
    monkeypatch.setattr(SubscriptionService, "_sync_device_to_servers", sync)

    for _ in range(2):
        async with async_session_maker() as session:
            subscription = await session.get(Subscription, sub_id)
            device = await SubscriptionService.get_or_create_device(session, subscription, UA)
            assert (device.id, device.uuid) == canonical
            assert device.is_suspended is suspended
            await session.commit()
            assert await _device_identities(session) == identities
    sync.assert_not_awaited()


async def test_linux_subscription_http_response_survives_duplicate_devices(monkeypatch):
    _sub_id, canonical, identities = await _seed_duplicates()
    sync = AsyncMock(side_effect=AssertionError("A subscription fetch must reuse the existing device"))
    builder = AsyncMock(return_value=("json", json.dumps([{"remarks": "test subscription"}])))
    monkeypatch.setattr(SubscriptionService, "_sync_device_to_servers", sync)
    monkeypatch.setattr(SubscriptionService, "build_xray_json_subscription", builder)
    request = make_mocked_request(
        "GET",
        f"/sub/{TOKEN}",
        headers={"User-Agent": UA},
        match_info={"token": TOKEN},
    )

    response = await subscription_response(request, SubscriptionService.SAFE_PROFILE)

    assert response.status == 200
    assert response.content_type == "application/json"
    assert json.loads(response.text) == [{"remarks": "test subscription"}]
    assert builder.await_args.kwargs["device_uuid"] == canonical[1]
    sync.assert_not_awaited()
    async with async_session_maker() as session:
        assert await _device_identities(session) == identities
