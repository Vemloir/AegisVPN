"""The base plan: the one every other plan is measured against.

"Exactly one plan is the base" is an invariant, not a UI convention, so setting
it lives here rather than in the admin handler: two callers (or one impatient
admin double-tapping) must not be able to leave two plans marked.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.plan import Plan


async def set_base_plan(session: AsyncSession, plan_id: int) -> Plan | None:
    """Make ``plan_id`` the base plan and clear the flag from every other.

    Both statements run in one transaction, so there is never a moment with two
    base plans (or none). Returns the plan, or None if it does not exist.
    """
    plan = await session.get(Plan, plan_id)
    if plan is None:
        return None

    await session.execute(update(Plan).where(Plan.id != plan_id).values(is_base=False))
    plan.is_base = True
    await session.commit()
    await session.refresh(plan)
    return plan


async def get_base_plan(session: AsyncSession) -> Plan | None:
    return (
        await session.execute(select(Plan).where(Plan.is_base == True))  # noqa: E712
    ).scalars().first()


def monthly_price(plan: Plan, *, stars: bool = False) -> float | None:
    """Price normalised to 30 days, or None when the plan cannot be compared.

    A lifetime plan (days == 0) has no per-month price — dividing by zero here
    would either crash or, worse, silently produce a nonsense "saving".
    """
    price = plan.stars_price if stars else plan.rub_price
    if not price or not plan.days:
        return None
    return price / (plan.days / 30)


def savings_percent(plan: Plan, base: Plan | None, *, stars: bool = False) -> int | None:
    """How much cheaper (positive) or dearer (negative) ``plan`` is per month
    than the base plan, in whole percent. None when the two cannot be compared."""
    if base is None or plan.id == base.id:
        return None
    plan_monthly = monthly_price(plan, stars=stars)
    base_monthly = monthly_price(base, stars=stars)
    if plan_monthly is None or base_monthly is None or base_monthly == 0:
        return None
    return round((base_monthly - plan_monthly) / base_monthly * 100)
