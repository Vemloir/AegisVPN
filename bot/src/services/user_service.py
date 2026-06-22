"""Business logic for the end-user flow (registration, privacy, trial, account).

Pure data access and state mutations — no aiogram / presentation concerns.
The user handlers translate the returned values into localized Telegram UI.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from src.core.database import async_session_maker
from src.core.logger import logger
from src.core.terms import TERMS_VERSION
from src.models import Subscription, User
from src.services.server_access_service import ServerAccessService
from src.services.subscription_service import SubscriptionService

TRIAL_DAYS = 3


def pick_language(code: str | None) -> str:
    """Map a Telegram language_code to a supported UI language."""
    return "en" if (code or "").lower().startswith("en") else "ru"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class UserService:
    @staticmethod
    async def is_privacy_accepted(tg_id: int) -> bool:
        async with async_session_maker() as session:
            result = await session.execute(select(User.privacy_accepted).where(User.tg_id == tg_id))
            return bool(result.scalar_one_or_none())

    @staticmethod
    async def is_terms_accepted(tg_id: int) -> bool:
        """True only if the user accepted the CURRENT TERMS_VERSION.

        Unknown users (no row yet) and users on an older version count as not
        accepted, so a version bump re-prompts everyone via the gate.
        """
        async with async_session_maker() as session:
            result = await session.execute(select(User.accepted_terms_version).where(User.tg_id == tg_id))
            return result.scalar_one_or_none() == TERMS_VERSION

    @staticmethod
    async def accept_terms(
        tg_id: int, username: str | None = None, language_code: str | None = None
    ) -> tuple[str, bool]:
        """Record acceptance of the current Privacy Policy + ToS version.

        Creates the user row if it does not exist yet (the gate may fire before
        any /start handler ran). Returns ``(language, can_use_trial)``.
        """
        async with async_session_maker() as session:
            user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
            if user is None:
                user = User(
                    tg_id=tg_id,
                    username=username,
                    referrer_id=None,
                    language=pick_language(language_code),
                )
                session.add(user)
                await session.flush()
            # Keep the legacy flag in sync for any code still reading it.
            user.privacy_accepted = True
            user.accepted_terms_version = TERMS_VERSION
            user.accepted_terms_at = _now()
            language = user.language
            can_use_trial = not user.trial_used
            await session.commit()
            return language, can_use_trial

    @staticmethod
    async def subscription_state(tg_id: int) -> tuple[bool, bool]:
        """Returns (has_active_subscription, is_lifetime)."""
        async with async_session_maker() as session:
            user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
            if not user or user.is_banned:
                return False, False

            sub = (
                await session.execute(
                    select(Subscription).where(
                        Subscription.user_id == user.id,
                        Subscription.is_active == True,  # noqa: E712
                        Subscription.expires_at > _now(),
                    )
                )
            ).scalar_one_or_none()
            if not sub:
                return False, False
            return True, SubscriptionService.is_lifetime_subscription(sub)

    @staticmethod
    async def has_active_subscription(tg_id: int) -> bool:
        active, _ = await UserService.subscription_state(tg_id)
        return active

    @staticmethod
    async def register_or_update_on_start(
        tg_id: int,
        username: str | None,
        language_code: str | None,
        referrer_id: int | None,
    ) -> tuple[str, bool, bool, bool]:
        """Create the user on first /start (or refresh username on return).

        Returns ``(language, can_use_trial, terms_ok, is_banned)`` where
        ``terms_ok`` is True only if the stored acceptance matches the current
        TERMS_VERSION.
        """
        async with async_session_maker() as session:
            user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()

            if not user:
                if referrer_id == tg_id:
                    referrer_id = None
                user = User(
                    tg_id=tg_id,
                    username=username,
                    referrer_id=referrer_id,
                    language=pick_language(language_code),
                )
                session.add(user)
            else:
                user.username = username

            await session.commit()
            terms_ok = user.accepted_terms_version == TERMS_VERSION
            return user.language, not user.trial_used, terms_ok, user.is_banned

    @staticmethod
    async def ensure_user(tg_id: int, username: str | None, language_code: str | None) -> None:
        async with async_session_maker() as session:
            user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
            if user is None:
                session.add(
                    User(
                        tg_id=tg_id,
                        username=username,
                        referrer_id=None,
                        language=pick_language(language_code),
                    )
                )
                await session.commit()

    @staticmethod
    async def accept_privacy(tg_id: int) -> tuple[str, bool] | None:
        """Mark the policy accepted. Returns ``(language, can_use_trial)``."""
        async with async_session_maker() as session:
            user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
            if user is None:
                return None
            user.privacy_accepted = True
            language = user.language
            can_use_trial = not user.trial_used
            await session.commit()
            return language, can_use_trial

    @staticmethod
    async def delete_account(tg_id: int) -> str | None:
        """Revoke access on all nodes for every subscription, then delete the
        user (subscriptions cascade with the row). Returns the user's language."""
        async with async_session_maker() as session:
            user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
            if user is None:
                return None
            language = user.language
            subs = (await session.execute(select(Subscription).where(Subscription.user_id == user.id))).scalars().all()
            for sub in subs:
                await SubscriptionService.remove_subscription_from_servers(session, sub)
            await session.delete(user)
            await session.commit()
            return language

    @staticmethod
    async def reissue_subscription(tg_id: int) -> tuple[str, str, str | None]:
        """Rotate sub_token + client_uuid so old URL and VLESS credentials stop working.

        Returns (language, status, new_sub_token) where status is one of:
        no_user, no_active, failed, done.
        """
        async with async_session_maker() as session:
            user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
            if user is None:
                return "ru", "no_user", None
            language = user.language
            if user.is_banned:
                return language, "no_active", None
            now = _now()
            sub = (
                await session.execute(
                    select(Subscription).where(
                        Subscription.user_id == user.id,
                        Subscription.is_active == True,  # noqa: E712
                        Subscription.expires_at > now,
                    )
                )
            ).scalar_one_or_none()
            if sub is None:
                return language, "no_active", None
            try:
                await SubscriptionService.remove_subscription_from_servers(session, sub)
                sub.sub_token = await SubscriptionService.generate_sub_token(session)
                sub.client_uuid = str(uuid.uuid4())
                await session.flush()
                active_servers = await ServerAccessService.get_accessible_servers_for_user(session, user.id)
                await SubscriptionService.sync_subscription_to_servers(session, sub, active_servers)
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logger.error("Failed to reissue subscription for %s: %s", tg_id, exc)
                return language, "failed", None
            return language, "done", sub.sub_token

    @staticmethod
    async def activate_trial(tg_id: int, username: str | None, language_code: str | None) -> tuple[str, str]:
        """Activate the free trial. Returns ``(language, status)`` where status is
        one of: banned, already_used, active_exists, failed, started."""
        async with async_session_maker() as session:
            user = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
            if user is None:
                user = User(
                    tg_id=tg_id,
                    username=username,
                    referrer_id=None,
                    language=pick_language(language_code),
                )
                session.add(user)
                await session.flush()
            else:
                user.username = username

            language = user.language
            if user.is_banned:
                return language, "banned"
            if user.trial_used:
                return language, "already_used"

            now = _now()
            active_sub = (
                await session.execute(
                    select(Subscription).where(
                        Subscription.user_id == user.id,
                        Subscription.is_active == True,  # noqa: E712
                        Subscription.expires_at > now,
                    )
                )
            ).scalar_one_or_none()
            if active_sub is not None:
                return language, "active_exists"

            # Reuse the user's latest subscription row so the URL is stable.
            sub = await SubscriptionService.find_latest_subscription(session, user.id)
            if sub is not None:
                sub.is_active = True
                sub.started_at = now
                sub.expires_at = now + timedelta(days=TRIAL_DAYS)
                sub.plan_days = TRIAL_DAYS
                if not sub.sub_token:
                    sub.sub_token = await SubscriptionService.generate_sub_token(session)
                if not sub.client_uuid:
                    sub.client_uuid = str(uuid.uuid4())
            else:
                sub = Subscription(
                    user_id=user.id,
                    sub_token=await SubscriptionService.generate_sub_token(session),
                    client_uuid=str(uuid.uuid4()),
                    plan_days=TRIAL_DAYS,
                    started_at=now,
                    expires_at=now + timedelta(days=TRIAL_DAYS),
                    is_active=True,
                )
                session.add(sub)
            user.trial_used = True

            try:
                await session.flush()
                active_servers = await ServerAccessService.get_accessible_servers_for_user(session, user.id)
                await SubscriptionService.sync_subscription_to_servers(session, sub, active_servers)
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logger.error("Failed to activate trial for %s: %s", tg_id, exc)
                return language, "failed"

            return language, "started"
