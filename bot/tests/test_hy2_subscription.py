"""Hysteria2 subscription emission: the hysteria2:// link shape (UDP :443, sni
only, no obfs/hop/insecure), the hy2_capable gate (enabled + port + SNI), and the
xray-JSON delivery path emitting the fork's hysteria outbound (BBR, no obfs) so a
Happ/v2rayTun user who picks Hy2 keeps the baked-in routing instead of a flat
link list.
"""

import base64
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


def _hy2_node(**overrides) -> Server:
    """A fully provisioned, fully Hy2-provisioned server (UDP :443 + LE-cert SNI)."""
    fields = {
        "name": "Testland",
        "flag": "\U0001f1ec\U0001f1f7",
        "host": "203.0.113.10",
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
    assert _hy2_node().hy2_capable is True
    # No CA cert SNI (operator-set, left NULL by the migration) -> not capable.
    assert _hy2_node(hy2_sni=None).hy2_capable is False
    # Not enabled -> not capable.
    assert _hy2_node(hy2_enabled=False).hy2_capable is False
    # No client target port -> not capable.
    assert _hy2_node(hy2_port=None).hy2_capable is False
    # The obfs password is NO LONGER part of capability (Hy2 is bare QUIC on :443).
    assert _hy2_node(hy2_obfs_password=None).hy2_capable is True


def test_has_alt_transports_true_for_hy2_only_node():
    # A node with Hy2 but no tcp_port still offers a protocol choice.
    assert _hy2_node(tcp_port=None).has_alt_transports is True


# --- resolve_protocol --------------------------------------------------------


def test_resolve_protocol_uses_hy2_only_on_capable_node():
    assert SubscriptionService.resolve_protocol(_hy2_node(), "hy2") == "hy2"
    assert SubscriptionService.resolve_protocol(_hy2_node(hy2_sni=None), "hy2") == "vless"
    assert SubscriptionService.resolve_protocol(_hy2_node(), "vless") == "vless"
    assert SubscriptionService.resolve_protocol(_hy2_node(), None) == "vless"


# --- link shape --------------------------------------------------------------


def test_build_hy2_link_shape():
    uuid = "11111111-2222-3333-4444-555555555555"
    link = SubscriptionService.build_hy2_link(_hy2_node(), uuid)
    assert link is not None
    parts = urlsplit(link)
    assert parts.scheme == "hysteria2"
    # Auth is the device/sub UUID; host:port is the bare UDP :443 (no hop range,
    # so the port parses cleanly).
    assert parts.username == uuid
    assert parts.hostname == "203.0.113.10"
    assert parts.port == 443
    q = parse_qs(parts.query)
    # ONLY sni — no obfs, no obfs-password, no mport/hop, no insecure, no pinSHA256.
    assert q["sni"] == ["aegis.example.test"]
    assert "obfs" not in q and "obfs-password" not in q
    assert "insecure" not in q and "pinSHA256" not in link
    assert "mport" not in link and "," not in parts.netloc
    assert "up=" not in link and "down=" not in link
    # The flag/name fragment is preserved (Happ shows it).
    assert "Testland" in link


def test_build_hy2_link_emits_obfs_when_node_has_password():
    # A node carrying an obfs password emits salamander obfs; a node without one
    # stays plain QUIC (mobile-friendly).
    link = SubscriptionService.build_hy2_link(
        _hy2_node(hy2_obfs_password="s4l4m"), "11111111-2222-3333-4444-555555555555"
    )
    q = parse_qs(urlsplit(link).query)
    assert q["obfs"] == ["salamander"]
    assert q["obfs-password"] == ["s4l4m"]
    assert q["sni"] == ["aegis.example.test"]


def test_xray_json_hy2_finalmask_added_only_with_obfs_password():
    server = _hy2_node(hy2_obfs_password="s4l4m")
    link = SubscriptionService.build_hy2_link(server, "11111111-2222-3333-4444-555555555555")
    cfg = SubscriptionService._hy2_link_to_xray_config(link, server)
    proxy = next(o for o in cfg["outbounds"] if o["tag"] == "proxy")
    fm = proxy["streamSettings"]["finalmask"]
    assert fm["udp"][0]["type"] == "salamander"
    assert fm["udp"][0]["settings"]["password"] == "s4l4m"
    assert fm["quicParams"] == {
        "congestion": "reno",
        "disablePathMTUDiscovery": True,
        "keepAlivePeriod": 10,
    }
    # Plain HY2 must use the same native Xray shape as a working imported config.
    plain = SubscriptionService._hy2_link_to_xray_config(
        SubscriptionService.build_hy2_link(_hy2_node(), "11111111-2222-3333-4444-555555555555"),
        _hy2_node(),
    )
    pproxy = next(o for o in plain["outbounds"] if o["tag"] == "proxy")
    assert "finalmask" not in pproxy["streamSettings"]
    assert pproxy["streamSettings"]["tlsSettings"]["fingerprint"] == "qq"


def test_build_hy2_link_none_when_not_capable():
    # No CA cert SNI -> not emittable -> None (caller falls back to vless).
    assert SubscriptionService.build_hy2_link(_hy2_node(hy2_sni=None), "uuid") is None
    # No client target port -> None.
    assert SubscriptionService.build_hy2_link(_hy2_node(hy2_port=None), "uuid") is None
    # Empty device uuid -> None.
    assert SubscriptionService.build_hy2_link(_hy2_node(), "") is None


# --- end-to-end delivery -----------------------------------------------------


async def _seed_hy2_sub(*, capable: bool) -> str:
    """One user + one Testland server (Hy2 capable or not) + an active sub synced
    to it + a protocol=hy2 pref. Returns the sub token."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with async_session_maker() as session:
        user = User(tg_id=920001)
        session.add(user)
        await session.flush()
        server = _hy2_node(hy2_sni="aegis.example.test" if capable else None)
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
        session.add(SubscriptionServer(subscription_id=sub.id, server_id=server.id, is_synced=True))
        session.add(ServerTransportPref(user_id=user.id, server_id=server.id, protocol="hy2", transport="xhttp"))
        await session.commit()
        return sub.sub_token


async def test_misconfigured_hy2_falls_back_not_emitted_as_hy2():
    # Server enabled for Hy2 but missing its SNI: the pref resolves to vless, so
    # the Hy2 short-circuit is skipped. The key assertion is that NO hysteria2://
    # link is produced from a misconfigured node.
    token = await _seed_hy2_sub(capable=False)
    async with async_session_maker() as session:
        encoded = await SubscriptionService.get_subscription_vless_links(session, token)
    body = base64.b64decode(encoded).decode() if encoded else ""
    assert "hysteria2://" not in body


async def test_capable_hy2_preference_emits_hysteria_link():
    token = await _seed_hy2_sub(capable=True)
    async with async_session_maker() as session:
        encoded = await SubscriptionService.get_subscription_vless_links(session, token)
    body = base64.b64decode(encoded).decode() if encoded else ""
    assert body.startswith("hysteria2://")
    assert "sni=aegis.example.test" in body
