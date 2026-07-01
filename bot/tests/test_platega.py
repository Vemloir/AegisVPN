"""Platega СБП integration: callback auth, activation, idempotency, button label."""

from datetime import UTC, datetime

from pydantic import SecretStr

from src.core.config import settings
from src.core.database import async_session_maker, engine
from src.handlers.payment import payment_method_keyboard
from src.main import platega_callback_handler
from src.models import Payment, Plan, Subscription, User
from src.models.base import Base
from src.services.platega_client import verify_callback_headers

_TX = "3fa85f64-5717-4562-b3fc-2c463f66afa6"


class _FakeRequest:
    def __init__(self, headers: dict, body: dict, bot=None):
        self.headers = headers
        self._body = body
        self.app = {"bot": bot}

    async def json(self):
        return self._body


def _enable(monkeypatch):
    monkeypatch.setattr(settings, "platega_merchant_id", "MID", raising=False)
    monkeypatch.setattr(settings, "platega_secret", SecretStr("SECRET"), raising=False)


async def _seed_pending() -> int:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with async_session_maker() as session:
        user = User(tg_id=950001)
        session.add(user)
        await session.flush()
        session.add(
            Payment(
                user_id=user.id,
                tg_payment_id=f"platega_{_TX}",
                stars_amount=0,
                plan_days=30,
                provider="platega",
                rub_amount=149,
                status="pending",
            )
        )
        await session.commit()
        return user.id


def test_verify_callback_headers(monkeypatch):
    monkeypatch.setattr(settings, "platega_merchant_id", None, raising=False)
    monkeypatch.setattr(settings, "platega_secret", None, raising=False)
    assert verify_callback_headers("MID", "SECRET") is False  # disabled
    _enable(monkeypatch)
    assert verify_callback_headers("MID", "SECRET") is True
    assert verify_callback_headers("MID", "wrong") is False
    assert verify_callback_headers("other", "SECRET") is False


async def test_callback_confirms_and_is_idempotent(monkeypatch):
    _enable(monkeypatch)
    user_id = await _seed_pending()
    headers = {"X-MerchantId": "MID", "X-Secret": "SECRET"}
    body = {"id": _TX, "amount": 149, "currency": "RUB", "status": "CONFIRMED", "paymentMethod": 2, "payload": ""}

    resp = await platega_callback_handler(_FakeRequest(headers, body))
    assert resp.status == 200

    async with async_session_maker() as session:
        payment = (await session.execute(_sel_payment())).scalar_one()
        assert payment.status == "confirmed"
        subs = (await session.execute(_sel_subs(user_id))).scalars().all()
        assert len(subs) == 1 and subs[0].is_active
        first_expiry = subs[0].expires_at

    # A retried callback must NOT extend the subscription again.
    resp2 = await platega_callback_handler(_FakeRequest(headers, body))
    assert resp2.status == 200
    async with async_session_maker() as session:
        subs = (await session.execute(_sel_subs(user_id))).scalars().all()
        assert len(subs) == 1
        assert subs[0].expires_at == first_expiry


async def test_two_payments_same_user_both_apply(monkeypatch):
    """Two different Platega payments confirming concurrently for one user must
    both extend the subscription — no lost update (finding #1)."""
    import asyncio

    from src.services.payment_service import confirm_platega_payment

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with async_session_maker() as session:
        user = User(tg_id=950002)
        session.add(user)
        await session.flush()
        uid = user.id
        for tx in ("tx-a", "tx-b"):
            session.add(
                Payment(
                    user_id=uid,
                    tg_payment_id=f"platega_{tx}",
                    stars_amount=0,
                    plan_days=30,
                    provider="platega",
                    rub_amount=149,
                    status="pending",
                )
            )
        await session.commit()

    async def _confirm(tx: str):
        async with async_session_maker() as s:
            from sqlalchemy import select

            p = (await s.execute(select(Payment).where(Payment.tg_payment_id == f"platega_{tx}"))).scalar_one()
            await confirm_platega_payment(s, p, bot=None)

    await asyncio.gather(_confirm("tx-a"), _confirm("tx-b"))

    async with async_session_maker() as session:
        subs = (await session.execute(_sel_subs(uid))).scalars().all()
        assert len(subs) == 1  # not two competing rows
        # Both 30-day grants landed: expiry is ~60 days out, not ~30.
        days_out = (subs[0].expires_at - datetime.now(UTC).replace(tzinfo=None)).days
        assert days_out >= 59, f"expected both grants (~60d), got {days_out}d, lost update"


async def test_callback_rejects_bad_secret(monkeypatch):
    _enable(monkeypatch)
    await _seed_pending()
    resp = await platega_callback_handler(
        _FakeRequest({"X-MerchantId": "MID", "X-Secret": "nope"}, {"id": _TX, "status": "CONFIRMED"})
    )
    assert resp.status == 403
    async with async_session_maker() as session:
        payment = (await session.execute(_sel_payment())).scalar_one()
        assert payment.status == "pending"  # untouched


def test_sbp_button_label_reflects_enabled(monkeypatch):
    plan = Plan(id=1, days=30, stars_price=0, rub_price=149, is_active=True)

    monkeypatch.setattr(settings, "platega_merchant_id", None, raising=False)
    monkeypatch.setattr(settings, "platega_secret", None, raising=False)
    kb = payment_method_keyboard("ru", plan)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("Скоро" in x for x in labels)

    _enable(monkeypatch)
    kb = payment_method_keyboard("ru", plan)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any(x == "149₽" for x in labels)  # no "СБП" word on the button
    assert not any("Скоро" in x for x in labels)


# --- small query helpers (kept out of the flow for readability) ---------------


def _sel_payment():
    from sqlalchemy import select

    return select(Payment).where(Payment.tg_payment_id == f"platega_{_TX}")


def _sel_subs(user_id: int):
    from sqlalchemy import select

    return select(Subscription).where(Subscription.user_id == user_id)
