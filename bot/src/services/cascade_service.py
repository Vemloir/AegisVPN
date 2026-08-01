from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    CascadeRoute,
    CascadeRouteAck,
    CascadeRouteExit,
    NodeSnapshotPage,
    Server,
)

CASCADE_CAPABILITY = "cascade-v2"


def supports_cascade(server: Server) -> bool:
    capabilities = server.control_capabilities or {}
    return CASCADE_CAPABILITY in set(capabilities.get("features") or [])


def _role_allows(server: Server, role: str) -> bool:
    return server.node_role in {role, "both"}


async def _route_exits(
    session: AsyncSession,
    route_id: int,
) -> list[tuple[CascadeRouteExit, Server]]:
    rows = (
        await session.execute(
            select(CascadeRouteExit, Server)
            .join(Server, Server.id == CascadeRouteExit.exit_server_id)
            .where(
                CascadeRouteExit.route_id == route_id,
                CascadeRouteExit.enabled == True,  # noqa: E712
            )
            .order_by(CascadeRouteExit.position, CascadeRouteExit.exit_server_id)
        )
    ).all()
    return list(rows)


async def current_route_digest(
    session: AsyncSession,
    route_id: int,
) -> str:
    route = await session.get(CascadeRoute, route_id)
    if route is None:
        raise LookupError(f"cascade route {route_id} does not exist")
    exits = await _route_exits(session, route_id)
    payload = {
        "route_id": route.id,
        "revision": route.revision,
        "label": route.label,
        "entry_server_id": route.entry_server_id,
        "health_policy": route.health_policy or {},
        "transport_policy": route.transport_policy or {},
        "exits": [
            {
                "exit_server_id": route_exit.exit_server_id,
                "position": route_exit.position,
                "service_uuid": route_exit.service_uuid,
                "server_name": route_exit.server_name,
                "xhttp_path": route_exit.xhttp_path or "/",
                "host": exit_server.host,
                "port": exit_server.port,
                "public_key": exit_server.public_key,
                "short_id": exit_server.short_id,
            }
            for route_exit, exit_server in exits
        ],
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


async def build_cascade_items(
    session: AsyncSession,
    server: Server,
) -> list[dict]:
    if not supports_cascade(server):
        return []
    routes = (
        (
            await session.execute(
                select(CascadeRoute)
                .where(CascadeRoute.enabled == True)  # noqa: E712
                .order_by(CascadeRoute.id)
            )
        )
        .scalars()
        .all()
    )
    items: list[dict] = []
    for route in routes:
        entry = await session.get(Server, route.entry_server_id)
        exits = await _route_exits(session, route.id)
        config_digest = await current_route_digest(session, route.id)
        if (
            entry is None
            or not entry.is_active
            or not supports_cascade(entry)
            or not _role_allows(entry, "entry")
            or not exits
            or any(
                not exit_server.is_active or not supports_cascade(exit_server) or not _role_allows(exit_server, "exit")
                for _, exit_server in exits
            )
        ):
            continue

        if server.id == entry.id:
            inbound_tags = list((route.transport_policy or {}).get("inbound_tags") or ["vless-in"])
            items.append(
                {
                    "kind": "cascade_route",
                    "route_id": route.id,
                    "revision": route.revision,
                    "config_digest": config_digest,
                    "label": route.label,
                    "inbound_tags": inbound_tags,
                    "exits": [
                        {
                            "position": route_exit.position,
                            "host": exit_server.host,
                            "port": exit_server.port,
                            "uuid": route_exit.service_uuid,
                            "public_key": exit_server.public_key,
                            "short_id": exit_server.short_id,
                            "server_name": route_exit.server_name,
                            "xhttp_path": route_exit.xhttp_path or "/",
                        }
                        for route_exit, exit_server in exits
                    ],
                    "health_policy": route.health_policy or {},
                }
            )
        for route_exit, exit_server in exits:
            if server.id != exit_server.id:
                continue
            items.append(
                {
                    "kind": "cascade_service",
                    "route_id": route.id,
                    "revision": route.revision,
                    "config_digest": config_digest,
                    "uuid": route_exit.service_uuid,
                    "email": f"cascade_route_{route.id}_entry_{entry.id}",
                }
            )
    return items


async def record_cascade_ack(
    session: AsyncSession,
    *,
    server_id: int,
    generation: int,
) -> None:
    pages = (
        (
            await session.execute(
                select(NodeSnapshotPage).where(
                    NodeSnapshotPage.server_id == server_id,
                    NodeSnapshotPage.generation == generation,
                )
            )
        )
        .scalars()
        .all()
    )
    revisions: dict[int, tuple[int, str]] = {}
    for page in pages:
        if page.schema_version < 2:
            continue
        for item in page.items:
            if item.get("kind") in {"cascade_route", "cascade_service"}:
                revisions[int(item["route_id"])] = (
                    int(item["revision"]),
                    str(item["config_digest"]),
                )
    await session.execute(delete(CascadeRouteAck).where(CascadeRouteAck.server_id == server_id))
    session.add_all(
        CascadeRouteAck(
            route_id=route_id,
            server_id=server_id,
            revision=revision,
            config_digest=config_digest,
            generation=generation,
        )
        for route_id, (revision, config_digest) in revisions.items()
    )


async def advertisable_routes(
    session: AsyncSession,
    entry_server_ids: set[int],
) -> list[CascadeRoute]:
    if not entry_server_ids:
        return []
    routes = (
        (
            await session.execute(
                select(CascadeRoute)
                .where(
                    CascadeRoute.enabled == True,  # noqa: E712
                    CascadeRoute.entry_server_id.in_(entry_server_ids),
                )
                .order_by(CascadeRoute.id)
            )
        )
        .scalars()
        .all()
    )
    ready: list[CascadeRoute] = []
    by_entry: dict[int, list[CascadeRoute]] = defaultdict(list)
    for route in routes:
        by_entry[route.entry_server_id].append(route)
    for entry_routes in by_entry.values():
        # One client-facing inbound cannot distinguish two route labels using
        # the same user UUID. Suppress ambiguous routes instead of leaking
        # traffic through an unintended exit.
        if len(entry_routes) != 1:
            continue
        route = entry_routes[0]
        config_digest = await current_route_digest(session, route.id)
        entry = await session.get(Server, route.entry_server_id)
        exits = await _route_exits(session, route.id)
        participants = [entry, *(exit_server for _, exit_server in exits)]
        if (
            entry is None
            or not exits
            or any(
                participant is None or not participant.is_active or not supports_cascade(participant)
                for participant in participants
            )
        ):
            continue
        acks = (
            (
                await session.execute(
                    select(CascadeRouteAck).where(
                        CascadeRouteAck.route_id == route.id,
                        CascadeRouteAck.server_id.in_([participant.id for participant in participants]),
                    )
                )
            )
            .scalars()
            .all()
        )
        if {ack.server_id for ack in acks if ack.revision == route.revision and ack.config_digest == config_digest} == {
            participant.id for participant in participants
        }:
            ready.append(route)
    return ready
