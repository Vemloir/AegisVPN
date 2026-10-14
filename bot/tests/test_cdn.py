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


def _cdn_paths(cfg: dict) -> list[str]:
    """Tags of the CDN outbounds proper (not the loopback into the CDN tier)."""
    return [o["tag"] for o in cfg["outbounds"] if o["tag"].startswith("cdn-") and o["protocol"] == "vless"]


BEGET = {"provider": "Beget", "host": "abc.begetcdn.cloud"}


async def test_location_gets_its_cdn_resources_as_a_fallback_tier(monkeypatch):
    token = await _seed(monkeypatch, cdn_server=_cdn_node(), endpoints=[BEGET])
    configs = await _json_configs(token)
    cfg = _by_host(configs, "203.0.113.30")

    tags = [outbound["tag"] for outbound in cfg["outbounds"]]
    assert tags[0] == "proxy"  # the direct path stays first and default
    assert tags[1] == "cdn-1"
    assert "to-cdn" in tags

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
    assert balancers["primary"]["fallbackTag"] == "to-cdn"
    # Not leastPing: with no health check yet that would send every fresh
    # connect through the CDN; random keeps the unchecked direct path.
    assert balancers["primary"]["strategy"] == {"type": "random"}
    assert balancers["cdn"]["selector"] == ["cdn-1"]
    # Loopback traffic is routed to the CDN tier before anything else ...
    assert routing["rules"][0] == {"type": "field", "inboundTag": ["to-cdn-in"], "balancerTag": "cdn"}
    # ... while RU/private still bypass, and the rest goes through the balancer.
    assert routing["rules"][-1] == {"type": "field", "network": "tcp,udp", "balancerTag": "primary"}
    assert cfg["burstObservatory"]["subjectSelector"] == ["proxy"]
    assert any(inbound.get("tag") == "to-cdn-in" for inbound in cfg["inbounds"])


async def test_preview_user_gets_a_cdn_only_wl_location(monkeypatch):
    monkeypatch.setattr(settings, "preview_tg_ids", [940001])
    token = await _seed(monkeypatch, cdn_server=_cdn_node(), endpoints=[{**BEGET, "preview_only": True}])
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
    monkeypatch.setattr(settings, "preview_tg_ids", [123])
    token = await _seed(monkeypatch, cdn_server=_cdn_node(), endpoints=[{**BEGET, "preview_only": True}])
    configs = await _json_configs(token)
    assert not any(cfg["remarks"].endswith(" | WL") for cfg in configs)


async def test_resource_in_the_fallback_chain_gets_no_wl_entry(monkeypatch):
    # Once a resource joins the chain the location itself exercises it; a WL
    # entry of its own is only for a resource still on trial.
    monkeypatch.setattr(settings, "preview_tg_ids", [940001])
    token = await _seed(monkeypatch, cdn_server=_cdn_node(), endpoints=[BEGET, {**AVITO, "preview_only": False}])
    configs = await _json_configs(token)
    assert not any(" | WL" in cfg["remarks"] for cfg in configs)


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
    # Ranked by ping first; with no ranking yet (or none alive) any direct node
    # not known to be dead, and only when every one is dead the CDN tier.
    assert balancers["auto"]["fallbackTag"] == "primary-fallback"
    assert balancers["primary-any"]["strategy"] == {"type": "random"}
    assert balancers["primary-any"]["selector"] == balancers["auto"]["selector"]
    assert balancers["primary-any"]["fallbackTag"] == "to-cdn"
    rules = autoselect["routing"]["rules"]
    assert {"type": "field", "inboundTag": ["primary-fallback-in"], "balancerTag": "primary-any"} in rules
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


PIN_A = "a1" * 32
PIN_B = "b2" * 32
AVITO = {
    "provider": "Beget",
    "host": "abc.begetcdn.cloud",
    "sni": "12.img.avito.st",
    "pin_cert": True,
    "pinned_cert_sha256": f"{PIN_A},{PIN_B}",
    "preview_only": True,
}


async def test_preview_only_resource_is_a_wl_entry_and_never_a_fallback(monkeypatch):
    monkeypatch.setattr(settings, "preview_tg_ids", [940001])
    token = await _seed(monkeypatch, cdn_server=_cdn_node(), endpoints=[BEGET, AVITO])
    configs = await _json_configs(token)
    # The fallback tier (location and auto-select) carries the plain resource only.
    location = _by_host(configs, "203.0.113.30")
    assert _cdn_paths(location) == ["cdn-1"]
    assert _cdn_paths(configs[0]) == ["cdn-1"]
    # Only the resource on trial gets an entry; the plain one is in the chain.
    wl = [cfg for cfg in configs if " | WL" in cfg["remarks"]]
    assert [cfg["remarks"] for cfg in wl] == [f"{location['remarks']} | WL"]
    tls = wl[0]["outbounds"][0]["streamSettings"]["tlsSettings"]
    assert tls["serverName"] == "12.img.avito.st"
    assert tls["pinnedPeerCertSha256"] == f"{PIN_A},{PIN_B}"


async def test_cdn_paths_form_a_checked_chain_in_order(monkeypatch):
    # Direct first, then the plain resource, then the fronted one: each tier is
    # taken only while the one before it is found dead.
    token = await _seed(monkeypatch, cdn_server=_cdn_node(), endpoints=[BEGET, {**AVITO, "preview_only": False}])
    cfg = _by_host(await _json_configs(token), "203.0.113.30")
    by_tag = {o["tag"]: o for o in cfg["outbounds"]}
    assert by_tag["cdn-1"]["streamSettings"]["tlsSettings"]["serverName"] == "abc.begetcdn.cloud"
    assert by_tag["cdn-2"]["streamSettings"]["tlsSettings"]["serverName"] == "12.img.avito.st"
    balancers = {b["tag"]: b for b in cfg["routing"]["balancers"]}
    assert balancers["primary"]["fallbackTag"] == "to-cdn"
    assert balancers["cdn"] == {
        "tag": "cdn",
        "selector": ["cdn-1"],
        "strategy": {"type": "random"},
        "fallbackTag": "to-cdn-2",
    }
    assert balancers["cdn-tier-2"] == {"tag": "cdn-tier-2", "selector": ["cdn-2"], "strategy": {"type": "random"}}
    rules = cfg["routing"]["rules"]
    assert rules[:2] == [
        {"type": "field", "inboundTag": ["to-cdn-in"], "balancerTag": "cdn"},
        {"type": "field", "inboundTag": ["to-cdn-2-in"], "balancerTag": "cdn-tier-2"},
    ]
    # The chain only moves on from a path a check found dead.
    assert cfg["burstObservatory"]["subjectSelector"] == ["proxy", "cdn-"]
    # No selector reaches a loopback: a loopback picked as a path would loop.
    loopbacks = {o["tag"] for o in cfg["outbounds"] if o["protocol"] == "loopback"}
    for balancer in balancers.values():
        assert not [t for t in loopbacks for sel in balancer["selector"] if t.startswith(sel)]


async def test_single_cdn_path_is_not_health_checked(monkeypatch):
    token = await _seed(monkeypatch, cdn_server=_cdn_node(), endpoints=[BEGET])
    cfg = _by_host(await _json_configs(token), "203.0.113.30")
    assert cfg["burstObservatory"]["subjectSelector"] == ["proxy"]


def test_cdn_tags_are_never_a_prefix_of_one_another():
    tags = cdn_service._cdn_tier_tags(12)
    assert tags[0] == "cdn-01" and tags[-1] == "cdn-12"
    assert not [a for a in tags for b in tags if a != b and b.startswith(a)]


async def test_preview_only_resource_stays_out_for_everyone_else(monkeypatch):
    token = await _seed(monkeypatch, cdn_server=_cdn_node(), endpoints=[AVITO])
    for cfg in await _json_configs(token):
        assert not any(o["tag"].startswith("cdn-") for o in cfg["outbounds"])
        assert " | WL" not in cfg["remarks"]
    async with async_session_maker() as session:
        body = await SubscriptionService.get_subscription_vless_links(session, token)
    assert len(base64.b64decode(body).decode().splitlines()) == 2


async def test_pinned_resource_waits_for_its_first_certificate(monkeypatch):
    monkeypatch.setattr(settings, "preview_tg_ids", [940001])
    unlearned = {**AVITO, "pinned_cert_sha256": None, "preview_only": False}
    token = await _seed(monkeypatch, cdn_server=_cdn_node(), endpoints=[unlearned])
    for cfg in await _json_configs(token):
        assert not any(o["tag"].startswith("cdn-") for o in cfg["outbounds"])
        assert " | WL" not in cfg["remarks"]


async def test_pinned_link_carries_the_pins(monkeypatch):
    token = await _seed(monkeypatch, cdn_server=_cdn_node(), endpoints=[{**AVITO, "preview_only": False}])
    async with async_session_maker() as session:
        body = await SubscriptionService.get_subscription_vless_links(session, token)
    cdn_link = next(link for link in base64.b64decode(body).decode().splitlines() if "abc.begetcdn.cloud" in link)
    query = dict(parse_qsl(urlsplit(cdn_link).query))
    assert query["sni"] == "12.img.avito.st"
    assert query["pcs"] == f"{PIN_A},{PIN_B}"


def test_new_certificate_goes_first_and_old_ones_age_out():
    assert cdn_service.merge_cert_pins([], PIN_A) == PIN_A
    assert cdn_service.merge_cert_pins([PIN_A], PIN_A) == PIN_A
    assert cdn_service.merge_cert_pins([PIN_A], PIN_B) == f"{PIN_B},{PIN_A}"
    pins = [f"{n:064x}" for n in range(1, 5)]
    assert cdn_service.merge_cert_pins(pins, PIN_B).split(",") == [PIN_B, *pins[:3]]


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


async def test_check_learns_the_certificate_of_a_pinned_resource(monkeypatch):
    await _seed(monkeypatch, cdn_server=_cdn_node(), endpoints=[{**AVITO, "pinned_cert_sha256": PIN_A}, BEGET])

    async def fake_probe(server, endpoint):
        return True, "origin reached (HTTP 400)"

    seen: list[int] = []

    async def fake_cert(endpoint):
        seen.append(endpoint.id)
        return PIN_B

    monkeypatch.setattr(cdn_service, "probe_endpoint", fake_probe)
    monkeypatch.setattr(cdn_service, "edge_cert_sha256", fake_cert)
    monkeypatch.setattr(tasks, "_alert_admins", lambda bot, text: None)

    await tasks.check_cdn_endpoints(bot=None)
    async with async_session_maker() as session:
        assert (await session.get(CdnEndpoint, 1)).pinned_cert_sha256 == f"{PIN_B},{PIN_A}"
        assert (await session.get(CdnEndpoint, 2)).pinned_cert_sha256 is None
    assert seen == [1]  # only pinned resources are asked for their certificate


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
