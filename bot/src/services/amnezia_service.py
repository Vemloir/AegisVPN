import base64
from datetime import UTC, datetime

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models import Server, Subscription, SubscriptionServer
from src.services.server_access_service import ServerAccessService


class AmneziaService:
    @staticmethod
    def is_enabled() -> bool:
        return bool(settings.amnezia_enabled)

    @staticmethod
    def _encode_key(raw: bytes) -> str:
        return base64.b64encode(raw).decode("ascii")

    @staticmethod
    def _generate_keypair() -> tuple[str, str]:
        private_key = X25519PrivateKey.generate()
        private_raw = private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_raw = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return AmneziaService._encode_key(private_raw), AmneziaService._encode_key(public_raw)

    @staticmethod
    def _allocate_ipv4(subscription_id: int, server_id: int) -> str:
        index = max(((server_id - 1) * 4096) + subscription_id, 1) - 1
        third_octet = (index // 253) % 256
        fourth_octet = (index % 253) + 2
        return f"10.244.{third_octet}.{fourth_octet}"

    @staticmethod
    def format_server_name(server: Server) -> str:
        if server.amnezia_name and server.amnezia_name.strip():
            return server.amnezia_name.strip()
        base = server.name.strip() or f"Server {server.id}"
        return f"{base} WG"

    @staticmethod
    async def _get_or_create_link(session: AsyncSession, sub: Subscription, server: Server) -> SubscriptionServer:
        result = await session.execute(
            select(SubscriptionServer).where(
                SubscriptionServer.subscription_id == sub.id,
                SubscriptionServer.server_id == server.id,
            )
        )
        link = result.scalar_one_or_none()
        if link is None:
            link = SubscriptionServer(subscription_id=sub.id, server_id=server.id, is_synced=True)
            session.add(link)
            await session.flush()
        return link

    @staticmethod
    async def ensure_peer(session: AsyncSession, sub: Subscription, server: Server) -> tuple[SubscriptionServer, bool]:
        link = await AmneziaService._get_or_create_link(session, sub, server)
        changed = False
        if not link.amnezia_private_key or not link.amnezia_public_key:
            private_key, public_key = AmneziaService._generate_keypair()
            link.amnezia_private_key = private_key
            link.amnezia_public_key = public_key
            changed = True
        if not link.amnezia_ipv4:
            link.amnezia_ipv4 = AmneziaService._allocate_ipv4(sub.id, server.id)
            changed = True
        if changed:
            await session.flush()
        return link, changed

    @staticmethod
    async def get_accessible_servers(session: AsyncSession, sub: Subscription) -> list[Server]:
        accessible = await ServerAccessService.get_accessible_servers_for_user(session, sub.user_id)
        return [
            server
            for server in accessible
            if server.amnezia_enabled
            and server.is_active
            and server.amnezia_endpoint_host
            and server.amnezia_public_key
            and server.amnezia_port
        ]

    @staticmethod
    def render_client_config(link: SubscriptionServer, server: Server) -> str:
        if not link.amnezia_private_key or not link.amnezia_ipv4:
            raise ValueError("AmneziaVPN peer is not initialized")
        if not server.amnezia_endpoint_host or not server.amnezia_public_key or not server.amnezia_port:
            raise ValueError("AmneziaVPN server is not configured")

        lines = [
            f"# {AmneziaService.format_server_name(server)}",
            "[Interface]",
            f"PrivateKey = {link.amnezia_private_key}",
            f"Address = {link.amnezia_ipv4}/32",
            "DNS = 1.1.1.1, 1.0.0.1",
            "Jc = 5",
            "Jmin = 64",
            "Jmax = 256",
            "S1 = 32",
            "S2 = 64",
            "H1 = 12452345",
            "H2 = 24563456",
            "H3 = 35674567",
            "H4 = 46785678",
            "",
            "[Peer]",
            f"PublicKey = {server.amnezia_public_key}",
            "AllowedIPs = 0.0.0.0/0, ::/0",
            f"Endpoint = {server.amnezia_endpoint_host}:{server.amnezia_port}",
            "PersistentKeepalive = 25",
        ]
        return "\n".join(lines) + "\n"

    @staticmethod
    async def get_subscription_by_token(session: AsyncSession, token: str) -> Subscription | None:
        from src.services.subscription_service import SubscriptionService

        sub = await SubscriptionService.get_subscription_by_token(session, token)
        if not sub:
            return None

        now = datetime.now(UTC).replace(tzinfo=None)
        if not sub.is_active or sub.expires_at <= now:
            return None

        return sub

    @staticmethod
    async def get_server_by_id(session: AsyncSession, server_id: int) -> Server | None:
        result = await session.execute(select(Server).where(Server.id == server_id, Server.is_active == True))
        return result.scalar_one_or_none()
