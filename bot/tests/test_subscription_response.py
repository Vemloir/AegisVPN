from datetime import UTC, datetime, timedelta

from src.main import client_wants_xray_json, subscription_metadata_headers
from src.models import Subscription
from src.services import SubscriptionService


def _subscription() -> Subscription:
    return Subscription(
        plan_days=30,
        expires_at=(datetime.now(UTC) + timedelta(days=30)).replace(tzinfo=None),
        traffic_up_bytes=123,
        traffic_down_bytes=456,
    )


def test_subscription_metadata_advertises_support_bot_and_website():
    headers = subscription_metadata_headers(
        _subscription(),
        SubscriptionService.SAFE_PROFILE,
    )

    assert headers["Support-Url"] == "https://t.me/AegisVPNsupportBot"
    assert headers["Profile-Web-Page-Url"] == "https://aegisvpn.org"
    assert "AegisEcoVPN_bot" not in headers["Support-Url"]


def test_subscription_metadata_keeps_usage_and_update_contract():
    sub = _subscription()
    headers = subscription_metadata_headers(
        sub,
        SubscriptionService.SAFE_PROFILE,
    )

    assert headers["Profile-Title"] == "AegisVPN"
    assert headers["Profile-Update-Interval"] == "1"
    assert headers["Subscription-Ping-Onopen-Enabled"] == "1"
    assert headers["Subscription-Userinfo"].startswith("upload=123; download=456; total=0; expire=")


def test_varmlen_receives_xray_json_subscription():
    assert client_wants_xray_json("Varmlen/1.4.2")
    assert client_wants_xray_json("varmlen")
