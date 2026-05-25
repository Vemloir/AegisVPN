from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models import Server, ServerAccessGrant, Subscription, SubscriptionServer, User
from src.services.agent_client import AgentClient


class ServerAccessService:
    @staticmethod
    def _group_clause(subscription_group: str | None):
        if subscription_group in {"safe", "fast"}:
            return or_(
                Server.subscription_group == subscription_group,
                Server.subscription_group == "both",
            )
        return None

    @staticmethod
    async def get_accessible_servers_for_user(
        session: AsyncSession,
        user_id: int,
        subscription_group: str | None = None,
    ) -> list[Server]:
        stmt = (
            select(Server)
            .outerjoin(
                ServerAccessGrant,
                and_(
                    ServerAccessGrant.server_id == Server.id,
                    ServerAccessGrant.user_id == user_id,
                ),
            )
            .where(
                Server.is_active == True,
                or_(
                    Server.access_mode == "public",
                    ServerAccessGrant.user_id.is_not(None),
                ),
            )
        )
        group_clause = ServerAccessService._group_clause(subscription_group)
        if group_clause is not None:
            stmt = stmt.where(group_clause)
        result = await session.execute(stmt.order_by(Server.id))
        return result.scalars().all()

    @staticmethod
    async def set_server_access_mode(session: AsyncSession, server: Server, access_mode: str) -> None:
        server.access_mode = access_mode
        await session.flush()
        await ServerAccessService.reconcile_server_subscriptions(session, server.id)

    @staticmethod
    async def grant_user_access(session: AsyncSession, server: Server, user: User) -> bool:
        existing = await session.get(ServerAccessGrant, {"server_id": server.id, "user_id": user.id})
        if existing is not None:
            await ServerAccessService.reconcile_user_subscriptions(session, user.id)
            return False

        session.add(ServerAccessGrant(server_id=server.id, user_id=user.id))
        await session.flush()
        await ServerAccessService.reconcile_user_subscriptions(session, user.id)
        return True

    @staticmethod
    async def revoke_user_access(session: AsyncSession, server: Server, user: User) -> bool:
        grant = await session.get(ServerAccessGrant, {"server_id": server.id, "user_id": user.id})
        if grant is None:
            await ServerAccessService.reconcile_user_subscriptions(session, user.id)
            return False

        await session.delete(grant)
        await session.flush()
        await ServerAccessService.reconcile_user_subscriptions(session, user.id)
        return True

    @staticmethod
    async def reconcile_user_subscriptions(session: AsyncSession, user_id: int) -> None:
        result = await session.execute(
            select(Subscription).where(
                Subscription.user_id == user_id,
                Subscription.is_active == True,
            )
        )
        subscriptions = result.scalars().all()
        for subscription in subscriptions:
            await ServerAccessService.reconcile_subscription_servers(session, subscription)

    @staticmethod
    async def reconcile_server_subscriptions(session: AsyncSession, server_id: int) -> None:
        result = await session.execute(
            select(Subscription)
            .join(User, User.id == Subscription.user_id)
            .where(Subscription.is_active == True)
        )
        subscriptions = result.scalars().all()
        for subscription in subscriptions:
            await ServerAccessService.reconcile_subscription_servers(session, subscription, target_server_id=server_id)

    @staticmethod
    async def reconcile_subscription_servers(
        session: AsyncSession,
        subscription: Subscription,
        target_server_id: int | None = None,
    ) -> None:
        allowed_servers = await ServerAccessService.get_accessible_servers_for_user(session, subscription.user_id)
        allowed_server_ids = {server.id for server in allowed_servers}
        if target_server_id is not None:
            allowed_servers = [server for server in allowed_servers if server.id == target_server_id]
            allowed_server_ids = {server.id for server in allowed_servers}

        current_links_result = await session.execute(
            select(SubscriptionServer)
            .options(selectinload(SubscriptionServer.server))
            .where(SubscriptionServer.subscription_id == subscription.id)
        )
        current_links = current_links_result.scalars().all()

        if target_server_id is not None:
            current_links = [link for link in current_links if link.server_id == target_server_id]

        current_server_ids = {link.server_id for link in current_links}

        allowed_to_add = [server for server in allowed_servers if server.id not in current_server_ids]
        if allowed_to_add:
            from src.services.subscription_service import SubscriptionService

            await SubscriptionService.sync_subscription_to_servers(session, subscription, allowed_to_add)

        for link in current_links:
            if link.server_id in allowed_server_ids:
                continue

            shared_agent_still_allowed = any(
                server.agent_url == link.server.agent_url
                and server.agent_token == link.server.agent_token
                for server in allowed_servers
            )

            if link.is_synced and not shared_agent_still_allowed:
                client = AgentClient(link.server.agent_url, link.server.agent_token)
                try:
                    await client.remove_client(subscription.client_uuid)
                except Exception:
                    pass
            await session.execute(
                delete(SubscriptionServer).where(
                    SubscriptionServer.subscription_id == subscription.id,
                    SubscriptionServer.server_id == link.server_id,
                )
            )
