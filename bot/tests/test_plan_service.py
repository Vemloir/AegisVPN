"""The base plan is a singleton and the yardstick every other plan is measured
against: two base plans (or a "saving" computed against nothing) would put a
wrong number in front of a buyer."""

import pytest
from sqlalchemy import select

from src.core.database import async_session_maker, engine
from src.models.base import Base
from src.models.plan import Plan
from src.services.plan_service import get_base_plan, monthly_price, savings_percent, set_base_plan


@pytest.fixture(autouse=True)
async def _schema():
    # Drop first: the migration tests leave a deliberately stripped-down schema
    # behind in the shared SQLite file, and create_all does not alter an
    # existing table — inserting a Plan into it would fail on a missing column.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed() -> dict[int, int]:
    """Four terms priced so the longer ones are cheaper per month."""
    async with async_session_maker() as session:
        plans = [
            Plan(days=7, stars_price=39, rub_price=49, is_active=True),
            Plan(days=30, stars_price=99, rub_price=149, is_active=True),
            Plan(days=90, stars_price=249, rub_price=399, is_active=True),
        ]
        session.add_all(plans)
        await session.commit()
        return {p.days: p.id for p in plans}


async def test_setting_a_base_plan_clears_the_previous_one():
    ids = await _seed()
    async with async_session_maker() as session:
        await set_base_plan(session, ids[30])
    async with async_session_maker() as session:
        await set_base_plan(session, ids[90])

    async with async_session_maker() as session:
        base = await get_base_plan(session)
        assert base.days == 90
        flagged = [p for p in (await session.execute(select(Plan))).scalars() if p.is_base]
        assert len(flagged) == 1


async def test_setting_an_unknown_plan_changes_nothing():
    ids = await _seed()
    async with async_session_maker() as session:
        await set_base_plan(session, ids[30])
    async with async_session_maker() as session:
        assert await set_base_plan(session, 9999) is None
    async with async_session_maker() as session:
        assert (await get_base_plan(session)).days == 30


def test_savings_are_measured_per_month_not_per_price():
    base = Plan(id=2, days=30, stars_price=99, rub_price=149)
    quarter = Plan(id=3, days=90, stars_price=249, rub_price=399)
    week = Plan(id=1, days=7, stars_price=39, rub_price=49)

    # 399 over 90 days is 133/month against the base's 149 → ~11% cheaper, even
    # though the sticker price is nearly three times higher.
    assert savings_percent(quarter, base) == 11
    # A week costs 210/month — dearer, and it says so instead of staying quiet.
    assert savings_percent(week, base) == -41
    # The comparison follows whichever unit the site is showing.
    assert savings_percent(quarter, base, stars=True) == 16


def test_a_plan_is_not_compared_with_itself_and_lifetime_has_no_monthly_price():
    base = Plan(id=2, days=30, stars_price=99, rub_price=149)
    lifetime = Plan(id=4, days=0, stars_price=999, rub_price=1999)

    assert savings_percent(base, base) is None
    assert savings_percent(lifetime, base) is None  # would be a division by zero
    assert monthly_price(lifetime) is None
    assert savings_percent(base, None) is None  # no base set yet
