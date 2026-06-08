import asyncio
import base64
import hashlib
import re
import secrets
import uuid as _uuid_mod
from collections import Counter
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.logger import logger
from src.models import Device, Server, Subscription, SubscriptionServer
from src.services import geoip
from src.services.agent_client import AgentClient
from src.services.server_access_service import ServerAccessService

_UA_VERSION_RE = re.compile(r"/[\d.]+")
_UA_DIGITS_RE = re.compile(r"\b\d[\d.]*\b")
_UA_PRODUCT_RE = re.compile(r"^([A-Za-z0-9][\w\-.+]*)")
_UA_IOS_VER_RE = re.compile(r"(?:iPhone OS|iOS)\s+([\d_]+)", re.IGNORECASE)
_UA_AND_VER_RE = re.compile(r"Android[/ ]([\d.]+)", re.IGNORECASE)
_UA_WIN_VER_RE = re.compile(r"Windows NT\s+([\d.]+)", re.IGNORECASE)
_WIN_NT = {"10.0": "10/11", "6.3": "8.1", "6.2": "8", "6.1": "7"}
_UA_MAC_VER_RE = re.compile(r"Mac OS X[/ ]([\d_]+)", re.IGNORECASE)


class SubscriptionService:
    SAFE_PROFILE = "safe"
    FAST_PROFILE = "fast"
    LIFETIME_PLAN_DAYS = 0
    LIFETIME_EXPIRES_AT = datetime(2099, 12, 31, 23, 59, 59)

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
    def format_server_label(server: Server, duplicate_name_keys: set[str] | None = None) -> str:
        name = server.name.strip() or f"Server {server.id}"
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
    ) -> str:
        parts = urlsplit(raw_uri)
        userinfo, _, _ = parts.netloc.rpartition("@")
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        transport = (query.get("type") or "tcp").lower()
        is_xhttp = transport == "xhttp"
        query["type"] = "xhttp" if is_xhttp else "tcp"
        query["security"] = "reality"
        query["encryption"] = "none"
        query["fp"] = query.get("fp", "chrome")
        query["sni"] = query.get("sni", "")
        query["pbk"] = server.public_key
        query["sid"] = server.short_id
        query["spx"] = query.get("spx", "/")
        if is_xhttp:
            query.pop("headerType", None)
            query.pop("flow", None)
            query["path"] = query.get("path", "/")
            query["mode"] = query.get("mode", "packet-up")
            if not query.get("packetEncoding"):
                query.pop("packetEncoding", None)
            if not query.get("host"):
                query.pop("host", None)
        else:
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
        target_port = parts.port or server.port
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

        async def sync_to_server(server: Server) -> tuple[int, bool]:
            if getattr(server, "static_uri", None):
                return server.id, True
            client = AgentClient(server.agent_url, server.agent_token)

            try:
                success = await client.add_client(sub.client_uuid, email)
            except Exception as exc:
                logger.error(f"Failed to sync client {sub.client_uuid} to server {server.id}: {exc}")
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

    @staticmethod
    async def get_subscription_vless_links(
        session: AsyncSession,
        sub_token: str,
        profile: str | None = None,
        device_uuid: str | None = None,
    ) -> str:
        normalized_profile = SubscriptionService.normalize_profile(profile)
        include_all_profiles = profile != SubscriptionService.FAST_PROFILE
        sub = await SubscriptionService.get_subscription_by_token(session, sub_token)

        if not sub:
            return ""

        now = datetime.now(UTC).replace(tzinfo=None)
        if sub.expires_at <= now:
            sub.is_active = False
            await SubscriptionService.remove_subscription_from_servers(session, sub)
            await session.commit()
            return ""

        if include_all_profiles:
            server_filter = Server.is_active == True
        else:
            server_filter = and_(
                Server.is_active == True,
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

        servers = sorted(servers, key=SubscriptionService.server_sort_key)
        server_name_counts = Counter(server.name.strip().casefold() for server in servers if server.name.strip())
        duplicate_name_keys = {name for name, count in server_name_counts.items() if count > 1}

        # device_uuid is used for xray-backed servers; static-URI servers always
        # use sub.client_uuid (they have their own auth mechanism).
        effective_uuid = device_uuid or sub.client_uuid

        async def fetch_link(server: Server) -> str:
            # Static-URI servers (e.g. a standalone Hysteria2 node) aren't backed
            # by an agent — serve their ready-made URI verbatim, substituting
            # {uuid}/{host} and setting the flagged label as the #fragment.
            static_uri = getattr(server, "static_uri", None)
            if static_uri:
                uri = static_uri.replace("{uuid}", sub.client_uuid).replace("{host}", server.host)
                base = uri.split("#", 1)[0]
                label = SubscriptionService.format_server_label(server, duplicate_name_keys)
                return f"{base}#{quote(label, safe='')}"
            client = AgentClient(server.agent_url, server.agent_token)
            target_profile = (
                SubscriptionService.FAST_PROFILE
                if server.subscription_group == SubscriptionService.FAST_PROFILE
                else SubscriptionService.SAFE_PROFILE
            )
            try:
                text = await client.get_subscription(effective_uuid, profile=target_profile)
                if text:
                    return SubscriptionService.normalize_vless_uri(
                        text,
                        server,
                        duplicate_name_keys,
                        profile=target_profile,
                    )
            except Exception as exc:
                logger.error(f"Failed to fetch sub from server {server.id}: {exc}")
            return ""

        links = await asyncio.gather(*(fetch_link(server) for server in servers))
        valid_links = [link for link in links if link]
        full_content = "\n".join(valid_links)
        return base64.b64encode(full_content.encode("utf-8")).decode("utf-8")

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

        async def remove_from_server(server: Server) -> None:
            client = AgentClient(server.agent_url, server.agent_token)
            try:
                await client.remove_client(sub.client_uuid)
            except Exception as exc:
                logger.error(f"Failed to remove client {sub.client_uuid} from server {server.id}: {exc}")

        await asyncio.gather(*(remove_from_server(server) for server in servers))

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
                SubscriptionServer.is_synced == True,  # noqa: E712
                Server.is_active == True,  # noqa: E712
                Server.static_uri.is_(None),
            )
        )
        servers = result.scalars().all()
        email = f"user_{sub.user_id}_sub_{sub.id}_dev_{device.id}"

        async def _add(server: Server) -> None:
            try:
                await AgentClient(server.agent_url, server.agent_token).add_client(device.uuid, email)
            except Exception as exc:
                logger.error("device sync to server %s failed: %s", server.id, exc)

        await asyncio.gather(*(_add(s) for s in servers))

    @staticmethod
    async def suspend_device(
        session: AsyncSession,
        sub: "Subscription",
        device: "Device",
    ) -> None:
        result = await session.execute(
            select(Server)
            .join(SubscriptionServer)
            .where(SubscriptionServer.subscription_id == sub.id)
        )
        servers = result.scalars().all()

        async def _remove(server: Server) -> None:
            if getattr(server, "static_uri", None):
                return
            try:
                await AgentClient(server.agent_url, server.agent_token).remove_client(device.uuid)
            except Exception as exc:
                logger.error("device suspend on server %s failed: %s", server.id, exc)

        await asyncio.gather(*(_remove(s) for s in servers))
        device.is_suspended = True

    @staticmethod
    async def resume_device(
        session: AsyncSession,
        sub: "Subscription",
        device: "Device",
    ) -> None:
        await SubscriptionService._sync_device_to_servers(session, sub, device)
        device.is_suspended = False

    @staticmethod
    async def remove_device(
        session: AsyncSession,
        sub: "Subscription",
        device: "Device",
    ) -> None:
        result = await session.execute(
            select(Server)
            .join(SubscriptionServer)
            .where(SubscriptionServer.subscription_id == sub.id)
        )
        servers = result.scalars().all()

        async def _remove(server: Server) -> None:
            if getattr(server, "static_uri", None):
                return
            try:
                await AgentClient(server.agent_url, server.agent_token).remove_client(device.uuid)
            except Exception as exc:
                logger.error("device remove from server %s failed: %s", server.id, exc)

        await asyncio.gather(*(_remove(s) for s in servers))
        device.is_active = False

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
