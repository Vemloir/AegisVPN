import asyncio
import base64
import copy
import hashlib
import json
import re
import secrets
import uuid as _uuid_mod
from collections import Counter
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.logger import logger
from src.models import Device, Server, ServerTransportPref, Subscription, SubscriptionServer
from src.services import geoip
from src.services.agent_client import AgentClient
from src.services.cascade_service import advertisable_routes
from src.services.node_control_service import NodeControlService
from src.services.server_access_service import ServerAccessService

_UA_VERSION_RE = re.compile(r"/[\d.]+")
_UA_DIGITS_RE = re.compile(r"\b\d[\d.]*\b")
_UA_PRODUCT_RE = re.compile(r"^([A-Za-z0-9][\w\-.+]*)")
_UA_IOS_VER_RE = re.compile(r"(?:iPhone OS|iOS)\s+([\d_]+)", re.IGNORECASE)
_UA_AND_VER_RE = re.compile(r"Android[/ ]([\d.]+)", re.IGNORECASE)
_UA_WIN_VER_RE = re.compile(r"Windows NT\s+([\d.]+)", re.IGNORECASE)
_WIN_NT = {"10.0": "10/11", "6.3": "8.1", "6.2": "8", "6.1": "7"}
_UA_MAC_VER_RE = re.compile(r"Mac OS X[/ ]([\d_]+)", re.IGNORECASE)

# --- xray-JSON subscription building blocks ---------------------------------
# Clean "default-proxy" policy: everything is tunneled EXCEPT RU/CN/private,
# which go direct (so RU banking/gosuslugi see the user's real RU IP, not the
# exit's).

# RU/CN kept off the tunnel as a small static suffix list instead of
# geosite:category-ru / geosite:cn (those .dat files blow the iOS 50 MB cap).
# National TLDs + a couple of big RU services that aren't on a .ru TLD.
_RU_CN_DIRECT_DOMAINS = [
    "domain:ru",
    "domain:su",
    "domain:рф",
    "domain:moscow",
    "domain:cn",
    "domain:中国",
    "domain:vk.com",
    "domain:yandex.net",
]

# Split DNS. The rule that matters: a name whose traffic rides the tunnel must be
# RESOLVED through the tunnel too.
#
# It is not enough that routing forwards the hostname to the exit node (sniffing
# + domainStrategy AsIs do exactly that). The client still asks for the name
# first — in TUN mode the OS query lands in this dns section — and whatever this
# section does with it is visible to whoever answers. Point it at the system
# resolver and every domain the user visits goes to their ISP in clear text, from
# their real IP, tunnel or no tunnel. That is a textbook DNS leak, and dnsleak
# tests report it as such.
#
# So:
#   - RU/CN suffixes -> the system resolver. Their traffic bypasses the tunnel
#     anyway, so this reveals nothing the ISP cannot already see, and it keeps RU
#     CDN answers local and fast.
#   - everything else -> DoH over `https://` (NOT `https+local://`). Without the
#     +local suffix the query follows the routing rules, so it rides the vless
#     outbound: the resolver sees the exit node's IP and never the user's.
#
# The node address in the outbound is a literal IP, so there is no bootstrap
# name to resolve and no chicken-and-egg: after a Wi-Fi<->cellular switch the
# tunnel re-establishes on its own and DNS resumes with it.
#
# No geosite/geoip: the geo .dat files blow the ~50 MB iOS Network Extension cap
# ("XrayCore: tunnel memory limit exceeded").
_XRAY_CLEAN_DNS = {
    "queryStrategy": "UseIPv4",
    "servers": [
        {
            "address": "localhost",
            "domains": _RU_CN_DIRECT_DOMAINS,
            "skipFallback": True,
        },
        "https://9.9.9.9/dns-query",
    ],
}
_XRAY_CLEAN_INBOUNDS = [
    {
        "tag": "socks",
        "listen": "127.0.0.1",
        "port": 10808,
        "protocol": "socks",
        "settings": {"auth": "noauth", "udp": True},
        "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
    },
    {
        "tag": "http",
        "listen": "127.0.0.1",
        "port": 10809,
        "protocol": "http",
        "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
    },
]
# Private/reserved ranges as explicit CIDRs so the config never loads geoip.dat.
_PRIVATE_CIDRS = [
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
]
_XRAY_CLEAN_ROUTING = {
    # AsIs: never resolve a domain to an IP for matching, so geoip.dat is never
    # loaded. With the geosite-free rules below the whole config stays well under
    # the iOS Network Extension ~50 MB cap — geo .dat files were the cause of
    # "XrayCore: tunnel memory limit exceeded (50 MB)" on iPhone clients.
    "domainStrategy": "AsIs",
    "rules": [
        {"type": "field", "protocol": ["bittorrent"], "outboundTag": "direct"},
        {"type": "field", "ip": _PRIVATE_CIDRS, "outboundTag": "direct"},
        {"type": "field", "domain": _RU_CN_DIRECT_DOMAINS, "outboundTag": "direct"},
    ],
}
# Clients key the location icon off the leading regional-indicator pair and look
# it up in a flag set built from ISO 3166-1. Unicode has exactly two
# supranational sequences, EU and UN, and UN is not an ISO country code — Happ
# has no asset for it and renders no icon at all. EU is a reserved ISO code and
# is present in essentially every flag set, so it is the only non-country flag
# that actually draws. It overstates the case once a non-European node exists,
# which is the price of having an icon at all.
_XRAY_AUTOSELECT_REMARKS = "🇪🇺 Автовыбор"



def _cascade_visible_servers(
    servers: list[Server],
    route_labels_by_entry: dict[int, str],
) -> list[Server]:
    # An entry-only node must never be emitted as a direct exit. It appears only
    # after its enabled route has been acknowledged by the entry and all exits.
    return [server for server in servers if server.node_role != "entry" or server.id in route_labels_by_entry]


def _replace_link_label(link: str, label: str) -> str:
    parts = urlsplit(link)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, label.strip()))


class SubscriptionService:
    SAFE_PROFILE = "safe"
    FAST_PROFILE = "fast"
    LIFETIME_PLAN_DAYS = 0
    LIFETIME_EXPIRES_AT = datetime(2099, 12, 31, 23, 59, 59)

    # Per-location protocol/transport preferences.
    PROTOCOL_VLESS = "vless"
    PROTOCOL_HY2 = "hy2"  # emitted only for an hy2_capable server (see build_hy2_link).
    DEFAULT_PROTOCOL = PROTOCOL_VLESS
    TRANSPORT_XHTTP = "xhttp"
    TRANSPORT_TCP = "tcp"
    DEFAULT_TRANSPORT = TRANSPORT_TCP
    # Transports the bot can actually emit a VLESS link for, given a server's
    # capability fields. xhttp is always available; tcp needs its port.
    VLESS_TRANSPORTS = (TRANSPORT_TCP, TRANSPORT_XHTTP)

    @staticmethod
    def default_transport_for(server: Server) -> str:
        """The best VLESS transport this server can serve by default."""
        if getattr(server, "tcp_port", None):
            return SubscriptionService.TRANSPORT_TCP
        return SubscriptionService.TRANSPORT_XHTTP

    @staticmethod
    def available_transports(server: Server) -> list[str]:
        """VLESS transports this server can serve, best default first."""
        if getattr(server, "tcp_port", None):
            return [SubscriptionService.TRANSPORT_TCP, SubscriptionService.TRANSPORT_XHTTP]
        return [SubscriptionService.TRANSPORT_XHTTP]

    @staticmethod
    def resolve_protocol(server: Server, protocol: str | None) -> str:
        """Emit Hy2 only when both the user selected it and the node can serve it."""
        if protocol == SubscriptionService.PROTOCOL_HY2 and getattr(server, "hy2_capable", False):
            return SubscriptionService.PROTOCOL_HY2
        return SubscriptionService.PROTOCOL_VLESS

    @staticmethod
    def resolve_transport(server: Server, protocol: str | None, transport: str | None) -> str:
        """Collapse a stored (protocol, transport) preference into the concrete
        VLESS transport to emit. Hy2 (handled separately) and a tcp pref on a
        server that lost the capability fall back to the server's current
        capability-aware default."""
        default_transport = SubscriptionService.default_transport_for(server)
        if protocol and protocol != SubscriptionService.PROTOCOL_VLESS:
            # hy2 (or any future non-vless protocol) is not a VLESS transport.
            return default_transport
        if transport in SubscriptionService.available_transports(server):
            return transport
        return default_transport

    @staticmethod
    def build_hy2_link(
        server: Server,
        device_uuid: str,
        duplicate_name_keys: set[str] | None = None,
    ) -> str | None:
        """A ``hysteria2://`` URI for an hy2_capable server, or None.

        The auth secret is the SAME per-device UUID the vless link carries, so
        device suspension / conn-limit / re-issue key identically on the node
        (Hy2 auth maps that UUID -> the device's email via the agent). The node
        serves a real Let's Encrypt cert for server.hy2_sni, so the client
        validates normally (no insecure). Xray JSON clients may additionally use
        the node's explicit Reno/BBR selection; a NULL selection keeps their
        native congestion-control default.

        salamander obfs is emitted ONLY when the node carries an obfs password
        (``server.hy2_obfs_password``). On some networks a no-obfs connection
        completes the QUIC handshake but loses its data streams (observed
        server-side: auth OK, then "accepting stream failed: timeout"); obfs
        keeps the stream packets intact. A no-obfs node looks like plain HTTP/3
        QUIC, which other networks carry fine. Per-node so we can serve both kinds.
        Returns None when not Hy2-capable so the caller falls back to a vless link.
        """
        if not getattr(server, "hy2_capable", False) or not device_uuid:
            return None
        userinfo = quote(device_uuid, safe="")
        fragment = SubscriptionService.format_server_label(server, duplicate_name_keys)
        netloc = f"{userinfo}@{server.host}:{server.hy2_port}"
        query = {"sni": server.hy2_sni}
        if server.hy2_obfs_password:
            query["obfs"] = "salamander"
            query["obfs-password"] = server.hy2_obfs_password
        return urlunsplit(("hysteria2", netloc, "", urlencode(query), fragment))

    @staticmethod
    def build_mtproxy_link(server: Server) -> str | None:
        """A ``https://t.me/proxy?...`` MTProto-proxy link for an
        ``mtproxy_capable`` server, or None. The fake-TLS (``ee``) secret already
        encodes the camouflage domain the mtg server fronts, so the link only
        needs the node host, the listen port, and that secret."""
        if not getattr(server, "mtproxy_capable", False):
            return None
        query = {
            "server": server.host,
            "port": server.mtproxy_port,
            "secret": server.mtproxy_secret,
        }
        return urlunsplit(("https", "t.me", "/proxy", urlencode(query), ""))

    @staticmethod
    async def get_transport_pref(session: AsyncSession, user_id: int, server_id: int) -> tuple[str, str]:
        """The effective choice shown by the UI and emitted right now.

        Keep stale rows in storage so an explicitly selected protocol can become
        active again after an operator restores that capability.  The user must
        nevertheless see the current fallback, not a selected-but-unavailable
        protocol that the subscription builder has already collapsed to VLESS.
        """
        pref = await session.get(ServerTransportPref, (user_id, server_id))
        server = await session.get(Server, server_id)
        if pref is None:
            transport = (
                SubscriptionService.default_transport_for(server)
                if server is not None
                else SubscriptionService.DEFAULT_TRANSPORT
            )
            return SubscriptionService.DEFAULT_PROTOCOL, transport
        if server is None:
            return SubscriptionService.DEFAULT_PROTOCOL, SubscriptionService.DEFAULT_TRANSPORT
        protocol = SubscriptionService.resolve_protocol(server, pref.protocol)
        transport = SubscriptionService.resolve_transport(server, pref.protocol, pref.transport)
        return protocol, transport

    @staticmethod
    async def set_transport_pref(
        session: AsyncSession,
        user_id: int,
        server_id: int,
        protocol: str,
        transport: str,
    ) -> None:
        """Store an explicit choice; selecting this server's default deletes it."""
        server = await session.get(Server, server_id)
        default_transport = (
            SubscriptionService.default_transport_for(server)
            if server is not None
            else SubscriptionService.DEFAULT_TRANSPORT
        )
        if protocol == SubscriptionService.DEFAULT_PROTOCOL and transport == default_transport:
            await SubscriptionService.reset_transport_pref(session, user_id, server_id)
            return
        pref = await session.get(ServerTransportPref, (user_id, server_id))
        if pref is None:
            session.add(
                ServerTransportPref(
                    user_id=user_id,
                    server_id=server_id,
                    protocol=protocol,
                    transport=transport,
                )
            )
        else:
            pref.protocol = protocol
            pref.transport = transport
        await session.commit()

    @staticmethod
    async def reset_transport_pref(session: AsyncSession, user_id: int, server_id: int) -> None:
        """Drop a location's preference, returning it to the server default."""
        await session.execute(
            delete(ServerTransportPref).where(
                ServerTransportPref.user_id == user_id,
                ServerTransportPref.server_id == server_id,
            )
        )
        await session.commit()

    @staticmethod
    async def _transport_prefs_for_user(session: AsyncSession, user_id: int, server_ids: list[int]) -> dict[int, str]:
        """Resolve a concrete VLESS transport for every existing requested server."""
        if not server_ids:
            return {}
        prefs = (
            (
                await session.execute(
                    select(ServerTransportPref).where(
                        ServerTransportPref.user_id == user_id,
                        ServerTransportPref.server_id.in_(server_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        servers = (
            (await session.execute(select(Server).where(Server.id.in_(server_ids))))
            .scalars()
            .all()
        )
        prefs_by_server = {pref.server_id: pref for pref in prefs}
        return {
            server.id: SubscriptionService.resolve_transport(
                server,
                prefs_by_server[server.id].protocol if server.id in prefs_by_server else None,
                prefs_by_server[server.id].transport if server.id in prefs_by_server else None,
            )
            for server in servers
        }

    @staticmethod
    async def _hy2_servers_for_user(session: AsyncSession, user_id: int, server_ids: list[int]) -> set[int]:
        """The subset of ``server_ids`` whose stored preference resolves to Hy2.

        A server is included only when the user picked protocol=hy2 AND the
        server is Hy2-capable (enabled + port + CA-certificate SNI). A stale hy2 pref
        on a node that is not capable resolves to vless and is omitted, so the
        caller emits a vless link there instead of a broken Hy2 one."""
        if not server_ids:
            return set()
        rows = (
            (
                await session.execute(
                    select(ServerTransportPref).where(
                        ServerTransportPref.user_id == user_id,
                        ServerTransportPref.server_id.in_(server_ids),
                        ServerTransportPref.protocol == SubscriptionService.PROTOCOL_HY2,
                    )
                )
            )
            .scalars()
            .all()
        )
        result: set[int] = set()
        for pref in rows:
            server = await session.get(Server, pref.server_id)
            if server is None:
                continue
            if SubscriptionService.resolve_protocol(server, pref.protocol) == SubscriptionService.PROTOCOL_HY2:
                result.add(pref.server_id)
        return result

    @staticmethod
    def normalize_profile(profile: str | None) -> str:
        return (
            SubscriptionService.FAST_PROFILE
            if profile == SubscriptionService.FAST_PROFILE
            else SubscriptionService.SAFE_PROFILE
        )

    @staticmethod
    def is_lifetime_subscription(sub: Subscription | None) -> bool:
        if sub is None:
            return False
        return (
            sub.plan_days == SubscriptionService.LIFETIME_PLAN_DAYS
            or sub.expires_at >= SubscriptionService.LIFETIME_EXPIRES_AT
        )

    @staticmethod
    def generate_sub_token_value(existing_tokens: set[str] | None = None) -> str:
        taken = existing_tokens or set()
        while True:
            token = secrets.token_urlsafe(24)
            if token not in taken:
                return token

    @staticmethod
    async def generate_sub_token(session: AsyncSession) -> str:
        while True:
            token = SubscriptionService.generate_sub_token_value()
            result = await session.execute(select(Subscription.id).where(Subscription.sub_token == token))
            if result.scalar_one_or_none() is None:
                return token

    @staticmethod
    def build_subscription_url(sub_token: str, profile: str | None = None) -> str:
        normalized_profile = SubscriptionService.normalize_profile(profile)
        path = "sub-fast" if normalized_profile == SubscriptionService.FAST_PROFILE else "sub"
        return f"{settings.subscription_base_url}/{path}/{sub_token}"

    @staticmethod
    async def get_subscription_by_token(
        session: AsyncSession, token: str, active_only: bool = True
    ) -> Subscription | None:
        stmt = select(Subscription).where(or_(Subscription.sub_token == token, Subscription.legacy_sub_token == token))
        if active_only:
            stmt = stmt.where(Subscription.is_active == True)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def find_latest_subscription(session: AsyncSession, user_id: int) -> "Subscription | None":
        result = await session.execute(
            select(Subscription).where(Subscription.user_id == user_id).order_by(Subscription.id.desc()).limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    def server_sort_key(server: Server) -> tuple[int, int, str, int]:
        # Explicit display_order (>0) takes priority; ties or unset fall back to
        # alphabetical name, with "Exp" servers winning their base-name group.
        name = server.name.strip() or f"server {server.id}"
        folded = name.casefold()
        is_exp = folded.endswith(" exp")
        base_name = folded[:-4] if is_exp else folded
        explicit_order = getattr(server, "display_order", 0) or 0
        if explicit_order > 0:
            return (0, explicit_order, "", server.id)
        return (1, 0 if is_exp else 1, base_name, server.id)

    @staticmethod
    def server_display_name(server: Server) -> str:
        """User-facing label text: the full stored name, e.g. 'Germany | Frankfurt'.
        Locations are shown as 'Country | City' both in the bot UI and in the
        subscription (the per-server remark carried in the vless link fragment)."""
        name = (server.name or "").strip()
        return name or f"Server {server.id}"

    @staticmethod
    def format_server_label(server: Server, duplicate_name_keys: set[str] | None = None) -> str:
        name = SubscriptionService.server_display_name(server)
        key = name.casefold()
        suffix = f" \u2116{server.id}" if duplicate_name_keys and key in duplicate_name_keys else ""
        label = f"{server.flag} {name}{suffix}".strip()
        return label or f"Server {server.id}"

    @staticmethod
    def normalize_vless_uri(
        raw_uri: str,
        server: Server,
        duplicate_name_keys: set[str] | None = None,
        profile: str | None = None,
        transport: str | None = None,
    ) -> str:
        """Normalize the agent's raw vless link onto the bot's authoritative
        params (host/port/reality keypair).

        ``transport`` selects which of the server's VLESS+REALITY inbounds to
        target. ``None`` resolves to TCP+Vision when ``tcp_port`` is provisioned,
        otherwise XHTTP. An explicit choice always wins when the node supports it.
        """
        parts = urlsplit(raw_uri)
        userinfo, _, _ = parts.netloc.rpartition("@")
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        effective = SubscriptionService.resolve_transport(
            server,
            SubscriptionService.PROTOCOL_VLESS,
            transport.lower() if transport else None,
        )
        is_xhttp = effective == "xhttp"
        is_tcp = effective == SubscriptionService.TRANSPORT_TCP
        query["security"] = "reality"
        query["encryption"] = "none"
        query["fp"] = query.get("fp", "chrome")
        query["sni"] = query.get("sni", "")
        query["pbk"] = server.public_key
        query["sid"] = server.short_id
        query["spx"] = query.get("spx", "/")
        # Per-transport stream params.
        override_port: int | None = None
        if is_tcp:
            query["type"] = "tcp"
            query["headerType"] = "none"
            # The tcp alt-transport runs TCP+REALITY with the vision flow
            # (must match the agent's vless-in-tcp inbound, whose clients carry
            # flow=xtls-rprx-vision). No Mux/xudp — vision breaks under Mux.
            query["flow"] = "xtls-rprx-vision"
            query.pop("path", None)
            query.pop("mode", None)
            query.pop("host", None)
            query.pop("serviceName", None)
            query.pop("packetEncoding", None)
            override_port = server.tcp_port
        elif is_xhttp:
            query["type"] = "xhttp"
            query.pop("headerType", None)
            query.pop("flow", None)
            query["path"] = query.get("path", "/")
            query["mode"] = query.get("mode", "packet-up")
            if not query.get("packetEncoding"):
                query.pop("packetEncoding", None)
            if not query.get("host"):
                query.pop("host", None)
        else:
            query["type"] = "tcp"
            query["headerType"] = "none"
            query["flow"] = "xtls-rprx-vision"
            query.pop("path", None)
            query.pop("mode", None)
            query.pop("host", None)
        normalized_query = urlencode(query)
        # Happ expects raw emoji in subscription headers; percent-encoding them
        # makes the server name degrade to n/a there.
        fragment = SubscriptionService.format_server_label(server, duplicate_name_keys)
        # server.host (from the bot DB) is authoritative for the address the
        # client connects to — we deliberately override whatever hostname the
        # agent baked into its own URL. This lets us pin bare IPs in the DB and
        # drop the sslip.io dependency (a third-party DNS that, when it hiccups,
        # takes down every location except the one already on a bare IP).
        target_host = server.host or parts.hostname
        # TCP carries its own inbound port; XHTTP keeps the agent/server port.
        target_port = override_port or parts.port or server.port
        netloc = f"{userinfo}@{target_host}:{target_port}" if userinfo else f"{target_host}:{target_port}"
        return urlunsplit((parts.scheme, netloc, parts.path, normalized_query, fragment))

    @staticmethod
    async def sync_subscription_to_servers(
        session: AsyncSession,
        sub: Subscription,
        servers: list[Server],
    ) -> None:
        if not servers:
            return

        email = f"user_{sub.user_id}_sub_{sub.id}"
        # The sub's own UUID PLUS every active, non-suspended device. A newly
        # added server MUST receive all of them: a device whose UUID isn't on the
        # node makes that node's per-device /sub fetch 404, so the whole location
        # is silently dropped from that device's config — it shows in the bot's
        # location list (which never hits the agent) but never in the imported
        # subscription. Bulk-add so it's one round-trip per node. (Without the
        # devices, only the bare sub UUID synced, so existing devices created
        # before the node lost that location.)
        devices = (
            (
                await session.execute(
                    select(Device).where(
                        Device.subscription_id == sub.id,
                        Device.is_active == True,  # noqa: E712
                        Device.is_suspended == False,  # noqa: E712
                    )
                )
            )
            .scalars()
            .all()
        )
        clients = [{"uuid": sub.client_uuid, "email": email, "expire_ms": 0}]
        clients += [{"uuid": d.uuid, "email": f"{email}_dev_{d.id}", "expire_ms": 0} for d in devices]

        async def sync_to_server(server: Server) -> tuple[int, bool]:
            if not NodeControlService.pushes_to(server):
                return server.id, False
            client = AgentClient(server.agent_url, server.agent_token)
            try:
                success = await client.bulk_add(clients)
            except Exception as exc:
                logger.error(f"Failed to sync sub {sub.id} ({len(clients)} clients) to server {server.id}: {exc}")
                success = False
            return server.id, success

        sync_results = dict(await asyncio.gather(*(sync_to_server(server) for server in servers)))
        existing_links = await session.execute(
            select(SubscriptionServer).where(
                SubscriptionServer.subscription_id == sub.id,
                SubscriptionServer.server_id.in_(sync_results.keys()),
            )
        )
        by_server_id = {link.server_id: link for link in existing_links.scalars().all()}

        for server_id, is_synced in sync_results.items():
            link = by_server_id.get(server_id)
            if link:
                link.is_synced = is_synced
                continue

            session.add(
                SubscriptionServer(
                    subscription_id=sub.id,
                    server_id=server_id,
                    is_synced=is_synced,
                )
            )
        await session.flush()
        await NodeControlService.publish_for_servers(
            session,
            set(sync_results),
        )

    @staticmethod
    async def _collect_links(
        session: AsyncSession,
        sub_token: str,
        profile: str | None = None,
        device_uuid: str | None = None,
    ) -> list[tuple[Server, str]]:
        normalized_profile = SubscriptionService.normalize_profile(profile)
        include_all_profiles = profile != SubscriptionService.FAST_PROFILE
        sub = await SubscriptionService.get_subscription_by_token(session, sub_token)

        if not sub:
            return []

        now = datetime.now(UTC).replace(tzinfo=None)
        if sub.expires_at <= now:
            sub.is_active = False
            await SubscriptionService.remove_subscription_from_servers(session, sub)
            await session.commit()
            return []

        if include_all_profiles:
            server_filter = and_(Server.is_active == True, Server.hidden_from_subscription == False)
        else:
            server_filter = and_(
                Server.is_active == True,
                Server.hidden_from_subscription == False,
                or_(
                    Server.subscription_group == normalized_profile,
                    Server.subscription_group == "both",
                ),
            )
        result = await session.execute(
            select(Server)
            .join(SubscriptionServer)
            .where(
                SubscriptionServer.subscription_id == sub.id,
                SubscriptionServer.is_synced == True,
                server_filter,
            )
        )
        servers = result.scalars().all()

        routes = await advertisable_routes(
            session,
            {server.id for server in servers if server.node_role == "entry"},
        )
        route_labels_by_entry = {route.entry_server_id: route.label for route in routes}
        servers = _cascade_visible_servers(servers, route_labels_by_entry)

        servers = sorted(servers, key=SubscriptionService.server_sort_key)
        server_name_counts = Counter(SubscriptionService.server_display_name(server).casefold() for server in servers)
        duplicate_name_keys = {name for name, count in server_name_counts.items() if count > 1}

        effective_uuid = device_uuid or sub.client_uuid

        # Resolve a concrete per-location transport. A missing preference row
        # becomes TCP on a TCP-capable server and XHTTP everywhere else.
        server_ids = [server.id for server in servers]
        transport_by_server = await SubscriptionService._transport_prefs_for_user(session, sub.user_id, server_ids)
        # Locations the user pinned to Hysteria2 (only those that are actually
        # Hy2-capable; a stale/misconfigured pref is dropped so the vless path
        # below runs instead).
        hy2_server_ids = await SubscriptionService._hy2_servers_for_user(session, sub.user_id, server_ids)
        # Cascade is deliberately client-facing VLESS only. Per-location Hy2
        # remains authoritative for direct locations and is ignored for entry
        # routes until a separate UDP cascade is designed.
        hy2_server_ids.difference_update(route_labels_by_entry)
        # AsyncSession cannot run concurrent queries. Resolve pull-node templates
        # sequentially before the network fetches below are gathered.
        pull_links: dict[int, str | None] = {}
        for server in servers:
            if server.control_mode != "pull":
                continue
            target_profile = (
                SubscriptionService.FAST_PROFILE
                if server.subscription_group == SubscriptionService.FAST_PROFILE
                else SubscriptionService.SAFE_PROFILE
            )
            pull_links[server.id] = await NodeControlService.build_subscription_uri(
                session,
                server,
                effective_uuid,
                profile=target_profile,
            )

        async def fetch_link(server: Server) -> str:
            # Hy2-picked location: emit a hysteria2:// link directly. xray-core
            # cannot speak Hy2, so this never goes through the agent's vless
            # /sub endpoint. Same auth (effective_uuid = device or sub UUID) as
            # vless, so suspension / conn-limit / re-issue key identically.
            if server.id in hy2_server_ids:
                hy2 = SubscriptionService.build_hy2_link(server, effective_uuid, duplicate_name_keys)
                if hy2:
                    return hy2
                # Capability lost between pref-resolution and emission: fall back
                # to a vless link rather than ship nothing.
            target_profile = (
                SubscriptionService.FAST_PROFILE
                if server.subscription_group == SubscriptionService.FAST_PROFILE
                else SubscriptionService.SAFE_PROFILE
            )
            try:
                if server.control_mode == "pull":
                    text = pull_links.get(server.id)
                else:
                    client = AgentClient(server.agent_url, server.agent_token)
                    text = await client.get_subscription(
                        effective_uuid,
                        profile=target_profile,
                    )
                if text:
                    normalized = SubscriptionService.normalize_vless_uri(
                        text,
                        server,
                        duplicate_name_keys,
                        profile=target_profile,
                        transport=transport_by_server.get(server.id),
                    )
                    if server.id in route_labels_by_entry:
                        return _replace_link_label(
                            normalized,
                            route_labels_by_entry[server.id],
                        )
                    return normalized
            except Exception as exc:
                logger.error(f"Failed to fetch sub from server {server.id}: {exc}")
            return ""

        links = await asyncio.gather(*(fetch_link(server) for server in servers))
        return [(server, link) for server, link in zip(servers, links, strict=False) if link]

    @staticmethod
    async def get_subscription_vless_links(
        session: AsyncSession,
        sub_token: str,
        profile: str | None = None,
        device_uuid: str | None = None,
    ) -> str:
        pairs = await SubscriptionService._collect_links(session, sub_token, profile, device_uuid)
        body = "\n".join(link for _, link in pairs)
        return base64.b64encode(body.encode("utf-8")).decode("utf-8") if body else ""

    @staticmethod
    async def build_xray_json_subscription(
        session: AsyncSession,
        sub_token: str,
        profile: str | None = None,
        device_uuid: str | None = None,
    ) -> tuple[str, str]:
        """Subscription for xray-JSON clients (Happ, v2rayTun, v2rayNG, …).

        Returns ``(kind, body)`` where ``kind`` is ``"json"`` for the xray-JSON
        config array, or ``"links"`` only as a safety-net fallback for an entry
        that cannot be expressed as a config object. Each JSON config carries the
        clean default-proxy routing (everything tunneled except RU/CN/private) +
        DNS resolved DIRECT + the xhttp recovery knobs (hKeepAlivePeriod /
        tcpKeepAlive), so a Wi-Fi<->cellular switch recovers instead of hanging.

        Both vless AND hysteria2 entries become xray config objects — the Hy2 one
        via the fork's hysteria outbound (see _hy2_link_to_xray_config), which is
        exactly the config Happ/v2rayTun build from our hysteria2:// link
        themselves. So a Hy2-picked location keeps the SAME baked-in routing as
        the vless ones instead of forcing the whole subscription down to a flat
        link list. Non-Hy2 subscriptions are byte-identical to before.
        """
        pairs = await SubscriptionService._collect_links(session, sub_token, profile, device_uuid)
        configs = []
        for server, link in pairs:
            if link.startswith("vless://"):
                cfg = SubscriptionService._vless_link_to_xray_config(link, server)
            elif link.startswith("hysteria2://"):
                cfg = SubscriptionService._hy2_link_to_xray_config(link, server)
            else:
                cfg = None
            if cfg is None:
                # An entry we cannot express as an xray config object — fall the
                # WHOLE subscription back to the base64 link list so nothing is
                # silently dropped (vless + hysteria2 both convert, so this is
                # only a safety net).
                body = "\n".join(lnk for _, lnk in pairs)
                return "links", (base64.b64encode(body.encode("utf-8")).decode("utf-8") if body else "")
            configs.append((server, cfg))
        autoselect = SubscriptionService._build_autoselect_config(configs)
        plain_configs = [cfg for _server, cfg in configs]
        if autoselect is not None:
            # First, not last: it is the entry most users should take, and the
            # one that stops mattering the moment it is scrolled past.
            plain_configs.insert(0, autoselect)
        return "json", (json.dumps(plain_configs, ensure_ascii=False) if plain_configs else "")

    @staticmethod
    def _build_autoselect_config(configs: list[tuple[Server, dict]]) -> dict | None:
        """One extra xray-JSON entry that bundles every location's proxy outbound
        into a single balancer, so the client itself (not us) measures real
        per-user RTT to each node and picks between them — the thing a
        server-rendered subscription structurally cannot do on its own. Skipped
        below 2 locations, where there is nothing to choose between.

        Load is balanced without the bot deciding anything. `expected` makes the
        client spread traffic randomly across the best-ranked nodes rather than
        funnelling everyone onto a single winner, so across the user base the
        split evens out on its own — no per-node capacity number, no threshold,
        and no dependence on telemetry that can be stale or missing. Ranking
        still comes first, so a node that is meaningfully worse than the others
        never enters the set being spread across.

        Every candidate is offered; the bot deliberately does not pre-filter by
        its own load telemetry. It cannot see the one number that decides the
        outcome — the latency between THIS user and each node — so any list it
        trimmed would be trimmed blind."""
        candidates = configs
        if len(candidates) < 2:
            return None
        outbounds: list[dict] = []
        for i, (_server, cfg) in enumerate(candidates, start=1):
            proxy = copy.deepcopy(cfg["outbounds"][0])
            proxy["tag"] = "proxy" if i == 1 else f"proxy-{i}"
            outbounds.append(proxy)
        outbounds.append({"tag": "direct", "protocol": "freedom", "settings": {"domainStrategy": "UseIP"}})
        outbounds.append({"tag": "block", "protocol": "blackhole"})
        routing = copy.deepcopy(_XRAY_CLEAN_ROUTING)
        routing["rules"].append({"type": "field", "network": "tcp,udp", "balancerTag": "auto"})
        routing["balancers"] = [
            {
                "tag": "auto",
                "selector": ["proxy"],
                # leastPing, NOT leastLoad. leastLoad sorts on RTTDeviationCost
                # — the jitter of the link — and only breaks ties on average
                # RTT, so a far-away node with a metronome-steady connection
                # outranks a near one that wobbles. In practice that handed
                # European users Hong Kong. leastPing ranks on the single number
                # that matters here, lowest measured delay.
                #
                # The cost is that leastPing returns exactly one node, so there
                # is no spreading across comparable nodes any more. That is the
                # right trade while no node is anywhere near saturated: picking
                # the wrong continent is a real, visible defect, and uneven load
                # is currently hypothetical.
                #
                # No fallbackTag on purpose: the strategy returns nothing when
                # the observatory has no usable report, and the caller then
                # defers to fallbackTag — pointing that at "direct" would push
                # traffic outside the tunnel while the user still sees a
                # connected VPN.
                "strategy": {"type": "leastPing"},
            }
        ]
        return {
            "remarks": _XRAY_AUTOSELECT_REMARKS,
            "dns": _XRAY_CLEAN_DNS,
            "inbounds": _XRAY_CLEAN_INBOUNDS,
            "outbounds": outbounds,
            "routing": routing,
            "burstObservatory": {
                "subjectSelector": ["proxy"],
                "pingConfig": {
                    "destination": "http://www.gstatic.com/generate_204",
                    "connectivity": "",
                    "interval": "1m",
                    "sampling": 3,
                    "timeout": "3s",
                },
            },
        }

    @staticmethod
    def _vless_link_to_xray_config(link: str, server: Server) -> dict | None:
        """Parse one normalized vless:// link into a complete, standalone xray
        client config. Returns None for non-vless links (which xray-core cannot
        run as an outbound)."""
        if not link.startswith("vless://"):
            return None
        parts = urlsplit(link)
        q = dict(parse_qsl(parts.query, keep_blank_values=True))
        network = (q.get("type") or "tcp").lower()
        user: dict = {"id": parts.username or "", "encryption": "none", "level": 8}
        stream: dict = {
            "network": network,
            "security": "reality",
            "realitySettings": {
                "publicKey": q.get("pbk", ""),
                "shortId": q.get("sid", ""),
                "serverName": q.get("sni", ""),
                "fingerprint": q.get("fp", "firefox"),
                "show": False,
            },
        }
        if network == "xhttp":
            stream["xhttpSettings"] = {
                "host": q.get("host", ""),
                "path": q.get("path", "/"),
                "mode": q.get("mode", "auto"),
                # Primary roaming-recovery knob: the H2 PING health-check fires
                # in ~15s instead of the 45-60s default, tearing down the dead
                # download GET fast after a network change.
                "xmux": {"hKeepAlivePeriod": 15},
            }
            stream["sockopt"] = {"tcpKeepAliveIdle": 10, "tcpKeepAliveInterval": 5}
        elif q.get("flow"):
            # tcp/REALITY with the vision flow (the tcp alt-transport, and
            # any legacy tcp inbound). Plain reality stream settings, no Mux/xudp —
            # Mux breaks xtls-rprx-vision (see the note in main.py).
            user["flow"] = q["flow"]
        proxy = {
            "tag": "proxy",
            "protocol": "vless",
            "settings": {"vnext": [{"address": parts.hostname, "port": parts.port or 443, "users": [user]}]},
            "streamSettings": stream,
        }
        return {
            "remarks": unquote(parts.fragment) if parts.fragment else (server.name or "").strip(),
            "dns": _XRAY_CLEAN_DNS,
            "inbounds": _XRAY_CLEAN_INBOUNDS,
            "outbounds": [
                proxy,
                {"tag": "direct", "protocol": "freedom", "settings": {"domainStrategy": "UseIP"}},
                {"tag": "block", "protocol": "blackhole"},
            ],
            "routing": _XRAY_CLEAN_ROUTING,
        }

    @staticmethod
    def _hy2_link_to_xray_config(link: str, server: Server) -> dict | None:
        """Parse one hysteria2:// link into an xray-JSON config object.

        xray-core proper has no hysteria2 outbound, but the xray FORK Happ /
        v2rayTun bundle DOES run hysteria as an xray outbound — it is exactly the
        config those clients generate from our hysteria2:// link themselves
        (protocol "hysteria", auth under hysteriaSettings). Emitting it directly
        lets the Hy2 location keep the SAME baked-in routing/DNS as the vless
        entries instead of forcing the whole subscription down to a flat link
        list. The client validates the real Let's
        Encrypt cert (no allowInsecure). Plain HY2 normally omits finalmask to
        match the native Xray HY2 shape used by compatible clients. An explicit
        per-node congestion controller is the one exception: it is emitted as
        finalmask.quicParams independently of Salamander. Xray's native default
        is BBR when no bandwidth is specified. Salamander obfs is added as
        finalmask.udp only when the link carries it.
        """
        if not link.startswith("hysteria2://"):
            return None
        parts = urlsplit(link)
        q = dict(parse_qsl(parts.query, keep_blank_values=True))
        userinfo = parts.netloc.split("@", 1)[0] if "@" in parts.netloc else ""
        hostpart = parts.netloc.split("@", 1)[1] if "@" in parts.netloc else parts.netloc
        host = hostpart.split(":", 1)[0]
        # netloc port may carry a hop range (host:port,start-end) — take the base.
        port_str = hostpart.split(":", 1)[1].split(",", 1)[0] if ":" in hostpart else "443"
        try:
            port = int(port_str)
        except ValueError:
            return None
        proxy = {
            "tag": "proxy",
            "protocol": "hysteria",
            "settings": {"address": host, "port": port, "version": 2},
            "streamSettings": {
                "network": "hysteria",
                "security": "tls",
                "hysteriaSettings": {"version": 2, "auth": userinfo},
                "tlsSettings": {
                    "serverName": q.get("sni", ""),
                    # Match the client-native HY2 JSON shape used by Happ/
                    # Varmlen. This is intentionally independent of the REALITY
                    # fingerprint used by VLESS transports.
                    "fingerprint": "qq",
                    "alpn": ["h3"],
                },
            },
        }
        # Some filtered paths classify QUIC by ClientHello SNI even after the
        # TLS/HY2 auth exchange succeeds. Keep the URI's SNI as the certificate
        # name for native clients, but Xray JSON can safely separate the visible
        # SNI from certificate verification. ``verifyPeerCertByName`` performs
        # normal system-root validation against the real name; unlike
        # allowInsecure, a forged or MITM certificate is still rejected.
        certificate_sni = q.get("sni", "").strip()
        camouflage_sni = (getattr(server, "hy2_camouflage_sni", None) or "").strip()
        if camouflage_sni and certificate_sni and camouflage_sni != certificate_sni:
            tls_settings = proxy["streamSettings"]["tlsSettings"]
            tls_settings["serverName"] = camouflage_sni
            tls_settings["verifyPeerCertByName"] = certificate_sni

        # Both Salamander and Xray's QUIC tuning live under ``finalmask``, but
        # they are independent: a plain-QUIC node may need Reno while carrying
        # no UDP mask at all. Keep NULL/unknown operator values out of generated
        # configs; ``brutal`` is intentionally excluded because it additionally
        # requires valid per-user bandwidth settings.
        finalmask: dict = {}
        has_obfs = q.get("obfs") == "salamander" and bool(q.get("obfs-password"))
        if has_obfs:
            finalmask["udp"] = [{"type": "salamander", "settings": {"password": q["obfs-password"]}}]

        congestion = (getattr(server, "hy2_congestion", None) or "").strip().lower()
        if congestion not in {"reno", "bbr"}:
            congestion = ""
        if congestion:
            finalmask["quicParams"] = {
                "congestion": congestion,
                # Keep the explicit controller usable on lossy/mobile paths:
                # Xray otherwise enables PMTU probing and has no HY2 keepalive
                # by default. Both knobs are valid independently of obfs.
                "disablePathMTUDiscovery": True,
                "keepAlivePeriod": 10,
            }
        elif has_obfs:
            # Preserve the already deployed exceptional Salamander profile until
            # an operator chooses an explicit per-node controller.
            finalmask["quicParams"] = {
                "congestion": "reno",
                "disablePathMTUDiscovery": True,
                "keepAlivePeriod": 10,
            }

        if finalmask:
            proxy["streamSettings"]["finalmask"] = finalmask
        return {
            "remarks": unquote(parts.fragment) if parts.fragment else (server.name or "").strip(),
            "dns": _XRAY_CLEAN_DNS,
            "inbounds": _XRAY_CLEAN_INBOUNDS,
            "outbounds": [
                proxy,
                {"tag": "direct", "protocol": "freedom", "settings": {"domainStrategy": "UseIP"}},
                {"tag": "block", "protocol": "blackhole"},
            ],
            "routing": _XRAY_CLEAN_ROUTING,
        }

    @staticmethod
    async def remove_subscription_from_servers(session: AsyncSession, sub: Subscription) -> None:
        result = await session.execute(
            select(Server)
            .join(SubscriptionServer)
            .where(
                SubscriptionServer.subscription_id == sub.id,
            )
        )
        servers = result.scalars().all()

        # The sub's own UUID PLUS every device UUID. sync_subscription_to_servers
        # pushes all of them to each node, and a device connects with its OWN
        # UUID (not the sub's). Removing only sub.client_uuid left every device
        # authenticated, so revoke/expiry didn't actually cut off access. Pull
        # all device UUIDs (regardless of active/suspended) so nothing lingers.
        device_uuids = (
            (await session.execute(select(Device.uuid).where(Device.subscription_id == sub.id))).scalars().all()
        )
        uuids = [sub.client_uuid, *device_uuids]

        async def remove_from_server(server: Server) -> None:
            if not NodeControlService.pushes_to(server):
                return
            client = AgentClient(server.agent_url, server.agent_token)
            for client_uuid in uuids:
                try:
                    await client.remove_client(client_uuid)
                except Exception as exc:
                    logger.error(f"Failed to remove client {client_uuid} from server {server.id}: {exc}")

        await asyncio.gather(*(remove_from_server(server) for server in servers))
        await session.flush()
        await NodeControlService.publish_for_servers(
            session,
            {server.id for server in servers},
        )

    # ------------------------------------------------------------------
    # Device management
    # ------------------------------------------------------------------

    @staticmethod
    def _os_version(raw: str, keep_minor: bool = False) -> str:
        """A plausible OS version from a raw UA token, or '' if implausible.

        Guards against clients (e.g. Happ) that append a long build number after the
        OS name like ``Android/17800541067281831514`` — a real OS major version is a
        small integer, so anything above 99 is rejected.
        """
        parts = raw.replace("_", ".").split(".")
        head = parts[0]
        if not head.isdigit() or not (1 <= int(head) <= 99):
            return ""
        if keep_minor and len(parts) > 1 and parts[1].isdigit():
            return f"{head}.{parts[1]}"
        return head

    @staticmethod
    def _detect_platform(ua: str) -> str:
        ua_lower = ua.lower()
        if "ipad" in ua_lower:
            m = _UA_IOS_VER_RE.search(ua)
            ver = SubscriptionService._os_version(m.group(1)) if m else ""
            return f"iPad · iOS {ver}" if ver else "iPad"
        if "iphone" in ua_lower or "iphone os" in ua_lower:
            m = _UA_IOS_VER_RE.search(ua)
            ver = SubscriptionService._os_version(m.group(1)) if m else ""
            return f"iPhone · iOS {ver}" if ver else "iPhone"
        if "android" in ua_lower:
            m = _UA_AND_VER_RE.search(ua)
            ver = SubscriptionService._os_version(m.group(1)) if m else ""
            return f"Android {ver}" if ver else "Android"
        if "windows" in ua_lower:
            m = _UA_WIN_VER_RE.search(ua)
            ver = _WIN_NT.get(m.group(1), "") if m else ""
            return f"Windows {ver}" if ver else "Windows"
        if "mac os x" in ua_lower or "macos" in ua_lower:
            m = _UA_MAC_VER_RE.search(ua)
            ver = SubscriptionService._os_version(m.group(1), keep_minor=True) if m else ""
            return f"macOS {ver}" if ver else "macOS"
        if "linux" in ua_lower:
            return "Linux"
        return ""

    @staticmethod
    def make_device_display_name(ua: str) -> str:
        m = _UA_PRODUCT_RE.match(ua.strip())
        client = m.group(1) if m else ""
        platform = SubscriptionService._detect_platform(ua)
        if client and platform:
            return f"{platform} · {client}"
        if client:
            return client
        if platform:
            return platform
        return "Device"

    @staticmethod
    def extract_build(ua: str) -> str | None:
        """Client build number from the UA, if present.

        Many clients append a long numeric build as a slash segment, e.g. Happ's
        ``Happ/2.9.1/Android/17800541067281831514``. Return the first all-digit
        segment of 4+ digits (short tokens like an OS major version are ignored).
        """
        for part in ua.split("/"):
            part = part.strip()
            if part.isdigit() and len(part) >= 4:
                return part
        return None

    @staticmethod
    def fingerprint_ua(ua: str) -> str:
        normalized = _UA_VERSION_RE.sub("", ua)
        normalized = _UA_DIGITS_RE.sub("", normalized)
        normalized = " ".join(normalized.split())
        return hashlib.sha256(normalized.encode()).hexdigest()[:32]

    @staticmethod
    async def get_or_create_device(
        session: AsyncSession,
        sub: "Subscription",
        ua: str,
        client_ip: str | None = None,
    ) -> "Device":
        fingerprint = SubscriptionService.fingerprint_ua(ua)
        now = datetime.now(UTC).replace(tzinfo=None)

        result = await session.execute(
            select(Device).where(
                Device.subscription_id == sub.id,
                Device.ua_fingerprint == fingerprint,
                Device.is_active == True,  # noqa: E712
            )
        )
        device = result.scalar_one_or_none()

        if device is not None:
            # Re-derive name/OS/build from the current UA so records created by an
            # older, buggier parser self-heal on the next subscription fetch.
            new_name = SubscriptionService.make_device_display_name(ua)
            new_os = SubscriptionService._detect_platform(ua) or None
            new_build = SubscriptionService.extract_build(ua)
            if device.display_name != new_name:
                device.display_name = new_name
            if device.os_label != new_os:
                device.os_label = new_os
            if device.build_number != new_build:
                device.build_number = new_build
            if not device.is_suspended:
                device.last_active_at = now
            return device

        # New device: capture OS + build (from UA) and the approximate "added from"
        # location (GeoIP of the requesting IP, resolved once; the IP isn't stored).
        os_label = SubscriptionService._detect_platform(ua) or None
        build_number = SubscriptionService.extract_build(ua)
        added_location, added_country_code = geoip.lookup(client_ip)

        device = Device(
            subscription_id=sub.id,
            uuid=str(_uuid_mod.uuid4()),
            ua_fingerprint=fingerprint,
            display_name=SubscriptionService.make_device_display_name(ua),
            os_label=os_label,
            build_number=build_number,
            added_location=added_location,
            added_country_code=added_country_code,
            last_active_at=now,
            is_active=True,
        )
        session.add(device)
        await session.flush()

        await SubscriptionService._sync_device_to_servers(session, sub, device)
        return device

    @staticmethod
    async def _sync_device_to_servers(
        session: AsyncSession,
        sub: "Subscription",
        device: "Device",
    ) -> None:
        result = await session.execute(
            select(Server)
            .join(SubscriptionServer)
            .where(
                SubscriptionServer.subscription_id == sub.id,
                Server.is_active == True,  # noqa: E712
                or_(
                    SubscriptionServer.is_synced == True,  # noqa: E712
                    Server.control_mode.in_(("observe", "pull")),
                ),
            )
        )
        servers = result.scalars().all()
        email = f"user_{sub.user_id}_sub_{sub.id}_dev_{device.id}"

        async def _add(server: Server) -> None:
            if not NodeControlService.pushes_to(server):
                return
            try:
                await AgentClient(server.agent_url, server.agent_token).add_client(device.uuid, email)
            except Exception as exc:
                logger.error("device sync to server %s failed: %s", server.id, exc)

        await asyncio.gather(*(_add(s) for s in servers))
        await session.flush()
        await NodeControlService.publish_for_servers(
            session,
            {server.id for server in servers},
        )

    @staticmethod
    async def suspend_device(
        session: AsyncSession,
        sub: "Subscription",
        device: "Device",
    ) -> None:
        result = await session.execute(
            select(Server).join(SubscriptionServer).where(SubscriptionServer.subscription_id == sub.id)
        )
        servers = result.scalars().all()
        device.is_suspended = True
        await session.flush()

        async def _remove(server: Server) -> None:
            if not NodeControlService.pushes_to(server):
                return
            try:
                await AgentClient(server.agent_url, server.agent_token).remove_client(device.uuid)
            except Exception as exc:
                logger.error("device suspend on server %s failed: %s", server.id, exc)

        await asyncio.gather(*(_remove(s) for s in servers))
        await NodeControlService.publish_for_servers(
            session,
            {server.id for server in servers},
        )

    @staticmethod
    async def resume_device(
        session: AsyncSession,
        sub: "Subscription",
        device: "Device",
    ) -> None:
        device.is_suspended = False
        await session.flush()
        await SubscriptionService._sync_device_to_servers(session, sub, device)

    @staticmethod
    async def remove_device(
        session: AsyncSession,
        sub: "Subscription",
        device: "Device",
    ) -> None:
        result = await session.execute(
            select(Server).join(SubscriptionServer).where(SubscriptionServer.subscription_id == sub.id)
        )
        servers = result.scalars().all()
        device.is_active = False
        await session.flush()

        async def _remove(server: Server) -> None:
            if not NodeControlService.pushes_to(server):
                return
            try:
                await AgentClient(server.agent_url, server.agent_token).remove_client(device.uuid)
            except Exception as exc:
                logger.error("device remove from server %s failed: %s", server.id, exc)

        await asyncio.gather(*(_remove(s) for s in servers))
        await NodeControlService.publish_for_servers(
            session,
            {server.id for server in servers},
        )

    @staticmethod
    async def get_active_devices(session: AsyncSession, sub: "Subscription") -> list["Device"]:
        result = await session.execute(
            select(Device)
            .where(Device.subscription_id == sub.id, Device.is_active == True)  # noqa: E712
            .order_by(Device.id)
        )
        return list(result.scalars().all())

    @staticmethod
    async def reissue_subscription(
        session: AsyncSession,
        user_id: int,
        plan_days: int,
    ) -> tuple[str | None, str | None]:
        """Reissue a subscription for a user.

        Deactivates the old active subscription and creates a new one with the
        same client_uuid (so the agent on servers still knows the client).
        Returns (new_sub_token, new_client_uuid) or (None, error_key).
        """
        # Find the active subscription
        result = await session.execute(
            select(Subscription).where(
                Subscription.user_id == user_id,
                Subscription.is_active == True,
            )
        )
        old_sub = result.scalar_one_or_none()

        if not old_sub:
            return None, "no_active"

        # Deactivate old subscription
        old_sub.is_active = False
        await SubscriptionService.remove_subscription_from_servers(session, old_sub)

        # Create new subscription with the SAME client_uuid
        new_token = await SubscriptionService.generate_sub_token(session)
        new_sub = Subscription(
            user_id=user_id,
            sub_token=new_token,
            client_uuid=old_sub.client_uuid,  # same UUID → servers already know this client
            plan_days=plan_days,
            started_at=datetime.now(UTC).replace(tzinfo=None),
            expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=plan_days),
            is_active=True,
        )
        session.add(new_sub)
        await session.flush()

        # Sync to servers
        servers = await ServerAccessService.get_accessible_servers_for_user(session, user_id)
        await SubscriptionService.sync_subscription_to_servers(session, new_sub, servers)
        await session.commit()

        return new_token, old_sub.client_uuid
