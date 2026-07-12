"""Public HTTP API for the website.

It runs beside the bot, against the SAME database and the same models, so the
site cannot disagree with what the bot sells: plans, prices and locations are
read from the tables the admin panel writes. Nothing is duplicated here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Cookie, FastAPI, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_SECONDS,
    issue_session,
    read_session,
    verify_telegram_login,
)
from src.core.config import settings
from src.core.database import async_session_maker
from src.models.plan import Plan
from src.models.server import Server
from src.models.subscription import Subscription
from src.models.user import User
from src.services.subscription_service import SubscriptionService

app = FastAPI(title="AegisVPN site API", docs_url=None, redoc_url=None, openapi_url=None)


def _plan_name(days: int, lang: str = "ru") -> str:
    if days % 365 == 0:
        years = days // 365
        return f"{years} год" if years == 1 else f"{years} года"
    if days % 30 == 0:
        return f"{days} дней"
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
            # Device (connection) limit is a global setting, not per-plan, but the
            # site shows it on every plan card, so echo it on each row.
            "devices": settings.default_conn_limit,
        }
        for p in rows
    ]


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
        "display_name": user.username or f"id{user.tg_id}",
        "tg": f"@{user.username}" if user.username else None,
        "subscription": None,
    }
    if sub is not None:
        payload["subscription"] = {
            "is_active": sub.expires_at > now,
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

        if user is None:
            # A visitor who signs in on the site before ever opening the bot still
            # gets an account; the bot will find it by tg_id on /start.
            user = User(tg_id=tg_id, username=payload.get("username"))
            session.add(user)
            await session.commit()
            await session.refresh(user)
        elif user.is_banned:
            response.status_code = 403
            return None
        elif payload.get("username") and user.username != payload["username"]:
            user.username = payload["username"]
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
