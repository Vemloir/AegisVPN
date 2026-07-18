"""Public HTTP API for the website.

It runs beside the bot, against the SAME database and the same models, so the
site cannot disagree with what the bot sells: plans, prices and locations are
read from the tables the admin panel writes. Nothing is duplicated here.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime

import aiohttp
from fastapi import Cookie, FastAPI, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api import checkout, legal
from src.api.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_SECONDS,
    issue_session,
    read_session,
    verify_telegram_login,
)
from src.api.checkout import terms_accepted
from src.core.config import settings
from src.core.database import async_session_maker
from src.models.plan import Plan
from src.models.server import Server
from src.models.subscription import Subscription
from src.models.user import User
from src.services.subscription_service import SubscriptionService

app = FastAPI(title="AegisVPN site API", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(checkout.router)
app.include_router(legal.router)


def _plan_name(days: int, lang: str = "ru") -> str:
    # days == 0 is the lifetime plan (SubscriptionService.LIFETIME_PLAN_DAYS),
    # and it must be caught first: 0 % 365 == 0 would otherwise name it "0 года".
    if days == 0:
        return "Бессрочная"
    if days % 365 == 0:
        years = days // 365
        return f"{years} год" if years == 1 else f"{years} года"
    return f"{days} дней"


@app.get("/api/locations")
async def locations() -> list[dict]:
    """Active locations, as the globe and the location list need them.

    The `servers` row also carries reality keys, the agent token, the MTProxy
    secret and the Hy2 obfs password. This endpoint is public, so it whitelists
    fields explicitly — never serialise the model.
    """
    async with async_session_maker() as session:
        rows = (
            await session.execute(
                select(Server)
                .where(Server.is_active == True)  # noqa: E712 - SQLAlchemy needs ==
                .order_by(Server.display_order, Server.id)
            )
        ).scalars().all()

    return [
        {"id": s.id, "name": s.name, "flag": s.flag, "code": s.country_code}
        for s in rows
    ]


@app.get("/api/plans")
async def plans() -> list[dict]:
    async with async_session_maker() as session:
        rows = (
            await session.execute(
                select(Plan).where(Plan.is_active == True).order_by(Plan.days)  # noqa: E712
            )
        ).scalars().all()

    return [
        {
            "id": p.id,
            "days": p.days,
            "name": _plan_name(p.days),
            "stars_price": p.stars_price,
            "rub_price": p.rub_price,
            # NOT a device count: the limit is on SIMULTANEOUS connections
            # (distinct source IPs at one time). A subscription can be installed
            # on any number of devices; only this many may be connected at once.
            # It is a global setting rather than a per-plan one, but the site
            # shows it on the plan card, so echo it on each row. 0 = unlimited.
            "conn_limit": settings.default_conn_limit,
            # The reference plan the site opens on, and the one every other
            # plan's per-month price is compared against. The comparison itself
            # is computed by the site, in whichever unit it is displaying.
            "is_base": bool(p.is_base),
        }
        for p in rows
    ]


def _display_name(user: User) -> str:
    """Profile name, falling back to the handle, then to the bare id.

    first_name/last_name are only known for users who have signed in on the
    site at least once (the widget carries them); bot-only accounts keep
    falling back to the handle.
    """
    name = " ".join(p for p in (user.first_name, user.last_name) if p).strip()
    return name or user.username or f"id{user.tg_id}"


_AVATAR_TIMEOUT = aiohttp.ClientTimeout(total=8, connect=4)
_AVATAR_MAX_BYTES = 512 * 1024  # a profile picture is a few dozen KiB; cap the rest


async def _fetch_avatar(url: str) -> tuple[bytes, str] | None:
    """Download a Telegram avatar so it can be served from our own domain.

    Best-effort: any failure — blocked host, timeout, oversize, not an image —
    returns None and the caller keeps whatever it already had.
    """
    try:
        async with aiohttp.ClientSession(timeout=_AVATAR_TIMEOUT) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if not mime.startswith("image/"):
                    return None
                data = await resp.content.read(_AVATAR_MAX_BYTES + 1)
                if not data or len(data) > _AVATAR_MAX_BYTES:
                    return None
                return data, mime
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return None


async def _current_user(session: AsyncSession, cookie: str | None) -> User | None:
    user_id = read_session(cookie)
    if user_id is None:
        return None
    return await session.get(User, user_id)


async def _me_payload(session: AsyncSession, user: User) -> dict:
    now = datetime.now(UTC).replace(tzinfo=None)
    sub = (
        await session.execute(
            select(Subscription)
            .where(Subscription.user_id == user.id, Subscription.is_active == True)  # noqa: E712
            .order_by(Subscription.expires_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    payload: dict = {
        # The profile NAME, not the @username: that is what Telegram shows in
        # chats and what a person recognises as themselves. The handle is a
        # separate field; the site prints it as secondary detail.
        "display_name": _display_name(user),
        "tg": f"@{user.username}" if user.username else None,
        # Prefer our own copy (works where Telegram's CDN is blocked); the ?v
        # token is a digest of photo_url, so the URL — and thus the browser
        # cache — changes exactly when the avatar does. photo_url stays as a
        # last-resort fallback for accounts whose image we couldn't fetch.
        "avatar_url": (
            f"/api/avatar/{user.id}?v={hashlib.sha1((user.photo_url or '').encode()).hexdigest()[:12]}"
            if user.avatar_data
            else None
        ),
        "photo_url": user.photo_url,
        # Drives the consent checkbox at checkout: a user who already accepted
        # in the bot is never asked again.
        "terms_accepted": terms_accepted(user),
        "subscription": None,
    }
    if sub is not None:
        payload["subscription"] = {
            "is_active": sub.expires_at > now,
            # A lifetime subscription is stored with a sentinel far-future date
            # (2099-12-31); the site must say "forever", not print the sentinel.
            "is_lifetime": SubscriptionService.is_lifetime_subscription(sub),
            "expires_at": sub.expires_at.isoformat(),
            "sub_url": SubscriptionService.build_subscription_url(sub.sub_token),
        }
    return payload


@app.get("/api/me")
async def me(response: Response, aegis_session: str | None = Cookie(default=None)) -> dict | None:
    async with async_session_maker() as session:
        user = await _current_user(session, aegis_session)
        if user is None or user.is_banned:
            response.status_code = 401
            return None
        return await _me_payload(session, user)


@app.post("/api/auth/telegram")
async def auth_telegram(request: Request, response: Response) -> dict | None:
    payload = await request.json()
    tg_id = verify_telegram_login(payload)
    if tg_id is None:
        response.status_code = 401
        return None

    async with async_session_maker() as session:
        user = (
            await session.execute(select(User).where(User.tg_id == tg_id))
        ).scalar_one_or_none()

        # Profile fields the widget signed for us. Empty strings mean "not set"
        # (Telegram omits absent fields, but be defensive) — store NULL, not "".
        fresh = {
            field: (payload.get(field) or None)
            for field in ("username", "first_name", "last_name", "photo_url")
        }

        if user is None:
            # A visitor who signs in on the site before ever opening the bot still
            # gets an account; the bot will find it by tg_id on /start.
            user = User(tg_id=tg_id, **fresh)
            if fresh["photo_url"]:
                got = await _fetch_avatar(fresh["photo_url"])
                if got:
                    user.avatar_data, user.avatar_mime = got
            session.add(user)
            await session.commit()
            await session.refresh(user)
        elif user.is_banned:
            response.status_code = 403
            return None
        else:
            # Refresh on every sign-in: handles get re-taken, people rename
            # themselves, and Telegram's avatar URL changes with the photo. A
            # field the payload doesn't carry is left alone rather than wiped.
            # Re-fetch the avatar bytes ONLY when the URL actually changed (or we
            # never cached one) — an unchanged photo is left exactly as it is.
            photo_changed = fresh["photo_url"] is not None and user.photo_url != fresh["photo_url"]
            changed = False
            for field, value in fresh.items():
                if value is not None and getattr(user, field) != value:
                    setattr(user, field, value)
                    changed = True
            if fresh["photo_url"] and (photo_changed or not user.avatar_data):
                got = await _fetch_avatar(fresh["photo_url"])
                if got:
                    user.avatar_data, user.avatar_mime = got
                    changed = True
            if changed:
                await session.commit()

        body = await _me_payload(session, user)

    response.set_cookie(
        SESSION_COOKIE,
        issue_session(user.id),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.base_url.startswith("https://"),
        path="/",
    )
    return body


@app.post("/api/auth/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/avatar/{user_id}")
async def avatar(user_id: int) -> Response:
    """Serve a user's cached avatar from our own domain. Public (a profile
    picture is not a secret) and immutable per ?v token, so it caches hard."""
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
    if user is None or not user.avatar_data:
        return Response(status_code=404)
    return Response(
        content=user.avatar_data,
        media_type=user.avatar_mime or "image/jpeg",
        headers={"Cache-Control": "public, max-age=604800, immutable"},
    )
