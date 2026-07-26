import hashlib
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from src.api.main import app
from src.core.config import settings
from src.core.database import async_session_maker, engine
from src.models import Base, NodeTelemetry, Server, Subscription, SubscriptionServer, User

PROXY_SECRET = "proxy-secret"
NODE_TOKEN = "node-token-with-enough-entropy"
NODE_FINGERPRINT = "aabbccddeeff"


def _headers(
    *,
    proxy_secret: str = PROXY_SECRET,
    fingerprint: str = NODE_FINGERPRINT,
    token: str = NODE_TOKEN,
) -> dict[str, str]:
    return {
        "X-Aegis-Proxy-Secret": proxy_secret,
        "X-Aegis-Node-Fingerprint": fingerprint,
        "Authorization": f"Bearer {token}",
    }


async def _seed_control_node() -> tuple[int, int]:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    async with async_session_maker() as session:
        user = User(tg_id=820001)
        node = Server(
            name="Node one",
            flag="1",
            host="203.0.113.41",
            port=443,
            public_key="pk",
            short_id="sid",
            agent_url="http://127.0.0.1:8444",
            agent_token="legacy",
            control_mode="observe",
            control_token_hash=hashlib.sha256(NODE_TOKEN.encode()).hexdigest(),
            control_cert_fingerprint=NODE_FINGERPRINT,
            is_active=True,
        )
        other = Server(
            name="Node two",
            flag="2",
            host="203.0.113.42",
            port=443,
            public_key="pk",
            short_id="sid",
            agent_url="http://127.0.0.1:8444",
            agent_token="legacy",
            control_mode="observe",
            control_token_hash=hashlib.sha256(b"other-token").hexdigest(),
            control_cert_fingerprint="112233445566",
            is_active=True,
        )
        session.add_all([user, node, other])
        await session.flush()
        subscription = Subscription(
            user_id=user.id,
            sub_token="control-api-sub",
            client_uuid="10000000-0000-0000-0000-000000000001",
            plan_days=30,
            expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30),
            is_active=True,
        )
        session.add(subscription)
        await session.flush()
        session.add(
            SubscriptionServer(
                subscription_id=subscription.id,
                server_id=node.id,
                is_synced=False,
            )
        )
        await session.commit()
        return node.id, other.id


def _configure_control(monkeypatch, *, telemetry_bytes: int = 4096) -> None:
    monkeypatch.setitem(
        settings.__dict__,
        "node_control_proxy_secret",
        SecretStr(PROXY_SECRET),
    )
    monkeypatch.setitem(settings.__dict__, "node_control_long_poll_seconds", 0.0)
    monkeypatch.setitem(settings.__dict__, "node_control_poll_interval_seconds", 0.01)
    monkeypatch.setitem(settings.__dict__, "node_control_page_size", 1)
    monkeypatch.setitem(
        settings.__dict__,
        "node_control_max_telemetry_bytes",
        telemetry_bytes,
    )


async def test_sync_requires_all_auth_layers_and_returns_manifest(monkeypatch):
    await _seed_control_node()
    _configure_control(monkeypatch)
    body = {
        "applied_generation": 0,
        "applied_digest": None,
        "agent_version": "test-agent",
        "capabilities": ["xray-live-api"],
    }

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        assert (await client.post("/api/node/v1/sync", json=body)).status_code == 401
        assert (
            await client.post(
                "/api/node/v1/sync",
                json=body,
                headers=_headers(proxy_secret="wrong"),
            )
        ).status_code == 401
        assert (
            await client.post(
                "/api/node/v1/sync",
                json=body,
                headers=_headers(fingerprint="wrong"),
            )
        ).status_code == 401
        assert (
            await client.post(
                "/api/node/v1/sync",
                json=body,
                headers=_headers(token="wrong"),
            )
        ).status_code == 401

        response = await client.post(
            "/api/node/v1/sync",
            json=body,
            headers=_headers(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "generation": 1,
        "digest": response.json()["digest"],
        "item_count": 1,
        "page_count": 1,
        "page_size": 1,
    }


async def test_pages_ack_and_telemetry_are_node_scoped_and_monotonic(monkeypatch):
    node_id, other_id = await _seed_control_node()
    _configure_control(monkeypatch)
    sync_body = {
        "applied_generation": 0,
        "agent_version": "test-agent",
        "capabilities": [],
    }

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        manifest_response = await client.post(
            "/api/node/v1/sync",
            json=sync_body,
            headers=_headers(),
        )
        manifest = manifest_response.json()
        page = await client.get(
            f"/api/node/v1/snapshots/{manifest['generation']}/pages/0",
            headers=_headers(),
        )
        missing_page = await client.get(
            f"/api/node/v1/snapshots/{manifest['generation']}/pages/99",
            headers=_headers(),
        )

        wrong_ack = await client.post(
            "/api/node/v1/ack",
            json={
                "generation": manifest["generation"],
                "digest": "0" * 64,
                "success": True,
            },
            headers=_headers(),
        )
        good_ack = await client.post(
            "/api/node/v1/ack",
            json={
                "generation": manifest["generation"],
                "digest": manifest["digest"],
                "success": True,
            },
            headers=_headers(),
        )
        duplicate_ack = await client.post(
            "/api/node/v1/ack",
            json={
                "generation": manifest["generation"],
                "digest": manifest["digest"],
                "success": True,
            },
            headers=_headers(),
        )
        telemetry_new = await client.post(
            "/api/node/v1/telemetry",
            json={"sequence": 2, "payload": {"online": 4}},
            headers=_headers(),
        )
        telemetry_stale = await client.post(
            "/api/node/v1/telemetry",
            json={"sequence": 1, "payload": {"online": 99}},
            headers=_headers(),
        )

    assert page.status_code == 200
    assert page.json()["items"][0]["kind"] == "client"
    assert missing_page.status_code == 404
    assert wrong_ack.status_code == 409
    assert good_ack.status_code == duplicate_ack.status_code == 200
    assert telemetry_new.status_code == telemetry_stale.status_code == 200

    async with async_session_maker() as session:
        node = await session.get(Server, node_id)
        other = await session.get(Server, other_id)
        telemetry = await session.get(NodeTelemetry, node_id)
    assert node.applied_generation == manifest["generation"]
    assert node.applied_digest == manifest["digest"]
    assert other.applied_generation == 0
    assert telemetry.sequence == 2
    assert telemetry.payload == {"online": 4}


async def test_telemetry_payload_is_bounded(monkeypatch):
    await _seed_control_node()
    _configure_control(monkeypatch, telemetry_bytes=32)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/node/v1/telemetry",
            json={"sequence": 1, "payload": {"value": "x" * 128}},
            headers=_headers(),
        )

    assert response.status_code == 413
