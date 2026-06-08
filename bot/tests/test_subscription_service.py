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


def test_happ_android_build_number_is_not_os_version():
    # Happ appends its own build number after the OS name; it must not be read as
    # an Android version (regression: "Android 17800541067281831514").
    ua = "Happ/2.9.1/Android/17800541067281831514"
    assert SubscriptionService._detect_platform(ua) == "Android"
    assert SubscriptionService.make_device_display_name(ua) == "Android · Happ"


def test_standard_android_version_is_parsed():
    ua = "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36"
    assert SubscriptionService._detect_platform(ua) == "Android 14"


def test_happ_windows_has_no_bogus_version():
    assert SubscriptionService._detect_platform("Happ/2.9.1/Windows/2604241012503") == "Windows"


def test_ios_version_parsed_but_build_rejected():
    assert SubscriptionService._detect_platform("Happ/2.9.1/iPhone/99887766554433") == "iPhone"
    ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X)"
    assert SubscriptionService._detect_platform(ua) == "iPhone · iOS 17"


def test_extract_build_from_happ_ua():
    assert SubscriptionService.extract_build("Happ/2.9.1/Android/17800541067281831514") == "17800541067281831514"
    assert SubscriptionService.extract_build("Happ/2.9.1/Windows/2604241012503") == "2604241012503"


def test_extract_build_none_when_absent():
    assert SubscriptionService.extract_build("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X)") is None
    assert SubscriptionService.extract_build("v2rayNG/1.8.5") is None
