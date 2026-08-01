import asyncio

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models import Device, Server, ServerAccessGrant, Subscription, SubscriptionServer, User
from src.services.agent_client import AgentClient
from src.services.node_control_service import NodeControlService


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
    async def set_server_active(session: AsyncSession, server: Server, is_active: bool) -> None:
        """Take a location online/offline. Disabling it (is_active=False) drops it
        from every user's accessible set: reconcile then strips the location from
        all subscriptions (and removes the client from the — possibly dead — agent,
        best-effort). Re-enabling adds it back. Reversible decommission for a node
        we stopped paying for, without deleting its row/traffic history."""
        server.is_active = is_active
        # Commit the flag BEFORE touching the node. A location is taken offline
        # precisely because its node is going away, so the node is usually already
        # unreachable — and an unreachable node must not be able to delay, let
        # alone veto, the operator's decision. Everything after this point is
        # cleanup that can fail harmlessly.
        await session.commit()
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
    async def _flush_removals(removals: set[tuple[str, str, str]]) -> None:
        """Strip clients from nodes, all at once and best-effort.

        Concurrent, because these calls are independent and one dead node used to
        serialise the backoff of every other. Best-effort, because a node we
        cannot reach has no client left to strip.
        """
        if not removals:
            return

        async def strip(agent_url: str, agent_token: str, client_uuid: str) -> None:
            await AgentClient(agent_url, agent_token).remove_client_best_effort(client_uuid)

        await asyncio.gather(*(strip(*removal) for removal in removals))

    @staticmethod
    async def reconcile_user_subscriptions(session: AsyncSession, user_id: int) -> None:
        result = await session.execute(
            select(Subscription).where(
                Subscription.user_id == user_id,
                Subscription.is_active == True,
            )
        )
        subscriptions = result.scalars().all()
        removals: set[tuple[str, str, str]] = set()
        publications: set[int] = set()
        for subscription in subscriptions:
            await ServerAccessService.reconcile_subscription_servers(
                session,
                subscription,
                removals=removals,
                publications=publications,
            )
        await NodeControlService.publish_for_servers(session, publications)
        await ServerAccessService._flush_removals(removals)

    @staticmethod
    async def reconcile_server_subscriptions(session: AsyncSession, server_id: int) -> None:
        result = await session.execute(
            select(Subscription).join(User, User.id == Subscription.user_id).where(Subscription.is_active == True)
        )
        subscriptions = result.scalars().all()
        removals: set[tuple[str, str, str]] = set()
        publications: set[int] = set()
        for subscription in subscriptions:
            await ServerAccessService.reconcile_subscription_servers(
                session,
                subscription,
                target_server_id=server_id,
                removals=removals,
                publications=publications,
            )
        await NodeControlService.publish_for_servers(session, publications)
        await ServerAccessService._flush_removals(removals)

    @staticmethod
    async def reconcile_subscription_servers(
        session: AsyncSession,
        subscription: Subscription,
        target_server_id: int | None = None,
        removals: set[tuple[str, str, str]] | None = None,
        publications: set[int] | None = None,
    ) -> None:
        """Bring one subscription's links in line with what its user may access.

        ``removals`` lets a caller looping over many subscriptions collect the
        agent calls and fire them together at the end (see :meth:`_flush_removals`)
        instead of paying a dead node's timeout once per subscription.
        """
        own_removals = removals is None
        if removals is None:
            removals = set()
        own_publications = publications is None
        if publications is None:
            publications = set()
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
                NodeControlService.pushes_to(server)
                and server.agent_url == link.server.agent_url
                and server.agent_token == link.server.agent_token
                for server in allowed_servers
            )

            if link.is_synced and NodeControlService.pushes_to(link.server) and not shared_agent_still_allowed:
                device_uuids = (
                    (await session.execute(select(Device.uuid).where(Device.subscription_id == subscription.id)))
                    .scalars()
                    .all()
                )
                for client_uuid in (
                    subscription.client_uuid,
                    *device_uuids,
                ):
                    removals.add(
                        (
                            link.server.agent_url,
                            link.server.agent_token,
                            client_uuid,
                        )
                    )
            if NodeControlService.publishes_for(link.server):
                publications.add(link.server_id)
            await session.execute(
                delete(SubscriptionServer).where(
                    SubscriptionServer.subscription_id == subscription.id,
                    SubscriptionServer.server_id == link.server_id,
                )
            )

        if own_publications:
            await NodeControlService.publish_for_servers(session, publications)
        if own_removals:
            await ServerAccessService._flush_removals(removals)
