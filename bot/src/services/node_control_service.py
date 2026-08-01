from urllib.parse import quote, urlencode

from sqlalchemy.ext.asyncio import AsyncSession

from src.control.state import publish_snapshot
from src.core.config import settings
from src.models import NodeTelemetry, Server


class NodeControlService:
    @staticmethod
    def pushes_to(server: Server) -> bool:
        return server.control_mode in {"push", "observe"}

    @staticmethod
    def publishes_for(server: Server) -> bool:
        return server.control_mode in {"observe", "pull"}

    @staticmethod
    async def publish_for_servers(
        session: AsyncSession,
        server_ids: set[int],
    ) -> None:
        for server_id in sorted(server_ids):
            server = await session.get(Server, server_id)
            if server is None or not NodeControlService.publishes_for(server):
                continue
            await publish_snapshot(
                session,
                server_id,
                page_size=settings.node_control_page_size,
            )

    @staticmethod
    async def telemetry_for(
        session: AsyncSession,
        server_id: int,
    ) -> dict | None:
        telemetry = await session.get(NodeTelemetry, server_id)
        if telemetry is None:
            return None
        return dict(telemetry.payload or {})

    @staticmethod
    async def build_subscription_uri(
        session: AsyncSession,
        server: Server,
        client_uuid: str,
        *,
        profile: str,
    ) -> str | None:
        telemetry = await NodeControlService.telemetry_for(session, server.id)
        templates = (telemetry or {}).get("subscription_templates")
        if not isinstance(templates, list):
            return None
        template = next(
            (item for item in templates if isinstance(item, dict) and item.get("profile") == profile),
            None,
        )
        if template is None:
            return None

        host = template.get("host")
        port = template.get("port")
        query = template.get("query")
        if (
            not isinstance(host, str)
            or not host
            or not isinstance(port, int)
            or not 1 <= port <= 65535
            or not isinstance(query, list)
        ):
            return None

        allowed_query_keys = {
            "type",
            "security",
            "encryption",
            "sni",
            "fp",
            "pbk",
            "sid",
            "spx",
            "path",
            "mode",
            "headerType",
            "flow",
            "packetEncoding",
            "serviceName",
            "host",
        }
        query_pairs: list[tuple[str, str]] = []
        for pair in query:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not all(isinstance(value, str) for value in pair)
                or pair[0] not in allowed_query_keys
            ):
                return None
            query_pairs.append((pair[0], pair[1]))

        return f"vless://{client_uuid}@{host}:{port}?{urlencode(query_pairs)}#{quote(host, safe='')}"
