"""Business logic for the admin panel.

Pure data access and state mutations — no aiogram / presentation concerns.
Handlers in ``src.handlers.admin`` call into this layer and only build the
Telegram UI (text + keyboards) on top of the returned values.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from src.core.database import async_session_maker
from src.models import Payment, Subscription, User
from src.services.server_access_service import ServerAccessService
from src.services.subscription_service import SubscriptionService


@dataclass(slots=True)
class AdminStats:
    users: int
    active_subscriptions: int
    banned_users: int
    revenue_stars: int


class AdminService:
    @staticmethod
    async def get_stats() -> AdminStats:
        async with async_session_maker() as session:
            users_count = await session.scalar(select(func.count(User.id))) or 0
            active_subs = (
                await session.scalar(
                    select(func.count(Subscription.id)).where(Subscription.is_active == True)  # noqa: E712
                )
                or 0
            )
            banned_users = (
                await session.scalar(
                    select(func.count(User.id)).where(User.is_banned == True)  # noqa: E712
                )
                or 0
            )
            revenue = await session.scalar(select(func.sum(Payment.stars_amount))) or 0
        return AdminStats(users_count, active_subs, banned_users, revenue)

    @staticmethod
    async def count_active_non_lifetime_subscriptions() -> int:
        async with async_session_maker() as session:
            now = datetime.now(UTC).replace(tzinfo=None)
            result = await session.execute(
                select(Subscription).where(
                    Subscription.is_active == True,  # noqa: E712
                    Subscription.expires_at > now,
                )
            )
            subscriptions = result.scalars().all()
            return sum(1 for sub in subscriptions if not SubscriptionService.is_lifetime_subscription(sub))

    @staticmethod
    async def extend_active_non_lifetime_subscriptions(days: int) -> int:
        if days <= 0:
            return 0
        async with async_session_maker() as session:
            now = datetime.now(UTC).replace(tzinfo=None)
            result = await session.execute(
                select(Subscription).where(
                    Subscription.is_active == True,  # noqa: E712
                    Subscription.expires_at > now,
                )
            )
            subscriptions = result.scalars().all()

            updated = 0
            for sub in subscriptions:
                if SubscriptionService.is_lifetime_subscription(sub):
                    continue
                sub.expires_at = max(sub.expires_at, now) + timedelta(days=days)
                sub.plan_days = days
                updated += 1

            await session.commit()
            return updated

    @staticmethod
    async def resolve_user_tg_id(query: str) -> int | None:
        """Resolve an admin lookup query (numeric Telegram ID or @username) to a
        tg_id. Username match is case-insensitive and ignores a leading '@'."""
        query = query.strip()
        if not query:
            return None
        if query.lstrip("-").isdigit():
            return int(query)
        uname = query.lstrip("@").lower()
        async with async_session_maker() as session:
            result = await session.execute(select(User.tg_id).where(func.lower(User.username) == uname))
            return result.scalar_one_or_none()

    @staticmethod
    async def grant_subscription(tg_id: int, days: int) -> Subscription | None:
        async with async_session_maker() as session:
            user_result = await session.execute(select(User).where(User.tg_id == tg_id))
            user = user_result.scalar_one_or_none()
            if user is None or days <= 0:
                return None

            now = datetime.now(UTC).replace(tzinfo=None)
            sub = await SubscriptionService.find_latest_subscription(session, user.id)

            if sub and sub.is_active and sub.expires_at > now:
                sub.expires_at = max(sub.expires_at, now) + timedelta(days=days)
                sub.plan_days = days
            elif sub:
                sub.is_active = True
                sub.started_at = now
                sub.expires_at = now + timedelta(days=days)
                sub.plan_days = days
                if not sub.sub_token:
                    sub.sub_token = await SubscriptionService.generate_sub_token(session)
                if not sub.client_uuid:
                    sub.client_uuid = str(uuid.uuid4())
            else:
                sub = Subscription(
                    user_id=user.id,
                    sub_token=await SubscriptionService.generate_sub_token(session),
                    client_uuid=str(uuid.uuid4()),
                    plan_days=days,
                    started_at=now,
                    expires_at=now + timedelta(days=days),
                    is_active=True,
                )
                session.add(sub)

            await session.flush()
            servers = await ServerAccessService.get_accessible_servers_for_user(session, user.id)
            await SubscriptionService.sync_subscription_to_servers(session, sub, servers)
            await session.commit()
            await session.refresh(sub)
            return sub

    @staticmethod
    async def grant_lifetime_subscription(tg_id: int) -> Subscription | None:
        async with async_session_maker() as session:
            user_result = await session.execute(select(User).where(User.tg_id == tg_id))
            user = user_result.scalar_one_or_none()
            if user is None:
                return None

            now = datetime.now(UTC).replace(tzinfo=None)
            sub = await SubscriptionService.find_latest_subscription(session, user.id)

            if sub:
                sub.is_active = True
                if sub.expires_at <= now:
                    sub.started_at = now
                sub.expires_at = SubscriptionService.LIFETIME_EXPIRES_AT
                sub.plan_days = SubscriptionService.LIFETIME_PLAN_DAYS
                if not sub.sub_token:
                    sub.sub_token = await SubscriptionService.generate_sub_token(session)
                if not sub.client_uuid:
                    sub.client_uuid = str(uuid.uuid4())
            else:
                sub = Subscription(
                    user_id=user.id,
                    sub_token=await SubscriptionService.generate_sub_token(session),
                    client_uuid=str(uuid.uuid4()),
                    plan_days=SubscriptionService.LIFETIME_PLAN_DAYS,
                    started_at=now,
                    expires_at=SubscriptionService.LIFETIME_EXPIRES_AT,
                    is_active=True,
                )
                session.add(sub)

            await session.flush()
            servers = await ServerAccessService.get_accessible_servers_for_user(session, user.id)
            await SubscriptionService.sync_subscription_to_servers(session, sub, servers)
            await session.commit()
            await session.refresh(sub)
            return sub

    @staticmethod
    async def revoke_subscription(tg_id: int) -> bool:
        async with async_session_maker() as session:
            user_result = await session.execute(select(User).where(User.tg_id == tg_id))
            user = user_result.scalar_one_or_none()
            if user is None:
                return False

            sub_result = await session.execute(
                select(Subscription).where(
                    Subscription.user_id == user.id,
                    Subscription.is_active == True,  # noqa: E712
                )
            )
            sub = sub_result.scalar_one_or_none()
            if sub is None:
                return False

            sub.is_active = False
            sub.expires_at = datetime.now(UTC).replace(tzinfo=None)
            await SubscriptionService.remove_subscription_from_servers(session, sub)
            await session.commit()
            return True

    @staticmethod
    async def set_user_ban_status(tg_id: int, is_banned: bool) -> User | None:
        async with async_session_maker() as session:
            user_result = await session.execute(select(User).where(User.tg_id == tg_id))
            user = user_result.scalar_one_or_none()
            if user is None:
                return None

            user.is_banned = is_banned
            if is_banned:
                sub_result = await session.execute(
                    select(Subscription).where(
                        Subscription.user_id == user.id,
                        Subscription.is_active == True,  # noqa: E712
                    )
                )
                sub = sub_result.scalar_one_or_none()
                if sub:
                    sub.is_active = False
                    sub.expires_at = datetime.now(UTC).replace(tzinfo=None)
                    await SubscriptionService.remove_subscription_from_servers(session, sub)

            await session.commit()
            return user
