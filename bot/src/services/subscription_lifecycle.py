"""Database transitions for a subscription, independent of HTTP and Telegram."""

from datetime import datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Subscription


async def claim_expirations(
    session: AsyncSession, *, cutoff: datetime, subscription_id: int | None = None
) -> list[int]:
    """Return only subscriptions this transaction actually deactivated.

    Checking the deadline in the UPDATE prevents a stale expiry scan/import
    from winning over a renewal that committed first. It also makes repeat
    expiry calls no-ops. NULL is lifetime, not due. The caller keeps this
    transition and pull desired-state publication in the same transaction.
    """
    stmt = update(Subscription).where(Subscription.is_active.is_(True), Subscription.expires_at <= cutoff)
    if subscription_id is not None:
        stmt = stmt.where(Subscription.id == subscription_id)
    result = await session.scalars(
        stmt.values(is_active=False).returning(Subscription.id).execution_options(synchronize_session=False)
    )
    return sorted(result.all())
