"""CDN fallback: subscription rendering, origin certificates, reachability."""

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from src.api.main import app
from src.core.config import settings
from src.core.database import async_session_maker, engine
from src.models import CdnEndpoint, Server, Subscription, SubscriptionServer, User
from src.models.base import Base
from src.scheduler import tasks
from src.services import SubscriptionService, cdn_service
from src.services.agent_client import AgentClient
from tests.test_node_certificate_api import _certificate_pair

UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


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


def _cdn_node(**overrides) -> Server:
    fields = {
        "name": "Germany | Frankfurt",
        "host": "203.0.113.30",
        "agent_url": "http://203.0.113.30",
        "cdn_port": 8443,
        "cdn_path": "/media/",
        "cdn_origin_host": "n1.origin.test",
        "control_capabilities": {"features": ["cascade-v2", "cdn-v1"]},
    }
    fields.update(overrides)
    return _node(**fields)


async def _seed(monkeypatch, *, cdn_server: Server, endpoints: list[dict]) -> str:
    async def fake_get_subscription(self, token, profile="safe"):
        host = self.base_url.split("//", 1)[1].split(":", 1)[0]
        return (
            f"vless://{UUID}@{host}:443"
            "?type=xhttp&security=reality&encryption=none&sni=www.example.test"
            f"&fp=firefox&pbk=PBK&sid=SID&path=%2F#{host}"
        )

    monkeypatch.setattr(AgentClient, "get_subscription", fake_get_subscription)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with async_session_maker() as session:
        user = User(tg_id=940001)
        plain = _node(name="Finland | Helsinki", host="203.0.113.40", agent_url="http://203.0.113.40")
        session.add_all([user, cdn_server, plain])
        await session.flush()
        for fields in endpoints:
            session.add(CdnEndpoint(server_id=cdn_server.id, **fields))
        now = datetime.now(UTC).replace(tzinfo=None)
        sub = Subscription(
            user_id=user.id,
            sub_token="tok-cdn",
            client_uuid=UUID,
            plan_days=30,
            started_at=now,
            expires_at=now + timedelta(days=30),
            is_active=True,
        )
        session.add(sub)
        await session.flush()
        session.add_all(
            [
                SubscriptionServer(subscription_id=sub.id, server_id=cdn_server.id, is_synced=True),
                SubscriptionServer(subscription_id=sub.id, server_id=plain.id, is_synced=True),
            ]
        )
        await session.commit()
        return sub.sub_token


async def _json_configs(token: str) -> list[dict]:
    async with async_session_maker() as session:
        kind, body = await SubscriptionService.build_xray_json_subscription(session, token)
    assert kind == "json"
    return json.loads(body)


def _by_host(configs: list[dict], host: str) -> dict:
    """The per-location config whose primary outbound dials ``host``."""
    locations = configs[1:]  # configs[0] is the auto-select entry
    return next(cfg for cfg in locations if cfg["outbounds"][0]["settings"]["vnext"][0]["address"] == host)


BEGET = {"provider": "Beget", "host": "abc.begetcdn.cloud"}


async def test_location_gets_its_cdn_resources_as_a_fallback_tier(monkeypatch):
    token = await _seed(monkeypatch, cdn_server=_cdn_node(), endpoints=[BEGET])
    configs = await _json_configs(token)
    cfg = _by_host(configs, "203.0.113.30")

    tags = [outbound["tag"] for outbound in cfg["outbounds"]]
    assert tags[0] == "proxy"  # the direct path stays first and default
    assert tags[1] == "cdn-1"
    assert "cdn-fallback" in tags

    cdn = cfg["outbounds"][1]
    vnext = cdn["settings"]["vnext"][0]
    assert vnext["address"] == "abc.begetcdn.cloud"
    assert vnext["port"] == 443
    assert vnext["users"][0]["id"] == UUID
    assert "flow" not in vnext["users"][0]
    stream = cdn["streamSettings"]
    assert stream["security"] == "tls"
    assert stream["tlsSettings"]["serverName"] == "abc.begetcdn.cloud"
    assert stream["xhttpSettings"]["host"] == "abc.begetcdn.cloud"
    assert stream["xhttpSettings"]["path"] == "/media/"
    assert stream["xhttpSettings"]["mode"] == "packet-up"
    for key, value in cdn_service.CDN_XHTTP_PROFILE.items():
        assert stream["xhttpSettings"][key] == value

    routing = cfg["routing"]
    balancers = {balancer["tag"]: balancer for balancer in routing["balancers"]}
    assert balancers["primary"]["selector"] == ["proxy"]
    assert balancers["primary"]["fallbackTag"] == "cdn-fallback"
    assert balancers["cdn"]["selector"] == ["cdn-"]
    # Loopback traffic is routed to the CDN tier before anything else ...
    assert routing["rules"][0] == {"type": "field", "inboundTag": ["cdn-fallback-in"], "balancerTag": "cdn"}
    # ... while RU/private still bypass, and the rest goes through the balancer.
    assert routing["rules"][-1] == {"type": "field", "network": "tcp,udp", "balancerTag": "primary"}
    assert cfg["burstObservatory"]["subjectSelector"] == ["proxy"]
    assert any(inbound.get("tag") == "cdn-fallback-in" for inbound in cfg["inbounds"])


async def test_preview_user_gets_a_cdn_only_wl_location(monkeypatch):
    monkeypatch.setattr(settings, "cdn_wl_preview_tg_ids", [940001])
    token = await _seed(monkeypatch, cdn_server=_cdn_node(), endpoints=[BEGET])
    configs = await _json_configs(token)
    wl = [cfg for cfg in configs if cfg["remarks"].endswith(" | WL")]
    assert len(wl) == 1
    direct = _by_host(configs, "203.0.113.30")
    assert wl[0]["remarks"] == direct["remarks"] + " | WL"
    proxy = wl[0]["outbounds"][0]
    # Only the CDN: the edge is dialled, never the node itself.
    assert proxy["tag"] == "proxy"
    assert proxy["settings"]["vnext"][0]["address"] == "abc.begetcdn.cloud"
    assert proxy["settings"]["vnext"][0]["users"][0]["id"] == UUID
    assert proxy["streamSettings"]["xhttpSettings"]["sessionIDPlacement"] == "query"
    assert not any(o["tag"].startswith("cdn-") for o in wl[0]["outbounds"])
    assert "balancers" not in wl[0]["routing"]


async def test_wl_location_is_only_for_preview_users(monkeypatch):
    monkeypatch.setattr(settings, "cdn_wl_preview_tg_ids", [123])
    token = await _seed(monkeypatch, cdn_server=_cdn_node(), endpoints=[BEGET])
    configs = await _json_configs(token)
    assert not any(cfg["remarks"].endswith(" | WL") for cfg in configs)


async def test_location_without_cdn_is_unchanged(monkeypatch):
    token = await _seed(monkeypatch, cdn_server=_cdn_node(), endpoints=[BEGET])
    configs = await _json_configs(token)
    cfg = _by_host(configs, "203.0.113.40")
    assert [outbound["tag"] for outbound in cfg["outbounds"]] == ["proxy", "direct", "block"]
    assert "balancers" not in cfg["routing"]
    assert "burstObservatory" not in cfg


async def test_autoselect_falls_back_to_every_cdn_resource(monkeypatch):
    token = await _seed(monkeypatch, cdn_server=_cdn_node(), endpoints=[BEGET])
    configs = await _json_configs(token)
    autoselect = configs[0]
    balancers = {balancer["tag"]: balancer for balancer in autoselect["routing"]["balancers"]}
    assert balancers["auto"]["fallbackTag"] == "cdn-fallback"
    assert "cdn" in balancers
    assert any(outbound["tag"] == "cdn-1" for outbound in autoselect["outbounds"])


async def test_fronting_and_extra_are_carried_through(monkeypatch):
    yandex = {
        "provider": "Yandex",
        "host": "static1.origin-cdn.test",
        "address": "188.72.103.4",
        "sni": "cdn.tracker.yandex.net",
        "xhttp_extra": {"uplinkHTTPMethod": "GET", "uplinkDataPlacement": "header"},
    }
    token = await _seed(monkeypatch, cdn_server=_cdn_node(), endpoints=[yandex])
    cdn = _by_host(await _json_configs(token), "203.0.113.30")["outbounds"][1]
    assert cdn["settings"]["vnext"][0]["address"] == "188.72.103.4"
    assert cdn["streamSettings"]["tlsSettings"]["serverName"] == "cdn.tracker.yandex.net"
    assert cdn["streamSettings"]["xhttpSettings"]["host"] == "static1.origin-cdn.test"
    assert cdn["streamSettings"]["xhttpSettings"]["extra"] == yandex["xhttp_extra"]


@pytest.mark.parametrize(
    ("node_overrides", "endpoint_overrides"),
    [
        ({}, {"last_check_ok": False}),  # known broken
        ({}, {"enabled": False}),
        ({"control_capabilities": {"features": ["cascade-v2"]}}, {}),  # agent lacks the inbound
        ({"cdn_port": None}, {}),
    ],
)
async def test_unusable_cdn_paths_are_not_advertised(monkeypatch, node_overrides, endpoint_overrides):
    token = await _seed(
        monkeypatch,
        cdn_server=_cdn_node(**node_overrides),
        endpoints=[{**BEGET, **endpoint_overrides}],
    )
    configs = await _json_configs(token)
    for cfg in configs:
        assert not any(outbound["tag"].startswith("cdn-") for outbound in cfg["outbounds"])


async def test_link_subscription_lists_cdn_paths_after_their_location(monkeypatch):
    token = await _seed(monkeypatch, cdn_server=_cdn_node(), endpoints=[BEGET])
    async with async_session_maker() as session:
        body = await SubscriptionService.get_subscription_vless_links(session, token)
    links = base64.b64decode(body).decode().splitlines()
    assert len(links) == 3
    direct_index = next(i for i, link in enumerate(links) if "@203.0.113.30:" in link)
    cdn_link = links[direct_index + 1]
    parts = urlsplit(cdn_link)
    query = dict(parse_qsl(parts.query))
    assert parts.username == UUID
    assert parts.hostname == "abc.begetcdn.cloud"
    assert query["security"] == "tls"
    assert query["type"] == "xhttp"
    assert query["host"] == "abc.begetcdn.cloud"
    assert query["path"] == "/media/"
    assert query["mode"] == "packet-up"
    # A link has no fields for the placements: they travel in "extra".
    assert json.loads(query["extra"]) == cdn_service.CDN_XHTTP_PROFILE
    assert unquote(parts.fragment).endswith("· CDN")


def test_client_and_node_share_the_cdn_xhttp_profile():
    """The node's CDN inbound and the subscription's client must agree."""
    import re
    from pathlib import Path

    entrypoint = (Path(__file__).resolve().parents[2] / "agent" / "entrypoint.sh").read_text()
    literal = re.search(r"^CDN_XHTTP_PROFILE = (\{.*?^\})", entrypoint, re.S | re.M)
    assert literal, "CDN_XHTTP_PROFILE not found in agent/entrypoint.sh"
    scope: dict = {}
    exec(f"profile = {literal.group(1)}", scope)
    assert scope["profile"] == cdn_service.CDN_XHTTP_PROFILE


# --- reachability check ---------------------------------------------------------


async def test_check_records_results_and_alerts_on_transitions(monkeypatch):
    await _seed(monkeypatch, cdn_server=_cdn_node(), endpoints=[BEGET])
    outcomes = iter([(False, "HTTP 504 without the origin's marker")] * 2 + [(True, "origin reached (HTTP 400)")])

    async def fake_probe(server, endpoint):
        return next(outcomes)

    alerts: list[str] = []

    async def fake_alert(bot, text):
        alerts.append(text)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(cdn_service, "probe_endpoint", fake_probe)
    monkeypatch.setattr(tasks, "_alert_admins", fake_alert)
    monkeypatch.setattr(tasks.asyncio, "sleep", no_sleep)

    await tasks.check_cdn_endpoints(bot=None)
    async with async_session_maker() as session:
        endpoint = await session.get(CdnEndpoint, 1)
        assert endpoint.last_check_ok is False
        assert "504" in endpoint.last_check_detail
    assert len(alerts) == 1 and "НЕ работает" in alerts[0]

    await tasks.check_cdn_endpoints(bot=None)
    async with async_session_maker() as session:
        assert (await session.get(CdnEndpoint, 1)).last_check_ok is True
    assert len(alerts) == 2 and "снова работает" in alerts[1]


# --- origin certificate delivery ------------------------------------------------

PROXY_SECRET = "cdn-proxy-secret"
NODE_TOKEN = "cdn-node-token"
NODE_FINGERPRINT = "ccdd5566eeff7788"


def _headers() -> dict[str, str]:
    return {
        "X-Aegis-Proxy-Secret": PROXY_SECRET,
        "X-Aegis-Node-Fingerprint": NODE_FINGERPRINT,
        "Authorization": f"Bearer {NODE_TOKEN}",
    }


@pytest.fixture
async def control_node(tmp_path, monkeypatch):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setitem(settings.__dict__, "node_control_proxy_secret", SecretStr(PROXY_SECRET))
    monkeypatch.setitem(settings.__dict__, "node_cdn_certificate_dir", str(tmp_path))
    async with async_session_maker() as session:
        session.add(
            _cdn_node(
                control_mode="observe",
                control_token_hash=hashlib.sha256(NODE_TOKEN.encode()).hexdigest(),
                control_cert_fingerprint=NODE_FINGERPRINT,
                is_active=True,
            )
        )
        await session.commit()
    return tmp_path


async def test_shared_wildcard_certificate_is_served_for_the_origin_host(control_node):
    cert, key = _certificate_pair("*.origin.test")
    (control_node / "cert.pem").write_bytes(cert)
    (control_node / "key.pem").write_bytes(key)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/node/v1/cdn-certificate", headers=_headers())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["hostname"] == "n1.origin.test"
    assert body["certificate"].startswith("-----BEGIN CERTIFICATE-----")


async def test_certificate_not_covering_the_origin_host_is_refused(control_node):
    cert, key = _certificate_pair("other.example.test")
    (control_node / "cert.pem").write_bytes(cert)
    (control_node / "key.pem").write_bytes(key)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/node/v1/cdn-certificate", headers=_headers())
    assert response.status_code == 503


async def test_node_without_cdn_origin_gets_404(control_node):
    async with async_session_maker() as session:
        node = await session.get(Server, 1)
        node.cdn_port = None
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/node/v1/cdn-certificate", headers=_headers())
    assert response.status_code == 404
