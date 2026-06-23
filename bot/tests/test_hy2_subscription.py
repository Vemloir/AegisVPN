"""Hysteria2 subscription emission: the hysteria2:// link shape (UDP :443, sni
only, no obfs/hop/insecure), the hy2_capable gate (enabled + port + SNI), and the
xray-JSON delivery path emitting the fork's hysteria outbound (BBR, no obfs) so a
Happ/v2rayTun user who picks Hy2 keeps the baked-in routing instead of a flat
link list.
"""

import base64
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from src.core.database import async_session_maker, engine
from src.models import (
    Server,
    ServerTransportPref,
    Subscription,
    SubscriptionServer,
    User,
)
from src.models.base import Base
from src.services import SubscriptionService


def _greece(**overrides) -> Server:
    """A Greece-like, fully Hy2-provisioned server (UDP :443 + LE-cert SNI)."""
    fields = {
        "name": "Greece",
        "flag": "\U0001f1ec\U0001f1f7",
        "host": "45.142.31.13",
        "port": 443,
        "public_key": "PBK",
        "short_id": "SID",
        "agent_url": "http://x",
        "agent_token": "t",
        "hy2_enabled": True,
        "hy2_port": 443,
        "hy2_sni": "aegis.example.test",
    }
    fields.update(overrides)
    return Server(**fields)


# --- hy2_capable gate --------------------------------------------------------


def test_hy2_capable_requires_enabled_port_and_sni():
    assert _greece().hy2_capable is True
    # No CA cert SNI (operator-set, left NULL by the migration) -> not capable.
    assert _greece(hy2_sni=None).hy2_capable is False
    # Not enabled -> not capable.
    assert _greece(hy2_enabled=False).hy2_capable is False
    # No client target port -> not capable.
    assert _greece(hy2_port=None).hy2_capable is False
    # The obfs password is NO LONGER part of capability (Hy2 is bare QUIC on :443).
    assert _greece(hy2_obfs_password=None).hy2_capable is True


def test_has_alt_transports_true_for_hy2_only_node():
    # A node with Hy2 but no tcp_port still offers a protocol choice.
    assert _greece(tcp_port=None).has_alt_transports is True


# --- resolve_protocol --------------------------------------------------------


def test_resolve_protocol_hy2_only_on_capable_server():
    assert SubscriptionService.resolve_protocol(_greece(), "hy2") == "hy2"
    # A misconfigured node (no SNI) falls back to vless.
    assert SubscriptionService.resolve_protocol(_greece(hy2_sni=None), "hy2") == "vless"
    # vless / unknown always resolve to vless.
    assert SubscriptionService.resolve_protocol(_greece(), "vless") == "vless"
    assert SubscriptionService.resolve_protocol(_greece(), None) == "vless"


# --- link shape --------------------------------------------------------------


def test_build_hy2_link_shape():
    uuid = "11111111-2222-3333-4444-555555555555"
    link = SubscriptionService.build_hy2_link(_greece(), uuid)
    assert link is not None
    parts = urlsplit(link)
    assert parts.scheme == "hysteria2"
    # Auth is the device/sub UUID; host:port is the bare UDP :443 (no hop range,
    # so the port parses cleanly).
    assert parts.username == uuid
    assert parts.hostname == "45.142.31.13"
    assert parts.port == 443
    q = parse_qs(parts.query)
    # ONLY sni — no obfs, no obfs-password, no mport/hop, no insecure, no pinSHA256.
    assert q["sni"] == ["aegis.example.test"]
    assert "obfs" not in q and "obfs-password" not in q
    assert "insecure" not in q and "pinSHA256" not in link
    assert "mport" not in link and "," not in parts.netloc
    assert "up=" not in link and "down=" not in link
    # The flag/name fragment is preserved (Happ shows it).
    assert "Greece" in link


def test_build_hy2_link_none_when_not_capable():
    # No CA cert SNI -> not emittable -> None (caller falls back to vless).
    assert SubscriptionService.build_hy2_link(_greece(hy2_sni=None), "uuid") is None
    # No client target port -> None.
    assert SubscriptionService.build_hy2_link(_greece(hy2_port=None), "uuid") is None
    # Empty device uuid -> None.
    assert SubscriptionService.build_hy2_link(_greece(), "") is None


# --- end-to-end delivery -----------------------------------------------------


async def _seed_hy2_sub(*, capable: bool) -> str:
    """One user + one Greece server (Hy2 capable or not) + an active sub synced
    to it + a protocol=hy2 pref. Returns the sub token."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with async_session_maker() as session:
        user = User(tg_id=920001)
        session.add(user)
        await session.flush()
        server = _greece(hy2_sni="aegis.example.test" if capable else None)
        session.add(server)
        await session.flush()
        now = datetime.now(UTC).replace(tzinfo=None)
        sub = Subscription(
            user_id=user.id,
            sub_token="tok-hy2",
            client_uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            plan_days=30,
            started_at=now,
            expires_at=now + timedelta(days=30),
            is_active=True,
        )
        session.add(sub)
        await session.flush()
        session.add(
            SubscriptionServer(subscription_id=sub.id, server_id=server.id, is_synced=True)
        )
        session.add(
            ServerTransportPref(
                user_id=user.id, server_id=server.id, protocol="hy2", transport="xhttp"
            )
        )
        await session.commit()
        return sub.sub_token


async def test_link_list_carries_hysteria2_for_hy2_picked_location():
    token = await _seed_hy2_sub(capable=True)
    async with async_session_maker() as session:
        encoded = await SubscriptionService.get_subscription_vless_links(session, token)
    body = base64.b64decode(encoded).decode()
    # The Hy2-picked location is delivered as a hysteria2:// URI (sni only).
    assert body.startswith("hysteria2://")
    assert ":443?" in body and "sni=" in body
    assert "obfs" not in body


async def test_xray_json_path_emits_hysteria_outbound_when_hy2_present():
    token = await _seed_hy2_sub(capable=True)
    async with async_session_maker() as session:
        kind, body = await SubscriptionService.build_xray_json_subscription(session, token)
    # A Happ/v2rayTun user who picked Hy2 gets a JSON config (so the baked-in
    # routing survives), with the Hy2 location as the fork's hysteria outbound:
    # bare QUIC on :443, BBR, no salamander obfs.
    assert kind == "json"
    configs = json.loads(body)
    assert len(configs) == 1
    proxy = next(o for o in configs[0]["outbounds"] if o["tag"] == "proxy")
    assert proxy["protocol"] == "hysteria"
    assert proxy["settings"]["version"] == 2
    ss = proxy["streamSettings"]
    assert ss["network"] == "hysteria"
    assert ss["hysteriaSettings"]["auth"]  # the device/sub uuid as the Hy2 auth
    assert ss["finalmask"]["quicParams"]["congestion"] == "bbr"
    assert "udp" not in ss["finalmask"]  # no salamander obfs
    assert ss["tlsSettings"]["alpn"] == ["h3"]
    assert "allowInsecure" not in ss["tlsSettings"]  # real LE cert -> validate
    # The whole point: the baked-in routing is preserved (not a flat link list).
    assert configs[0]["routing"]["rules"]


async def test_misconfigured_hy2_falls_back_not_emitted_as_hy2():
    # Server enabled for Hy2 but missing its SNI: the pref resolves to vless, so
    # the Hy2 short-circuit is skipped. The key assertion is that NO hysteria2://
    # link is produced from a misconfigured node.
    token = await _seed_hy2_sub(capable=False)
    async with async_session_maker() as session:
        encoded = await SubscriptionService.get_subscription_vless_links(session, token)
    body = base64.b64decode(encoded).decode() if encoded else ""
    assert "hysteria2://" not in body
