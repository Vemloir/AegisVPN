"""The state-changing part of fetching a subscription in a client."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Subscription
from src.services.subscription_lifecycle import claim_expirations
from src.services.subscription_service import SubscriptionService


@dataclass(frozen=True)
class PreparedImport:
    subscription: Subscription
    device_uuid: str | None


async def prepare_subscription_import(
    session: AsyncSession, token: str, ua: str, client_ip: str | None, headers: Mapping[str, str]
) -> PreparedImport | None:
    sub = await SubscriptionService.get_subscription_by_token(session, token)
    if sub is None:
        return None
    now = datetime.now(UTC).replace(tzinfo=None)
    if sub.is_expired(now):
        if await claim_expirations(session, cutoff=now, subscription_id=sub.id):
            # Preserve immediate revocation on fetch, without enrolling another device.
            await session.refresh(sub)
            await SubscriptionService.remove_subscription_from_servers(session, sub)
            await session.commit()
            return None
        # Another request renewed, revoked, or deleted it after the initial read.
        # Reload rather than using the stale ORM identity or issuing an old removal.
        await session.rollback()
        sub = await SubscriptionService.get_subscription_by_token(session, token)
        if sub is None or sub.is_expired(now):
            return None
    if not ua:
        return PreparedImport(sub, None)

    device = await SubscriptionService.get_or_create_device(session, sub, ua, client_ip, headers=headers)
    await session.commit()
    if device.is_suspended:
        return None
    return PreparedImport(sub, device.uuid)
