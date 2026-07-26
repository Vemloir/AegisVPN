import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models import (
    NodeSnapshot,
    NodeSnapshotPage,
    Server,
    Subscription,
    SubscriptionServer,
)


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _expiry_ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def _item_sort_key(item: dict) -> tuple[str, str, str]:
    if item["kind"] == "client":
        return "client", str(item["email"]), str(item["uuid"])
    return "conn_limit", f"{int(item['user_id']):020d}", ""


async def build_desired_items(
    session: AsyncSession,
    server_id: int,
    *,
    now: datetime | None = None,
) -> list[dict]:
    current_time = now or datetime.now(UTC).replace(tzinfo=None)
    subscriptions = (
        (
            await session.execute(
                select(Subscription)
                .join(
                    SubscriptionServer,
                    SubscriptionServer.subscription_id == Subscription.id,
                )
                .options(
                    selectinload(Subscription.user),
                    selectinload(Subscription.devices),
                )
                .where(
                    SubscriptionServer.server_id == server_id,
                    Subscription.is_active == True,  # noqa: E712
                    Subscription.expires_at > current_time,
                )
            )
        )
        .scalars()
        .unique()
        .all()
    )

    items: list[dict] = []
    conn_limits: dict[int, int] = {}
    for subscription in subscriptions:
        email = f"user_{subscription.user_id}_sub_{subscription.id}"
        expire_ms = _expiry_ms(subscription.expires_at)
        items.append(
            {
                "kind": "client",
                "uuid": subscription.client_uuid,
                "email": email,
                "expire_ms": expire_ms,
            }
        )
        for device in subscription.devices:
            if not device.is_active or device.is_suspended:
                continue
            items.append(
                {
                    "kind": "client",
                    "uuid": device.uuid,
                    "email": f"{email}_dev_{device.id}",
                    "expire_ms": expire_ms,
                }
            )
        if subscription.user.conn_limit is not None:
            conn_limits[subscription.user_id] = max(
                0,
                int(subscription.user.conn_limit),
            )

    items.extend(
        {
            "kind": "conn_limit",
            "user_id": user_id,
            "limit": limit,
        }
        for user_id, limit in conn_limits.items()
    )
    items.sort(key=_item_sort_key)
    return items


async def publish_snapshot(
    session: AsyncSession,
    server_id: int,
    *,
    page_size: int,
) -> NodeSnapshot:
    if page_size < 1:
        raise ValueError("page_size must be positive")

    server = (
        await session.execute(
            select(Server).where(Server.id == server_id).with_for_update()
        )
    ).scalar_one()
    items = await build_desired_items(session, server_id)
    digest = hashlib.sha256(canonical_json(items)).hexdigest()
    latest = (
        await session.execute(
            select(NodeSnapshot)
            .where(NodeSnapshot.server_id == server_id)
            .order_by(NodeSnapshot.generation.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is not None and latest.digest == digest:
        return latest

    generation = max(
        int(server.desired_generation or 0),
        int(latest.generation if latest is not None else 0),
    ) + 1
    pages = [
        items[offset : offset + page_size]
        for offset in range(0, len(items), page_size)
    ]
    snapshot = NodeSnapshot(
        server_id=server_id,
        generation=generation,
        digest=digest,
        item_count=len(items),
        page_count=len(pages),
        page_size=page_size,
    )
    session.add(snapshot)
    await session.flush()
    for page_index, page_items in enumerate(pages):
        session.add(
            NodeSnapshotPage(
                server_id=server_id,
                generation=generation,
                page_index=page_index,
                page_digest=hashlib.sha256(canonical_json(page_items)).hexdigest(),
                items=page_items,
            )
        )
    server.desired_generation = generation
    await session.flush()
    return snapshot
