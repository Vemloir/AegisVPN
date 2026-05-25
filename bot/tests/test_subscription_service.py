from datetime import UTC, datetime, timedelta

from src.models import Subscription
from src.services import SubscriptionService


def _sub(**kwargs) -> Subscription:
    return Subscription(**kwargs)


def test_is_lifetime_by_plan_days():
    sub = _sub(plan_days=SubscriptionService.LIFETIME_PLAN_DAYS, expires_at=datetime(2030, 1, 1))
    assert SubscriptionService.is_lifetime_subscription(sub)


def test_is_lifetime_by_expires_at():
    sub = _sub(plan_days=30, expires_at=SubscriptionService.LIFETIME_EXPIRES_AT)
    assert SubscriptionService.is_lifetime_subscription(sub)


def test_regular_subscription_is_not_lifetime():
    expires = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)
    sub = _sub(plan_days=30, expires_at=expires)
    assert not SubscriptionService.is_lifetime_subscription(sub)


def test_none_is_not_lifetime():
    assert not SubscriptionService.is_lifetime_subscription(None)


def test_generate_sub_token_value_is_unique():
    seen: set[str] = set()
    for _ in range(200):
        token = SubscriptionService.generate_sub_token_value(seen)
        assert token and token not in seen
        seen.add(token)


def test_build_subscription_url_contains_token():
    url = SubscriptionService.build_subscription_url("tok123")
    assert url.startswith("http")
    assert "/sub/tok123" in url
