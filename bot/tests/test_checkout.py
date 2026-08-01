"""/api/checkout starts payments; it must never start one for a stranger, for a
user who hasn't accepted the terms, or for a plan that isn't on sale.

The confirmation side (Platega callback, successful_payment) is deliberately not
re-tested here — the site reuses it verbatim.
"""

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from src.api.auth import issue_session
from src.api.checkout import validate_provider_redirect
from src.api.main import app
from src.core.database import async_session_maker, engine
from src.core.terms import TERMS_VERSION
from src.models.base import Base
from src.models.payment import Payment
from src.models.plan import Plan
from src.models.user import User


@pytest.fixture(autouse=True)
async def _schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed(*, accepted: bool = True, rub: int | None = 149, stars: int | None = 100) -> tuple[int, int]:
    async with async_session_maker() as session:
        user = User(
            tg_id=555001,
            username="buyer",
            accepted_terms_version=TERMS_VERSION if accepted else None,
        )
        plan = Plan(days=30, stars_price=stars or 0, rub_price=rub, is_active=True)
        session.add_all([user, plan])
        await session.commit()
        return user.id, plan.id


def _client(user_id: int | None) -> TestClient:
    client = TestClient(app)
    if user_id is not None:
        client.cookies.set("aegis_session", issue_session(user_id))
    return client


async def test_anonymous_visitor_cannot_start_a_payment():
    _, plan_id = await _seed()
    r = _client(None).post("/api/checkout", json={"plan_id": plan_id, "method": "sbp"})
    assert r.status_code == 401
    assert r.json()["error"] == "auth_required"


async def test_payment_requires_accepted_terms():
    user_id, plan_id = await _seed(accepted=False)
    r = _client(user_id).post("/api/checkout", json={"plan_id": plan_id, "method": "sbp"})
    assert r.status_code == 409
    assert r.json()["error"] == "terms_required"


async def test_accepting_terms_unblocks_checkout():
    user_id, _ = await _seed(accepted=False)
    r = _client(user_id).post("/api/terms/accept")
    assert r.status_code == 200

    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        assert user.accepted_terms_version == TERMS_VERSION
        assert user.privacy_accepted is True
        assert user.accepted_terms_at is not None


async def test_unknown_or_inactive_plan_is_rejected():
    user_id, plan_id = await _seed()
    async with async_session_maker() as session:
        plan = await session.get(Plan, plan_id)
        plan.is_active = False
        await session.commit()

    client = _client(user_id)
    assert client.post("/api/checkout", json={"plan_id": plan_id, "method": "sbp"}).status_code == 404
    assert client.post("/api/checkout", json={"plan_id": 999, "method": "sbp"}).status_code == 404


async def test_sbp_creates_the_pending_payment_the_callback_will_look_up(monkeypatch):
    user_id, plan_id = await _seed()

    async def fake_create(**kwargs):
        # The payer must land back on the site, not in the bot.
        assert kwargs["return_url"].startswith("https://")
        assert kwargs["amount_rub"] == 149
        return {"transactionId": "tx-777", "redirect": "https://app.platega.io/tx-777"}

    monkeypatch.setattr("src.api.checkout.create_sbp_transaction", fake_create)
    # platega_enabled is derived from BOTH credentials; without them the site
    # correctly refuses to offer СБП at all.
    monkeypatch.setattr("src.api.checkout.settings.platega_merchant_id", "merchant")
    monkeypatch.setattr("src.api.checkout.settings.platega_secret", SecretStr("secret"))

    r = _client(user_id).post("/api/checkout", json={"plan_id": plan_id, "method": "sbp"})
    assert r.status_code == 200, r.text
    assert r.json()["url"] == "https://app.platega.io/tx-777"

    async with async_session_maker() as session:
        payment = (await session.execute(select(Payment).where(Payment.tg_payment_id == "platega_tx-777"))).scalar_one()
        assert payment.status == "pending"
        assert payment.provider == "platega"
        assert payment.plan_days == 30
        assert payment.rub_amount == 149


async def test_stars_returns_an_invoice_link_carrying_the_buyer_in_its_payload(monkeypatch):
    user_id, plan_id = await _seed()
    seen = {}

    async def fake_link(*, title, description, payload, stars):
        seen.update(payload=payload, stars=stars)
        return "https://t.me/invoice/abc"

    monkeypatch.setattr("src.api.checkout.create_stars_invoice_link", fake_link)

    r = _client(user_id).post("/api/checkout", json={"plan_id": plan_id, "method": "stars"})
    assert r.status_code == 200, r.text
    assert r.json()["url"] == "https://t.me/invoice/abc"
    assert seen["stars"] == 100
    # The bot grants the subscription to the tg_id in this payload — get it
    # wrong and the buyer pays for a stranger.
    assert seen["payload"] == f"buy_plan_{plan_id}_555001"


async def test_an_unknown_method_is_rejected():
    user_id, plan_id = await _seed()
    r = _client(user_id).post("/api/checkout", json={"plan_id": plan_id, "method": "card"})
    assert r.status_code == 400


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("stars", "https://t.me/invoice/abc"),
        ("sbp", "https://app.platega.io/transaction/abc"),
    ],
)
def test_provider_redirect_allows_only_expected_https_hosts(method, url):
    assert validate_provider_redirect(url, method) == url


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("stars", "javascript:alert(1)"),
        ("stars", "https://t.me.evil.example/invoice/abc"),
        ("stars", "https://user:pass@t.me/invoice/abc"),
        ("stars", "http://t.me/invoice/abc"),
        ("sbp", "//app.platega.io/transaction/abc"),
        ("sbp", "https://app.platega.io.evil.example/transaction/abc"),
        ("sbp", "https://app.platega.io:444/transaction/abc"),
    ],
)
def test_provider_redirect_rejects_untrusted_targets(method, url):
    assert validate_provider_redirect(url, method) is None
