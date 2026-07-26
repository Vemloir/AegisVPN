"""Buying a plan from the website.

This module only ever *starts* a payment. Confirming one — granting the
subscription, handling retries, chargebacks and reconciliation — already exists
and is deliberately untouched:

* СБП runs through Platega and is confirmed by the bot's callback route
  (``/payment/platega/callback``), keyed on the transaction id we store here.
* Stars are physically only payable inside Telegram; ``createInvoiceLink``
  hands us a link to a pre-filled invoice, and the bot's ``successful_payment``
  handler grants the subscription to the tg_id carried in the invoice payload.

So a website purchase converges onto exactly the same code path as a purchase
made in the bot. Anything the site invents here would be a second, weaker copy.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

import aiohttp
from fastapi import APIRouter, Cookie, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import read_session
from src.core.config import settings
from src.core.database import async_session_maker
from src.core.logger import logger
from src.core.terms import TERMS_VERSION
from src.models.payment import Payment
from src.models.plan import Plan
from src.models.subscription import Subscription
from src.models.user import User
from src.services.platega_client import PlategaError, create_sbp_transaction

router = APIRouter()

_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=5)
_PROVIDER_REDIRECT_HOSTS = {
    "stars": frozenset({"t.me"}),
    "sbp": frozenset({"app.platega.io"}),
}


class CheckoutRequest(BaseModel):
    plan_id: int
    method: str  # "sbp" | "stars"


def _site_url() -> str:
    """Where Platega sends the payer back. The site is the apex domain; www
    redirects to it (the Telegram widget only works on one registered domain)."""
    return (settings.site_public_url or "https://aegisvpn.org").rstrip("/")


def validate_provider_redirect(url: object, method: str) -> str | None:
    """Return a provider redirect only when it is an expected HTTPS origin."""
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    allowed_hosts = _PROVIDER_REDIRECT_HOSTS.get(method, frozenset())
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return None
    return url


def terms_accepted(user: User) -> bool:
    """Acceptance is version-pinned: bumping TERMS_VERSION re-prompts everyone,
    on the site exactly as it does in the bot."""
    return user.accepted_terms_version == TERMS_VERSION


async def _has_active_subscription(session: AsyncSession, user: User) -> bool:
    now = datetime.now(UTC).replace(tzinfo=None)
    sub = (
        await session.execute(
            select(Subscription)
            .where(
                Subscription.user_id == user.id,
                Subscription.is_active == True,  # noqa: E712
                Subscription.expires_at > now,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return sub is not None


async def create_stars_invoice_link(*, title: str, description: str, payload: str, stars: int) -> str:
    """A t.me link to a pre-filled Stars invoice.

    Stars can only be charged inside Telegram, so the site cannot take this
    payment itself — the best it can do is drop the user straight into the
    payment sheet instead of making them hunt through the bot's menus.
    """
    token = settings.bot_token.get_secret_value()
    body = {
        "title": title,
        "description": description,
        "payload": payload,
        "currency": "XTR",
        "prices": [{"label": title, "amount": stars}],
    }
    url = f"https://api.telegram.org/bot{token}/createInvoiceLink"
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.post(url, json=body) as resp:
            data = await resp.json()
    if not data.get("ok") or not data.get("result"):
        raise RuntimeError(f"createInvoiceLink failed: {str(data)[:200]}")
    return data["result"]


@router.post("/api/terms/accept")
async def accept_terms(response: Response, aegis_session: str | None = Cookie(default=None)) -> dict | None:
    """Record acceptance of the current legal documents.

    Writes the same three fields the bot's gate writes, so acceptance made on
    the site is acceptance everywhere — the user is never asked twice.
    """
    response.headers["Cache-Control"] = "no-store"
    user_id = read_session(aegis_session)
    if user_id is None:
        response.status_code = 401
        return None

    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user is None or user.is_banned:
            response.status_code = 401
            return None
        user.privacy_accepted = True
        user.accepted_terms_version = TERMS_VERSION
        user.accepted_terms_at = datetime.now(UTC).replace(tzinfo=None)
        await session.commit()
    return {"ok": True}


@router.post("/api/checkout")
async def checkout(
    body: CheckoutRequest,
    response: Response,
    aegis_session: str | None = Cookie(default=None),
) -> dict | None:
    """Start a payment and return the URL the browser should go to."""
    response.headers["Cache-Control"] = "no-store"
    user_id = read_session(aegis_session)
    if user_id is None:
        response.status_code = 401
        return {"error": "auth_required"}

    if body.method not in ("sbp", "stars"):
        response.status_code = 400
        return {"error": "bad_method"}

    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user is None or user.is_banned:
            response.status_code = 401
            return {"error": "auth_required"}

        # Same gate as the bot: no payment without accepted terms.
        if not terms_accepted(user):
            response.status_code = 409
            return {"error": "terms_required"}

        plan = await session.get(Plan, body.plan_id)
        if plan is None or not plan.is_active:
            response.status_code = 404
            return {"error": "plan_unavailable"}

        renewing = await _has_active_subscription(session, user)

        if body.method == "stars":
            if not plan.stars_price:
                response.status_code = 404
                return {"error": "plan_unavailable"}
            # The payload is what the bot's successful_payment handler reads to
            # decide WHO gets the subscription and WHICH plan — its format is
            # part of that contract, not a local detail.
            prefix = "renew_plan" if renewing else "buy_plan"
            payload = f"{prefix}_{plan.id}_{user.tg_id}"
            title = f"AegisVPN — {plan.days} дней"
            try:
                link = await create_stars_invoice_link(
                    title=title,
                    description=f"Подписка AegisVPN на {plan.days} дней",
                    payload=payload,
                    stars=plan.stars_price,
                )
            except Exception as exc:  # noqa: BLE001 - upstream failure, not ours
                logger.error(f"Stars invoice link failed (user {user.id}, plan {plan.id}): {exc}")
                response.status_code = 502
                return {"error": "provider_error"}
            link = validate_provider_redirect(link, "stars")
            if link is None:
                logger.error(f"Stars returned an untrusted redirect (user {user.id}, plan {plan.id})")
                response.status_code = 502
                return {"error": "provider_error"}
            return {"url": link, "method": "stars"}

        # СБП
        if not settings.platega_enabled or not plan.rub_price:
            response.status_code = 404
            return {"error": "plan_unavailable"}

        site = _site_url()
        try:
            resp = await create_sbp_transaction(
                amount_rub=plan.rub_price,
                description=f"Подписка AegisVPN на {plan.days} дней",
                payload=f"sbp_{plan.id}_{user.tg_id}",
                user_id=user.tg_id,
                username=user.username,
                return_url=f"{site}/?paid=1",
                failed_url=f"{site}/?paid=0",
            )
        except PlategaError as exc:
            logger.error(f"Platega create failed on site (user {user.id}, plan {plan.id}): {exc}")
            response.status_code = 502
            return {"error": "provider_error"}

        tx_id = resp.get("transactionId")
        redirect = validate_provider_redirect(resp.get("redirect"), "sbp")
        if not tx_id or not redirect:
            logger.error("Platega create returned no transaction id or a rejected redirect")
            response.status_code = 502
            return {"error": "provider_error"}

        # The pending row IS the idempotency key the callback looks the payment
        # up by; without it a confirmed transaction has nothing to grant against.
        session.add(
            Payment(
                user_id=user.id,
                tg_payment_id=f"platega_{tx_id}",
                stars_amount=0,
                plan_days=plan.days,
                provider="platega",
                rub_amount=plan.rub_price,
                status="pending",
            )
        )
        await session.commit()

    return {"url": redirect, "method": "sbp"}
