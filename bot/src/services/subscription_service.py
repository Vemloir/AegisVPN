import asyncio
import base64
import secrets
from collections import Counter
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.logger import logger
from src.models import Server, Subscription, SubscriptionServer
from src.services.agent_client import AgentClient


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
    def server_sort_key(server: Server) -> tuple[str, int, int]:
        name = server.name.strip() or f"server {server.id}"
        folded = name.casefold()
        is_exp = folded.endswith(" exp")
        base_name = folded[:-4] if is_exp else folded
        return base_name, 0 if is_exp else 1, server.id

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
        target_host = parts.hostname or server.host
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

        async def fetch_link(server: Server) -> str:
            client = AgentClient(server.agent_url, server.agent_token)
            target_profile = (
                SubscriptionService.FAST_PROFILE
                if server.subscription_group == SubscriptionService.FAST_PROFILE
                else SubscriptionService.SAFE_PROFILE
            )
            try:
                text = await client.get_subscription(sub.client_uuid, profile=target_profile)
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
