"""Hysteria2 subscription emission: the hysteria2:// link shape, the
hy2_capable gate (enabled + port + obfs password, else vless fallback), and the
xray-JSON delivery path downgrading to a base64 link list when a Hy2 location is
present (so a Happ/v2rayTun user who picks Hy2 actually receives a usable
hysteria2:// entry instead of having it silently dropped).
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
    """A Greece-like, fully Hy2-provisioned server (incl. the obfs password)."""
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
        "hy2_port": 36500,
        "hy2_hop_start": 20000,
        "hy2_hop_end": 50000,
        "hy2_obfs_password": "s3cr3t-obfs",
        "hy2_up": "200 mbps",
        "hy2_down": "200 mbps",
        "hy2_sni": "aegis.example.test",
    }
    fields.update(overrides)
    return Server(**fields)


# --- hy2_capable gate --------------------------------------------------------


def test_hy2_capable_requires_enabled_port_and_password():
    assert _greece().hy2_capable is True
    # Enabled but no obfs password (the migration leaves it NULL) -> not capable.
    assert _greece(hy2_obfs_password=None).hy2_capable is False
    # No CA cert SNI (also operator-set, left NULL by the migration) -> not capable.
    assert _greece(hy2_sni=None).hy2_capable is False
    # Not enabled -> not capable even with a password.
    assert _greece(hy2_enabled=False).hy2_capable is False
    # No client target port -> not capable.
    assert _greece(hy2_port=None).hy2_capable is False


def test_has_alt_transports_true_for_hy2_only_node():
    # A node with Hy2 but no tcp_port still offers a protocol choice.
    assert _greece(tcp_port=None).has_alt_transports is True


# --- resolve_protocol --------------------------------------------------------


def test_resolve_protocol_hy2_only_on_capable_server():
    assert SubscriptionService.resolve_protocol(_greece(), "hy2") == "hy2"
    # A misconfigured node (no password) falls back to vless.
    assert (
        SubscriptionService.resolve_protocol(_greece(hy2_obfs_password=None), "hy2") == "vless"
    )
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
    # Auth is the device/sub UUID (so suspension/conn-limit/re-issue key the same
    # as vless), host + client target port from the server.
    assert parts.username == uuid
    # Spec form: port hopping lives in the ADDRESS (host:port,start-end); the
    # port can't be read via parts.port because of the comma.
    assert "@45.142.31.13:36500,20000-50000?" in link
    q = parse_qs(parts.query)
    assert q["obfs"] == ["salamander"]
    assert q["obfs-password"] == ["s3cr3t-obfs"]
    # The node serves a real CA cert for hy2_sni, so the client validates normally:
    # NO insecure and NO pinSHA256 (the xray-core fork rejects both with a self-
    # signed cert). The SNI is exactly the cert domain from the DB.
    assert "insecure" not in q
    assert "pinSHA256" not in q and "pinSHA256" not in link
    assert q["sni"] == ["aegis.example.test"]
    # Bandwidth (up/down) and mport are spec-forbidden in a hysteria2:// URI —
    # emitting them made the hysteria/sing-box core reject the profile locally.
    assert "up=" not in link and "down=" not in link
    assert "mport" not in link
    # The flag/name fragment is preserved (Happ shows it).
    assert "Greece" in link


def test_build_hy2_link_none_when_not_capable():
    # No obfs password -> not emittable -> None (caller falls back to vless).
    assert SubscriptionService.build_hy2_link(_greece(hy2_obfs_password=None), "uuid") is None
    # No CA cert SNI -> not emittable -> None (there is no insecure fallback now).
    assert SubscriptionService.build_hy2_link(_greece(hy2_sni=None), "uuid") is None
    # Empty device uuid -> None.
    assert SubscriptionService.build_hy2_link(_greece(), "") is None


def test_build_hy2_link_no_address_range_without_full_hop_range():
    link = SubscriptionService.build_hy2_link(_greece(hy2_hop_end=None), "uuid")
    assert link is not None
    # No full range -> plain host:port address, no ",start-end" appended.
    assert ":36500," not in link
    assert "mport" not in link


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
        server = _greece(hy2_obfs_password="s3cr3t-obfs" if capable else None)
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
    # The Hy2-picked location is delivered as a hysteria2:// URI, NOT a vless one.
    assert body.startswith("hysteria2://")
    assert "obfs=salamander" in body


async def test_xray_json_path_emits_hysteria_outbound_when_hy2_present():
    token = await _seed_hy2_sub(capable=True)
    async with async_session_maker() as session:
        kind, body = await SubscriptionService.build_xray_json_subscription(session, token)
    # A Happ/v2rayTun user who picked Hy2 now gets a JSON config (so the baked-in
    # routing survives), with the Hy2 location as the fork's hysteria outbound —
    # exactly the config those clients build from the hysteria2:// link themselves.
    assert kind == "json"
    configs = json.loads(body)
    assert len(configs) == 1
    proxy = next(o for o in configs[0]["outbounds"] if o["tag"] == "proxy")
    assert proxy["protocol"] == "hysteria"
    assert proxy["settings"]["version"] == 2
    ss = proxy["streamSettings"]
    assert ss["network"] == "hysteria"
    assert ss["finalmask"]["udp"][0]["type"] == "salamander"
    assert ss["hysteriaSettings"]["auth"]  # the device/sub uuid as the Hy2 auth
    assert "allowInsecure" not in ss["tlsSettings"]  # real LE cert -> validate
    # The whole point: the baked-in routing is preserved (not a flat link list).
    assert configs[0]["routing"]["rules"]


async def test_misconfigured_hy2_falls_back_not_emitted_as_hy2():
    # Server enabled for Hy2 but missing its obfs password: the pref resolves to
    # vless, so the Hy2 short-circuit is skipped (the link would come from the
    # agent, which is unreachable here -> empty body). The key assertion is that
    # NO hysteria2:// link is produced from a misconfigured node.
    token = await _seed_hy2_sub(capable=False)
    async with async_session_maker() as session:
        encoded = await SubscriptionService.get_subscription_vless_links(session, token)
    body = base64.b64decode(encoded).decode() if encoded else ""
    assert "hysteria2://" not in body
