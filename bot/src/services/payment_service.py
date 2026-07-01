"""Subscription activation shared by every paid path (Telegram Stars + Platega).

Keeping the find-active / restore-expired / create-new + sync-to-nodes dance in
one place means a Stars charge and a Platega СБП callback grant access
identically. Callers own the session transaction (add their provider-specific
Payment row, then commit).
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from aiogram import Bot, html
from sqlalchemy import select, update

from src.core.logger import logger
from src.models import Payment, Subscription, User
from src.services.i18n import t
from src.services.server_access_service import ServerAccessService
from src.services.subscription_service import SubscriptionService

# Per-user grant lock. The bot is a single process on one event loop, so two
# grants for the same user can only interleave at await points; serialising them
# here prevents a read-modify-write lost update (two payments confirming at once
# each reading the same expires_at and one overwriting the other) and a
# double-insert of a fresh subscription. Keyed by user DB id so Stars and Platega
# share the same lock for a given user.
_USER_GRANT_LOCKS: dict[int, asyncio.Lock] = {}


def user_grant_lock(user_id: int) -> asyncio.Lock:
    lock = _USER_GRANT_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _USER_GRANT_LOCKS[user_id] = lock
    return lock


async def apply_paid_subscription(session, user: User, plan_days: int) -> tuple[str, bool]:
    """Activate/renew/restore the user's subscription by ``plan_days`` and sync it
    to the accessible nodes. Returns ``(sub_token, is_renewal)``. Does NOT commit —
    the caller flushes/commits after recording its Payment row.
    """
    now = datetime.now(UTC).replace(tzinfo=None)

    active_sub = (
        await session.execute(
            select(Subscription).where(
                Subscription.user_id == user.id,
                Subscription.is_active == True,  # noqa: E712
            )
        )
    ).scalar_one_or_none()

    if active_sub:
        # Renew the live subscription (same URL/token).
        active_sub.expires_at = max(active_sub.expires_at, now) + timedelta(days=plan_days)
        sub_token = active_sub.sub_token
        is_renewal = True
    else:
        # No active sub: restore the most recent expired one (keeps URL/token) or
        # create a fresh one.
        expired_sub = (
            await session.execute(
                select(Subscription)
                .where(Subscription.user_id == user.id)
                .order_by(Subscription.expires_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        if expired_sub and expired_sub.expires_at < now:
            expired_sub.is_active = True
            expired_sub.plan_days = plan_days
            expired_sub.expires_at = now + timedelta(days=plan_days)
            active_sub = expired_sub
            sub_token = expired_sub.sub_token
            is_renewal = True
        else:
            sub_token = await SubscriptionService.generate_sub_token(session)
            active_sub = Subscription(
                user_id=user.id,
                sub_token=sub_token,
                client_uuid=str(uuid.uuid4()),
                plan_days=plan_days,
                started_at=now,
                expires_at=now + timedelta(days=plan_days),
                is_active=True,
            )
            session.add(active_sub)
            is_renewal = False

    await session.flush()
    servers = await ServerAccessService.get_accessible_servers_for_user(session, user.id)
    await SubscriptionService.sync_subscription_to_servers(session, active_sub, servers)
    return sub_token, is_renewal


async def confirm_platega_payment(session, payment, bot: Bot | None = None) -> bool:
    """Idempotently confirm a Platega payment: activate the subscription, mark the
    row confirmed, and (if ``bot`` given) DM the user their link. Returns True if
    this call performed the activation, False if it was already confirmed.
    """
    # Atomic claim: only ONE caller flips pending->confirmed. This is what stops a
    # "Проверить оплату" tap and the async webhook (which can arrive together) from
    # both granting/extending the subscription. rowcount 0 => already claimed.
    claimed = await session.execute(
        update(Payment).where(Payment.id == payment.id, Payment.status != "confirmed").values(status="confirmed")
    )
    if claimed.rowcount == 0:
        await session.rollback()
        return False

    user = await session.get(User, payment.user_id)
    if user is None:
        logger.error(f"Platega payment {payment.tg_payment_id} references missing user {payment.user_id}")
        await session.commit()  # keep it confirmed; nothing to grant, don't reprocess
        return False

    async with user_grant_lock(payment.user_id):
        sub_token, is_renewal = await apply_paid_subscription(session, user, payment.plan_days)
        await session.commit()

    if bot is not None:
        language = user.language
        link = html.code(SubscriptionService.build_subscription_url(sub_token))
        action_key = "payment_action_renewed" if is_renewal else "payment_action_activated"
        text = t(language, "payment_success", days=payment.plan_days, action_text=t(language, action_key), link=link)
        try:
            await bot.send_message(user.tg_id, text, parse_mode="HTML")
        except Exception as exc:  # noqa: BLE001 — never let a DM failure lose the payment
            logger.error(f"Failed to DM user {user.tg_id} after Platega confirm: {exc}")
    return True
