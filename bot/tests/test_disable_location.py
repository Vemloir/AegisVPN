"""Taking a location offline must not depend on that location being reachable.

An operator disables a location precisely BECAUSE the node is going away, so the
node is usually already dead. Removing the clients from it is a courtesy — it
must never stall, and must never veto, the operator's decision.

These tests fake the HTTP layer, not AgentClient's methods: the retry/backoff
policy under test lives in a decorator ON those methods, so patching them out
would patch the bug out with them.
"""

import datetime
import time

import pytest
from sqlalchemy import select

from src.core.database import async_session_maker, engine
from src.models import Base, Server, Subscription, SubscriptionServer, User
from src.services import agent_client
from src.services.server_access_service import ServerAccessService


@pytest.fixture(autouse=True)
async def _schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


class _Response:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return {}

    def raise_for_status(self):
        pass


class _FakeSession:
    """Stands in for the shared aiohttp session."""

    def __init__(self, attempts: list, alive: bool):
        self.attempts = attempts
        self.alive = alive

    def post(self, url, **kwargs):
        self.attempts.append((url, kwargs.get("json", {}).get("uuid")))
        if not self.alive:
            # What an unreachable node looks like to aiohttp.
            raise ConnectionError("node is gone")
        return _Response()


def _install_node(monkeypatch, attempts: list, *, alive: bool) -> None:
    monkeypatch.setattr(agent_client, "get_session", lambda: _FakeSession(attempts, alive))


async def _seed(session, n_subs: int) -> Server:
    server = Server(
        name="Швеция",
        flag="🇸🇪",
        host="203.0.113.20",
        port=443,
        public_key="pk",
        short_id="sid",
        agent_url="http://node.invalid:8444",
        agent_token="tok",
        is_active=True,
    )
    session.add(server)
    await session.flush()

    for i in range(n_subs):
        user = User(tg_id=1000 + i)
        session.add(user)
        await session.flush()
        sub = Subscription(
            user_id=user.id,
            sub_token=f"tok{i}",
            client_uuid=f"uuid-{i}",
            plan_days=30,
            expires_at=datetime.datetime(2099, 1, 1),
            is_active=True,
        )
        session.add(sub)
        await session.flush()
        session.add(SubscriptionServer(subscription_id=sub.id, server_id=server.id, is_synced=True))
    await session.commit()
    return server


async def test_disabling_a_dead_location_persists_and_does_not_stall(monkeypatch):
    attempts: list = []
    _install_node(monkeypatch, attempts, alive=False)

    async with async_session_maker() as session:
        server = await _seed(session, n_subs=3)
        server_id = server.id

        started = time.monotonic()
        await ServerAccessService.set_server_active(session, server, False)
        await session.commit()
        elapsed = time.monotonic() - started

    # The decision is durable even though the node never answered.
    async with async_session_maker() as session:
        refetched = await session.get(Server, server_id)
        assert refetched.is_active is False
        assert (await session.execute(select(SubscriptionServer))).scalars().all() == []

    # One courtesy attempt per subscription — no retry storm. The retrying
    # remove_client fires 3 attempts each with 2s + 4s of backoff between them:
    # ~18s for these 3 subscriptions, and many minutes across a real user base.
    # That is what made "disable this location" time out before reaching commit.
    assert len(attempts) == 3, f"expected 1 attempt per subscription, got {len(attempts)}"
    assert elapsed < 2.0, f"disabling stalled for {elapsed:.1f}s on an unreachable node"


async def test_disabling_still_removes_clients_from_a_live_node(monkeypatch):
    """A disabled location must not leave users authenticated on a node that IS
    reachable — the courtesy call still happens."""
    attempts: list = []
    _install_node(monkeypatch, attempts, alive=True)

    async with async_session_maker() as session:
        server = await _seed(session, n_subs=2)
        await ServerAccessService.set_server_active(session, server, False)
        await session.commit()

    assert sorted(uuid for _, uuid in attempts) == ["uuid-0", "uuid-1"]
    assert all(url.endswith("/client/remove") for url, _ in attempts)
